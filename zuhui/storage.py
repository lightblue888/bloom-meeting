# -*- coding: utf-8 -*-
"""设置 → 存储：Bloom 在这台电脑上占了多少、各是什么，能清的怎么清。

    GET  /api/storage                  scan(viewer)：总共占多少、磁盘还剩多少、四类（录屏 / 模型 / 会议记录 / 其他）各多少，
                                       每一类的明细（见 scan 的返回）
    POST /api/storage/recordings       {"days": 30|90|180|null, "which": "meetings"|"others"}：这些录屏送进回收站
    POST /api/storage/auto             {"days": 0|30|90|180}：自动删除录屏（0 = 从不）；存下来马上按新期限清一次
    POST /api/storage/models           {"paths": [...]}：不用的模型送进回收站（只认 scan 里列为不用的）
    POST /api/storage/caches           {}：清临时文件（语音缓存不动）
    POST /api/storage/retention        {"days": 0|30|90|180|365}：自动删除旧会议的逐字稿和纪要（0 = 从不，默认）；
                                       存下来马上按新期限清一次（会删哪些先由 scan 的 records.retention.preview 列给你确认）

逐字稿和纪要的保留期（第 8 批）：到期的会议，逐字稿（各个版本、删改清单、重转写记录、手改记录 transcript.edits.json，
录屏文件夹里录屏软件自己那份转写）、纪要、书签、遗留勾选送进回收站（RECORD_FILES、SESSION_RECORD_FILES）。留着：
meta.json（会议名、时间、时长、论文）、participants.json（谁来了）、board.json（修改清单）、
diff.html（修订对比）、usage.json（用量）、录屏（录屏有自己的期限）、截图。修改清单和修订
对比是论文改了什么，论文本身也留着，所以不删；它们每条修改里带着当时说的那句话（board.json 的 remark）。散会后自动
保存到你选的文件夹里的那几份不动。删过的会在 meta.json 记 records_removed（时间）和 records_days（按几天删的）。
按散会时间（没有就开会时间）算几天前；正在开的会、还在收尾的会不动。示例会议不在会议库里，不受影响。

录屏 = 会议录屏文件夹（meta.json 的 session，Blurt 录屏软件建的 …/sessions/<时间戳>/）里的录像和录音：recording.*
（录像，连同 recording.scan.npz）、audio.*、live/audio.pcm。逐字稿、截图（meeting/shot-*.png，逐字稿里的行指着它们）、
meta.json 都留着；会议库（~/.zuhui/meetings/<id>/）里的逐字稿、纪要、修订记录一概不碰。删了录屏的会照样在回顾里，只是
没有录像（view._video_of 找不到文件就是 None，回顾页按没录屏画）。

自动删除录屏：期限存在 ~/.zuhui/storage.json（{"auto_days": 90}），后端起来后清一次、之后每天一次（start_auto_clean）。
正在开的会、散会后还在收尾的会（viewer.processing）、正在录的那段（viewer.busy_session、Blurt 正在跑的录屏任务）、
最近 15 分钟还在写的录屏，一律不动。

送回收站用 Windows 自己的回收站（找得回来）；回收站放不下、要彻底删时 Windows 会先问（FOF_WANTNUKEWARNING）。
ZUHUI_HOME 指到别处（测试）时只动测试目录（ZUHUI_HOME / BLURT_HOME / BLURT_WORKSPACE）里面的东西，真的 ~/.zuhui、
~/.blurt、~/Blurt 只读大小。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from .config import ROOT, STATE

log = logging.getLogger("zuhui.storage")

SETTINGS = STATE / "storage.json"
AUTO_CHOICES = (0, 30, 90, 180)
AGES = (30, 90, 180)
FRESH_S = 15 * 60                  # 录屏文件这么久之内还在写：当它正在录，不动
TINY = 2 * 1000 * 1000             # 小于这个的内部文件（语音活动检测那 0.6 MB）不单列
TMP_AGE_S = 24 * 3600              # 临时文件一天没动过才清


# ---------------------------------------------------------------- 在哪
def blurt_home() -> Path:
    return Path(os.environ.get("BLURT_HOME") or (Path.home() / ".blurt"))


def workspace() -> Path:
    return Path(os.environ.get("BLURT_WORKSPACE") or (Path.home() / "Blurt"))


def session_roots() -> list[Path]:
    """录屏软件放录屏的地方：收件箱的 sessions/，还有老版本的 ~/Blurt/recordings（和 Rust 的 session_roots 一样）。"""
    return [p for p in (workspace() / "inbox" / "sessions", workspace() / "recordings") if p.is_dir()]


def _key(p) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(p)))
    except (TypeError, ValueError):
        return ""


def _under(p, root) -> bool:
    a, b = _key(p), _key(root)
    return bool(a and b) and (a == b or a.startswith(b.rstrip("\\/") + os.sep))


def _sandboxed() -> bool:
    return _key(STATE) != _key(Path.home() / ".zuhui")


def _touchable(p) -> bool:
    """能不能送回收站 / 删：真的 ~/.zuhui 在用时都能；测试（ZUHUI_HOME 指到别处）时只能动测试目录里面的。"""
    if not _sandboxed():
        return True
    real = {_key(Path.home() / n) for n in (".zuhui", ".blurt", "Blurt")}
    roots = [STATE] + [Path(v) for v in (os.environ.get("BLURT_HOME"), os.environ.get("BLURT_WORKSPACE")) if v]
    return any(_under(p, r) for r in roots if _key(r) not in real)


# ---------------------------------------------------------------- 大小
def du(p) -> int:
    """文件或整个文件夹的字节数（不跟着链接走）；不存在是 0。"""
    try:
        st = os.lstat(p)
    except OSError:
        return 0
    if not os.path.isdir(p) or os.path.islink(p):
        return st.st_size
    total, stack = 0, [str(p)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _children(d: Path) -> list[Path]:
    try:
        return sorted(Path(e.path) for e in os.scandir(d) if not e.name.startswith("."))
    except OSError:
        return []


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def free_bytes() -> int | None:
    try:
        return shutil.disk_usage(str(STATE)).free
    except OSError:
        return None


# ---------------------------------------------------------------- 送回收站
def _recycle(paths: list[Path]) -> list[Path]:
    """一批文件 / 文件夹送进回收站，返回真的没了的那些。Windows：外壳自己的删除（FOF_ALLOWUNDO = 进回收站，
    回收站放不下要彻底删时先问）；别的系统：挪进 ~/.zuhui/trash/<时间>/。测试里换成假的（不碰真的回收站）。"""
    paths = [Path(p) for p in paths if Path(p).exists()]
    if not paths:
        return []
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 3, 0x4, 0x10, 0x40, 0x400, 0x4000
        src = "".join(str(p) + chr(0) for p in paths)
        op = SHFILEOPSTRUCTW(None, FO_DELETE, src, None,
                             FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOF_WANTNUKEWARNING,
                             False, None, None)
        ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    else:
        dst = STATE / "trash" / time.strftime("%Y%m%d-%H%M%S")
        for p in paths:
            try:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(dst / f"{p.parent.name}-{p.name}"))
            except OSError:
                pass
    return [p for p in paths if not p.exists()]


# ---------------------------------------------------------------- 录屏
MEDIA_PREFIX = ("recording.", "audio.")


def media_files(sd: Path) -> list[Path]:
    """一段录屏里的录像和录音（删掉也不影响逐字稿、截图）。"""
    out = []
    try:
        with os.scandir(sd) as it:
            for e in it:
                if e.name.lower().startswith(MEDIA_PREFIX) and e.is_file(follow_symlinks=False):
                    out.append(Path(e.path))
    except OSError:
        return []
    pcm = Path(sd) / "live" / "audio.pcm"
    if pcm.is_file():
        out.append(pcm)
    return sorted(out)


def _meetings_root(viewer) -> Path:
    m = getattr(viewer, "meetings", None)
    return Path(getattr(m, "root", None) or (STATE / "meetings"))


def _is_session_dir(p: Path) -> bool:
    from .meetings import _is_recording_dir
    return p.is_dir() and (_is_recording_dir(p) or any(_under(p, r) and _key(p) != _key(r) for r in session_roots()))


def _running_jobs() -> set[str]:
    """录屏软件正在跑的录屏任务的那段（会议还没建起来时也算在录）。"""
    out = set()
    jobs = blurt_home() / "jobs"
    try:
        names = [e.path for e in os.scandir(jobs) if e.name.startswith("j-") and e.name.endswith(".json")]
    except OSError:
        return out
    for f in names:
        j = _read_json(Path(f))
        if isinstance(j, dict) and j.get("type") == "record" and j.get("state") in ("running", "queued") and j.get("session"):
            out.add(_key(j["session"]))
    return out


def _busy(viewer, linked: dict[str, dict]) -> set[str]:
    """不能动的录屏：正在开的会、还在收尾的会、正在录的那段、录屏软件正在录的。"""
    out = set(_running_jobs())
    m = getattr(viewer, "meetings", None)
    try:
        cur = m.current() if m is not None else None
        if cur and cur.get("session"):
            out.add(_key(cur["session"]))
    except Exception:
        pass
    fn = getattr(viewer, "busy_session", None)
    if callable(fn):
        try:
            s = fn()
            if s:
                out.add(_key(s))
        except Exception:
            pass
    proc = getattr(viewer, "processing", None)
    for k, meta in linked.items():
        if meta.get("state") == "live":
            out.add(k)
            continue
        if callable(proc):
            try:
                if proc(str(meta.get("id") or "")):
                    out.add(k)
            except Exception:
                pass
    return out


def sessions(viewer=None) -> list[dict]:
    """每段录屏：{path, meeting（会议编号，没有会议的是 None）, media（录像录音文件）, bytes（它们的大小）,
    rest（文件夹里留着的：逐字稿、截图……）, age_days, busy}。会议指着的在前，录屏目录里没有会议的在后。"""
    linked: dict[str, dict] = {}
    for d in _children(_meetings_root(viewer)):
        meta = _read_json(d / "meta.json")
        if isinstance(meta, dict) and meta.get("session"):
            linked.setdefault(_key(meta["session"]), meta)
    busy = _busy(viewer, linked)
    now = time.time()
    rows, seen = [], set()

    def row(p: Path, meta: dict | None) -> dict:
        media, sizes, newest = [], {}, 0.0
        for f in media_files(p):
            try:
                st = f.stat()
            except OSError:
                continue
            media.append(f)
            sizes[_key(f)] = st.st_size
            newest = max(newest, st.st_mtime)
        if not newest:
            try:
                newest = p.stat().st_mtime
            except OSError:
                newest = now
        total = sum(sizes.values())
        return {"path": str(p), "meeting": (str(meta.get("id")) if meta else None), "media": media, "sizes": sizes,
                "bytes": total, "rest": max(0, du(p) - total), "age_days": max(0.0, (now - newest) / 86400),
                "busy": _key(p) in busy or (total > 0 and now - newest < FRESH_S)}

    for k, meta in linked.items():
        p = Path(str(meta["session"]))
        if k in seen or not _is_session_dir(p):
            continue
        seen.add(k)
        rows.append(row(p, meta))
    for r in session_roots():
        for p in _children(r):
            k = _key(p)
            if k in seen or not p.is_dir():
                continue
            seen.add(k)
            rows.append(row(p, None))
    return rows


def _pick(rows: list[dict], days: int | None, which: str) -> list[dict]:
    """能送回收站的那些：有录屏、没在用、至少 days 天了（None = 不论多久）；which：meetings / others / all。"""
    out = []
    for r in rows:
        if not r["bytes"] or r["busy"] or (days and r["age_days"] < days):
            continue
        if which == "meetings" and not r["meeting"]:
            continue
        if which == "others" and r["meeting"]:
            continue
        out.append(r)
    return out


def _tally(rows: list[dict]) -> dict:
    return {"count": len(rows), "bytes": sum(r["bytes"] for r in rows)}


def _stamp(sd: Path) -> None:
    """录屏没了：会话 meta.json 记一笔 video_removed（和录屏软件自己删视频时一样），文件夹里别的照旧。"""
    f = sd / "meta.json"
    if not f.is_file():
        return
    m = _read_json(f)
    if not isinstance(m, dict):
        return
    m["video_removed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = f.with_name(f.name + ".storage.tmp")
    try:
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass


_lock = threading.Lock()


def trash_recordings(viewer=None, days: int | None = None, which: str = "all") -> dict:
    """录屏（录像和录音）送回收站，逐字稿、截图、纪要、修订记录都留着 → {count, bytes}。"""
    with _lock:
        picked = [r for r in _pick(sessions(viewer), days, which) if _touchable(r["path"])]
        n, freed = 0, 0
        for r in picked:
            files = [f for f in r["media"] if _touchable(f)]
            gone = _recycle(files)
            if gone:
                _stamp(Path(r["path"]))
                n += 1
                freed += sum(r["sizes"].get(_key(f), 0) for f in gone)
        if n:
            log.info("录屏送回收站：%d 段，%.1f MB（days=%s, which=%s）", n, freed / 1e6, days, which)
        return {"count": n, "bytes": freed}


# ---------------------------------------------------------------- 自动删除
def auto_days() -> int:
    d = _read_json(SETTINGS)
    try:
        v = int((d or {}).get("auto_days") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0
    return v if v in AUTO_CHOICES else 0


def set_auto(viewer, days) -> dict:
    """自动删除录屏的期限（0 = 从不）存进 ~/.zuhui/storage.json，马上按新期限清一次 → {auto_days, count, bytes}。"""
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        raise ValueError("days")
    if days not in AUTO_CHOICES:
        raise ValueError("days")
    d = _read_json(SETTINGS)
    d = d if isinstance(d, dict) else {}
    d["auto_days"] = days
    d["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, SETTINGS)
    out = apply_auto(viewer)
    return {"auto_days": days, **out}


def apply_auto(viewer=None) -> dict:
    days = auto_days()
    if not days:
        return {"count": 0, "bytes": 0}
    return trash_recordings(viewer, days, "all")


_auto_started = False


def start_auto_clean(viewer, every: float = 24 * 3600, delay: float = 120.0) -> None:
    """后端起来后：等一会儿（会议接上、收尾状态读好）清一次，之后每天一次。只起一次。"""
    global _auto_started
    if _auto_started:
        return
    _auto_started = True

    def run():
        time.sleep(delay)
        while True:
            try:
                r = apply_auto(viewer)
                if r["count"]:
                    log.info("自动删除录屏：%d 段，%.1f MB", r["count"], r["bytes"] / 1e6)
            except Exception as e:          # 一次没清成不该让线程死掉
                log.warning("自动删除录屏没成：%r", e)
            try:
                r = apply_records_auto(viewer)
                if r["count"]:
                    log.info("自动删除逐字稿和纪要：%d 场", r["count"])
            except Exception as e:
                log.warning("自动删除逐字稿和纪要没成：%r", e)
            time.sleep(every)

    threading.Thread(target=run, name="storage-auto-clean", daemon=True).start()


# ---------------------------------------------------------------- 逐字稿和纪要的保留期（见文件头）
RECORD_CHOICES = (0, 30, 90, 180, 365)
RECORD_AGES = (30, 90, 180, 365)
RECORD_FILES = ("transcript.jsonl", "transcript.live.jsonl", "transcript.clean.jsonl", "cleanup.json", "retranscribe.json",
                "transcript.edits.json", "summary.json", "summary.md", "bookmarks.json", "carry.json", "carry_in.json")
SESSION_RECORD_FILES = ("transcript.json", "transcript.txt", "live/transcript.jsonl", "live/preview.json")
PREVIEW_MAX = 200                  # 确认框里最多列这么多场（再多只报个数）


def records_days() -> int:
    d = _read_json(SETTINGS)
    try:
        v = int((d or {}).get("records_days") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0
    return v if v in RECORD_CHOICES else 0


def _iso_ts(s) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(s)).timestamp() if s else None
    except (TypeError, ValueError):
        return None


def _clean_title(t) -> str:
    import re
    return re.sub(r"\s*·\s*\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*$", "", str(t or "")).strip()


def record_rows(viewer=None) -> list[dict]:
    """会议库里还有逐字稿或纪要的每场会：{id, title, started, age_days, files（会议文件夹里的）, session_files
    （录屏文件夹里那份转写，别的会也指着这段录屏、或者它正在录时不算）, bytes, busy}。新的在前。"""
    mroot = _meetings_root(viewer)
    metas: list[tuple[Path, dict]] = []
    for d in _children(mroot):
        m = _read_json(d / "meta.json")
        if isinstance(m, dict) and m.get("id"):
            metas.append((d, m))
    linked: dict[str, dict] = {}
    by_session: dict[str, list[str]] = {}
    for _d, m in metas:
        if m.get("session"):
            k = _key(m["session"])
            linked.setdefault(k, m)
            by_session.setdefault(k, []).append(str(m["id"]))
    busy_sessions = _busy(viewer, linked)
    proc = getattr(viewer, "processing", None)
    now = time.time()
    rows = []
    for d, m in sorted(metas, key=lambda x: x[0].name, reverse=True):
        if m.get("sample"):
            continue
        files = [d / n for n in RECORD_FILES if (d / n).is_file()]
        sfiles: list[Path] = []
        sd = Path(str(m["session"])) if m.get("session") else None
        if sd is not None and _is_session_dir(sd) and _key(sd) not in busy_sessions:
            sfiles = [sd / n for n in SESSION_RECORD_FILES if (sd / n).is_file()]
        if not files and not sfiles:
            continue
        busy = m.get("state") == "live"
        if not busy and callable(proc):
            try:
                busy = bool(proc(str(m.get("id") or "")))
            except Exception:
                busy = False
        t = _iso_ts(m.get("ended")) or _iso_ts(m.get("started"))
        if t is None:
            try:
                t = d.stat().st_mtime
            except OSError:
                t = now
        rows.append({"id": str(m["id"]), "title": _clean_title(m.get("title")), "started": m.get("started"),
                     "age_days": max(0.0, (now - t) / 86400), "files": files, "session_files": sfiles,
                     "session": _key(sd) if sd is not None else None,
                     "bytes": sum(du(f) for f in files + sfiles), "busy": busy, "dir": d})
    for r in rows:
        r["shared"] = len(by_session.get(r["session"] or "", [])) > 1
    return rows


def _pick_records(rows: list[dict], days: int) -> list[dict]:
    return [r for r in rows if days and not r["busy"] and r["age_days"] >= days]


def records_preview(rows: list[dict], days: int) -> dict:
    """打开 days 天的保留期时马上会删的：{count, bytes, meetings: [{id, title, started}]}（最多 PREVIEW_MAX 场）。"""
    picked = _pick_records(rows, days)
    return {"count": len(picked), "bytes": sum(r["bytes"] for r in picked),
            "meetings": [{"id": r["id"], "title": r["title"], "started": r["started"]} for r in picked[:PREVIEW_MAX]]}


def _stamp_records(d: Path, days: int) -> None:
    f = d / "meta.json"
    m = _read_json(f)
    if not isinstance(m, dict):
        return
    m["records_removed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    m["records_days"] = int(days)
    tmp = f.with_name(f.name + ".storage.tmp")
    try:
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass


def trash_records(viewer=None, days: int = 0) -> dict:
    """到期会议的逐字稿和纪要送回收站（见文件头）→ {count（场）, bytes}。录屏文件夹里那份转写：指着这段录屏的会都到期了才删。"""
    if not days:
        return {"count": 0, "bytes": 0}
    with _lock:
        rows = record_rows(viewer)
        picked = _pick_records(rows, days)
        ids = {r["id"] for r in picked}
        keep_sessions = {r["session"] for r in rows if r["id"] not in ids and r["session"]}
        n, freed = 0, 0
        for r in picked:
            files = [f for f in r["files"] if _touchable(f)]
            if r["session"] not in keep_sessions:
                files += [f for f in r["session_files"] if _touchable(f)]
            if not files:
                continue
            sizes = {_key(f): du(f) for f in files}
            gone = _recycle(files)
            if gone:
                _stamp_records(r["dir"], days)
                n += 1
                freed += sum(sizes.get(_key(f), 0) for f in gone)
        if n:
            log.info("逐字稿和纪要送回收站：%d 场，%.1f MB（%d 天前的）", n, freed / 1e6, days)
        return {"count": n, "bytes": freed}


def set_records_auto(viewer, days) -> dict:
    """自动删除逐字稿和纪要的期限（0 = 从不）存进 ~/.zuhui/storage.json，马上按新期限清一次 → {days, count, bytes}。"""
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        raise ValueError("days")
    if days not in RECORD_CHOICES:
        raise ValueError("days")
    with _lock:
        d = _read_json(SETTINGS)
        d = d if isinstance(d, dict) else {}
        d["records_days"] = days
        d["records_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, SETTINGS)
    out = trash_records(viewer, days) if days else {"count": 0, "bytes": 0}
    return {"days": days, **out}


def apply_records_auto(viewer=None) -> dict:
    return trash_records(viewer, records_days())


# ---------------------------------------------------------------- 模型
NAMES = {
    "fun-asr-nano-int8": "Fun-ASR Nano",
    "qwen3-asr-0.6b-int8": "Qwen3-ASR 0.6B",
    "sensevoice": "SenseVoice Small",
    "faster-whisper": "Whisper",
    "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx": "CAM++",
    "pyannote-segmentation-3-0.int8.onnx": "pyannote",
    "smart-turn-v3.2-cpu.onnx": "Smart Turn",
    "silero_vad.onnx": "Silero VAD",
}
VOICE_FILE = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
TURN_FILE = "smart-turn-v3.2-cpu.onnx"
VAD_FILE = "silero_vad.onnx"
LIVE_DIR = "fun-asr-nano-int8"
ROLE_ORDER = ("live", "after", "addr", "voice", "turn", "asr")


def _asr_catalog() -> list[dict]:
    """录屏软件的转写模型表（asr_models.json）：认模型文件夹叫什么、设置里选的是哪个文件夹。"""
    for d in (os.environ.get("BLURT_SCRIPTS"), ROOT / "blurt" / "skills" / "blurt" / "scripts"):
        if not d:
            continue
        doc = _read_json(Path(d) / "asr_models.json")
        if isinstance(doc, dict) and isinstance(doc.get("models"), list):
            return [m for m in doc["models"] if isinstance(m, dict)]
    return []


def _catalog_dir(m: dict) -> str | None:
    """模型表里一个模型装在 models/ 下的哪个文件夹（相对路径）。"""
    inst = m.get("install") or {}
    if inst.get("dir"):
        return str(inst["dir"])
    if inst.get("files"):
        first = next(iter(inst["files"]), "")
        return str(Path(first).parent) if first else None
    if inst.get("hf"):
        return "asr/" + str(inst["hf"]).split("/")[-1]
    return None


def _model_entries() -> list[tuple[Path, str]]:
    """模型文件夹里的每一个：(路径, 在哪：blurt / bloom / trained)。asr/、polish/ 往里再看一层。"""
    out = []
    bm = blurt_home() / "models"
    for e in _children(bm):
        if e.name in ("asr", "polish") and e.is_dir():
            out += [(x, "blurt") for x in _children(e)]
        else:
            out.append((e, "blurt"))
    out += [(e, "bloom") for e in _children(STATE / "models")]
    out += [(e, "trained") for e in _children(STATE / "asr_train" / "models")]
    return out


def _in_use(viewer) -> dict[str, tuple[str, str | None]]:
    """在用的模型：路径 key → (role, lang)。"""
    cfg = getattr(viewer, "cfg", None) or {}
    bm = blurt_home() / "models"
    used: dict[str, tuple[str, str | None]] = {}
    after = None
    a = _read_json(STATE / "asr_train" / "active.json")
    if isinstance(a, dict) and a.get("model_dir") and Path(str(a["model_dir"])).is_dir():
        after = Path(str(a["model_dir"]))
    used[_key(bm / LIVE_DIR)] = ("live", None)
    if after is not None and _key(after) != _key(bm / LIVE_DIR):
        used[_key(after)] = ("after", None)
    # 录屏软件设置里明确选了的转写模型（实时、录完、备注……）：也在用
    bc = _read_json(blurt_home() / "config.json")
    picked = ((bc or {}).get("asr") or {}).get("models") if isinstance(bc, dict) else None
    if isinstance(picked, dict) and picked:
        cat = {m.get("id"): m for m in _asr_catalog()}
        for purpose, mid in picked.items():
            rel = _catalog_dir(cat.get(mid) or {})
            if rel and _key(bm / rel) not in used:
                used[_key(bm / rel)] = ("live" if purpose == "live" else "asr", None)
    used[_key(bm / VOICE_FILE)] = ("voice", None)
    used[_key(bm / TURN_FILE)] = ("turn", None)
    used[_key(bm / VAD_FILE)] = ("vad", None)
    ac = cfg.get("addr") or {}
    if ac.get("enabled"):
        from . import addr
        for lang in ("zh", "en"):
            try:
                p = addr.model_path({**cfg, "meeting": {**(cfg.get("meeting") or {}), "lang": lang}})
            except Exception:
                continue
            if p and Path(p).exists() and _key(p) not in used:
                used[_key(p)] = ("addr", lang)
    return used


def _model_name(p: Path) -> str:
    if p.name in NAMES:
        return NAMES[p.name]
    for m in _asr_catalog():
        rel = _catalog_dir(m)
        if rel and _key(blurt_home() / "models" / rel) == _key(p) and m.get("name"):
            return str(m["name"])
    if (p / "llm.int8.onnx").exists() and (p / "encoder_adaptor.int8.onnx").exists():
        return "Fun-ASR Nano"
    return p.stem if p.is_file() else p.name


def models(viewer=None) -> dict:
    """{bytes（全部，含不单列的小文件）, used: [{role, name, paths, bytes, langs}], unused: [{path, name, bytes}]}。"""
    used_map = _in_use(viewer)
    groups: dict[str, dict] = {}
    unused, total = [], 0
    for p, _where in _model_entries():
        b = du(p)
        total += b
        hit = used_map.get(_key(p))
        if hit:
            role, lang = hit
            if role == "vad":                  # 语音活动检测（0.6 MB）：算进总数，不单列
                continue
            g = groups.setdefault(role, {"role": role, "name": _model_name(p), "paths": [], "bytes": 0, "langs": []})
            g["paths"].append(str(p))
            g["bytes"] += b
            if lang and lang not in g["langs"]:
                g["langs"].append(lang)
        elif b >= TINY:
            unused.append({"path": str(p), "name": _model_name(p), "bytes": b})
    used = sorted(groups.values(), key=lambda g: ROLE_ORDER.index(g["role"]) if g["role"] in ROLE_ORDER else 99)
    unused.sort(key=lambda u: -u["bytes"])
    return {"bytes": total, "used": used, "unused": unused,
            "folder": str(blurt_home() / "models") if (blurt_home() / "models").is_dir() else str(STATE / "models")}


def trash_models(viewer, paths) -> dict:
    """不用的模型送回收站。只认 models() 里列为不用的；有一个不是就整批不动（ValueError("in_use")）。"""
    want = [str(p) for p in (paths or []) if p]
    if not want:
        return {"count": 0, "bytes": 0}
    with _lock:
        free = {_key(u["path"]): u for u in models(viewer)["unused"]}
        if any(_key(p) not in free for p in want) or not all(_touchable(p) for p in want):
            raise ValueError("in_use")
        items = [free[_key(p)] for p in want]
        gone = {_key(g) for g in _recycle([Path(u["path"]) for u in items])}
        done = [u for u in items if _key(u["path"]) in gone]
        return {"count": len(done), "bytes": sum(u["bytes"] for u in done)}


# ---------------------------------------------------------------- 缓存、日志
def _cache_parts() -> list[tuple[Path, bool]]:
    """(路径, 能不能清)。语音缓存（接话常用句预先合成好的）不清：清了下一场开头要重新合成。"""
    bh = blurt_home()
    return [(STATE / "cache", False), (STATE / "cache.old", True), (STATE / "tmp", True),
            (bh / "tmp", True), (bh / "build", True),
            (bh / "index.db", False), (bh / "index.db-wal", False), (bh / "index.db-shm", False)]


def _proc_start() -> float | None:
    """这个后端进程是什么时候起来的（它自己的嘴的临时目录 mouth-* 是那之后建的，不能清）。"""
    try:
        import psutil
        return float(psutil.Process().create_time())
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
            if k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                ft = (c.dwHighDateTime << 32) | c.dwLowDateTime
                return ft / 1e7 - 11644473600.0
        except Exception:
            pass
    return None


def _clearable(viewer) -> list[Path]:
    """能清掉的临时文件：cache.old、build 整个；tmp 里：之前几次后端留下的 mouth-*（每次起来建一个，被强关时没删），
    其余一天没动过的。开着会时 tmp 一概不动；访客转写的工作目录不动。"""
    m = getattr(viewer, "meetings", None)
    try:
        live = bool(m.current()) if m is not None else False
    except Exception:
        live = False
    now, boot = time.time(), _proc_start()
    out = []
    for p, ok in _cache_parts():
        if not ok or not p.exists():
            continue
        if p.name != "tmp":
            out.append(p)
            continue
        if live:
            continue
        for c in _children(p):
            if c.name == "guest_asr":
                continue
            try:
                mt = c.stat().st_mtime
            except OSError:
                continue
            if c.name.startswith("mouth-"):
                if boot is not None and mt < boot - 5:
                    out.append(c)
            elif now - mt >= TMP_AGE_S:
                out.append(c)
    return [p for p in out if _touchable(p)]


def _log_files() -> list[Path]:
    out = []
    try:
        for e in os.scandir(STATE):
            n = e.name.lower()
            if e.is_file() and (n.endswith(".log") or n.endswith(".log.err") or n.endswith(".log.jsonl")):
                out.append(Path(e.path))
    except OSError:
        pass
    if (blurt_home() / "logs").is_dir():
        out.append(blurt_home() / "logs")
    return out


def clear_caches(viewer=None) -> dict:
    """清临时文件（彻底删：都是会重新生成的）→ {bytes}。"""
    with _lock:
        freed = 0
        for p in _clearable(viewer):
            b = du(p)
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            except OSError:
                pass
            if not p.exists():
                freed += b
        return {"bytes": freed}


# ---------------------------------------------------------------- 一起看
def scan(viewer=None) -> dict:
    t0 = time.time()
    rows = sessions(viewer)
    meet_rows = [r for r in rows if r["meeting"]]
    other_rows = [r for r in rows if not r["meeting"]]
    rec_bytes = sum(r["bytes"] for r in rows)
    roots = session_roots()
    rec_folder = str(roots[0]) if roots else (str(Path(meet_rows[0]["path"]).parent) if meet_rows else None)

    md = models(viewer)

    mroot = _meetings_root(viewer)
    meeting_dirs = [d for d in _children(mroot) if (d / "meta.json").is_file()]
    meetings_bytes = du(mroot) + sum(r["rest"] for r in meet_rows)
    papers_dir = STATE / "papers"
    papers = [d for d in _children(papers_dir)]
    papers_bytes = du(papers_dir)

    parts = _cache_parts()
    cache_bytes = sum(du(p) for p, _ in parts)
    clearable = sum(du(p) for p in _clearable(viewer))
    logs = _log_files()
    logs_bytes = sum(du(p) for p in logs)

    # 总共：~/.zuhui、~/.blurt 整个，加上录屏文件夹（它们在别处）
    counted = [STATE, blurt_home()]
    total = du(STATE) + (du(blurt_home()) if not _under(blurt_home(), STATE) else 0)
    for r in rows:
        if not any(_under(r["path"], c) for c in counted):
            total += r["bytes"] + r["rest"]
    models_in_home = md["bytes"]
    records = meetings_bytes + papers_bytes
    try:
        rrows = record_rows(viewer)
    except Exception as e:                 # 保留期那一行读不出来，别的照样给
        log.warning("逐字稿保留期：列不出来 %r", e)
        rrows = []
    retention = {"days": records_days(), "preview": {str(d): records_preview(rrows, d) for d in RECORD_AGES}}
    split = {"recordings": rec_bytes, "models": models_in_home, "records": records}
    split["other"] = max(0, total - sum(split.values()))

    return {
        "total": total, "free": free_bytes(), "split": split, "ms": int((time.time() - t0) * 1000),
        "recordings": {
            "bytes": sum(r["bytes"] for r in meet_rows),
            "meetings": sum(1 for r in meet_rows if r["bytes"]),
            "folder": rec_folder,
            "older": {**{str(d): _tally(_pick(rows, d, "meetings")) for d in AGES}, "all": _tally(_pick(rows, None, "meetings"))},
            "others": {"count": sum(1 for r in other_rows if r["bytes"]), "bytes": sum(r["bytes"] for r in other_rows),
                       "ready": _tally(_pick(rows, None, "others"))},
            "auto_days": auto_days(),
            "auto_preview": {str(d): _tally(_pick(rows, d, "all")) for d in AGES},
        },
        "models": md,
        "records": {"bytes": records,
                    "meetings": {"count": len(meeting_dirs), "bytes": meetings_bytes, "path": str(mroot)},
                    "papers": {"count": len(papers), "bytes": papers_bytes, "path": str(papers_dir)},
                    "retention": retention},
        "caches": {"bytes": cache_bytes, "clearable": clearable, "path": str(STATE / "cache")},
        "logs": {"bytes": logs_bytes, "path": str(STATE)},
    }


def post(viewer, path: str, body: dict) -> tuple[dict, int]:
    """POST /api/storage/<动作> → (返回的 JSON, 状态码)。"""
    body = body if isinstance(body, dict) else {}
    act = path.rstrip("/").rsplit("/", 1)[-1]
    try:
        if act == "recordings":
            days = body.get("days")
            if days is not None and days not in AGES:
                return {"error": "days"}, 400
            which = body.get("which") or "meetings"
            if which not in ("meetings", "others", "all"):
                return {"error": "which"}, 400
            return {"ok": True, **trash_recordings(viewer, days, which)}, 200
        if act == "auto":
            return {"ok": True, **set_auto(viewer, body.get("days"))}, 200
        if act == "retention":
            return {"ok": True, **set_records_auto(viewer, body.get("days"))}, 200
        if act == "models":
            return {"ok": True, **trash_models(viewer, body.get("paths"))}, 200
        if act == "caches":
            return {"ok": True, **clear_caches(viewer)}, 200
    except ValueError as e:
        return {"error": str(e) or "bad"}, 400
    except OSError:
        return {"error": "failed"}, 500
    return {"error": "unknown"}, 404
