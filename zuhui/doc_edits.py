# -*- coding: utf-8 -*-
"""会上有人在对比页里就地改了一段（点「编辑」、改完保存）：记成逐字稿上的一行系统行（2026-11-21 第二场组会之后加的）。

以前手改只让快嘴重读了论文：逐字稿上没有痕迹，改稿工人、/bloom 技能、纪要都不知道改了哪里，
后台照着旧的说法接着改，或者把作者刚改好的又改回去。现在每保存一处记一行：

    {"who": "system", "tag": "#doc_edit", "text": "小林 改了 5.1 第（1）段",
     "by": 谁改的, "where": "5.1 第（1）段", "section": "5.1 调度偏差", "before": 改前片段, "after": 改后片段,
     "paper": 文档编号, "paper_name": 文档名}

- 片段只截改动附近（前后各带十几个字，最长 SNIP 字），不放整段；
- 同一个人接着改同一段（MERGE_S 秒内，这次的改前就是上次的改后）：并进上一行，改前留最早的、改后换成最新的；
- 没在开会（大厅里、回顾里）不记。
读的一方：逐字稿界面（系统行，和"已切换到 X"一样显示）、快嘴（note）、改稿工人（worker_ctx.talk 里的系统行、本场手改列表）、
/bloom 技能（skills/bloom/scripts，直接读逐字稿的这几个字段）、纪要（summary_block）。都是纯函数，record 写逐字稿。"""
from __future__ import annotations

import difflib
import re
import threading
import time

TAG = "#doc_edit"
SNIP = 160                 # 改前、改后片段各最多这么多字
CTX = 16                   # 片段在改动前后各多带几个字
MERGE_S = 180.0            # 同一段接着改，这么久以内并成一行
DIFF_MAX = 8000            # 两段加起来超过这么长不逐字比（表格、整页公式）：直接截开头
FULL_N = 2                 # 给快嘴的手改说明：最近这几处附上改后的整段（2026-11-21 组会：只给片段，快嘴说"看不到你改成什么样"）
FULL_MAX = 700             # 每段最多这么多字

_MD_H = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TEX_H = re.compile(r"^\s*\\(?:part|chapter|section|subsection|subsubsection|paragraph)\*?\{(.+)\}")
_NUM = re.compile(r"\s*((?:\d+(?:\.\d+)*)|(?:[A-Z](?:\.\d+)+)|(?:[A-Z](?=[\s.])))[\s.]")
_ITEM = re.compile(r"\s*[（(]\s*(\d{1,3}|[一二三四五六七八九十]{1,3}|[a-zA-Z])\s*[）)]")
_BLOCKS = (("|", "table"), ("![", "figure"), ("$$", "formula"), ("\\[", "formula"), ("\\begin{equation", "formula"),
           ("\\begin{align", "formula"), ("\\begin{table", "table"), ("\\begin{figure", "figure"), ("<", "other"),
           ("```", "other"), (">", "other"))
_KIND = {"table": ("的表格", "table"), "figure": ("的图", "figure"), "formula": ("的公式", "equation"),
         "other": ("的一处", "a block"), "heading": ("的标题", "heading")}

_lock = threading.Lock()
_last: dict[int, dict] = {}            # id(meetings) → 上一次记的那一处（并行用）


def _norm(s) -> str:
    return str(s or "").replace("\r\n", "\n").strip()


def _one(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _paras(src: str) -> list[str]:
    """按空行切段，和对比页的 Viewer._spans 切法一样（段落序号对得上）。"""
    return [p.strip() for p in re.split(r"\n\s*\n", str(src or "").replace("\r\n", "\n")) if p.strip()]


def _heading(line: str) -> str | None:
    m = _MD_H.match(line)
    if m:
        return re.sub(r"[*_`]", "", m.group(2)).strip()
    m = _TEX_H.match(line)
    return m.group(1).strip() if m else None


def _short(title: str) -> str:
    """小节的简称：多级编号只用编号（"5.1"），一级的连标题（"1 引言"，光说"1"听不懂），没有编号用标题（太长截短）。"""
    m = _NUM.match(title + " ")
    return m.group(1) if m and "." in m.group(1) else _one(title, 14)


def _join(a: str, b: str) -> str:
    """"5.1" + "的表格" → "5.1 的表格"；"引言" + "的表格" → "引言的表格"。"""
    return f"{a} {b}" if a and re.search(r"[0-9A-Za-z)]$", a) else a + b


def _kind(text: str) -> str:
    s = text.lstrip()
    for pre, k in _BLOCKS:
        if s.startswith(pre):
            return k
    return "para"


def locate(src: str, index: int) -> dict:
    """第 index 段（按空行切）在哪：{"section": 所在小节的标题, "where": "5.1 第（1）段", "where_en": "5.1 paragraph (1)",
    "kind": para | heading | table | figure | formula | other}。段落从小节标题往下数，表格、图、公式不算段（和快嘴给段落标号一样）；
    段首有（1）这种编号的，直接说第（1）段。"""
    paras = _paras(src)
    sec, n = "", 0
    kind, label_zh, label_en = "para", "", ""
    for i, p in enumerate(paras[:max(0, int(index)) + 1]):
        lines = p.split("\n")
        head = False
        while lines and _heading(lines[0]) is not None:
            sec, n, head = _heading(lines[0]) or sec, 0, True
            lines = lines[1:]
        body = "\n".join(lines).strip()
        k = _kind(body) if body else "heading"
        if body and k == "para":
            n += 1
        if i == int(index):
            kind = "heading" if head and not body else k
            if kind == "para":
                m = _ITEM.match(body)
                label_zh = f"第（{m.group(1)}）段" if m else f"第 {n} 段"
                label_en = f"paragraph ({m.group(1)})" if m else f"paragraph {n}"
    short = _short(sec) if sec else ""
    if kind == "para":
        where = _join(short, label_zh) if short else label_zh
        where_en = f"{short} {label_en}".strip()
    else:
        zh, en = _KIND.get(kind, _KIND["other"])
        where = _join(short, zh) if short else zh.lstrip("的")
        where_en = f"{en} in {short}" if short else en
    return {"section": _one(sec, 60), "where": where, "where_en": where_en, "kind": kind}


def _clip_mid(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n // 2 - 1] + "…" + s[-(n - n // 2):]


def snippets(old: str, new: str, n: int = SNIP) -> tuple[str, str]:
    """改前、改后的片段：只截改动的那一截，前后各带 CTX 个字，各最长 n 字（太长的中间省掉）。没改返回 ("", "")。"""
    old, new = _norm(old), _norm(new)
    if old == new:
        return "", ""
    if len(old) + len(new) > DIFF_MAX:
        return _one(old, n), _one(new, n)
    ops = [o for o in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if o[0] != "equal"]
    i1, i2, j1, j2 = ops[0][1], ops[-1][2], ops[0][3], ops[-1][4]

    def cut(s, a, b):
        a0, b0 = max(0, a - CTX), min(len(s), b + CTX)
        if a0 >= b0:
            return ""
        mid = " ".join(s[a0:b0].split())
        return ("…" if a0 > 0 else "") + _clip_mid(mid, n - 2) + ("…" if b0 < len(s) else "")
    return cut(old, i1, i2) if old else "", cut(new, j1, j2) if new else ""


def line_text(by: str, where: str, deleted: bool, en: bool = False) -> str:
    """逐字稿上那一行：小林 改了 5.1 第（1）段 / Lin edited 5.1 paragraph (1)。"""
    by = _one(by, 32) or ("Someone" if en else "有人")
    if en:
        return f"{by} {'deleted' if deleted else 'edited'} {where}"
    return f"{by} {'删了' if deleted else '改了'} {where}"


def record(meetings, src: str, index: int, old: str, new: str, by: str, paper: str | None = None,
           paper_name: str | None = None, en: bool = False, now: float | None = None) -> dict | None:
    """保存了一处手改：逐字稿加一行系统行（同一个人接着改同一段就并进上一行）。返回那一行；没在开会、没改返回 None。
    src：保存之前的整篇原文（算这一段在哪一节、第几段）；old / new：这一段改前、改后的原文。"""
    cur = meetings.current() if meetings is not None else None
    if not cur:
        return None
    old_n, new_n = _norm(old), _norm(new)
    if old_n == new_n:
        return None
    now = time.time() if now is None else now
    loc = locate(src, index)
    where = loc["where_en"] if en else loc["where"]
    key = (cur.get("id"), by, paper, loc["where"])
    with _lock:
        last = _last.get(id(meetings))
        if (last and last["key"] == key and now - last["t"] <= MERGE_S and last["new"] == old_n
                and any(x is last["line"] for x in meetings.current_lines())):
            line = last["line"]
            before, after = snippets(last["first"], new_n)
            for k in ("before", "after"):          # annotate 不写空值：改成空的先拿掉
                line.pop(k, None)
            meetings.annotate(line, text=line_text(by, where, not new_n, en), before=before, after=after)
            last.update(new=new_n, t=now)
            return line
        before, after = snippets(old_n, new_n)
        extra = {"by": _one(by, 32), "where": where, "section": loc["section"], "before": before, "after": after,
                 "paper": paper, "paper_name": _one(paper_name, 80) if paper_name else None}
        line = meetings.add_line("system", line_text(by, where, not new_n, en), tag=TAG, extra=extra)
        if line is not None:
            _last[id(meetings)] = {"key": key, "line": line, "first": old_n, "new": new_n, "t": now}
        return line


def from_viewer(viewer, src: str, index: int, old: str, new: str, by: str | None = None) -> dict | None:
    """对比页存好一段之后调（Viewer.para_save）：谁改的（访客的名字，没给就是主持人自己）、哪篇、会议语言都从对比页取。"""
    meetings = getattr(viewer, "meetings", None)
    cur = meetings.current() if meetings is not None else None
    if not cur:
        return None
    name = " ".join(str(by or "").split()) or viewer.me_name()
    lang = str(cur.get("lang") or ((getattr(viewer, "cfg", None) or {}).get("meeting") or {}).get("lang") or "zh")
    pid = pname = None
    try:
        from .meetings import paper_id
        pid = paper_id(viewer.mirror)
        pname = viewer._doc_name()
    except Exception:
        pass
    return record(meetings, src, index, old, new, by=name, paper=pid, paper_name=pname, en=lang == "en")


# ---------------------------------------------------------------- 读
def edits(lines, paper: str | None = None) -> list[dict]:
    """逐字稿里的手改记录（paper 给了：只要这篇的，没记是哪篇的也算）。"""
    return [ln for ln in lines or [] if isinstance(ln, dict) and ln.get("tag") == TAG
            and (paper is None or not ln.get("paper") or ln.get("paper") == paper)]


def detail(ln: dict) -> str:
    """一处手改的改前改后：改前「…」，改后「…」/ 删掉「…」。"""
    b, a = str(ln.get("before") or ""), str(ln.get("after") or "")
    if b and not a:
        return f"删掉「{b}」"
    if a and not b:
        return f"加上「{a}」"
    return f"改前「{b}」，改后「{a}」" if a or b else ""


def fmt(ln: dict) -> str:
    """给 AI 看的一行：29:30 小林 改了 5.1 第（1）段：改前「…」，改后「…」。"""
    d = detail(ln)
    return f"{ln.get('t') or ''} {ln.get('text') or ''}".strip() + (f"：{d}" if d else "")


def current_para(text: str, ln: dict) -> str:
    """这处手改现在在论文里是哪一整段：拿改后片段里最长的那截（片段是压了空白、两头带"…"的）去全文里找，
    正好一段里有它就是那段。删掉的、后来又被改过找不到的、好几段都有的，返回 ""。"""
    after = str(ln.get("after") or "")
    if not text or not after:
        return ""
    key = max((p.strip() for p in after.split("…")), key=len, default="")
    if len(re.sub(r"\s+", "", key)) < 6:
        return ""
    hits = [p for p in _paras(text) if key in " ".join(p.split())]
    return hits[0] if len(hits) == 1 else ""


def note(lines, paper: str | None = None, limit: int = 8, chars: int = 1600, text: str | None = None,
         full: int = FULL_N) -> str:
    """本场的手改，一行一处（新的在后），最多 limit 处、chars 字（超了丢最早的）。没有返回 ""。
    text：论文现在的全文（给快嘴时传）：最近 full 处附上改后的整段，每段最多 FULL_MAX 字，
    对方说"我改了 5.2 第一段"时快嘴照着新文字回话，不说看不到。"""
    eds = edits(lines, paper)
    rows = [fmt(ln) for ln in eds]
    dropped = max(0, len(rows) - limit)
    rows = rows[dropped:]
    while len(rows) > 1 and sum(len(r) + 1 for r in rows) > chars:
        rows.pop(0)
        dropped += 1
    if not rows:
        return ""
    out = "\n".join(([f"（更早的 {dropped} 处略）"] if dropped else []) + rows)
    blocks, seen = [], set()
    for ln in reversed(eds if text and full > 0 else []):
        if len(blocks) >= full:
            break
        p = current_para(text, ln)
        if p and p not in seen:
            seen.add(p)
            blocks.append(f"〔{ln.get('where') or ln.get('section') or ''}〕{_one(p, FULL_MAX)}")
    if blocks:
        out += "\n最近改的段落现在整段是（论文全文里也是这样）：\n" + "\n".join(reversed(blocks))
    return out


def summary_block(lines) -> str:
    """纪要的材料：会上直接在文档里改过的地方（没有返回 ""）。"""
    rows = [f"[{ln.get('t') or ''}] {ln.get('text') or ''}" + (f"：{detail(ln)}" if detail(ln) else "") for ln in edits(lines)]
    return "\n".join(rows[-30:])
