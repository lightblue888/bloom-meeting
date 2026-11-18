# -*- coding: utf-8 -*-
"""散会后自动保存（设置 → 通用 → 散会后自动保存）：~/.zuhui/autosave.json {"on", "dir", "items"}，默认关。

开着时，散会收尾（转写 → 整理 → 纪要，zuhui/aftermeeting.py）做完后，把勾了的几样存进选的文件夹，文件名是
"日期 会议名 纪要.docx"这样（同名的已经有了：加 (2)、(3)）。items：
  minutes_docx 纪要 Word · minutes_md 纪要 Markdown · todos 修改清单（Markdown）· diff 修订对比（HTML，离线能开）·
  transcript 逐字稿（TXT）
这场没有的（没有修改清单、没有修订对比、没人说话）跳过，不算失败；纪要没生成出来、文件夹没了、写不进去算失败。
结果记在 ~/.zuhui/autosave_last.json，/api/current 带出去，界面提示"保存失败：原因"。

文件夹：完整路径，最多 DIR_MAX 字（再加上文件名也在 Windows 的 260 字以内），要存在、能写。
出错抛 ValueError，参数是界面翻译用的代码：empty、not_abs、too_long、not_dir、no_write、items。"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from .config import STATE

PREFS_FILE = STATE / "autosave.json"
LAST_FILE = STATE / "autosave_last.json"
ITEMS = ("minutes_docx", "minutes_md", "todos", "diff", "transcript")
DEFAULT_ITEMS = ("minutes_docx", "todos", "diff")
DIR_MAX = 180
PATH_MAX = 250                       # 文件夹 + 文件名一共最多多少字（Windows 老程序认 260）
LAST_KEEP_S = 6 * 3600               # 结果给界面看多久
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(p: Path, obj: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def load() -> dict:
    """{"on", "dir", "items"}；没设过、文件坏了：关着、没有文件夹、默认那几样。"""
    d = _read(PREFS_FILE)
    items = [x for x in d.get("items") or [] if x in ITEMS] if isinstance(d.get("items"), list) else list(DEFAULT_ITEMS)
    folder = str(d.get("dir") or "")[:DIR_MAX]
    return {"on": bool(d.get("on")) and bool(folder), "dir": folder, "items": [x for x in ITEMS if x in items]}


def check_dir(path) -> str:
    """选的文件夹：完整路径、不太长、在、能写。返回整理过的路径。"""
    s = _CTRL.sub("", str(path or "")).strip().strip('"').strip()
    if not s:
        raise ValueError("empty")
    if len(s) > DIR_MAX:
        raise ValueError("too_long")
    p = Path(s).expanduser()
    if not p.is_absolute():
        raise ValueError("not_abs")
    if not p.is_dir():
        raise ValueError("not_dir")
    probe = p / f".bloom-{uuid.uuid4().hex[:8]}.tmp"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        raise ValueError("no_write") from None
    return str(p)


def save(body: dict) -> dict:
    """{"on"?, "dir"?, "items"?}：给了哪个改哪个，返回存好的。开着就得有文件夹、至少一样。"""
    cur = load()
    if "dir" in body:
        cur["dir"] = check_dir(body.get("dir")) if str(body.get("dir") or "").strip() else ""
        if not cur["dir"] and "on" not in body:
            cur["on"] = False                    # 文件夹清掉了：跟着关
    if "items" in body:
        items = body.get("items")
        if not isinstance(items, list) or any(x not in ITEMS for x in items):
            raise ValueError("items")
        cur["items"] = [x for x in ITEMS if x in items]
    if "on" in body:
        cur["on"] = bool(body.get("on"))
    if cur["on"] and not cur["dir"]:
        raise ValueError("empty")
    if cur["on"] and not cur["items"]:
        raise ValueError("items")
    if not cur["dir"]:
        cur["on"] = False
    _write(PREFS_FILE, dict(cur, at=time.strftime("%Y-%m-%dT%H:%M:%S")))
    return cur


def public() -> dict:
    return dict(load(), items_all=list(ITEMS), dir_max=DIR_MAX, last=last_result())


def last_result(max_age: float = LAST_KEEP_S) -> dict | None:
    """上一次自动保存的结果（这么久以内的）：{meeting, title, at, ok, error, detail, dir, files, failed}。"""
    d = _read(LAST_FILE)
    try:
        if d and time.time() - float(d.get("at") or 0) <= max_age:
            return d
    except (TypeError, ValueError):
        pass
    return None


def _target(folder: Path, stem: str, word: str, ext: str) -> Path:
    """"日期 会议名 纪要.docx"：太长先截会议名；同名的已经有了加 (2)、(3)。"""
    room = PATH_MAX - len(str(folder)) - len(word) - len(ext) - 8
    stem = stem[:max(10, room)].rstrip(" .")
    p = folder / f"{stem} {word}.{ext}"
    n = 2
    while p.exists():
        p = folder / f"{stem} {word} ({n}).{ext}"
        n += 1
    return p


def run(meetings, mid: str, prefs: dict | None = None, lang: str = "zh", cfg: dict | None = None) -> dict:
    """把这场会勾了的几样存进文件夹。返回结果（也写进 autosave_last.json）。"""
    from . import export as ex
    prefs = prefs or load()
    res = {"meeting": str(mid), "title": "", "at": time.time(), "dir": prefs.get("dir") or "", "files": [], "failed": [],
           "ok": True, "error": None, "detail": None}

    def fail(item: str, code: str, detail: str = "") -> None:
        res["failed"].append(item)
        if res["ok"]:
            res.update(ok=False, error=code, detail=detail or None)

    try:
        g = ex.detail_of(meetings, mid)
    except ex.ExportError as e:
        fail("*", e.code, e.detail)
        g = None
    folder = Path(res["dir"]) if res["dir"] else None
    if g is not None and (folder is None or not folder.is_dir()):
        fail("*", "not_dir", res["dir"])
        g = None
    if g is not None:
        inf = ex.info(g, cfg, lang)
        res["title"] = inf["title"]
        head = [("h1", inf["title"])] + ([("meta", " · ".join(x for x in (inf["when"], inf["dur"]) if x))] if inf["when"] else [])
        for item in prefs.get("items") or []:
            try:
                if item == "minutes_docx":
                    out = ex.html_to_docx(ex.blocks_html(ex.minutes_blocks(g, cfg, lang), rich=False),
                                          _target(folder, inf["stem"], ex.tr("notes", lang), "docx"), lang)
                elif item == "minutes_md":
                    out = _target(folder, inf["stem"], ex.tr("notes", lang), "md")
                    ex.write_text(out, ex.minutes_md(g, cfg, lang))
                elif item == "todos":
                    tb = ex.todos_blocks(g, lang)
                    if not tb:
                        continue
                    out = _target(folder, inf["stem"], ex.tr("changes", lang), "md")
                    ex.write_text(out, ex.blocks_md(head + tb))
                elif item == "diff":
                    page = ex.diff_text(meetings, g["meeting"]["id"])
                    if not page:
                        continue
                    out = _target(folder, inf["stem"], ex.tr("diff", lang), "html")
                    ex.write_text(out, ex.offline_diff(page, ex.figures_of(g["meeting"])))
                elif item == "transcript":
                    tb = ex.transcript_blocks(g, cfg, lang)
                    if not tb:
                        continue
                    out = _target(folder, inf["stem"], ex.tr("transcript", lang), "txt")
                    ex.write_text(out, ex.blocks_text(head + tb).replace("\n", "\r\n"), bom=True)
                else:
                    continue
                res["files"].append(str(out))
            except ex.ExportError as e:
                fail(item, e.code, e.detail)
            except Exception as e:                     # 一样没存成不拦别的几样
                fail(item, "write", repr(e)[:200])
    try:
        _write(LAST_FILE, res)
    except OSError:
        pass
    return res


def after_meeting(meetings, mid: str, lang: str = "zh", cfg: dict | None = None) -> dict | None:
    """散会收尾的最后一步：设置里开着才存。没开返回 None。"""
    prefs = load()
    if not prefs["on"] or not prefs["dir"]:
        return None
    return run(meetings, mid, prefs, lang=lang, cfg=cfg)
