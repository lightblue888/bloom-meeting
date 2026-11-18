# -*- coding: utf-8 -*-
"""API 用量和估算费用：模型来源是 API 时，llm_api 每调一次接口就记一笔（输入、输出、缓存读写各多少 token），
按当时的标价（zuhui/prices.py）算好钱一起记下。Claude Code 账号不记（订阅制，不按用量算钱）。

记在两处：
- 这场会的目录里 usage.json：开着会、散会后还在整理（纪要、整理逐字稿、没改完的稿）时的调用都算这场的；
  回顾里重新生成纪要算那一场的（bind）。回顾 / 纪要页显示。
- ~/.zuhui/usage/<年-月>.json：这个月所有的调用（不在会上的也算，比如测试连接、回顾里提问）。设置里显示本月累计。

两份的形状一样：{"models": {"<域名>/<型号>": {kind, label, model, currency, calls, in, out, cache_read, cache_write,
cost, unpriced}}, "updated"}。cost 是按价算出来的那几次加起来的钱（计价货币 currency），unpriced 是不认识的型号、
算不了钱的次数（这种只显示 token）。"""
from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import STATE
from .prices import PRICES

USAGE_DIR = STATE / "usage"
FILE = "usage.json"
_LOCK = threading.Lock()
_LIVE: dict = {"dir": None}               # 正在开（或刚散、会后整理还没完）的那场会的目录
_TL = threading.local()                   # bind()：这个线程上的调用算哪场会的


# ---------------- 算哪场会的 ----------------
def begin(meeting_dir) -> None:
    """开会（或接着开）：之后的调用都记进这场会。"""
    _LIVE["dir"] = Path(meeting_dir) if meeting_dir else None


def finish(meeting_dir=None) -> None:
    """会后整理做完了：之后的调用不再算这场的（只进本月）。给了目录就只在还是那场时才清。"""
    cur = _LIVE.get("dir")
    if meeting_dir is None or (cur is not None and Path(meeting_dir) == cur):
        _LIVE["dir"] = None


@contextmanager
def bind(meeting_dir):
    """这一段里（同一个线程）调的接口算这场会的（回顾里重新生成某场的纪要）。"""
    stack = getattr(_TL, "stack", None)
    if stack is None:
        stack = _TL.stack = []
    stack.append(Path(meeting_dir) if meeting_dir else None)
    try:
        yield
    finally:
        stack.pop()


def _target() -> Path | None:
    stack = getattr(_TL, "stack", None)
    if stack:
        return stack[-1]
    return _LIVE.get("dir")


# ---------------- 价格 ----------------
def host_of(url: str) -> str:
    m = re.match(r"^\w+://(\[[^\]]+\]|[^/:?#]+)", str(url or ""))
    return (m.group(1) if m else "").strip("[]").lower()


def _provider_by_host(host: str) -> tuple[str, dict] | None:
    for k, p in PRICES.items():
        for h in p.get("hosts") or []:
            if host == h or host.endswith("." + h):
                return k, p
    return None


def _model_entry(models: dict, model: str) -> dict | None:
    m = re.sub(r"\[1m\]$", "", str(model or "").strip().lower())
    m = m.rsplit("/", 1)[-1] if "/" in m and not m.startswith("claude") else m   # openrouter 式的 "deepseek/deepseek-flash"
    if m in models:
        return models[m]
    best = None
    for k in models:                     # claude-haiku-4-5-20251001 → claude-haiku-4-5；claude-opus-5 不能认成 claude-opus-5-5
        if m.startswith(k) and len(m) > len(k) and m[len(k)] in "-@:" and (best is None or len(k) > len(best)):
            best = k
    return models[best] if best else None


def _is_peak(spec: dict, at: datetime) -> bool:
    local = at.astimezone(timezone(timedelta(hours=float(spec.get("utc_offset", 0)))))
    if local.weekday() not in (spec.get("weekdays") or []):
        return False
    h = local.hour + local.minute / 60
    return any(a <= h < b for a, b in (spec.get("hours") or []))


def rates(base_url: str, model: str, prompt_tokens: int = 0, at: datetime | None = None) -> tuple[str, dict] | None:
    """这一次调用的单价：(货币, {in, cache_read, cache_write, out} 每百万 token)；不认识的服务商 / 型号返回 None。"""
    hit = _provider_by_host(host_of(base_url))
    if not hit:
        return None
    _, p = hit
    e = _model_entry(p.get("models") or {}, model)
    if not e:
        return None
    at = at or datetime.now(timezone.utc)
    if "peak" in e or "off" in e:
        e = e.get("peak" if _is_peak(p.get("peak") or {}, at) else "off") or e.get("off") or e.get("peak")
    if "tiers" in e:
        tiers = e["tiers"]
        e = next((t for t in tiers if t.get("upto") is None or prompt_tokens <= t["upto"]), tiers[-1])
    if "in" not in e or "out" not in e:
        return None
    r = {"in": float(e["in"]), "out": float(e["out"])}
    r["cache_read"] = float(e["cache_read"]) if "cache_read" in e else r["in"] * float(p.get("cache_read_ratio", 0.1))
    r["cache_write"] = float(e["cache_write"]) if "cache_write" in e else r["in"] * float(p.get("cache_write_ratio", 1.0))
    return str(p.get("currency") or "CNY"), r


def cost_of(base_url: str, model: str, inp: int, out: int, cache_read: int = 0, cache_write: int = 0,
            at: datetime | None = None) -> tuple[str, float] | None:
    """这一次调用估算多少钱：(货币, 金额)；算不了返回 None。"""
    got = rates(base_url, model, inp + cache_read + cache_write, at)
    if not got:
        return None
    cur, r = got
    total = (inp * r["in"] + out * r["out"] + cache_read * r["cache_read"] + cache_write * r["cache_write"]) / 1e6
    return cur, total


# ---------------- 记一笔 ----------------
def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("models"), dict) else {"models": {}}
    except (OSError, ValueError):
        return {"models": {}}


def _write(p: Path, d: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _add(p: Path, key: str, row: dict) -> None:
    d = _read(p)
    cur = d["models"].get(key) or {k: row[k] for k in ("kind", "label", "model", "host")}
    for k in ("kind", "label", "model", "host"):
        cur[k] = row[k]
    for k in ("calls", "in", "out", "cache_read", "cache_write", "unpriced"):
        cur[k] = int(cur.get(k) or 0) + int(row.get(k) or 0)
    if row.get("currency"):
        cur["currency"] = row["currency"]
        cur["cost"] = round(float(cur.get("cost") or 0) + float(row.get("cost") or 0), 8)
    d["models"][key] = cur
    d["updated"] = datetime.now().isoformat(timespec="seconds")
    _write(p, d)


def month_file(at: datetime | None = None) -> Path:
    return USAGE_DIR / f"{(at or datetime.now()).strftime('%Y-%m')}.json"


def record(d: dict, model: str, inp: int, out: int, cache_read: int = 0, cache_write: int = 0,
           label: str = "", base_url: str = "") -> None:
    """记一次调用。d 是在用的那份模型来源（provider.active()）。出什么错都不往外抛：记账不能耽误开会。"""
    try:
        inp, out, cache_read, cache_write = (max(0, int(x or 0)) for x in (inp, out, cache_read, cache_write))
        if not (inp or out or cache_read or cache_write):
            return
        host = host_of(base_url) or "api"
        row = {"kind": str(d.get("kind") or ""), "label": label, "model": str(model or ""), "host": host, "calls": 1,
               "in": inp, "out": out, "cache_read": cache_read, "cache_write": cache_write, "unpriced": 0}
        c = cost_of(base_url, model, inp, out, cache_read, cache_write)
        if c:
            row["currency"], row["cost"] = c
        else:
            row["unpriced"] = 1
        key = f"{host}/{row['model']}"
        with _LOCK:
            USAGE_DIR.mkdir(parents=True, exist_ok=True)
            _add(month_file(), key, row)
            t = _target()
            if t is not None and t.is_dir():
                _add(t / FILE, key, row)
    except Exception:
        pass


# ---------------- 给界面看的 ----------------
def _view(d: dict) -> dict | None:
    """{"models": [{model, label, calls, in, out, cache_read, cache_write, tokens, currency|None, cost|None}],
    "tokens": 总数, "costs": {货币: 金额}}；一笔都没有返回 None。cost 只在这个型号每次都算得出钱时给。"""
    rows = []
    for v in (d.get("models") or {}).values():
        if not isinstance(v, dict):
            continue
        n = {k: int(v.get(k) or 0) for k in ("calls", "in", "out", "cache_read", "cache_write", "unpriced")}
        n["tokens"] = n["in"] + n["out"] + n["cache_read"] + n["cache_write"]
        if not n["tokens"]:
            continue
        priced = bool(v.get("currency")) and not n["unpriced"]
        from .config import pretty_model
        rows.append({"model": str(v.get("model") or ""), "pretty": pretty_model(v.get("model")),
                     "label": str(v.get("label") or ""), **n,
                     "currency": str(v["currency"]) if priced else None,
                     "cost": round(float(v.get("cost") or 0), 6) if priced else None})
    if not rows:
        return None
    rows.sort(key=lambda r: -r["tokens"])
    costs: dict = {}
    for r in rows:
        if r["currency"]:
            costs[r["currency"]] = round(costs.get(r["currency"], 0.0) + r["cost"], 6)
    return {"models": rows, "tokens": sum(r["tokens"] for r in rows), "costs": costs}


def meeting(meeting_dir) -> dict | None:
    """一场会的用量（没用 API 的会：None）。"""
    p = Path(meeting_dir) / FILE
    return _view(_read(p)) if p.exists() else None


def month(at: datetime | None = None) -> dict:
    """这个月的累计：{"month": "2026-11", 和 _view 一样的几项（一笔都没有时 models 是空的）}。"""
    at = at or datetime.now()
    v = _view(_read(month_file(at))) or {"models": [], "tokens": 0, "costs": {}}
    return {"month": at.strftime("%Y-%m"), **v}


# ---------------- token 估算（流式被打断、接口没回用量时） ----------------
_CJK = re.compile(r"[　-鿿가-힯＀-￯]")


def estimate_tokens(text: str) -> int:
    """粗估：中日韩一个字算一个，别的四个字符算一个。只在接口没回用量时用。"""
    s = str(text or "")
    cjk = len(_CJK.findall(s))
    return cjk + max(0, len(s) - cjk) // 4
