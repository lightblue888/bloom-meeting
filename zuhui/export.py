# -*- coding: utf-8 -*-
"""导出和分享（第 5 批）：纪要存成 Word / PDF、带格式复制、会议包（一个自包含的 HTML 文件）。散会后自动保存在
zuhui/autosave.py，用的也是这里的几样。

纪要先排成一串"块"（标题、段落、列表……），同一串块再出 HTML（复制、PDF、会议包、Word 的来源）、Markdown、纯文本，
几种格式内容一样：主题摘要、核心结论、各主题、每个人、后续工作、上次遗留、发言时长，每条带它在会上的时间。
（旧纪要里的 agenda 字段不导出：议程功能 2026-11-18 删了。）
标题这类界面字跟着 Bloom 的界面语言（ui=zh / en），纪要正文是模型写的，原样。

- Word：安装包带的 pandoc（components.pandoc()）把 HTML 转成 .docx。
- PDF：不加依赖，用系统里的 Edge（没有就找 Chrome）无界面打印（--headless --print-to-pdf），A4。
- 会议包：纪要、修改清单、修订对比（散会快照 diff.html，放进 iframe 的 srcdoc），可选逐字稿；论文 figures/ 下的图
  转成 data: 地址内嵌，公式要排版时把本机那份 MathJax 一起放进去，不链任何网上的东西，离线能开。跟随系统深浅色。

出错抛 ExportError(code, detail)：code 给界面翻译（not_found、no_summary、no_content、no_pandoc、no_browser、
convert、bad_path、write、kind），detail 是原因的原文。
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
KINDS = ("docx", "pdf", "pack")
EXT = {"docx": "docx", "pdf": "pdf", "pack": "html"}
PDF_TIMEOUT = 90
DOCX_TIMEOUT = 60
FIG_MAX = 8 * 1024 * 1024            # 一张图最多多大（再大的不内嵌）
FIGS_MAX = 40 * 1024 * 1024          # 一个会议包里的图一共最多多大
FIG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
            ".svg": "image/svg+xml"}
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_DATE_TAIL = re.compile(r"\s*·\s*\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*$")
_SAID_BY = re.compile(r"^(?:参会的老师|参会者)(.+?)说[:：]\s*")

# 键 → (中文, English)；和界面上 meeting.summary.* / meeting.todos.* 的说法一样
WORDS = {
    "notes": ("纪要", "Notes"),
    "abstract": ("主题摘要", "Summary"),
    "conclusions": ("核心结论", "Key conclusions"),
    "people": ("参会者", "By person"),
    "said": ("发言要点", "Said"),
    "feedback": ("意见", "Feedback"),
    "asks": ("待办", "Asked to do"),
    "p_said": ("进展", "Progress"),
    "p_feedback": ("意见", "Feedback"),
    "p_asks": ("下周任务", "Next week"),
    "todos": ("后续工作安排", "Follow-ups"),
    "carried": ("遗留事项", "Left from last time"),
    "carried_done": ("已完成", "Done"),
    "carried_open": ("未完成", "Not yet"),
    "talk": ("发言时长", "Talk time"),
    "participants": ("参会者", "Participants"),
    "changes": ("修改清单", "To-dos"),
    "diff": ("修订对比", "Revisions"),
    "transcript": ("逐字稿", "Transcript"),
    "pack": ("会议包", "Meeting pack"),
    "done_in_meeting": ("会中已修改", "Edited in the meeting"),
    "plan": ("修改方案：{a}", "Plan: {a}"),
    "need": ("需补充：{a}", "Needs: {a}"),
    "tag_rerun": ("需重跑实验", "Rerun experiments"),
    "tag_data": ("需补数据", "Needs new data"),
    "tag_figure": ("需重画图表", "Redo figures or tables"),
    "tag_lit": ("需查文献", "Check the literature"),
    "tag_discuss": ("待讨论", "Needs discussion"),
    "writing": ("整理中", "Working on it"),
    "other": ("其他", "Other"),
    "app": ("软件", "App"),
    "guest": ("访客", "Guest"),
    "host": ("主持人", "Host"),
    "meeting": ("组会", "Meeting"),
    "no_notes": ("暂无纪要", "No notes"),
    "no_changes": ("暂无待修改项", "Nothing to fix after the meeting"),
    "no_lines": ("暂无发言", "Nobody spoke"),
}


class ExportError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def lang_of(ui) -> str:
    return "en" if str(ui or "").strip().lower().startswith("en") else "zh"


def tr(key: str, lang: str, **kw) -> str:
    zh, en = WORDS[key]
    s = en if lang == "en" else zh
    return s.format(**kw) if kw else s


def fmt_dur(sec) -> str:
    """和界面上的 fmtDuration 一样：mm:ss，过了一小时 h:mm:ss。"""
    try:
        n = max(0, int(float(sec or 0)))
    except (TypeError, ValueError):
        n = 0
    h, m, s = n // 3600, n % 3600 // 60, n % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _one(v) -> str:
    return " ".join(str(v or "").split())


def _text(x) -> str:
    return _one(x.get("text") if isinstance(x, dict) else x)


def _at(x, lang: str) -> str:
    """一条后面跟的会上时间：（12:34）；没有就空。"""
    t = _one(x.get("t")) if isinstance(x, dict) else ""
    if not t:
        return ""
    return f" ({t})" if lang == "en" else f"（{t}）"


def clean_title(t) -> str:
    return _DATE_TAIL.sub("", _one(t)).strip()


# ---------------------------------------------------------------- 一场会的基本信息
def names_of(g: dict, cfg: dict | None = None) -> tuple[str, str]:
    """(主持人, AI) 的名字：散会时存下的参会人优先，没有就按现在的设置。"""
    import getpass
    from .config import assistant_name, load
    people = g.get("participants") or []
    me = next((str(p.get("name")) for p in people if isinstance(p, dict) and p.get("name") and (p.get("you") or p.get("role") == "teacher")), "")
    ai = next((str(p.get("name")) for p in people if isinstance(p, dict) and p.get("name") and (p.get("id") == "claude" or p.get("role") == "student")), "")
    if not me or not ai:
        cfg = cfg if cfg is not None else load()
        me = me or str((cfg.get("meeting") or {}).get("me") or getpass.getuser() or "我")
        ai = ai or assistant_name(cfg)
    return me, ai


def who_of(line: dict, me: str, ai: str, lang: str) -> str:
    w = line.get("who")
    if w == "teacher":
        return me
    if w == "student":
        return ai
    if w == "guest":
        return _one(line.get("name")) or tr("guest", lang)
    return ""


def info(g: dict, cfg: dict | None, lang: str) -> dict:
    """标题、日期、时长、参会人，和文件名用的那一截（"2026-11-18 组会"）。"""
    m = g.get("meeting") or {}
    me, ai = names_of(g, cfg)
    title = clean_title(m.get("title")) or tr("meeting", lang)
    try:
        started = datetime.fromisoformat(str(m.get("started")))
        when, day = started.strftime("%Y-%m-%d %H:%M"), started.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        when, day = "", ""
    people: list[str] = []
    for p in g.get("participants") or []:
        n = _one(p.get("name")) if isinstance(p, dict) else ""
        if n and n not in people:
            people.append(n)
    lines = g.get("transcript") or []
    # 没有论文的会：AI 没参加（参会人名单里本来就没有它），导出的"参会人"也不写它
    ai_in = m.get("mode") != "chat" or any(l.get("who") == "student" for l in lines)
    for n in [me] + ([ai] if ai_in else []) + [who_of(l, me, ai, lang) for l in lines if l.get("who") == "guest"]:
        if n and n not in people:
            people.append(n)
    safe = _BAD_NAME.sub(" ", title).strip().strip(".") or tr("meeting", lang)
    return {"title": title, "when": when, "day": day, "dur": fmt_dur(m.get("duration_s")), "people": people,
            "me": me, "ai": ai, "stem": f"{day} {safe}".strip()}


def file_name(g: dict, kind: str, lang: str, cfg: dict | None = None) -> str:
    """默认文件名：日期 会议名 纪要.docx / 纪要.pdf / 会议包.html。"""
    word = {"docx": "notes", "pdf": "notes", "pack": "pack", "md": "notes"}.get(kind, "notes")
    return f"{info(g, cfg, lang)['stem']} {tr(word, lang)}.{EXT.get(kind, kind)}"


# ---------------------------------------------------------------- 纪要 → 块
# 块：("h1"|"h2"|"h3"|"meta"|"p"|"label", 文字) 或 ("ul"|"ol"|"check", [条目])；
# 条目 {"text", "b"?（前面加粗的一小截）, "at"?（会上时间）, "done"?（check）, "sub"?: [文字]}
def _item(x, lang: str, by: bool = False, b: str = "") -> dict:
    s = _text(x)
    if by and isinstance(x, dict) and _one(x.get("by")):
        s = f"{_one(x.get('by'))}：{s}" if lang != "en" else f"{_one(x.get('by'))}: {s}"
    return {"text": s, "b": b, "at": _at(x, lang)}


def minutes_blocks(g: dict, cfg: dict | None, lang: str, head: bool = True) -> list:
    """一场会的纪要排成块。没有纪要抛 ExportError("no_summary")。"""
    s = g.get("summary")
    if not isinstance(s, dict) or not (s.get("abstract") or s.get("conclusions") or s.get("sections") or s.get("people")
                                       or s.get("todos")):
        raise ExportError("no_summary")
    inf = info(g, cfg, lang)
    sep = ", " if lang == "en" else "、"
    colon = ": " if lang == "en" else "："
    out: list = []
    if head:
        out.append(("h1", _one(s.get("title")) or inf["title"]))
        meta = [x for x in (inf["when"], inf["dur"], inf["title"] if _one(s.get("title")) and _one(s.get("title")) != inf["title"] else "") if x]
        if meta:
            out.append(("meta", " · ".join(meta)))
        if inf["people"]:
            out.append(("meta", tr("participants", lang) + colon + sep.join(inf["people"])))
    if _one(s.get("abstract")):
        out += [("h2", tr("abstract", lang)), ("p", _one(s.get("abstract")))]
    if s.get("conclusions"):
        out += [("h2", tr("conclusions", lang)), ("ol", [_item(c, lang) for c in s["conclusions"]])]
    topics: list = []
    for i, sec in enumerate(x for x in s.get("sections") or [] if isinstance(x, dict)):
        topics.append(("h2", f"{i + 1:02d} {_one(sec.get('title'))}".strip() + _at(sec, lang)))
        for c in sec.get("cards") or []:
            if not isinstance(c, dict):
                continue
            if _one(c.get("title")):
                topics.append(("h3", _one(c.get("title"))))
            if c.get("bullets"):
                topics.append(("ul", [_item(b, lang) for b in c["bullets"]]))
    people: list = []
    persons = [p for p in s.get("people") or [] if isinstance(p, dict) and _one(p.get("name"))]
    if persons:
        heads = ("p_said", "p_feedback", "p_asks") if s.get("template") == "progress" else ("said", "feedback", "asks")
        people.append(("h2", tr("people", lang)))
        for p in persons:
            people.append(("h3", _one(p.get("name"))))
            for h, k in zip(heads, ("said", "feedback", "asks")):
                if p.get(k):
                    people += [("label", tr(h, lang)), ("ul", [_item(x, lang, by=k != "said") for x in p[k]])]
    out += (people + topics) if s.get("template") == "progress" else (topics + people)
    if s.get("todos"):
        out += [("h2", tr("todos", lang)), ("ul", [_item(x, lang) for x in s["todos"]])]
    carried = [c for c in s.get("carried") or [] if isinstance(c, dict)]
    if carried:
        rows = []
        for c in sorted(carried, key=lambda c: not c.get("done")):
            text = _one(c.get("item")) + (colon + _one(c.get("text")) if _one(c.get("text")) else "")
            rows.append({"text": text, "b": tr("carried_done" if c.get("done") else "carried_open", lang), "at": _at(c, lang)})
        out += [("h2", tr("carried", lang)), ("ul", rows)]
    talk =[x for x in g.get("talk") or [] if isinstance(x, dict)]
    if talk:
        rows = []
        for x in talk:
            name = inf["me"] if x.get("who") == "teacher" else inf["ai"] if x.get("who") == "student" \
                else (_one(x.get("name")) or tr("guest", lang))
            try:
                share = round(float(x.get("share") or 0) * 100)
            except (TypeError, ValueError):
                share = 0
            rows.append({"text": f"{name}  {fmt_dur(x.get('sec'))} · {share}%", "b": "", "at": ""})
        out += [("h2", tr("talk", lang)), ("ul", rows)]
    return out


def _sec_rank(s: str) -> list:
    """修改清单按论文里的先后：摘要、标题 → 编了号的节 → 其他 → 附录（和界面 secRank 一样）。"""
    if not s:
        return [9999]
    if re.match(r"^(摘要|abstract|标题|title)", s, re.I):
        return [0]
    m = re.match(r"^(\d+(?:\.\d+)*)", s)
    if m:
        return [1] + [int(x) for x in m.group(1).split(".")]
    if re.match(r"^(附录|appendix)", s, re.I):
        return [8000]
    return [5000]


def todos_blocks(g: dict, lang: str, head: bool = True) -> list:
    """修改清单（界面上 TodoList 那一页）：标注按论文、按节分组，各带谁说的、怎么改、要补什么，勾没勾；
    最后是会上已改好的。什么都没有返回 []。"""
    tasks = [x for x in ((g.get("board") or {}).get("tasks") or []) if isinstance(x, dict)]
    ticked = {k[5:] for k in g.get("carry_done") or [] if isinstance(k, str) and k.startswith("note:")}
    notes = sorted([x for x in tasks if x.get("note")], key=lambda x: x.get("idx") or 0)
    edits = [x for x in tasks if not x.get("note") and x.get("status") == "done" and not x.get("rejected")]
    if not notes and not edits:
        return []
    multi = len({x.get("paper") for x in tasks if x.get("paper")}) > 1
    paper_order: list[str] = []
    for x in tasks:
        n = str(x.get("paper_name") or "")
        if n not in paper_order:
            paper_order.append(n)

    def where(x) -> str:
        return _one(x.get("section") or (x.get("focus") or {}).get("section") or "")

    def is_app(x) -> bool:
        return bool(x.get("app")) or x.get("kind") == "app"

    groups: dict = {}
    for x in notes:
        k = ("app",) if is_app(x) else (str(x.get("paper_name") or "") if multi else "", where(x))
        groups.setdefault(k, []).append(x)

    def order(k):
        if k == ("app",):
            return (1, 0, [])
        return (0, paper_order.index(k[0]) if k[0] in paper_order else 0, _sec_rank(k[1]))

    out: list = [("h2", tr("changes", lang))] if head else []
    for k in sorted(groups, key=order):
        label = tr("app", lang) if k == ("app",) else " · ".join(x for x in (k[0], k[1] or tr("other", lang)) if x)
        rows = []
        for x in groups[k]:
            remark = str(x.get("remark") or "")
            m = _SAID_BY.match(remark)
            who, said = (m.group(1), remark[m.end():]) if m else ("", remark)
            sub = []
            if _one(said):
                sub.append((f"{who} " if who else "") + _one(said))
            if _one(x.get("plan")):
                sub.append(tr("plan", lang, a=_one(x.get("plan"))))
            elif x.get("status") != "done":
                sub.append(tr("writing", lang))
            from .board import note_tags            # 标注的标签：需重跑实验、需补数据……（界面上那排胶囊）
            tags = [tr("tag_" + k, lang) for k in note_tags(x)]
            if tags:
                sub.append((", " if lang == "en" else "、").join(tags))
            need = _one(x.get("need"))
            rows.append({"text": _one(x.get("title")) + ((f" ({need})" if lang == "en" else f"（{need}）") if need else ""),
                         "done": str(x.get("id")) in ticked, "sub": sub})
        out += [("h3", label), ("check", rows)]
    if edits:
        rows = [{"text": (f"{x.get('paper_name')} · " if multi and x.get("paper_name") else "") + _one(x.get("title")), "done": True}
                for x in edits]
        out += [("h3", tr("done_in_meeting", lang)), ("check", rows)]
    return out


def transcript_blocks(g: dict, cfg: dict | None, lang: str) -> list:
    """逐字稿（整理后的干净版，系统行不要）：每句一条，"[mm:ss] 名字：内容"。"""
    me, ai = names_of(g, cfg)
    rows = []
    for l in g.get("transcript") or []:
        if l.get("who") not in ("teacher", "student", "guest") or not _one(l.get("text")):
            continue
        rows.append({"text": _one(l.get("text")), "b": who_of(l, me, ai, lang), "t": _one(l.get("t"))})
    return [("lines", rows)] if rows else []


# ---------------------------------------------------------------- 块 → HTML / Markdown / 纯文本
def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def blocks_html(blocks: list, rich: bool = True) -> str:
    """块 → HTML 片段。rich=False：给剪贴板和 pandoc 的朴素版（只用 h1–h4、p、ul、ol、strong，时间用灰字）。"""
    out = []
    for kind, v in blocks:
        if kind in ("h1", "h2", "h3"):
            out.append(f"<{kind}>{_e(v)}</{kind}>")
        elif kind == "meta":
            out.append(f'<p class="meta">{_e(v)}</p>' if rich else f'<p><span style="color:#808080">{_e(v)}</span></p>')
        elif kind == "p":
            out.append(f"<p>{_e(v)}</p>")
        elif kind == "label":
            out.append(f'<p class="lbl"><strong>{_e(v)}</strong></p>' if rich else f"<p><strong>{_e(v)}</strong></p>")
        elif kind in ("ul", "ol"):
            lis = []
            for it in v:
                b = f"<strong>{_e(it['b'])}</strong> " if it.get("b") else ""
                at = (f'<span class="t">{_e(it["at"])}</span>' if rich else f'<span style="color:#808080">{_e(it["at"])}</span>') \
                    if it.get("at") else ""
                lis.append(f"<li>{b}{_e(it['text'])}{at}</li>")
            out.append(f"<{kind}>" + "".join(lis) + f"</{kind}>")
        elif kind == "check":
            lis = []
            for it in v:
                box = "☑" if it.get("done") else "☐"
                sub = "".join(f"<br>{_e(x)}" if not rich else f'<span class="sub">{_e(x)}</span>' for x in it.get("sub") or [])
                cls = ' class="done"' if it.get("done") and rich else ""
                lis.append(f'<li{cls}><span class="ck">{box}</span> {_e(it["text"])}{sub}</li>' if rich
                           else f"<li>{box} {_e(it['text'])}{sub}</li>")
            out.append('<ul class="check">' + "".join(lis) + "</ul>" if rich else "<ul>" + "".join(lis) + "</ul>")
        elif kind == "lines":
            rows = "".join(f'<li><span class="lt">{_e(it.get("t"))}</span><b>{_e(it.get("b"))}</b><span>{_e(it["text"])}</span></li>'
                           for it in v)
            out.append(f'<ol class="lines">{rows}</ol>')
    return "\n".join(out)


def blocks_md(blocks: list) -> str:
    out: list[str] = []
    for kind, v in blocks:
        if kind == "h1":
            out += [f"# {v}", ""]
        elif kind == "h2":
            out += [f"## {v}", ""]
        elif kind == "h3":
            out += [f"### {v}", ""]
        elif kind in ("meta", "p"):
            out += [str(v), ""]
        elif kind == "label":
            out += [f"**{v}**", ""]
        elif kind in ("ul", "ol"):
            for i, it in enumerate(v, 1):
                b = f"**{it['b']}** " if it.get("b") else ""
                out.append(f"{f'{i}.' if kind == 'ol' else '-'} {b}{it['text']}{it.get('at') or ''}")
            out.append("")
        elif kind == "check":
            for it in v:
                out.append(f"- [{'x' if it.get('done') else ' '}] {it['text']}")
                out += [f"  - {x}" for x in it.get("sub") or []]
            out.append("")
        elif kind == "lines":
            for it in v:
                out += [f"**{it.get('b')}** `{it.get('t')}`  ", it["text"], ""]
    return "\n".join(out).rstrip() + "\n"


def blocks_text(blocks: list) -> str:
    """纯文本（剪贴板里的另一份：粘进只认文字的地方）。标题单独一行，列表"- "、"1. "。"""
    out: list[str] = []
    for kind, v in blocks:
        if kind in ("h1", "h2", "h3", "meta", "p", "label"):
            if kind in ("h2", "h3") and out and out[-1] != "":
                out.append("")
            out.append(str(v))
            if kind in ("h1", "p"):
                out.append("")
        elif kind in ("ul", "ol"):
            for i, it in enumerate(v, 1):
                b = f"[{it['b']}] " if it.get("b") else ""
                out.append(f"{f'{i}.' if kind == 'ol' else '-'} {b}{it['text']}{it.get('at') or ''}")
        elif kind == "check":
            for it in v:
                out.append(f"{'☑' if it.get('done') else '☐'} {it['text']}")
                out += [f"    {x}" for x in it.get("sub") or []]
        elif kind == "lines":
            out += [f"[{it.get('t')}] {it.get('b')}：{it['text']}" for it in v]
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 纪要：复制、Markdown、Word、PDF
def minutes_clip(g: dict, cfg: dict | None, lang: str) -> dict:
    """复制（带格式）：剪贴板里同时放 HTML 和纯文本。"""
    b = minutes_blocks(g, cfg, lang)
    return {"html": blocks_html(b, rich=False), "text": blocks_text(b)}


def minutes_md(g: dict, cfg: dict | None, lang: str) -> str:
    return blocks_md(minutes_blocks(g, cfg, lang))


PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; color: #1d1d1f; background: #fff;
  font: 10.5pt/1.7 "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans SC", "Segoe UI", sans-serif; }
h1 { font-size: 19pt; line-height: 1.3; margin: 0 0 6pt; }
h2 { font-size: 13.5pt; margin: 18pt 0 6pt; padding-bottom: 3pt; border-bottom: 1px solid #d9d9de; }
h3 { font-size: 11.5pt; margin: 10pt 0 4pt; }
h2, h3, .lbl { break-after: avoid; page-break-after: avoid; }
p { margin: 4pt 0; }
.meta { margin: 0; color: #6e6e73; font-size: 9.5pt; }
.lbl { margin: 6pt 0 2pt; color: #424245; }
ul, ol { margin: 2pt 0 6pt; padding-left: 18pt; }
li { margin: 2pt 0; break-inside: avoid; page-break-inside: avoid; }
.t { color: #8e8e93; font-size: .9em; }
ul.check { list-style: none; padding-left: 0; }
ul.check li.done { color: #6e6e73; }
.ck { display: inline-block; width: 1.3em; }
.sub { display: block; margin-left: 1.3em; color: #6e6e73; font-size: .93em; }
"""


def minutes_page(g: dict, cfg: dict | None, lang: str) -> str:
    """打印成 PDF 用的整页（A4，浅色）。"""
    inf = info(g, cfg, lang)
    body = blocks_html(minutes_blocks(g, cfg, lang))
    return (f'<!doctype html>\n<html lang="{"en" if lang == "en" else "zh-CN"}">\n<head>\n<meta charset="utf-8">\n'
            f"<title>{_e(inf['stem'])} {_e(tr('notes', lang))}</title>\n<style>{PRINT_CSS}</style>\n</head>\n<body>\n"
            f"{body}\n</body>\n</html>\n")


def _check_target(to, kind: str) -> Path:
    """存到哪：要是完整路径、所在文件夹在；扩展名按格式改正。"""
    s = str(to or "").strip()
    if not s:
        raise ExportError("bad_path")
    p = Path(s).expanduser()
    if not p.is_absolute() or not p.parent.is_dir():
        raise ExportError("bad_path", s)
    ext = "." + EXT[kind]
    return p if p.suffix.lower() == ext else p.with_name(p.name + ext)


def _replace_into(src: Path, to: Path) -> None:
    """做好的临时文件换到目标位置（同名的旧文件换掉；对方开着那个文件时报 write）。"""
    tmp = to.with_name(f".{to.name}.{os.getpid()}.part")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, to)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ExportError("write", str(e)) from None


def html_to_docx(fragment: str, to: Path, lang: str = "zh") -> Path:
    """HTML 片段 → Word（安装包带的 pandoc）。"""
    from . import components
    exe = components.pandoc()
    if not exe:
        raise ExportError("no_pandoc")
    work = Path(tempfile.mkdtemp(prefix="bloom-docx-"))
    try:
        src = work / "in.html"
        src.write_text(f'<!doctype html><html lang="{"en" if lang == "en" else "zh-CN"}"><head><meta charset="utf-8"></head>'
                       f"<body>{fragment}</body></html>", encoding="utf-8")
        out = work / "out.docx"
        try:
            r = subprocess.run([exe, "-f", "html", "-t", "docx", "-o", str(out), str(src)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=DOCX_TIMEOUT, creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ExportError("convert", repr(e)[:200]) from None
        if r.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            raise ExportError("convert", (r.stderr or r.stdout or "").strip()[-300:])
        _replace_into(out, to)
        return to
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _reg_app_path(exe: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                v = winreg.QueryValue(k, None)
                if v and Path(v.strip('"')).is_file():
                    return v.strip('"')
        except OSError:
            continue
    return None


def find_browser() -> str | None:
    """打印 PDF 用的浏览器：BLOOM_PDF_BROWSER 指定的 → Edge（Windows 自带）→ Chrome。"""
    env = os.environ.get("BLOOM_PDF_BROWSER")
    if env:
        return env if Path(env).is_file() else None
    if os.name != "nt":
        for n in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            w = shutil.which(n)
            if w:
                return w
        return None
    for exe, rel in (("msedge.exe", r"Microsoft\Edge\Application\msedge.exe"), ("chrome.exe", r"Google\Chrome\Application\chrome.exe")):
        hit = _reg_app_path(exe)
        if hit:
            return hit
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base and (Path(base) / rel).is_file():
                return str(Path(base) / rel)
    return None


def html_to_pdf(page: str, to: Path) -> Path:
    """整页 HTML → PDF：浏览器无界面打印（单独的临时配置目录，不碰用户自己开着的 Edge；不联网）。"""
    exe = find_browser()
    if not exe:
        raise ExportError("no_browser")
    work = Path(tempfile.mkdtemp(prefix="bloom-pdf-"))
    try:
        src = work / "page.html"
        src.write_text(page, encoding="utf-8")
        out = work / "out.pdf"
        args = [exe, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-sync", "--disable-background-networking", "--disable-component-update", "--mute-audio",
                f"--user-data-dir={work / 'profile'}", "--no-pdf-header-footer", "--print-to-pdf-no-header",
                f"--print-to-pdf={out}", src.as_uri()]
        try:
            subprocess.run(args, capture_output=True, timeout=PDF_TIMEOUT, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise ExportError("convert", "timeout") from None
        except OSError as e:
            raise ExportError("convert", repr(e)[:200]) from None
        deadline = time.time() + 5                  # 有的版本进程先退出、文件随后才写完
        while time.time() < deadline and not (out.is_file() and out.stat().st_size > 0):
            time.sleep(0.2)
        try:
            ok = out.is_file() and out.read_bytes()[:5] == b"%PDF-"
        except OSError:
            ok = False
        if not ok:
            raise ExportError("convert", "no pdf")
        _replace_into(out, to)
        return to
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- 修订对比：离线能开
_GFONTS = re.compile(r'<link[^>]+(?:fonts\.googleapis\.com|fonts\.gstatic\.com)[^>]*>\s*', re.I)
_FONT_CSS = ("<style>:root{--serif:'Noto Serif SC','Source Han Serif SC','Songti SC','SimSun',serif;"
             "--sans:'Microsoft YaHei UI','Microsoft YaHei','PingFang SC','Noto Sans SC','Segoe UI',sans-serif}"
             ".zh-figrow .para{text-align:center}"
             "img.zh-fig{max-width:min(100%,880px);height:auto;border-radius:4px;background:#fff;padding:10px;box-sizing:border-box}"
             ".para.zh-math{text-align:center;overflow-x:auto;overflow-y:hidden;padding:6px 0;white-space:normal}"
             ".para.zh-math mjx-container{margin:0!important}.para.zh-math mjx-container>svg{max-width:100%;height:auto}</style>")
# 图放回原位（和实时页的注入一样：标签是"图 N"的那一行；"图 N 注"的上一行），公式排版（本机的 MathJax，不联网）
_FIG_JS = r"""<script>(function(){var FIGS=__FIGS__;var docs=document.querySelector('.docs');if(!docs)return;
function put(row,no){var f=FIGS[no],p=row&&row.querySelector('.para');if(!f||!p||row.classList.contains('zh-figrow'))return;
row.classList.add('zh-figrow');p.innerHTML='<img class="zh-fig" alt="">';p.firstChild.src=f;}
Array.prototype.forEach.call(docs.querySelectorAll('.row'),function(row){var lab=row.querySelector('.rowhead .label');if(!lab)return;
var name=(lab.textContent||'').replace(/\s+/g,' ').trim(),m=/^(?:图|Figure|Fig\.?)\s*(\d+)$/i.exec(name);if(m){put(row,m[1]);return;}
m=/^图\s*(\d+)\s*注$/.exec(name);if(m){var prev=row.previousElementSibling;if(prev&&prev.classList.contains('row')&&prev.querySelector('.para.fixed'))put(prev,m[1]);}});
var maths=[],inl=[];
Array.prototype.forEach.call(docs.querySelectorAll('.para.mono'),function(p){if(p.querySelector('ins, del'))return;
var lab=p.closest('.row')&&p.closest('.row').querySelector('.rowhead .label');if(lab&&/^(代码|Code)$/.test((lab.textContent||'').trim()))return;
var tex=(p.textContent||'').trim();if(!/\\[a-zA-Z]+|[_^]/.test(tex))return;p.classList.add('zh-math');p.textContent='\\['+tex+'\\]';maths.push(p);});
Array.prototype.forEach.call(docs.querySelectorAll('.para:not(.mono), .sect, table.dtable td, table.dtable th'),function(p){
if(p.querySelector('ins, del')||!/(^|[^\\$])\$[^$\n]+\$/.test(p.textContent||''))return;inl.push(p);});
var all=maths.concat(inl);
window.MathJax={tex:{tags:'ams',displayMath:[['\\[','\\]']],inlineMath:[['\\(','\\)'],['$','$']],processEscapes:true,
packages:{'[-]':['html','require']},macros:{href:['{#2}',2],class:['{#2}',2],style:['{#2}',2],cssId:['{#2}',2],data:['{#2}',2]}},svg:{fontCache:'global'},
startup:{typeset:false,ready:function(){MathJax.startup.defaultReady();if(!all.length)return;MathJax.typesetPromise(all).catch(function(){
var q=Promise.resolve();all.forEach(function(el){q=q.then(function(){return MathJax.typesetPromise([el]);}).catch(function(){});});});}}};
})();</script>"""
_HAS_MATH = re.compile(r"\\[a-zA-Z]{2,}|\$[^$<\n]{1,200}\$")


def mathjax_file() -> Path | None:
    """本机那份 MathJax（和实时页同一份：项目里的完整版，没有就用 live-doc 的精简版）。"""
    for p in (Path(__file__).resolve().parent.parent / "assets" / "mathjax-tex-svg-full.js",
              Path.home() / ".claude" / "tools" / "live-doc" / "mathjax-tex-svg.js"):
        if p.is_file():
            return p
    return None


def figures_of(meta: dict) -> dict[str, Path]:
    """这场会论文文件夹 figures/ 下的图（编号 → 文件）：figures.json 写了的，加上 fig1.png、图2.jpg 这样起名的。
    几篇论文都看：先看屏幕上最后那篇。"""
    from .meetings import papers_of
    ps = papers_of(meta)
    act = meta.get("active_paper")
    ps.sort(key=lambda p: 0 if p.get("id") == act else 1)
    out: dict[str, Path] = {}
    for p in ps:
        d = Path(str(p.get("path"))).parent / "figures"
        if not d.is_dir():
            continue

        def put(no, f: Path) -> None:
            if f.is_file() and f.suffix.lower() in FIG_MIME and str(no) not in out:
                out[str(no)] = f
        try:
            data = json.loads((d / "figures.json").read_text(encoding="utf-8"))
            for k, v in (data or {}).items():
                put(k, d / str((v or {}).get("file") or ""))
        except (OSError, ValueError, AttributeError):
            pass
        for f in sorted(d.glob("*")):
            m = re.match(r"(?:fig|figure|图)[\s_-]*(\d+)\.[A-Za-z]+$", f.name, re.I)
            if m:
                put(int(m.group(1)), f)
    return out


def data_uri(f: Path) -> str | None:
    try:
        if f.stat().st_size > FIG_MAX:
            return None
        return f"data:{FIG_MIME.get(f.suffix.lower(), 'application/octet-stream')};base64," + base64.b64encode(f.read_bytes()).decode("ascii")
    except OSError:
        return None


def offline_diff(page: str, figs: dict[str, Path] | None = None, math: bool = True) -> str:
    """散会快照的修订对比页 → 离线能开：不链 Google Fonts（换系统字体），本地图片地址换成 data:，
    论文的图放回原位，有公式时带上本机那份 MathJax。"""
    page = _GFONTS.sub("", page)
    page = page.replace("</head>", _FONT_CSS + "</head>", 1) if "</head>" in page else _FONT_CSS + page
    uris: dict[str, str] = {}
    total = 0
    for no, f in (figs or {}).items():
        u = data_uri(f)
        if u and total + len(u) <= FIGS_MAX * 4 // 3:
            uris[str(no)] = u
            total += len(u)
    js = _FIG_JS.replace("__FIGS__", json.dumps(uris).replace("</", "<\\/"))
    mj = mathjax_file() if math and _HAS_MATH.search(page) else None
    if mj:
        try:
            src = mj.read_text(encoding="utf-8").replace("</script", "<\\/script")
            js += "<script>" + src + "</script>"
        except OSError:
            pass
    return page.replace("</body>", js + "</body>", 1) if "</body>" in page else page + js


# ---------------------------------------------------------------- 会议包
PACK_CSS = """
:root { color-scheme: light dark; --bg: #ffffff; --ink: #1d1d1f; --ink2: #3a3a3c; --mute: #6e6e73; --faint: #8e8e93;
  --line: #e3e3e8; --card: #f5f5f7; --accent: #2f6fdf; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #151517; --ink: #ececf0; --ink2: #d1d1d6; --mute: #a1a1a8; --faint: #8a8a92; --line: #2e2e33;
    --card: #1e1e22; --accent: #7aa7ff; }
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--ink); }
body { font: 15px/1.75 "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans SC", "Segoe UI", system-ui, sans-serif; }
.wrap { max-width: 880px; margin: 0 auto; padding: 0 20px; }
header .wrap { padding-top: 28px; padding-bottom: 10px; }
header h1 { font-size: 24px; line-height: 1.35; margin: 0 0 6px; overflow-wrap: anywhere; }
.meta { margin: 0; color: var(--mute); font-size: 13px; overflow-wrap: anywhere; }
nav { position: sticky; top: 0; z-index: 5; background: var(--bg); border-bottom: 1px solid var(--line); }
nav .wrap { display: flex; gap: 2px; overflow-x: auto; }
nav a { flex: none; padding: 10px 14px; color: var(--mute); text-decoration: none; border-bottom: 2px solid transparent;
  margin-bottom: -1px; font-size: 14px; }
nav a:hover { color: var(--ink); }
nav a[aria-current="page"] { color: var(--ink); border-bottom-color: var(--accent); font-weight: 600; }
main > section { padding: 8px 0 56px; scroll-margin-top: 46px; }
main > section > .wrap > h2:first-child, main > section > .wrap > h3:first-child { margin-top: 18px; }
h2 { font-size: 18px; margin: 28px 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--line); }
h3 { font-size: 15.5px; margin: 18px 0 6px; }
p { margin: 6px 0; }
.lbl { margin: 10px 0 2px; color: var(--ink2); }
ul, ol { margin: 4px 0 10px; padding-left: 22px; }
li { margin: 3px 0; overflow-wrap: anywhere; }
.t { color: var(--faint); font-size: 13px; }
ul.check { list-style: none; padding-left: 0; }
ul.check li { padding: 6px 10px; border-radius: 8px; background: var(--card); margin: 6px 0; }
ul.check li.done { color: var(--mute); }
.ck { display: inline-block; width: 1.4em; }
.sub { display: block; margin-left: 1.4em; color: var(--mute); font-size: 13.5px; }
ol.lines { list-style: none; padding: 0; margin: 14px 0; }
ol.lines li { display: grid; grid-template-columns: 52px minmax(0, 7.5em) minmax(0, 1fr); gap: 10px; padding: 5px 0;
  border-bottom: 1px solid var(--line); }
ol.lines .lt { color: var(--faint); font-variant-numeric: tabular-nums; font-size: 13px; padding-top: 2px; }
ol.lines b { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.none { color: var(--mute); margin: 18px 0; }
#diff { padding: 0; }
#diff iframe { display: block; width: 100%; height: calc(100vh - 46px); border: 0; background: var(--bg); }
@media (max-width: 560px) { ol.lines li { grid-template-columns: 44px minmax(0, 1fr); } ol.lines li span:last-child { grid-column: 2; } }
@media print { nav { display: none; } #diff { display: none; } main > section[hidden] { display: block !important; } }
"""
PACK_JS = """<script>(function(){var tabs=[].slice.call(document.querySelectorAll('nav a')),secs=tabs.map(function(a){return document.getElementById(a.getAttribute('href').slice(1));});
function show(id){var hit=false;secs.forEach(function(s,i){var on=s&&s.id===id;if(on)hit=true;if(s)s.hidden=!on;if(on)tabs[i].setAttribute('aria-current','page');else tabs[i].removeAttribute('aria-current');});
if(!hit&&secs[0])show(secs[0].id);}
tabs.forEach(function(a){a.addEventListener('click',function(e){e.preventDefault();var id=a.getAttribute('href').slice(1);show(id);try{history.replaceState(null,'','#'+id);}catch(x){}window.scrollTo(0,0);});});
show((location.hash||'').slice(1));if(location.hash){window.scrollTo(0,0);window.addEventListener('load',function(){window.scrollTo(0,0);});}})();</script>"""


def pack_html(g: dict, cfg: dict | None, lang: str, transcript: bool = False, diff_page: str | None = None,
              figs: dict[str, Path] | None = None) -> str:
    """会议包：一个 HTML 文件，纪要、修改清单、修订对比、（可选）逐字稿，分页签。不带脚本也能看（几节依次排下来）。"""
    inf = info(g, cfg, lang)
    parts: list[tuple[str, str, str]] = []           # (id, 页签名, 内容)
    try:
        notes = blocks_html(minutes_blocks(g, cfg, lang, head=False))
    except ExportError:
        notes = f'<p class="none">{_e(tr("no_notes", lang))}</p>'
    parts.append(("notes", tr("notes", lang), notes))
    todo = todos_blocks(g, lang, head=False)
    parts.append(("todos", tr("changes", lang), blocks_html(todo) if todo else f'<p class="none">{_e(tr("no_changes", lang))}</p>'))
    if diff_page:
        page = offline_diff(diff_page, figs)
        parts.append(("diff", tr("diff", lang), f'<iframe title="{html.escape(tr("diff", lang))}" srcdoc="{html.escape(page, quote=True)}"></iframe>'))
    if transcript:
        tb = transcript_blocks(g, cfg, lang)
        parts.append(("transcript", tr("transcript", lang), blocks_html(tb) if tb else f'<p class="none">{_e(tr("no_lines", lang))}</p>'))
    sep = ", " if lang == "en" else "、"
    colon = ": " if lang == "en" else "："
    meta = " · ".join(x for x in (inf["when"], inf["dur"]) if x)
    people = (tr("participants", lang) + colon + sep.join(inf["people"])) if inf["people"] else ""
    nav = "".join(f'<a href="#{pid}">{_e(name)}</a>' for pid, name, _ in parts)
    secs = "\n".join(f'<section id="{pid}">' + (body if pid == "diff" else f'<div class="wrap">{body}</div>') + "</section>"
                     for pid, _, body in parts)
    title = f"{inf['title']} · {inf['day']}" if inf["day"] else inf["title"]
    return (f'<!doctype html>\n<html lang="{"en" if lang == "en" else "zh-CN"}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n<meta name="generator" content="Bloom">\n'
            f"<title>{_e(title)}</title>\n<style>{PACK_CSS}</style>\n</head>\n<body>\n"
            f'<header><div class="wrap"><h1>{_e(inf["title"])}</h1>'
            + (f'<p class="meta">{_e(meta)}</p>' if meta else "") + (f'<p class="meta">{_e(people)}</p>' if people else "")
            + f'</div></header>\n<nav aria-label="{_e(tr("pack", lang))}"><div class="wrap">{nav}</div></nav>\n<main>\n{secs}\n</main>\n'
            f"{PACK_JS}\n</body>\n</html>\n")


def diff_text(meetings, mid: str) -> str | None:
    p = meetings.diff_path(mid)
    if not p:
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------- 存文件（回顾 → 分享，和散会后自动保存）
def detail_of(meetings, mid: str) -> dict:
    mid = Path(str(mid or "")).name
    g = meetings.get(mid) if mid else None
    if not g:
        raise ExportError("not_found")
    return g


def save(meetings, mid: str, kind: str, to, lang: str = "zh", transcript: bool = False, cfg: dict | None = None) -> Path:
    """回顾里存一份：kind = docx（纪要 Word）/ pdf（纪要 PDF）/ pack（会议包）。返回存好的路径。"""
    if kind not in KINDS:
        raise ExportError("kind")
    g = detail_of(meetings, mid)
    target = _check_target(to, kind)
    if kind == "docx":
        return html_to_docx(blocks_html(minutes_blocks(g, cfg, lang), rich=False), target, lang)
    if kind == "pdf":
        return html_to_pdf(minutes_page(g, cfg, lang), target)
    mid = g["meeting"]["id"]
    text = pack_html(g, cfg, lang, transcript=transcript, diff_page=diff_text(meetings, mid), figs=figures_of(g["meeting"]))
    write_text(target, text)
    return target


def write_text(target: Path, text: str, bom: bool = False) -> None:
    """文字文件先写临时文件再换上去；写不进去报 write。bom：TXT 带 BOM（记事本认中文）。"""
    tmp = target.with_name(f".{target.name}.{os.getpid()}.part")
    try:
        tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
        os.replace(tmp, target)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ExportError("write", str(e)) from None


# ---------------------------------------------------------------- HTTP（view.py 转过来）
def _q(raw_path: str) -> dict:
    from urllib.parse import parse_qs, urlsplit
    return {k: v[0] for k, v in parse_qs(urlsplit(raw_path).query).items() if v}


def _err(e: ExportError) -> dict:
    return {"ok": False, "error": e.code, "detail": e.detail}


_STATUS = {"not_found": 404, "bad_path": 400, "kind": 400, "no_summary": 400}


def http_get(viewer, raw_path: str) -> tuple[int, dict]:
    """GET /api/meetings/<id>/minutes?ui=zh → {ok, html, text, name}（复制带格式）；GET /api/autosave → 设置。"""
    p = raw_path.split("?")[0]
    if p == "/api/autosave":
        from . import autosave
        return 200, autosave.public()
    q = _q(raw_path)
    lang = lang_of(q.get("ui") or getattr(viewer, "ui_lang", "zh"))
    try:
        g = detail_of(viewer.meetings, p.split("/")[3])
        clip = minutes_clip(g, getattr(viewer, "cfg", None), lang)
    except ExportError as e:
        return _STATUS.get(e.code, 500), _err(e)
    return 200, {"ok": True, **clip, "name": file_name(g, "docx", lang, getattr(viewer, "cfg", None))}


def http_post(viewer, p: str, body: dict) -> tuple[int, dict]:
    """POST /api/meetings/<id>/save {kind, to, transcript?, ui?} → {ok, path}；POST /api/autosave {on?, dir?, items?}。"""
    if p == "/api/autosave":
        from . import autosave
        try:
            autosave.save(body if isinstance(body, dict) else {})
        except ValueError as e:
            return 400, {"error": str(e)}
        except OSError:
            return 500, {"error": "failed"}
        return 200, autosave.public()
    lang = lang_of(body.get("ui") or getattr(viewer, "ui_lang", "zh"))
    try:
        path = save(viewer.meetings, p.split("/")[3], str(body.get("kind") or ""), body.get("to"), lang=lang,
                    transcript=bool(body.get("transcript")), cfg=getattr(viewer, "cfg", None))
    except ExportError as e:
        return _STATUS.get(e.code, 500), _err(e)
    except Exception as e:                          # 别让一次导出拖垮后端
        return 500, {"ok": False, "error": "convert", "detail": repr(e)[:200]}
    return 200, {"ok": True, "path": str(path)}
