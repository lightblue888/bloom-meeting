# -*- coding: utf-8 -*-
"""论文跨会议的版本历史：每篇论文每场会开会前、散会后的稿子存一份，能看历次版本、任选两版对比、恢复到某一版。

存在 ~/.zuhui/versions/ 下：
    blobs/<sha1 前两位>/<sha1>.gz    稿子的内容（gzip），按内容的 sha1 存：同样的内容只存一份（几篇论文之间也共用）
    <论文编号>/index.json            {"paper": {id, name, path}, "items": [{v, sha, at, kind, mid, title, chars, of?}]}
kind：before 开会前 / after 散会后 / backup 恢复前（恢复之前先把当时的稿子存一份）/ restore 恢复成了哪一版（of = 那一版的 v）。
和上一版内容一样的开会前 / 散会后不再记一条（会上没改、两场会之间没动过）。每篇最多 MAX_ITEMS 条，多了删最旧的，
没人再用的 blob 一起删掉。论文编号 = meetings.paper_id（规范化后完整路径的 sha1 前 8 位）。
对比页复用会上的修订对比生成器（view.HTMLDIFF），两版谁早谁在左边（"原文"）。"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import STATE

ROOT = STATE / "versions"
BLOBS = ROOT / "blobs"
MAX_ITEMS = 200
MAX_BYTES = 20_000_000          # 比这大的稿子不存（论文的 Markdown / LaTeX 一般几十到几百 KB）
KINDS = ("before", "after", "backup", "restore")
_lock = threading.RLock()


def _pid(path) -> str:
    from .meetings import paper_id
    return paper_id(path)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _index_p(pid: str) -> Path:
    return ROOT / re.sub(r"[^0-9a-zA-Z_-]", "", str(pid))[:40] / "index.json"


def _load(pid: str) -> dict:
    try:
        d = json.loads(_index_p(pid).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"paper": {"id": pid}, "items": []}


def _save(pid: str, d: dict) -> None:
    p = _index_p(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _blob_p(sha: str) -> Path:
    return BLOBS / sha[:2] / f"{sha}.gz"


def _put_blob(data: bytes) -> str:
    sha = hashlib.sha1(data).hexdigest()
    p = _blob_p(sha)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes(gzip.compress(data, 6))
        os.replace(tmp, p)
    return sha


def _get_blob(sha: str) -> bytes | None:
    try:
        return gzip.decompress(_blob_p(sha).read_bytes())
    except (OSError, EOFError, gzip.BadGzipFile):
        return None


def _text(data: bytes) -> str:
    t = data.decode("utf-8", errors="replace")
    return t[1:] if t.startswith("﻿") else t


def _chars(data: bytes) -> int:
    return len(re.sub(r"\s", "", _text(data)))


def _gc() -> None:
    """没有哪篇论文还用着的 blob 删掉。"""
    used = set()
    try:
        for f in ROOT.glob("*/index.json"):
            try:
                used |= {str(x.get("sha")) for x in json.loads(f.read_text(encoding="utf-8")).get("items") or []}
            except (OSError, ValueError, AttributeError):
                return                       # 读不懂某一份：宁可不删
        for b in BLOBS.glob("*/*.gz"):
            if b.name[:-3] not in used:
                try:
                    b.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def snapshot(path, name: str | None = None, kind: str = "before", mid: str | None = None,
             title: str | None = None, of: str | None = None) -> dict | None:
    """存这篇论文现在的稿子，返回记下的那一条（和上一条内容一样、不用再记时返回上一条；读不到文件返回 None）。"""
    p = Path(str(path))
    if kind not in KINDS:
        raise ValueError("kind")
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_BYTES:
        return None
    pid = _pid(p)
    with _lock:
        d = _load(pid)
        d["paper"] = {"id": pid, "name": str(name or (d.get("paper") or {}).get("name") or p.stem), "path": str(p)}
        sha = _put_blob(data)
        items = d["items"]
        last = items[-1] if items else None
        if last and last.get("sha") == sha and kind in ("before", "after", "backup"):
            _save(pid, d)                    # 和上一条一样（会上没改、两场会之间没动过、恢复前的已经存着）：不再记
            return last
        n = max([int(x.get("v") or 0) for x in items] + [0]) + 1
        item = {"v": str(n), "sha": sha, "at": _now(), "kind": kind, "mid": mid, "title": (title or "")[:80] or None,
                "chars": _chars(data), "bytes": len(data)}
        if of:
            item["of"] = str(of)
        items.append(item)
        drop = len(items) > MAX_ITEMS
        d["items"] = items[-MAX_ITEMS:]
        _save(pid, d)
    if drop:
        _gc()
    return item


def snapshot_meeting(meeting: dict | None, kind: str) -> list[dict]:
    """一场会上摊开的每一篇各存一份（开会时 kind=before，散会后 kind=after）。"""
    from .meetings import papers_of
    if not meeting:
        return []
    out = []
    title = re.sub(r"\s*·\s*\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*$", "", str(meeting.get("title") or "")).strip()
    for p in papers_of(meeting):
        try:
            r = snapshot(p["path"], p.get("name"), kind, meeting.get("id"), title)
        except Exception:
            r = None
        if r:
            out.append(r)
    return out


def info(pid: str, path=None, name: str | None = None) -> dict:
    """{"paper": {id, name}, "items": [新的在前], "current": {sha, chars}|None}。path / name：还没存过版本的论文
    （索引里没有路径）用调用方给的。"""
    with _lock:
        d = _load(pid)
    paper = dict(d.get("paper") or {"id": pid})
    if path and not paper.get("path"):
        paper["path"] = str(path)
    if name and not paper.get("name"):
        paper["name"] = name
    cur = None
    path = paper.get("path")
    if path and Path(path).is_file():
        try:
            data = Path(path).read_bytes()
            cur = {"sha": hashlib.sha1(data).hexdigest(), "chars": _chars(data), "bytes": len(data)}
        except OSError:
            cur = None
    items = [dict(x) for x in reversed(d.get("items") or [])]
    for x in items:
        x["same"] = bool(cur and x.get("sha") == cur["sha"])
    return {"paper": {k: paper.get(k) for k in ("id", "name")}, "items": items, "current": cur,
            "exists": bool(cur)}


def read(pid: str, v: str) -> str | None:
    """某一版的文字；v="cur" 是现在的稿子。"""
    with _lock:
        d = _load(pid)
    if str(v) == "cur":
        path = (d.get("paper") or {}).get("path")
        try:
            return _text(Path(path).read_bytes()) if path else None
        except OSError:
            return None
    it = next((x for x in d.get("items") or [] if str(x.get("v")) == str(v)), None)
    if not it:
        return None
    data = _get_blob(str(it.get("sha")))
    return _text(data) if data is not None else None


def _item(pid: str, v: str) -> dict | None:
    if str(v) == "cur":
        return {"v": "cur", "at": _now()}
    return next((x for x in _load(pid).get("items") or [] if str(x.get("v")) == str(v)), None)


def restore(pid: str, v: str, write=None) -> dict:
    """恢复到第 v 版：先把现在的稿子存成 backup，再整份换成那一版，记一条 restore。
    write(path, data)：写稿子的办法（默认把那一版的字节原子地换上）。出错抛 ValueError(代码)：not_found / gone / failed。"""
    with _lock:
        d = _load(pid)
        it = next((x for x in d.get("items") or [] if str(x.get("v")) == str(v)), None)
        if not it:
            raise ValueError("not_found")
        data = _get_blob(str(it.get("sha")))
        if data is None:
            raise ValueError("not_found")
        paper = d.get("paper") or {}
        path = Path(str(paper.get("path") or ""))
        if not path.is_file():
            raise ValueError("gone")
        snapshot(path, paper.get("name"), "backup")
        try:
            if write is not None:
                write(path, data)
            else:                            # 那一版的字节原样换上（换行、BOM 都是那时候的）
                from .view import _DOC_LOCK, _write_bytes_atomic
                with _DOC_LOCK:
                    _write_bytes_atomic(path, data)
        except OSError:
            raise ValueError("failed")
        return snapshot(path, paper.get("name"), "restore", of=str(v)) or {}


def _order(pid: str, a: str, b: str) -> tuple[str, str]:
    """早的在前（"原文"），晚的在后；现在的稿子最晚。"""
    def key(v):
        if str(v) == "cur":
            return (1, 10 ** 9)
        it = _item(pid, v) or {}
        try:
            return (0, int(it.get("v") or 0))
        except ValueError:
            return (0, 0)
    return (a, b) if key(a) <= key(b) else (b, a)


def label(it: dict | None, en: bool = False) -> str:
    if not it:
        return ""
    if it.get("v") == "cur":
        return "Now" if en else "现在"
    k = {"before": ("开会前", "Before meeting"), "after": ("散会后", "After meeting"),
         "backup": ("恢复前", "Before restore"), "restore": ("恢复", "Restored")}.get(str(it.get("kind")), ("", ""))
    when = str(it.get("at") or "")[5:16].replace("T", " ")
    return f"{k[1 if en else 0]} · {when}"


def diff_page(pid: str, a: str, b: str, ui: str = "zh") -> tuple[str | None, str]:
    """两版的修订对比页（完整页面，不带注入）。返回 (页面 or None, 出错时的原因)。"""
    from .view import HTMLDIFF
    a, b = _order(pid, a, b)
    ta, tb = read(pid, a), read(pid, b)
    if ta is None or tb is None:
        return None, "not_found"
    if not HTMLDIFF.exists():
        return None, "no_generator"
    d = _load(pid)
    en = ui == "en"
    path = str((d.get("paper") or {}).get("path") or "paper.md")
    ext = Path(path).suffix or ".md"
    work = Path(tempfile.mkdtemp(prefix="bloom-ver-"))
    try:
        (work / f"a{ext}").write_bytes(ta.encode("utf-8"))
        (work / f"b{ext}").write_bytes(tb.encode("utf-8"))
        name = str((d.get("paper") or {}).get("name") or Path(path).stem)
        spec = {"title": ("Versions" if en else "版本对比"), "intro": f"{label(_item(pid, a), en)} → {label(_item(pid, b), en)}",
                "ui": "en" if en else "zh", "out": "diff.html", "lang": "auto",
                "docs": [{"name": name, "before": str(work / f"a{ext}"), "after": str(work / f"b{ext}")}],
                "notes": {}, "rules": [], "closing_html": ""}
        (work / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run([sys.executable, "-X", "utf8", str(HTMLDIFF), str(work / "spec.json")], cwd=str(work),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sa = work / "diff.standalone.html"
        if r.returncode != 0 or not sa.exists():
            return None, (r.stderr or r.stdout or "failed")[-300:]
        return sa.read_text(encoding="utf-8"), ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def usage() -> dict:
    """占了多少（设置 → 存储可以用）：{"bytes", "papers", "blobs"}。"""
    total, n = 0, 0
    try:
        for b in BLOBS.glob("*/*.gz"):
            total += b.stat().st_size
            n += 1
    except OSError:
        pass
    return {"bytes": total, "papers": len(list(ROOT.glob("*/index.json"))) if ROOT.exists() else 0, "blobs": n}
