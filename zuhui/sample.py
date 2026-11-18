# -*- coding: utf-8 -*-
"""示例会议（第 8 批）：Bloom 自带的一场会。还没开过会时，回顾里能打开它，看看散会后有什么。

- 原件在 samples/demo-meeting/<zh|en>/（随安装包和开源导出一起带上，见 oss/export_oss.py），由
  tools/build_sample_meeting.py 生成：讨论一篇讲 Bloom 怎么用的短文（paper.before.md 是会前的，paper.md 是会上改过的），
  逐字稿、纪要、修改清单、修订对比都有。文字里的 {AI} 是 AI 的名字、{ME} 是主持人（用 Bloom 的人自己），都不写死。
- 第一次要用时拷一份到会议库旁边的 sample-meeting/（~/.zuhui/sample-meeting/，不在 meetings/ 里面：往期会议、
  大厅的上次遗留、跨会议提问、保留期、存储统计都看不到它）。拷的时候 {AI}、{ME} 换成现在设置里的名字（和导出逐字稿
  用的同一个），论文路径换成这份拷贝里的。界面语言、两个名字或原件版本变了就重拷一份（改过的会议名会回到原样）。
- 编号固定是 SAMPLE_ID。Meetings._dir 把它指到这份拷贝，所以打开、导出逐字稿、修订对比页、改名都和真的会一样。
- 会议库里一场自己的会都没有、请求带着 sample=1（回顾带）时才出现在 GET /api/meetings 里（view.py）。
  删掉（POST /api/meetings/sample/delete）：拷贝挪进 meetings-trash，会议库旁边的 sample.json 记一笔 deleted，以后不再出现。
"""
from __future__ import annotations

import html
import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path

from .config import ROOT

log = logging.getLogger("zuhui.sample")

SAMPLE_ID = "sample"
SRC = ROOT / "samples" / "demo-meeting"
LANGS = ("zh", "en")
MARK = ".sample.json"           # 拷贝里记着：哪种语言、AI 和主持人叫什么、原件第几版
_lock = threading.Lock()


def folder(meetings_root) -> Path:
    """示例会议的拷贝放哪：会议库旁边（测试的临时会议库就在临时目录里）。"""
    return Path(meetings_root).parent / "sample-meeting"


def _flag(meetings_root) -> Path:
    return Path(meetings_root).parent / "sample.json"


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def deleted(meetings_root) -> bool:
    d = _read_json(_flag(meetings_root))
    return isinstance(d, dict) and bool(d.get("deleted"))


def source(lang: str | None) -> Path | None:
    """原件：界面语言那份，没有就中文那份；都没有（源码里没带 samples/）None。"""
    for lg in ((lang if lang in LANGS else "zh"), "zh"):
        d = SRC / lg
        if (d / "meta.json").is_file():
            return d
    return None


def _fill(s: str, names: dict[str, str]) -> str:
    for k, v in names.items():
        s = s.replace("{" + k + "}", v)
    return s


def _swap(v, names: dict[str, str]):
    """JSON 里每个字符串的 {AI}、{ME} 换成名字（名字里有引号也不会把 JSON 弄坏）。"""
    if isinstance(v, str):
        return _fill(v, names)
    if isinstance(v, list):
        return [_swap(x, names) for x in v]
    if isinstance(v, dict):
        return {k: _swap(x, names) for k, x in v.items()}
    return v


def _copy(src: Path, dst: Path, names: dict[str, str], lang: str, version) -> None:
    from .meetings import paper_id
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for f in sorted(src.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        out = tmp / f.name
        if f.suffix == ".json":
            out.write_text(json.dumps(_swap(_read_json(f), names), ensure_ascii=False, indent=1), encoding="utf-8")
        elif f.suffix == ".jsonl":
            rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
            out.write_text("".join(json.dumps(_swap(r, names), ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        elif f.suffix == ".html":
            out.write_text(_fill(f.read_text(encoding="utf-8"), {k: html.escape(v) for k, v in names.items()}), encoding="utf-8")
        elif f.suffix == ".md":
            out.write_text(_fill(f.read_text(encoding="utf-8"), names), encoding="utf-8")
        else:
            shutil.copy2(f, out)
    # 论文：指到拷贝里的那份（编号按路径算，和真的会一样）
    paper = dst / "paper.md"
    pid = paper_id(paper)
    meta = _read_json(tmp / "meta.json") or {}
    meta["id"], meta["sample"], meta["state"] = SAMPLE_ID, True, "ended"
    for p in meta.get("papers") or []:
        p["id"], p["path"] = pid, str(paper)
    meta["active_paper"] = pid
    (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    board = _read_json(tmp / "board.json")
    if isinstance(board, dict):
        for t in board.get("tasks") or []:
            t["paper"], t["paper_path"] = pid, str(paper)
        (tmp / "board.json").write_text(json.dumps(board, ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / MARK).write_text(json.dumps({"lang": lang, "names": names, "version": version,
                                        "copied": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False),
                            encoding="utf-8")
    if dst.exists():
        shutil.rmtree(dst)
    tmp.rename(dst)


def ensure(meetings_root, lang: str | None, ai: str, me: str = "") -> Path | None:
    """拷贝在、而且是这种语言、这两个名字、原件这一版的，就用它；不是就重拷。删过、没有原件：None。"""
    if deleted(meetings_root):
        return None
    src = source(lang)
    if src is None:
        return None
    lg = src.name
    ver = (_read_json(src / "meta.json") or {}).get("sample_version")
    dst = folder(meetings_root)
    names = {"AI": str(ai or "AI"), "ME": str(me or ("主持人" if lg == "zh" else "Host"))}
    with _lock:
        mark = _read_json(dst / MARK)
        if (isinstance(mark, dict) and mark.get("lang") == lg and mark.get("names") == names and mark.get("version") == ver
                and (dst / "meta.json").is_file()):
            return dst
        try:
            _copy(src, dst, names, lg, ver)
        except OSError as e:
            log.warning("示例会议拷不过去：%r", e)
            return dst if (dst / "meta.json").is_file() else None
        return dst


def names(cfg: dict | None = None) -> tuple[str, str]:
    """(AI 的名字, 主持人的名字)：和导出逐字稿（Meetings.export）用的一样。"""
    import getpass
    from .config import assistant_name, load
    cfg = load() if cfg is None else cfg
    me = str((cfg.get("meeting") or {}).get("me") or getpass.getuser() or "")
    return assistant_name(cfg), me


def brief(meetings, lang: str | None, cfg: dict | None = None) -> dict | None:
    """回顾列表里的那一行（和 Meetings.list 的一行一样，多一个 sample: True）；不该出现时 None。
    两个名字和 Meetings.export 取的一样（cfg 不给就现读设置）。"""
    if ensure(meetings.root, lang, *names(cfg)) is None:
        return None
    g = meetings.get(SAMPLE_ID)
    if not g:
        return None
    m, tasks = g["meeting"], (g.get("board") or {}).get("tasks") or []
    return {"id": SAMPLE_ID, "title": m.get("title"), "title_auto": m.get("title_auto", True) is not False,
            "started": m.get("started"), "ended": m.get("ended"), "state": "ended", "duration_s": m.get("duration_s", 0),
            "n_tasks": len(tasks), "n_done": sum(1 for t in tasks if t.get("status") == "done"),
            "n_lines": len(g.get("transcript_raw") or []), "has_summary": bool(g.get("summary")),
            "has_diff": bool(g.get("diff_url")),
            "papers": [{"id": p.get("id"), "name": p.get("name")} for p in m.get("papers") or [] if p.get("id")],
            "processing": False, "sample": True}


def delete(meetings) -> dict:
    """删掉示例会议：记下来以后不再出现，拷贝挪进 meetings-trash（和删真的会一样找得回来）。"""
    root = Path(meetings.root)
    with _lock:
        f = _flag(root)
        try:
            f.write_text(json.dumps({"deleted": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": f"删除失败：{e}"}
        d = folder(root)
        if d.exists():
            trash = root.parent / "meetings-trash"
            trash.mkdir(parents=True, exist_ok=True)
            dst = trash / f"{SAMPLE_ID}-deleted-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            k = 2
            while dst.exists():
                dst = dst.with_name(f"{dst.name.split('~')[0]}~{k}")
                k += 1
            try:
                shutil.move(str(d), str(dst))
            except OSError:
                shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "recording": None}
