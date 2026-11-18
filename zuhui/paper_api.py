# -*- coding: utf-8 -*-
"""第 6 批（论文）的接口，view.py 转过来：

    GET  /api/paper/prefs                       设置 → 论文：{"docx_mode": "track" | "direct"}
    POST /api/paper/prefs        {docx_mode}    → 现在的；不对 400 {"error": "docx_mode"}
    GET  /api/paper/versions?paper=<编号>       这篇的历次版本（不给编号 = 屏幕上那篇）：{paper, items, current}
    GET  /api/paper/versions/diff?paper=&a=&b=  两版的修订对比页（a、b 是版本号，"cur" = 现在的稿子；embed=1 给 iframe）
    POST /api/paper/versions/restore {paper, v} 恢复到第 v 版（先存一份恢复前的）；开会时 409 {"error": "live"}
    POST /api/comment            {op, ...}      论文上的批注（zuhui/comments.py；访客的经中继，见 GUEST_PATHS）
出错 {"ok": false, "error": 代码}（界面按代码翻译）。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

GET_PATHS = ("/api/paper/prefs", "/api/paper/versions", "/api/paper/versions/diff")
POST_PATHS = ("/api/paper/prefs", "/api/paper/versions/restore", "/api/comment")
GUEST_PATHS = ("/api/comment",)        # 访客经中继能做的（relay/token_server.py 的 ACT_PATHS 里也有）


def _paper_path(viewer, pid: str) -> tuple[Path | None, str | None]:
    """论文编号 → (稿子路径, 名字)：屏幕上那篇、这场会上的、摊开的单子上的、版本索引里记的。"""
    from .meetings import load_open_papers, paper_id, papers_of
    try:
        if paper_id(viewer.mirror) == pid:
            try:
                name = viewer.paper_info().get("name")
            except Exception:
                name = None
            return Path(viewer.mirror), name
    except Exception:
        pass
    lib = getattr(viewer, "meetings", None)
    cands = (papers_of(lib.current()) if lib is not None and lib.current() else []) + load_open_papers()["papers"]
    for p in cands:
        if p.get("id") == pid:
            return Path(p["path"]), p.get("name")
    return None, None


def _pid(viewer, q: dict) -> str:
    from .meetings import paper_id
    pid = str((q.get("paper") or [""])[0] or "")
    if not pid:
        try:
            pid = paper_id(viewer.mirror)
        except Exception:
            pid = ""
    return pid


def get(viewer, raw_path: str) -> tuple[int, bytes | dict, str]:
    """→ (状态码, 内容, 类型)：类型是 "json" 时内容是 dict，否则是字节。"""
    from . import paper_prefs, paper_versions
    u = urlsplit(raw_path)
    p, q = u.path, parse_qs(u.query)
    if p == "/api/paper/prefs":
        return 200, paper_prefs.load(), "json"
    pid = _pid(viewer, q)
    if not pid:
        return 404, {"ok": False, "error": "no_paper"}, "json"
    if p == "/api/paper/versions":
        path, name = _paper_path(viewer, pid)
        out = paper_versions.info(pid, path, name)
        lib = getattr(viewer, "meetings", None)
        out["live"] = bool(lib is not None and lib.current())
        return 200, out, "json"
    if p == "/api/paper/versions/diff":
        ui = (q.get("ui") or [getattr(viewer, "ui_lang", "zh")])[0]
        a, b = (q.get("a") or [""])[0], (q.get("b") or ["cur"])[0]
        page, why = paper_versions.diff_page(pid, a, b, ui)
        if page is None:
            msg = "找不到这一版" if why == "not_found" else "对比生成失败"
            return 404, msg.encode("utf-8"), "text/plain; charset=utf-8"
        if "embed=1" in raw_path:
            from .view import _embed, _inject
            inj = _inject(None, [], viewer.me_name(), {}, ui)
            page = page.replace("</body>", inj + "</body>", 1) if "</body>" in page else page + inj
            page = _embed(page, local_fonts="host=bloom" in raw_path)
        return 200, page.encode("utf-8"), "text/html; charset=utf-8"
    return 404, {"ok": False, "error": "not_found"}, "json"


def post(viewer, path: str, body: dict, by: str | None = None, guest: bool = False) -> tuple[dict, int]:
    from . import comments, paper_prefs, paper_versions
    body = body if isinstance(body, dict) else {}
    if path == "/api/comment":
        return comments.post(viewer, body, by=by, guest=guest)
    if guest:
        return {"ok": False, "error": "host_only"}, 403
    if path == "/api/paper/prefs":
        try:
            return {"ok": True, **paper_prefs.save(body)}, 200
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        except OSError:
            return {"ok": False, "error": "failed"}, 500
    if path == "/api/paper/versions/restore":
        lib = getattr(viewer, "meetings", None)
        if lib is not None and lib.current():
            return {"ok": False, "error": "live"}, 409
        try:
            if viewer.board.counts().get("working"):
                return {"ok": False, "error": "busy"}, 409
        except Exception:
            pass
        pid = str(body.get("paper") or "")
        try:
            it = paper_versions.restore(pid, str(body.get("v") or ""))
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 404 if str(e) in ("not_found", "gone") else 500
        path_p, _ = _paper_path(viewer, pid)
        try:                                  # 恢复的是屏幕上这篇：以恢复后的为原文（页面上没有修订标记），重读论文
            from .meetings import paper_id
            if path_p is not None and paper_id(viewer.mirror) == pid:
                viewer.ensure_base(rebase=True)
                viewer._sig = None
                cb = getattr(viewer, "on_edit", None)
                if callable(cb):
                    cb()
            elif path_p is not None:
                viewer.rebase_paper(path_p)
        except Exception:
            pass
        log = getattr(viewer, "on_log", None)
        if callable(log):
            try:
                log("ready", text=f"论文恢复到第 {body.get('v')} 版")
            except Exception:
                pass
        return {"ok": True, "item": it, **paper_versions.info(pid)}, 200
    return {"ok": False, "error": "not_found"}, 404
