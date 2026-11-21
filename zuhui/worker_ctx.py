# -*- coding: utf-8 -*-
"""改稿工人开工时拿到的会上情况（2026-11-21 组会之后加的）。

以前单人会议里工人只看到触发任务的那一句原话，加上快嘴的复述和标题：前面的讨论、深想的结论、更早定下的限制
（"跳过引言、摘要、2.1"）它都看不到。T21 是 T20 的补充，却不知道 T20 的范围，把摘要、引言、2.1 全改了。
现在开工时现取（排队期间又说的话也能带上）：
- talk：这场会最近的对话，逐字稿原文，带说话人和 mm:ss，标出记下这条任务的那句，排队期间说的单独列；
- session_tasks：本场同一篇文档上已经记过的任务（标题、状态、结果）；
- toc：文档目录（标题和行号），工人不用再 Grep 标题；
- original：这篇文档本场第一条修改之前的原稿，只读副本（存在 STATE 下，不往文档所在的文件夹里写）。
都是纯函数（original 只写 STATE 下的副本），app.py / hands.py / hands_api.py 调。"""
from __future__ import annotations

import json
import re
from pathlib import Path

TALK_WINDOW_S = 900.0          # 最多往回 15 分钟
TALK_MAX_LINES = 60            # 记任务之前的对话最多这么多行、这么多字（超了丢最早的）
TALK_MAX_CHARS = 6000
AFTER_MAX_LINES = 30           # 排队期间说的最多这么多行、这么多字（超了丢最晚的）
AFTER_MAX_CHARS = 3000
LINE_MAX = 300                 # 一行最多这么多字（触发那句不截这么短）
TRIGGER_MAX = 1200
MERGE_BACK_S = 120.0           # 触发那句是几句并起来的：往前这么久里、原话里有的那几句也标成触发句

_TAG_NOTE = {"#report": "（后台改稿的汇报）", "#deep-answer": "（后台深想的结论）"}


def mmss(sec) -> str:
    s = int(max(0.0, float(sec or 0)))
    return f"{s // 60:02d}:{s % 60:02d}"


def _f(x) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def _who(line: dict, me: str, ai: str) -> str | None:
    w = line.get("who")
    if w == "teacher":
        return line.get("name") or me
    if w == "guest":
        return line.get("name") or "参会者"
    if w == "student":
        return ai
    return None                                # 系统行（"切到 某篇"）不是谁说的


def _bare(s) -> str:
    return re.sub(r"[\s，。,.！？!?、；;：:“”\"'（）()]+", "", str(s or ""))


def talk(lines: list[dict], task: dict, ask_sec=None, said_sec=None, me: str = "主持人", ai: str = "AI") -> str:
    """这场会最近的对话（逐字稿原文）。ask_sec：触发这条任务的那句话在第几秒（逐字稿那一行的 sec）；
    said_sec：记下这条任务时会开到第几秒。之后的行是"排队期间"说的，单独列。都没有（旧任务）返回 ""。"""
    ask, said = _f(ask_sec), _f(said_sec)
    if said is None and ask is None:
        return ""
    if said is None:
        said = ask
    if ask is None:
        ask = said
    rows = [ln for ln in lines or [] if isinstance(ln, dict) and str(ln.get("text") or "").strip()
            and _who(ln, me, ai) is not None]
    start = max(0.0, ask - TALK_WINDOW_S)
    before = [ln for ln in rows if start <= float(ln.get("sec") or 0) <= said + 0.05]
    after = [ln for ln in rows if float(ln.get("sec") or 0) > said + 0.05]
    if not before and not after:
        return ""
    remark = _bare(task.get("remark"))
    human = ("teacher", "guest")

    def is_trigger(ln) -> bool:
        if ln.get("who") not in human:
            return False
        sec = float(ln.get("sec") or 0)
        if sec >= ask - 0.5:
            return True
        b = _bare(ln.get("text"))               # 几句并起来记的任务：原话里有的那几句也算
        return sec >= ask - MERGE_BACK_S and len(b) >= 4 and b in remark

    marks = [is_trigger(ln) for ln in before]
    if not any(marks):                          # 没对上：记任务前最后一句人说的话
        for i in range(len(before) - 1, -1, -1):
            if before[i].get("who") in human:
                marks[i] = True
                break

    def fmt(ln, trig=False) -> str:
        text = _clip(ln.get("text"), TRIGGER_MAX if trig else LINE_MAX)
        note = _TAG_NOTE.get(str(ln.get("tag") or ""), "")
        return f"{'▶ ' if trig else ''}{mmss(ln.get('sec'))} {_who(ln, me, ai)}：{note}{text}"

    shown = [fmt(ln, m) for ln, m in zip(before, marks)]
    dropped = 0
    while len(shown) > 1 and (len(shown) > TALK_MAX_LINES or sum(len(s) for s in shown) > TALK_MAX_CHARS) \
            and not shown[0].startswith("▶"):
        shown.pop(0)
        dropped += 1
    out = []
    if shown:
        out.append("标 ▶ 的是记下这条任务的那句。")
        out.append("<<<对话开始>>>")
        if dropped:
            out.append(f"（更早的 {dropped} 句略）")
        out += shown
        out.append("<<<对话结束>>>")
    if after:
        late = [fmt(ln) for ln in after]
        more = 0
        while len(late) > 1 and (len(late) > AFTER_MAX_LINES or sum(len(s) for s in late) > AFTER_MAX_CHARS):
            late.pop()
            more += 1
        out.append("记下这条任务以后、你开工以前，会上又说了这些（排队期间对方又说的；补充、纠正的以这里为准，说的是别的事就别管）：")
        out.append("<<<排队期间开始>>>")
        out += late
        if more:
            out.append(f"（后面还有 {more} 句，略）")
        out.append("<<<排队期间结束>>>")
    return "\n".join(out)


def _status(t: dict, me_idx: int) -> str:
    st = t.get("status")
    if st == "queued":
        return "排队中，在你后面" if int(t.get("idx") or 0) > me_idx else "排队中"
    if st == "working":
        return "正在改"
    if st == "failed":
        return "失败"
    if t.get("rejected"):
        return "已撤回"
    if t.get("note"):
        return "没改正文，记进了修改清单" if t.get("unchanged") else "标注，没改正文"
    return "已改"


def _same_doc(a: dict, b: dict) -> bool:
    pa, pb = a.get("paper"), b.get("paper")
    if pa and pb:
        return str(pa) == str(pb)
    qa, qb = a.get("paper_path"), b.get("paper_path")
    if qa and qb:
        return str(qa) == str(qb)
    return True                                 # 旧任务没记是哪篇：算同一篇


def session_tasks(tasks: list[dict], task: dict, max_items: int = 30, max_chars: int = 3000) -> str:
    """本场同一篇文档上已经记过的任务（不含这一条、不含软件意见）：一行一条，"T20｜已改｜标题｜结果：……"。没有返回 ""。"""
    from .board import is_app_task
    me_idx = int(task.get("idx") or 0)
    same = [t for t in tasks or [] if isinstance(t, dict) and t.get("id") != task.get("id")
            and _same_doc(t, task) and not is_app_task(t)]
    same.sort(key=lambda t: int(t.get("idx") or 0))
    rows = []
    for t in same:
        row = f"{t.get('id')}｜{_status(t, me_idx)}｜{_clip(t.get('title'), 80)}"
        res = t.get("error") if t.get("status") == "failed" else (t.get("plan") or t.get("result"))
        if res and t.get("status") in ("done", "failed"):
            row += f"｜结果：{_clip(res, 160)}"
        rows.append(row)
    dropped = 0
    while rows and (len(rows) > max_items or sum(len(r) for r in rows) > max_chars):
        rows.pop(0)
        dropped += 1
    if not rows:
        return ""
    return "\n".join(([f"（更早的 {dropped} 条略）"] if dropped else []) + rows)


_MD_H = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TEX_H = re.compile(r"^\s*\\(part|chapter|section|subsection|subsubsection)\*?\{(.+)\}")
_TEX_LEVEL = {"part": 1, "chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4}


def toc(path, max_items: int = 200, max_chars: int = 6000) -> str:
    """文档目录：一行一个标题，"第 12 行：## 2.1 相关工作"。Markdown 的 # 标题、LaTeX 的 \\section 都认，代码块里的不算。
    标题太多：先去掉最深的那一级，还多就只留前面的。读不到、没有标题返回 ""。"""
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    tex = str(path).lower().endswith((".tex", ".ltx"))
    heads = []                                  # (行号, 级别, 标题)
    fence = False
    for i, line in enumerate(text.splitlines(), 1):
        if tex:
            m = _TEX_H.match(line)
            if m:
                heads.append((i, _TEX_LEVEL[m.group(1)], m.group(2).strip()))
            continue
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        m = None if fence else _MD_H.match(line)
        if m:
            heads.append((i, len(m.group(1)), re.sub(r"[*_`]", "", m.group(2))))
    if not heads:
        return ""

    def rows(hs):
        return [f"第 {i} 行：{'#' * lv} {_clip(t, 80)}" for i, lv, t in hs]

    hs = heads
    while len(hs) > max_items or sum(len(r) for r in rows(hs)) > max_chars:
        deepest = max(lv for _, lv, _ in hs)
        if deepest <= 1 or all(lv == deepest for _, lv, _ in hs):
            break
        hs = [h for h in hs if h[1] < deepest]
    out = rows(hs)
    more = 0
    while len(out) > 1 and (len(out) > max_items or sum(len(r) for r in out) > max_chars):
        out.pop()
        more += 1
    if more:
        out.append(f"（后面还有 {more} 个标题，略）")
    return "\n".join(out)


# ---------- 会前原稿 ----------
def _started(t: dict) -> bool:
    """这条改稿任务开过工（改没改成都算）：标注（只读地看一遍）、软件意见不算。"""
    if t.get("mode") == "annotate" or t.get("app"):
        return False
    return t.get("status") in ("working", "done", "failed") and bool(t.get("started"))


def original(paper, task: dict, tasks: list[dict], state: Path) -> Path | None:
    """这篇文档本场第一条修改之前的原稿，只读副本：state/worker_orig/<文档编号>/<文件名>（文件夹里只有这一个文件，
    工人按 read_dirs 只读地读它）。
    - 这篇上还没有别的改稿任务开过工（这条就是第一条）：存现在的稿子；
    - 已经有了：用存着的；存着的没了（升级、被删），按本场这篇最早那条改动记录（state/changes/<id>.json）里的 before 补，
      再没有就用对比页存的开会原文（state/base/<编号>-<文件名>.orig）。
    都没有返回 None。不往文档所在的文件夹里写任何东西。"""
    from .meetings import paper_id
    p = Path(str(paper))
    try:
        pid = re.sub(r"[^0-9a-zA-Z_-]", "", str(task.get("paper") or paper_id(p)))[:40] or "doc"
    except Exception:
        pid = "doc"
    d = Path(state) / "worker_orig" / pid
    dst = d / p.name
    others = [t for t in tasks or [] if isinstance(t, dict) and t.get("id") != task.get("id")
              and _same_doc(t, task) and _started(t)]
    try:
        if not others:                          # 本场第一条：现在的稿子就是原稿
            data = p.read_bytes()
            d.mkdir(parents=True, exist_ok=True)
            for old in d.iterdir():             # 文件夹里只留这一个文件（文档改过名的旧副本删掉）
                if old.name != dst.name and old.is_file():
                    old.unlink()
            tmp = d / f".{p.name}.tmp"
            tmp.write_bytes(data)
            tmp.replace(dst)
            return dst
        if dst.is_file():
            return dst
        text = None
        best = None
        for t in others:
            f = Path(state) / "changes" / f"{t.get('id')}.json"
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec.get("before"), str) and (best is None or float(rec.get("seq") or 0) < best[0]):
                best = (float(rec.get("seq") or 0), rec["before"])
        if best is not None:
            text = best[1].encode("utf-8")
        else:
            b0 = Path(state) / "base" / f"{paper_id(p)}-{p.name}.orig"
            if b0.is_file():
                text = b0.read_bytes()
        if text is None:
            return None
        d.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(text)
        return dst
    except OSError:
        return dst if dst.is_file() else None


def same_text(a, b) -> bool:
    """两份稿子内容一样（BOM、换行不算）。"""
    def norm(p):
        return Path(p).read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    try:
        return norm(a) == norm(b)
    except OSError:
        return False


def fmt_span(sec: float) -> str:
    """300 → "5 分钟"，1800 → "30 分钟"，90 → "90 秒"。"""
    s = int(round(float(sec)))
    return f"{s // 60} 分钟" if s >= 60 and s % 60 == 0 else f"{s} 秒"
