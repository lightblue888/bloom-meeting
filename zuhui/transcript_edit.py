# -*- coding: utf-8 -*-
"""回顾里手改逐字稿（第 7 批）：改一句的字、整场替换一个词（可以顺手加进热词）、给访客改名，都能撤销。

改法记在会议目录的 transcript.edits.json：{"version": 1, "ops": [一条一条，按先后照做]}
  {"op": "line", "i": 原始版第几行, "before": 改之前的字, "text": 改成的字（空串 = 删掉这一句）}
  {"op": "replace", "find": 原来的词, "repl": 换成的词, "n": 当时换了几处, "hot": 换成的词加进了热词}
  {"op": "speaker", "from": 原来的名字, "to": 新名字}      只改访客的（主持人、AI 的名字在设置里改）
每条还带 "at"（什么时候改的）。叠在干净版上（transcript.clean.jsonl；还没整理过就是原始版）；原始版 transcript.jsonl、
实时版、干净版、删改清单都不动，所以原话一直在。撤销 = 拿掉最后一条（加进热词的词不跟着删）。

Meetings.get 的 transcript（回顾、搜索、导出）、每人发言时长、参会人名单，past.py 的跨会议检索，用的都是改过的；
纪要不跟着重写（要的话在回顾里重新生成）。会还在开、会后整理还在跑的时候不能改（整理完逐字稿还会变）。
出错抛 ValueError，参数是界面翻译用的代码。"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

EDITS = "transcript.edits.json"
LINE_MAX = 2000          # 一句改成的字最多多长（AI 一段长回答也放得下）
WORD_MAX = 40            # 替换：找的词、换成的词各最多多长（和热词一样）
NAME_MAX = 32            # 访客名最多多长（和进会时填的一样）
OPS_MAX = 2000           # 一场会最多记这么多条改法
SPEAKERS = ("teacher", "student", "guest")
_EMPTY = re.compile(r"[\s，。！？、,.!?；;：:…“”\"'（）()]")
_lock = threading.RLock()


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load(d) -> list[dict]:
    """这场会的改法（没改过：空列表）。"""
    data = _read(Path(d) / EDITS)
    ops = data.get("ops") if isinstance(data, dict) else None
    return [x for x in ops if isinstance(x, dict) and x.get("op") in ("line", "replace", "speaker")] if isinstance(ops, list) else []


def _save(d, ops: list[dict]) -> None:
    p = Path(d) / EDITS
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"version": 1, "ops": ops}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _one(v, n: int) -> str:
    """一行字：换行、连着的空格并成一个，去首尾空格；超过 n 抛 too_long。"""
    s = " ".join(str(v if v is not None else "").split())
    if len(s) > n:
        raise ValueError("too_long")
    return s


def apply(rows: list[dict], ops: list[dict]) -> list[dict]:
    """干净版（或原始版）的行 + 改法 → 改过的行（新的 dict，原来的不动）。每行的 "i" = 原始版第几行（没有就按位置补上）；
    字被改过的行多一个 "edited": True。一句改成空的、替换完只剩标点的，这一句就不要了。"""
    out = [dict(r, i=r.get("i", k)) for k, r in enumerate(rows or [])]
    for op in ops or []:
        k = op.get("op")
        if k == "line":
            for n, r in enumerate(out):
                if r.get("i") == op.get("i"):
                    text = str(op.get("text") or "")
                    if not _EMPTY.sub("", text):
                        out.pop(n)
                    else:
                        r["text"], r["edited"] = text, True
                    break
        elif k == "replace":
            find, repl = str(op.get("find") or ""), str(op.get("repl") or "")
            if not find:
                continue
            keep = []
            for r in out:
                t = str(r.get("text") or "")
                if r.get("who") in SPEAKERS and find in t:
                    t = re.sub(r"[ \t]{2,}", " ", t.replace(find, repl)).strip()
                    if not _EMPTY.sub("", t):
                        continue
                    r["text"], r["edited"] = t, True
                keep.append(r)
            out = keep
        elif k == "speaker":
            for r in out:
                if r.get("who") == "guest" and r.get("name") == op.get("from"):
                    r["name"] = op.get("to")
    return out


def apply_speakers(rows: list[dict], ops: list[dict]) -> list[dict]:
    """只叠改名（原始版算每人发言时长用：字照原样，人换成新名字）。"""
    names = [op for op in ops or [] if op.get("op") == "speaker"]
    if not names:
        return list(rows or [])
    out = []
    for r in rows or []:
        r = dict(r)
        for op in names:
            if r.get("who") == "guest" and r.get("name") == op.get("from"):
                r["name"] = op.get("to")
        out.append(r)
    return out


def rename_people(people, ops: list[dict]):
    """参会人名单（participants.json）跟着改名：访客改成的新名字和别人重了就并成一个。"""
    if not isinstance(people, list):
        return people
    names = [op for op in ops or [] if op.get("op") == "speaker"]
    if not names:
        return people
    out, seen = [], set()
    for p in people:
        if not isinstance(p, dict):
            continue
        p = dict(p)
        if not p.get("you") and p.get("id") != "claude" and p.get("role") not in ("teacher", "student"):
            for op in names:
                if p.get("name") == op.get("from"):
                    p["name"] = op.get("to")
        key = str(p.get("name") or "")
        if key in seen and not p.get("you") and p.get("id") != "claude":
            continue
        seen.add(key)
        out.append(p)
    return out


def public(ops: list[dict]) -> dict:
    """给界面的：改过几条、撤销会拿掉的那条（{op, find/repl | from/to | before/text 截短}）。"""
    last = None
    if ops:
        o = ops[-1]
        k = o.get("op")
        if k == "replace":
            last = {"op": k, "find": o.get("find"), "repl": o.get("repl")}
        elif k == "speaker":
            last = {"op": k, "from": o.get("from"), "to": o.get("to")}
        else:
            last = {"op": k, "i": o.get("i"), "text": str(o.get("text") or "")[:40]}
    return {"n": len(ops), "last": last}


def _names(cfg: dict | None) -> tuple[str, str]:
    import getpass
    from .config import assistant_name
    cfg = cfg or {}
    return str((cfg.get("meeting") or {}).get("me") or getpass.getuser() or "主持人"), assistant_name(cfg)


def add_hotword(word: str, cfg: dict | None = None) -> str:
    """换成的词加进自己的热词（设置 → 语音 → 专有名词那一份）→ added / exists / full。下一场会的转写用上。"""
    from . import voice_prefs
    w = " ".join(str(word or "").split())
    if not w or len(w) > voice_prefs.HOT_LEN:
        return "bad"
    cur = voice_prefs.hotwords()
    if cur is None:
        v = ((cfg or {}).get("meeting") or {}).get("hotwords") or []
        cur = [str(x) for x in v] if isinstance(v, list) else []
    if w in cur:
        return "exists"
    if len(cur) >= voice_prefs.HOT_MAX:
        return "full"
    voice_prefs.save({"hotwords": cur + [w]})
    if cfg is not None:
        voice_prefs.apply(cfg)
    return "added"


def edit(lib, mid: str, body: dict, cfg: dict | None = None) -> dict:
    """改一处 → {"ok", "n", "last", "count"（替换：换了几处）, "hot"（加进热词：added / exists / full）}。
    body：{"op": "line", "i", "text"} / {"op": "replace", "find", "repl", "hot"} / {"op": "speaker", "from", "to"} /
    {"op": "undo"}。出错抛 ValueError：not_found / bad / empty / too_long / no_match / same / taken / nothing / too_many。"""
    mid = Path(str(mid or "")).name
    d = lib.root / mid
    body = body if isinstance(body, dict) else {}
    with _lock:
        g = lib.get(mid) if mid else None
        if not g:
            raise ValueError("not_found")
        ops = load(d)
        k = str(body.get("op") or "")
        out: dict = {}
        if k == "undo":
            if not ops:
                raise ValueError("nothing")
            ops.pop()
            _save(d, ops)
            return {"ok": True, **public(ops)}
        if len(ops) >= OPS_MAX:
            raise ValueError("too_many")
        rows = g.get("transcript") or []
        now = datetime.now().isoformat(timespec="seconds")
        if k == "line":
            try:
                i = int(body.get("i"))
            except (TypeError, ValueError):
                raise ValueError("bad") from None
            row = next((r for r in rows if r.get("i") == i), None)
            if row is None or row.get("who") not in SPEAKERS:
                raise ValueError("not_found")
            text = str(body.get("text") if body.get("text") is not None else "")
            text = re.sub(r"[ \t]+", " ", text.replace("\r", "")).strip()
            if len(text) > LINE_MAX:
                raise ValueError("too_long")
            if text == str(row.get("text") or ""):
                raise ValueError("same")
            op = {"op": "line", "i": i, "before": str(row.get("text") or ""), "text": text, "at": now}
        elif k == "replace":
            find, repl = _one(body.get("find"), WORD_MAX), _one(body.get("repl"), WORD_MAX)
            if not find:
                raise ValueError("empty")
            if find == repl:
                raise ValueError("same")
            n = sum(str(r.get("text") or "").count(find) for r in rows if r.get("who") in SPEAKERS)
            if not n:
                raise ValueError("no_match")
            op = {"op": "replace", "find": find, "repl": repl, "n": n, "at": now}
            out["count"] = n
            if body.get("hot") and repl:
                try:
                    out["hot"] = add_hotword(repl, cfg)
                except (OSError, ValueError):
                    out["hot"] = "failed"
                if out["hot"] == "added":
                    op["hot"] = True
        elif k == "speaker":
            frm, to = _one(body.get("from"), NAME_MAX), _one(body.get("to"), NAME_MAX)
            if not frm or not to:
                raise ValueError("empty")
            if frm == to:
                raise ValueError("same")
            if not any(r.get("who") == "guest" and r.get("name") == frm for r in rows):
                raise ValueError("not_found")
            me, ai = _names(cfg)
            hosts = {str(p.get("name")) for p in (g.get("participants") or []) if isinstance(p, dict)
                     and (p.get("you") or p.get("id") == "claude" or p.get("role") in ("teacher", "student"))}
            if to in {me, ai} | hosts:                 # 和主持人、AI 重名（现在的名字，或者这场会当时的名字）
                raise ValueError("taken")
            op = {"op": "speaker", "from": frm, "to": to, "at": now}
        else:
            raise ValueError("bad")
        ops.append(op)
        _save(d, ops)
        return {"ok": True, **public(ops), **out}


ERR_STATUS = {"not_found": 404, "live": 409, "busy": 409}


def post(viewer, mid: str, body) -> tuple[dict, int]:
    """POST /api/meetings/<id>/transcript（view.py 转到这里）→ (回给页面的, HTTP 状态码)。"""
    lib = getattr(viewer, "meetings", None)
    if lib is None:
        return {"ok": False, "error": "not_found"}, 404
    mid = Path(str(mid or "")).name
    cur = lib.current()
    if cur and cur.get("id") == mid:
        return {"ok": False, "error": "live"}, 409
    busy = getattr(viewer, "processing", None)
    try:
        if callable(busy) and busy(mid):
            return {"ok": False, "error": "busy"}, 409
    except Exception:
        pass
    try:
        out = edit(lib, mid, body if isinstance(body, dict) else {}, getattr(viewer, "cfg", None))
    except ValueError as e:
        return {"ok": False, "error": str(e)}, ERR_STATUS.get(str(e), 400)
    except OSError:
        return {"ok": False, "error": "failed"}, 500
    fn = getattr(viewer, "on_log", None)
    if callable(fn):
        try:
            fn("transcript_edit", text=f"{mid}：{json.dumps(out.get('last'), ensure_ascii=False)[:120]}")
        except Exception:
            pass
    return out, 200
