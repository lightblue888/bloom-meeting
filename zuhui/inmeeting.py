# -*- coding: utf-8 -*-
"""会中交互（第 3 批）：打字问 AI、书签、长时间没人说话、先选中再说。
打字问 AI 只有主持人和共同主持人能用（访客页上的共同主持人也行），打出来的和说出来的一样处理。

存在哪：
  meetings/<id>/bookmarks.json  书签 {"version": 1, "items": [{id, sec, t, note, by, host, at}]}
                                sec / t = 点"标记"那一刻是这场会的第几秒；by = 谁标的（主持人记自己的名字，host=True）
不存盘的（只在这次开会的内存里）：选中的那段（Anchor）。

出错抛 ValueError，参数是界面翻译用的代码：empty、too_long、too_many、not_found、not_yours。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

ASK_MAX = 500                  # 打字问 AI 一次最多几个字（前端输入框同样限）
NOTE_MAX = 100                 # 书签备注最多几个字
BOOKMARK_CAP = 500             # 一场会最多几个书签
NAME_MAX = 32                  # 书签上记的名字最多几个字（和访客名一样长）
QUIET_S = 600                  # 开着会这么久没人说话：主持人那边提醒一下（不自动散会）
ANCHOR_TTL = 60                # 选中的那段留多久（用掉了马上清）
ANCHOR_MAX = 1000              # 选中的那段最多几个字（更长的前端不报上来）

BOOKMARKS = "bookmarks.json"

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BID = re.compile(r"^b[0-9]{1,5}$")
_lock = threading.RLock()


def _line(v) -> str:
    """一行字：去掉控制字符，换行和连着的空格并成一个。"""
    return " ".join(_CTRL.sub("", str(v if v is not None else "")).split())


def _fmt(sec: float) -> str:
    s = int(max(0, sec))
    return f"{s // 60:02d}:{s % 60:02d}"


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- 打字问 AI
def clean_question(text) -> str:
    """打字问的那句：去掉控制字符和首尾空白，连着的空行并成一个。空的 ValueError("empty")，超过 ASK_MAX 字 ValueError("too_long")。"""
    s = _CTRL.sub("", str(text if text is not None else "")).replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(x for x in (" ".join(ln.split()) for ln in s.split("\n")) if x)
    if not s:
        raise ValueError("empty")
    if len(s) > ASK_MAX:
        raise ValueError("too_long")
    return s


# ---------------------------------------------------------------- 书签
def load_bookmarks(d: Path) -> list[dict]:
    raw = _read(Path(d) / BOOKMARKS)
    xs = raw.get("items") if isinstance(raw, dict) else None
    out = []
    for x in xs if isinstance(xs, list) else []:
        if isinstance(x, dict) and _BID.match(str(x.get("id") or "")) and isinstance(x.get("sec"), (int, float)):
            out.append(x)
    return sorted(out, key=lambda x: float(x["sec"]))


def clean_note(note) -> str:
    s = _line(note)
    if len(s) > NOTE_MAX:
        raise ValueError("too_long")
    return s


def add_bookmark(d: Path, sec: float, note="", by: str | None = None, host: bool = True, now: float | None = None) -> dict:
    """记一个书签：sec = 这场会的第几秒。备注超过 NOTE_MAX 字 ValueError("too_long")，一场会超过 BOOKMARK_CAP 个 ValueError("too_many")。"""
    note = clean_note(note)
    with _lock:
        items = load_bookmarks(d)
        if len(items) >= BOOKMARK_CAP:
            raise ValueError("too_many")
        n = max([int(str(x["id"])[1:]) for x in items] or [0]) + 1
        sec = max(0.0, float(sec))
        b = {"id": f"b{n}", "sec": round(sec, 1), "t": _fmt(sec), "note": note, "by": _line(by)[:NAME_MAX] or None,
             "host": bool(host), "at": round(now if now is not None else time.time(), 2)}
        _write(Path(d) / BOOKMARKS, {"version": 1, "items": items + [b]})
    return b


def update_bookmark(d: Path, bid: str, note, by: str | None = None, host: bool = True) -> dict:
    """改书签的备注：主持人能改所有的，访客只能改自己标的（ValueError("not_yours")）。"""
    note = clean_note(note)
    with _lock:
        items = load_bookmarks(d)
        hit = next((x for x in items if x.get("id") == str(bid or "")), None)
        if not hit:
            raise ValueError("not_found")
        if not host and (hit.get("host") or _line(hit.get("by")) != _line(by)):
            raise ValueError("not_yours")
        hit["note"] = note
        _write(Path(d) / BOOKMARKS, {"version": 1, "items": items})
        return dict(hit)


def bookmarks_block(items: list[dict], budget: int = 1500) -> str:
    """纪要用：会上有人标的重点那几处。"""
    if not items:
        return ""
    out, used = [], 0
    for b in items:
        line = f"[{b.get('t') or _fmt(float(b.get('sec') or 0))}] {b.get('by') or '主持人'}" + (f"：{b['note']}" if b.get("note") else "")
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line)
    return "\n".join(out)


# 纪要用：会上标的重点
BOOKMARK_RULE = "会上有人点了“标记”，标出了下面几处重点（用户消息最后列着）：纪要优先写这几处前后说的内容，conclusions 和 sections 里要覆盖到。"


# ---------------------------------------------------------------- 长时间没人说话
def quiet_seconds(now: float, t0: float | None, lines: list[dict], last_speech: float = 0.0, busy: bool = False) -> float:
    """开着会多久没人说话了：从最后一句说完（开口时刻 + 按字数估的时长）、最后一次听到声音、开会那一刻里最晚的算起。
    busy = 此刻有人在说 / AI 在说：0。"""
    if busy:
        return 0.0
    from .meetings import speech_seconds
    last = float(t0 or now)
    for l in (lines or [])[-8:]:
        try:
            last = max(last, float(t0 or 0) + float(l.get("sec") or 0) + speech_seconds(l.get("text")))
        except (TypeError, ValueError):
            continue
    last = max(last, float(last_speech or 0))
    return max(0.0, now - last)


# ---------------------------------------------------------------- 先选中再说
class Anchor:
    """主持人或共同主持人在论文里选中的那一段：他接下来对 AI 说的一句（语音或打字）以它为准。
    ANCHOR_TTL 秒没用就过期，用一次就清掉。by = None 是主持人，否则是共同主持人的名字。"""

    def __init__(self, ttl: float = ANCHOR_TTL):
        self.ttl = ttl
        self._v: dict | None = None
        self._lock = threading.Lock()

    def set(self, text, section=None, by: str | None = None, now: float | None = None) -> dict:
        s = _line(text)
        if not s:
            raise ValueError("empty")
        if len(s) > ANCHOR_MAX:
            raise ValueError("too_long")
        now = time.time() if now is None else now
        with self._lock:
            self._v = {"text": s, "section": _line(section)[:200] or None, "by": _line(by) or None, "at": round(now, 2)}
            return dict(self._v)

    def _live(self, now: float) -> dict | None:
        v = self._v
        if v and now - v["at"] > self.ttl:
            self._v = v = None
        return v

    def get(self, now: float | None = None) -> dict | None:
        with self._lock:
            v = self._live(time.time() if now is None else now)
            return dict(v) if v else None

    def take(self, by: str | None = None, now: float | None = None) -> dict | None:
        """这个人（None = 主持人）的那段：拿走并清掉；不是他选的、过期了：None，不动。"""
        with self._lock:
            v = self._live(time.time() if now is None else now)
            if not v or (v.get("by") or None) != (_line(by) or None):
                return None
            self._v = None
            return dict(v)

    def clear(self, by: str | None = None, host: bool = True) -> bool:
        """清掉：主持人能清任何人的，共同主持人只能清自己选的。"""
        with self._lock:
            v = self._v
            if not v or (not host and (v.get("by") or None) != (_line(by) or None)):
                return False
            self._v = None
            return True

    def public(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self._lock:
            v = self._live(now)
            if not v:
                return None
            return {**v, "left_s": max(0, round(self.ttl - (now - v["at"])))}


def anchor_note(a: dict) -> str:
    """送模型那句前面加的：这句指令以选中的那段为准。"""
    where = f"（在 {a['section']}）" if a.get("section") else ""
    return f"【对方先在论文里选中了这一段{where}，这句话说的就是它；要改就改这一段：「{a['text']}」】"
