# -*- coding: utf-8 -*-
"""Dev（侧栏"回顾"下面那一项）：模拟组会一场一场列出来看。只读，不碰真的会议库。

模拟会（scratch_ab/sim/sim_meeting.py）每场一个目录 runs/<场次>/：
    live.jsonl             这场的事件：meta（场景、人、备注、第几轮）、human、claude、speak、phase、auto（标签对上几句）、judge、end
    state/meetings/<id>/   这场会自己的会议记录（meta.json、transcript.jsonl，和真会一样的格式）
    state/board.json       任务板；state/changes/<任务>.json 每条改了什么
    paper/<论文>.md        改过的论文副本；母版在 sim 目录下同名文件
    now.json               开会时每半秒写一次：Claude 在干什么（和会议页状态栏同一个 brain_state）、正在说的半句
    judge.md               散会后 Claude 评审写的问题清单
这里把它们拼成和回顾一样的样子（列表一行 = MeetingBrief，一场 = MeetingDetail），界面照回顾的画法画；
评审放在"纪要"那一页（拆成纪要的结构），修订对比用同一个生成器从母版比到副本。"""
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

SIM = Path(__file__).resolve().parent.parent / "scratch_ab" / "sim"
LIVE_STALE_S = 180          # live.jsonl 这么久没动、又没散会：进程没了（被掐、电脑睡了），算中断

_cache: dict[str, tuple] = {}
_lock = threading.Lock()
_gen_lock = threading.Lock()


def runs_dir(cfg: dict | None = None) -> Path:
    p = ((cfg or {}).get("dev") or {}).get("runs")
    return Path(p) if p else SIM / "runs"


def _safe(rid: str) -> str:
    return Path(str(rid)).name


def _events(run: Path) -> list[dict]:
    out = []
    try:
        with open(run / "live.jsonl", encoding="utf-8") as f:
            for raw in f:
                if raw.strip():
                    try:
                        out.append(json.loads(raw))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


_SCEN = {"two": "两人会", "three": "三人会", "teacher": "导师在场", "monkey": "乱测"}


def _round_of(run: Path, meta: dict) -> int | None:
    if meta.get("round"):
        try:
            return int(meta["round"])
        except (TypeError, ValueError):
            pass
    m = re.search(r"第\s*(\d+)\s*轮", str(meta.get("note") or ""))
    if m:
        return int(m.group(1))
    try:
        head = (run / "judge.md").read_text(encoding="utf-8")[:300]
        m = re.search(r"第\s*(\d+)\s*轮", head)
        if m:
            return int(m.group(1))
    except OSError:
        pass
    return None


def _paper_short(meta: dict) -> str:
    p = str(meta.get("paper") or "")
    return re.split(r"[（(]", p)[0].strip() or "FinTSB"


def _brief(run: Path) -> dict | None:
    """列表的一行：缓存到 live.jsonl 变了为止。"""
    try:
        st = (run / "live.jsonl").stat()
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size, (run / "judge.md").exists())
    with _lock:
        hit = _cache.get(run.name)
        if hit and hit[0] == key:
            out = dict(hit[1])
            out.update(_state(run, out, st.st_mtime))
            return out
    evs = _events(run)
    meta = next((e for e in evs if e.get("type") == "meta"), None)
    if not meta:
        return None
    auto = next((e for e in reversed(evs) if e.get("type") == "auto"), None)
    last_at = max([float(e.get("at") or 0) for e in evs] or [0.0])
    humans = sum(1 for e in evs if e.get("type") == "human")
    claude = sum(1 for e in evs if e.get("type") == "claude")
    guests = meta.get("guests") or []
    scen = str(meta.get("scenario") or "")
    started = None
    m = re.match(r"(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", run.name)
    if m:
        try:
            started = datetime(datetime.now().year, int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)),
                               int(m.group(5))).isoformat(timespec="seconds")
        except ValueError:
            pass
    rnd = _round_of(run, meta)
    title = _SCEN.get(scen, scen) + (f" · {'、'.join(guests)}" if guests else "")
    out = {
        "id": run.name, "title": title, "scenario": scen, "round": rnd, "paper": _paper_short(meta),
        "note": meta.get("note") or "", "effort": meta.get("effort"), "started": started,
        "duration_s": int(last_at), "n_lines": humans + claude, "n_human": humans, "n_claude": claude,
        "match": (auto or {}).get("match"), "total": (auto or {}).get("total"),
        "has_summary": (run / "judge.md").exists(), "has_diff": True,
        "_ended": any(e.get("type") == "end" for e in evs), "_auto": auto is not None,
        "phase": next((e.get("text") for e in reversed(evs) if e.get("type") == "phase"), None),
    }
    with _lock:
        _cache[run.name] = (key, out)
    out = dict(out)
    out.update(_state(run, out, st.st_mtime))
    return out


def _state(run: Path, b: dict, mtime: float) -> dict:
    fresh = time.time() - mtime < LIVE_STALE_S
    if b.get("_ended"):
        return {"state": "ended", "processing": False, "ended": "1", "stopped": False}
    if not b.get("_auto"):
        return {"state": "live" if fresh else "ended", "processing": False, "ended": None if fresh else "1",
                "stopped": not fresh}
    return {"state": "ended", "processing": fresh, "ended": "1", "stopped": not fresh}     # 散会了，在收尾 / 评审


def list_runs(cfg: dict | None = None, limit: int = 120) -> list[dict]:
    root = runs_dir(cfg)
    if not root.is_dir():
        return []
    dirs = sorted((d for d in root.iterdir() if d.is_dir() and (d / "live.jsonl").exists()), key=lambda d: d.name,
                  reverse=True)[:limit]
    out = []
    for d in dirs:
        b = _brief(d)
        if b:
            out.append({k: v for k, v in b.items() if not k.startswith("_")})
    return out


def _meeting_dir(run: Path) -> Path | None:
    ds = sorted((run / "state" / "meetings").glob("*/meta.json"))
    return ds[-1].parent if ds else None


def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _lines(d: Path) -> list[dict]:
    out = []
    try:
        for raw in (d / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
            if raw.strip():
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def judge_summary(run: Path) -> dict | None:
    """评审的 judge.md 拆成纪要的结构：总评那一节的头一段当摘要、要点当结论，其余每节一张卡，表格一行一条。"""
    try:
        md = (run / "judge.md").read_text(encoding="utf-8")
    except OSError:
        return None
    title = next((l[2:].strip() for l in md.splitlines() if l.startswith("# ")), "评审")
    secs: list[dict] = []
    cur = None
    para: list[str] = []
    head: list[str] | None = None

    def flush():
        nonlocal para
        if para and cur is not None:
            cur["bullets"].append(" ".join(para).strip())
        para = []

    for l in md.splitlines():
        s = l.strip()
        if s.startswith("## "):
            flush()
            cur = {"title": re.sub(r"^\d+\)\s*", "", s[3:].strip()), "bullets": []}
            secs.append(cur)
            head = None
            continue
        if cur is None or s.startswith("# "):
            continue
        if not s:
            flush()
            continue
        if s.startswith("|"):
            flush()
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if head is None:
                head = cells
                continue
            cur["bullets"].append(" ｜ ".join(c for c in cells if c))
            continue
        head = None
        if re.match(r"^([-*]|\d+[.)])\s+", s):
            flush()
            cur["bullets"].append(re.sub(r"^([-*]|\d+[.)])\s+", "", s))
            continue
        para.append(s)
    flush()
    if not secs:
        return None
    def plain(x: str) -> str:                 # 纪要页按纯文字画：去掉加粗、代码的记号
        return re.sub(r"\*\*(.+?)\*\*", r"\1", x).replace("`", "")

    for x in secs:
        x["bullets"] = [plain(b) for b in x["bullets"]]
    first = secs[0]
    abstract = first["bullets"][0] if first["bullets"] else ""
    return {"title": title, "speakers": [], "abstract": abstract, "conclusions": first["bullets"][1:6],
            "sections": [{"title": x["title"], "cards": [{"title": "", "bullets": x["bullets"]}]} for x in secs[1:] if x["bullets"]],
            "todos": [], "markdown": md}


def detail(cfg: dict | None, rid: str, people) -> dict | None:
    """一场：和回顾的 MeetingDetail 一样的样子，多一个 dev（这场的场景、轮次、标签对上几句……）和开会时的 brain。
    people(meta) → 参会人列表（颜色由 view 那边定，和会议页一致）。"""
    run = runs_dir(cfg) / _safe(rid)
    b = _brief(run)
    if not b:
        return None
    evs_meta = next((e for e in _events(run) if e.get("type") == "meta"), {})
    md = _meeting_dir(run)
    meeting = _read(md / "meta.json", {}) if md else {}
    meeting = dict(meeting or {}, id=run.name, title=f"{b['paper']} · {b['title']}", state=b["state"],
                   # 模拟会自己声明的语言为准（模拟会直接建会议记录，旧场次的会议记录里没有语言或记成了中文）
                   lang="en" if evs_meta.get("lang") == "en" else ((meeting or {}).get("lang") or "zh"),
                   duration_s=max(int((meeting or {}).get("duration_s") or 0), int(b.get("duration_s") or 0)))
    if b["state"] != "live":
        meeting["ended"] = meeting.get("ended") or meeting.get("started")
    lines = _lines(md) if md else []
    board = {"tasks": []}
    try:
        from .board import Board
        bd = Board(run / "state" / "board.json")
        board = {"counts": bd.counts(), "tasks": bd.tasks()}
    except Exception:
        pass
    now = _read(run / "now.json", {}) if b["state"] == "live" else {}
    if isinstance((now or {}).get("brain"), dict):
        now["brain"]["now"] = time.time()          # 模拟会和后端在同一台机器上：用现在的钟，页面算时差才准（文件最多晚 3 秒）
    return {"meeting": meeting, "transcript": lines, "transcript_raw": None, "cleanup": None, "board": board,
            "summary": judge_summary(run), "diff_url": f"/api/dev/runs/{run.name}/diff.html",
            "participants": people(evs_meta), "video": None, "processing": b.get("processing"),
            "brain": (now or {}).get("brain"), "partial": (now or {}).get("partial"),
            "dev": {k: v for k, v in b.items() if not k.startswith("_")}}


def _master_and_copy(run: Path) -> tuple[Path | None, Path | None]:
    copies = sorted((run / "paper").glob("*.md"))
    if not copies:
        return None, None
    cp = copies[0]
    orig = cp.with_name(cp.name + ".orig")            # 这一场开始时的原稿（母版后来可能改过，比如表格里的数字去掉了公式）
    master = orig if orig.exists() else SIM / cp.name
    return (master if master.exists() else None), cp


def diff_html(cfg: dict | None, rid: str, htmldiff: Path, me: str = "主持人", ui: str = "zh") -> tuple[str | None, list]:
    """这场的修订对比：母版 → 改过的副本，右栏批注是任务板（和会议页同一个生成器）。
    开会时 10 秒内最多重算一次；文件没变就用上次的。返回 (页面, 任务)。"""
    run = runs_dir(cfg) / _safe(rid)
    master, cp = _master_and_copy(run)
    if not (master and cp and htmldiff.exists()):
        return None, []
    try:
        from .board import Board
        tasks = Board(run / "state" / "board.json").tasks()
    except Exception:
        tasks = []
    en = ui == "en"
    vd = run / ("view_en" if en else "view")        # 两种界面语言各存一份
    vd.mkdir(exist_ok=True)
    out = vd / "diff.standalone.html"
    parts = []
    for p in [cp, run / "state" / "board.json"] + sorted((run / "state" / "changes").glob("*.json")):
        try:
            parts.append(f"{p.name}:{p.stat().st_mtime_ns}")
        except OSError:
            pass
    sig = "|".join(parts)
    sig_p = vd / "sig.txt"
    with _gen_lock:
        try:
            old = sig_p.read_text(encoding="utf-8")
        except OSError:
            old = ""
        recent = out.exists() and time.time() - out.stat().st_mtime < 10
        if out.exists() and (old == sig or recent):
            return out.read_text(encoding="utf-8"), tasks
        notes, rules = {}, []
        for t in tasks:
            if t.get("status") != "done":
                continue
            key = str(t.get("idx"))
            notes[key] = [str(t.get("title") or "")[:14], f"{me}：{str(t.get('remark') or '')[:160]}<br>Claude：{str(t.get('result') or '')[:200]}"]
            ch = _read(run / "state" / "changes" / f"{t['id']}.json", {}) or {}
            ins = [s for s in ch.get("ins", []) if len(s) >= 2][:6]
            dele = [s for s in ch.get("del", []) if len(s) >= 2][:6]
            if ins:
                rules.append({"note": key, "ins": ins})
            elif dele:
                rules.append({"note": key, "del": dele})
        spec = {"title": "Simulated meeting revisions" if en else "模拟组会修订对比",
                "intro": ("Original: the master copy when the simulation started; current: Claude's edits in this meeting. "
                          "The numbers on the right are task numbers." if en else
                          "原文：模拟开始时的母版；现稿：这场会里 Claude 改到现在。右栏编号是任务板编号。"),
                "ui": "en" if en else "zh", "out": "diff.html", "lang": "auto",
                "docs": [{"name": cp.stem, "before": str(master), "after": str(cp)}], "notes": notes, "rules": rules}
        (vd / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        r = subprocess.run([sys.executable, "-X", "utf8", str(htmldiff), str(vd / "spec.json")], cwd=str(vd),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        if r.returncode != 0 or not out.exists():
            return None, tasks
        sig_p.write_text(sig, encoding="utf-8")
        return out.read_text(encoding="utf-8"), tasks
