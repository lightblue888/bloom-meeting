# -*- coding: utf-8 -*-
"""会中交互（第 3 批）的接口：主持人在 Bloom 里点的走本机 HTTP（view.py 转到这里），访客在会议页上点的经中继排队、
主持人这边取到后也转到这里（guest=True，by = 访客的名字，谁能做什么在这里按人定）。逻辑在 zuhui/inmeeting.py。

    POST /api/ask         {text}             打字问 AI（主持人、共同主持人）：和说了这句一样，进逐字稿、该念就念
    POST /api/bookmark    {note?} / {id, note}  标记此刻 / 改备注 → {ok, bookmark}
    POST /api/anchor      {text, section} / {clear: true}   先选中再说（主持人、共同主持人）
出错 {"ok": false, "error": 代码}（界面按代码翻译）。"""
from __future__ import annotations

import time

from . import inmeeting

PATHS = ("/api/ask", "/api/bookmark", "/api/anchor")
# 访客经中继能做的（relay/token_server.py 的 ACT_PATHS 里也要有）：标记谁都能；打字问 AI、选中一段只有共同主持人
GUEST_PATHS = ("/api/ask", "/api/bookmark", "/api/anchor")


def _err(code: str, status: int = 400) -> tuple[dict, int]:
    return {"ok": False, "error": code}, status


def anchor_of(viewer) -> inmeeting.Anchor:
    a = getattr(viewer, "anchor", None)
    if a is None:
        a = viewer.anchor = inmeeting.Anchor()
    return a


def _log(viewer, text: str) -> None:
    fn = getattr(viewer, "on_log", None)
    if callable(fn):
        try:
            fn("live_act", text=text)
        except Exception:
            pass


def _names(viewer):
    import getpass
    from .config import assistant_name
    cfg = getattr(viewer, "cfg", None) or {}
    me = str((cfg.get("meeting") or {}).get("me") or getpass.getuser() or "主持人")
    return me, assistant_name(cfg)


def post(viewer, path: str, body, by: str | None = None, guest: bool = False) -> tuple[dict, int]:
    """处理一件事 → (回给页面的, HTTP 状态码)。guest=True：访客经中继来的，by 是他的名字。"""
    from . import roles
    body = body if isinstance(body, dict) else {}
    lib = getattr(viewer, "meetings", None)
    cur = lib.current() if lib is not None else None
    if guest and path not in GUEST_PATHS:
        return _err("host_only", 403)
    if not cur:
        return _err("not_live", 409)
    if path == "/api/ask":
        if guest and not roles.is_cohost(by):          # 打字问 AI：只有主持人和共同主持人
            return _err("cohost_only", 403)
        fn = getattr(viewer, "on_ask", None)
        if not callable(fn):
            return _err("not_ready", 503)
        try:
            fn(body.get("text"), by=by if guest else None)
        except ValueError as e:
            return _err(str(e), 409 if str(e) == "not_live" else 400)
        return {"ok": True}, 200
    if path == "/api/bookmark":
        me, _ = _names(viewer)
        name = by if guest else me
        try:
            if body.get("id"):
                b = inmeeting.update_bookmark(lib.live_dir(), str(body.get("id")), body.get("note"), by=name, host=not guest)
            else:
                b = inmeeting.add_bookmark(lib.live_dir(), lib.elapsed() or 0.0, body.get("note"), by=name, host=not guest)
        except ValueError as e:
            return _err(str(e), 404 if str(e) == "not_found" else 403 if str(e) == "not_yours" else 400)
        except OSError:
            return _err("failed", 500)
        _log(viewer, f"{name} 标记 {b['t']}" + (f"：{b['note']}" if b.get("note") else ""))
        return {"ok": True, "bookmark": b}, 200
    if path == "/api/anchor":
        if guest and not roles.is_cohost(by):
            return _err("cohost_only", 403)
        a = anchor_of(viewer)
        if body.get("clear"):
            a.clear(by=by if guest else None, host=not guest)
            return {"ok": True, "anchor": None}, 200
        try:
            v = a.set(body.get("text"), body.get("section"), by=by if guest else None)
        except ValueError as e:
            return _err(str(e))
        return {"ok": True, "anchor": v}, 200
    return _err("not_found", 404)


def extras(viewer, host: bool = True, last_mid: str | None = None) -> dict:
    """/api/current（host=True）和访客状态（host=False）里多带的：书签、选中的那段；
    主持人的还有多久没人说话（quiet_s）。没开会时：上一场的书签。"""
    lib = getattr(viewer, "meetings", None)
    if lib is None:
        return {}
    now = time.time()
    cur = lib.current()
    out: dict = {}
    if not cur:
        if host and last_mid:
            out["bookmarks"] = inmeeting.load_bookmarks(lib.root / str(last_mid))
        return out
    d = lib.live_dir()
    out["bookmarks"] = inmeeting.load_bookmarks(d)
    out["anchor"] = anchor_of(viewer).public(now)
    if host:
        q = getattr(viewer, "quiet", None)
        try:
            out["quiet_s"] = round(float(q() if callable(q) else
                                         inmeeting.quiet_seconds(now, lib.t0, lib.current_lines())))
        except Exception:
            out["quiet_s"] = 0
    return out
