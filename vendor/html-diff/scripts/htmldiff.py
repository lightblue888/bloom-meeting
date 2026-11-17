#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""html-diff：把同一文稿的两个版本比成一页修订对比。

左栏是带修订标记的正文（中文按字、英文按词比），右栏是批注；页面带改动目录、上一处/下一处、
按批注条目筛选、未改动段落折叠、右侧缩略刻度，以及修订标记 / 修订后 / 原文三种视图。

  python htmldiff.py spec.json                     多文档、带批注；spec 格式见 references/spec.md
  python htmldiff.py before.md after.md -o diff.html [--title 标题] [--lang auto|zh|en] [--ui zh|en]

输出两份：diff.html（发 Artifact 用的片段）和 diff.standalone.html（完整 HTML，本机打开或当文件发）。
--dump changes.json 另存每处改动的新增、删去文字，写批注规则时对照用。
"""
import argparse, bisect, html, json, os, re, sys
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets")
esc = html.escape

UI = {
    "zh": {
        "prev": "‹ 上一处", "next": "下一处 ›", "prev_aria": "上一处改动", "next_aria": "下一处改动",
        "filter": "按条目", "filter_all": "全部改动", "unfold": "展开未改动", "fold": "收起未改动",
        "keys": "跳转", "toc": "改动目录", "toc_btn": "目录", "toc_total": "共 {n} 处", "toolbar": "对比视图",
        "v_mark": "修订标记", "v_new": "修订后", "v_old": "原文", "lg_ins": "插入", "lg_del": "删除",
        "fold_same": "{n} 段未改动", "fold_cite": "{n} 段只改了引用编号", "fold_sep": "，", "fold_open": "展开",
        "tag_same": "未改动", "tag_new": "新增段落", "tag_del": "删除段落", "tag_move": "挪位",
        "tag_edit": "{i} 处插入 · {d} 处删除", "rail": "第 {n} 处",
        "st_ins": "处插入", "st_del": "处删除", "st_notes": "条批注", "st_rows": "处改动",
        "sum_h": "改动量", "col_doc": "文档", "col_chap": "章", "col_a": "原", "col_b": "现", "col_ins": "增",
        "col_del": "删", "col_rows": "改动处", "col_paras": "段落（原 → 现）", "total": "合计", "front": "（开头）",
        "ratio": "{name}增 {i}、删 {d} {u}，约占原文的 {p}%{x}。", "ratio_x": "（不计参考文献）",
        "count_note": "中文按字计、不计空白，英文按词计；含表格和公式源码。",
        "nochange": "两个版本没有差别。", "fig_same": "{lab}未改动。", "fig_move": "{lab}挪了位置。",
        "intro": "原文：{a}；现稿：{b}。未改动的段落折叠起来，点开能看。",
        "unit": {"zh": "字", "en": "词"},
    },
    "en": {
        "prev": "‹ Prev", "next": "Next ›", "prev_aria": "Previous change", "next_aria": "Next change",
        "filter": "Note", "filter_all": "All changes", "unfold": "Expand unchanged", "fold": "Collapse unchanged",
        "keys": "jump", "toc": "Changes", "toc_btn": "Changes", "toc_total": "{n} in total", "toolbar": "Compare view",
        "v_mark": "Markup", "v_new": "Revised", "v_old": "Original", "lg_ins": "inserted", "lg_del": "deleted",
        "fold_same": "{n} unchanged", "fold_cite": "{n} with renumbered citations only", "fold_sep": ", ",
        "fold_open": "expand", "tag_same": "unchanged", "tag_new": "new", "tag_del": "deleted", "tag_move": "moved",
        "tag_edit": "{i} insertions · {d} deletions", "rail": "Change {n}",
        "st_ins": "insertions", "st_del": "deletions", "st_notes": "notes", "st_rows": "changes",
        "sum_h": "Size of the revision", "col_doc": "Document", "col_chap": "Section", "col_a": "Before",
        "col_b": "After", "col_ins": "Added", "col_del": "Removed", "col_rows": "Changes",
        "col_paras": "Paragraphs (before → after)",
        "total": "Total", "front": "(front matter)",
        "ratio": "{name}added {i}, removed {d} {u}, about {p}% of the original{x}.",
        "ratio_x": " (references excluded)",
        "count_note": "Chinese is counted in characters without whitespace, English in words; tables and equation source included.",
        "nochange": "The two versions are identical.", "fig_same": "{lab} unchanged.", "fig_move": "{lab} moved.",
        "intro": "Original: {a}; revised: {b}. Unchanged paragraphs are collapsed; click to expand.",
        "unit": {"zh": "chars", "en": "words"},
    },
}
LAB = {
    "zh": {"para": "第 {n} 段", "table": "表", "eq": "公式", "eqn": "式 ({t})", "code": "代码", "fig": "图",
           "list": "列表", "head": "标题", "deleted": "删去的段落", "cap": " 注"},
    "en": {"para": "Paragraph {n}", "table": "Table", "eq": "Equation", "eqn": "Eq. ({t})", "code": "Code",
           "fig": "Figure", "list": "List", "head": "Heading", "deleted": "Deleted paragraph", "cap": " caption"},
}
HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
REFS = re.compile(r"^(参考文献|References|Bibliography)", re.I)
DEFAULT_CITE = r"\[\d+(?:\s*[,，–-]\s*\d+)*\]"


# ---------- 读入与切段 ----------
def norm(t):
    """去掉文档导出带的转义和粗体、斜体记号，让比对只看文字"""
    t = t.replace("&#91;", "[").replace("\\[", "[").replace("\\]", "]").replace("\\_", "_").replace("&#32;", " ")
    t = t.replace("**", "")
    t = re.sub(r"(?<![\\\w^])\*([^*\n]+?)\*(?![\w])", r"\1", t)
    return t.replace("\\*", "*")


def blocks(text):
    """按空行切段；代码块里的空行不切，标题单独成段"""
    out, cur, fence = [], [], False

    def flush():
        if cur:
            b = "\n".join(cur).strip("\n")
            out.append(b.strip() if b.lstrip().startswith("```") else norm(b).strip())
            cur.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("```"):
            if not fence:
                flush()
            cur.append(line)
            fence = not fence
            if not fence:
                flush()
            continue
        if fence:
            cur.append(line)
        elif not s:
            flush()
        elif HEAD.match(s):
            flush(); cur.append(s); flush()
        else:
            cur.append(line)
    flush()
    return [b for b in out if b]


def detect_lang(t):
    n = len(re.sub(r"\s", "", t)) or 1
    return "zh" if len(CJK.findall(t)) / n > 0.1 else "en"


def size(s, lang):
    s = re.sub(r"^#{1,6}\s+", "", s)
    return len(re.findall(r"[^\W_]+", s)) if lang == "en" else len(re.sub(r"\s", "", s))


def btype(s):
    if HEAD.match(s):
        return "head"
    if s.startswith("```"):
        return "fence"
    if s.startswith("|"):
        return "table"
    if re.match(r"^\s*([-*+]|\d+[.)])\s", s):
        return "list"
    return "para"


def is_fig(s):
    return s.startswith("[embedded content") or s.startswith("[image") or s.startswith("![")


def prep(s, t):
    """比对前的整理：代码块去掉围栏行，列表换成圆点"""
    if s is None:
        return None
    if t == "fence":
        s = re.sub(r"^```[^\n]*\n?", "", s)
        return re.sub(r"\n?```\s*$", "", s)
    if t == "list":
        return re.sub(r"(?m)^(\s*)[-*+]\s", r"\1• ", s)
    return s


# ---------- 段内比对 ----------
def toks(s, lang):
    if lang == "zh":
        return re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?|\s+|.", s, re.S)
    return re.findall(r"\s+|[A-Za-z0-9_]+|.", s, re.S)


def nchar(s):
    return len(re.sub(r"\s", "", s))


def segs(a, b, lang):
    """按字（中文）或按词（英文）比；夹在两处改动之间的极短相同片段并进改动，免得碎成一地"""
    ta, tb = toks(a, lang), toks(b, lang)
    out = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=ta, b=tb, autojunk=False).get_opcodes():
        x, y = "".join(ta[i1:i2]), "".join(tb[j1:j2])
        if tag == "equal":
            if out and out[-1][0] != "eq" and nchar(x) <= (1 if lang == "zh" else 0) and j2 < len(tb):
                out[-1][1] += x; out[-1][2] += x
                continue
            out.append(["eq", x, x])
        elif out and out[-1][0] != "eq":
            out[-1][1] += x; out[-1][2] += y
        else:
            out.append(["ch", x, y])
    return out


def render(sg):
    h, ni, nd, ins_t, del_t = [], 0, 0, [], []
    for k, x, y in sg:
        if k == "eq":
            h.append(esc(x))
            continue
        if x:
            h.append("<del>" + esc(x) + "</del>"); nd += 1; del_t.append(x)
        if y:
            h.append("<ins>" + esc(y) + "</ins>"); ni += 1; ins_t.append(y)
    return "".join(h), ni, nd, " ".join(ins_t), " ".join(del_t)


def table_cells(b):
    rows = [r for r in b.split("\n") if r.strip().startswith("|")]
    rows = [r for r in rows if not re.match(r"^\|?\s*:?-{3,}", r.strip())]
    return [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]


def table_html(rows, cls="dtable"):
    hh = []
    for r, cells in enumerate(rows):
        t = "th" if r == 0 else "td"
        hh.append("<tr>" + "".join(f"<{t}>{c}</{t}>" for c in cells) + "</tr>")
    return (f"<div class='tablewrap'><table class='{cls}'><thead>" + hh[0] + "</thead><tbody>" +
            "".join(hh[1:]) + "</tbody></table></div>")


def diff_block(a, b, t, lang):
    """返回 (正文 html, 插入处数, 删除处数, 新增文字, 删去文字)；a 或 b 为 None 表示整段新增或删去"""
    if a is None or b is None:
        s, mark = (b, "ins") if a is None else (a, "del")
        if t == "table":
            return (table_html([[f"<{mark}>{esc(c)}</{mark}>" if c else "" for c in r] for r in table_cells(s)]),
                    int(a is None), int(b is None), b or "", a or "")
        return f"<{mark}>{esc(s)}</{mark}>", int(a is None), int(b is None), b or "", a or ""
    if t == "table":
        ra, rb = table_cells(a), table_cells(b)
        if ra and len(ra) == len(rb) and all(len(x) == len(y) for x, y in zip(ra, rb)):
            rows, ni, nd, it, dt = [], 0, 0, [], []
            for xa, xb in zip(ra, rb):
                cells = []
                for ca, cb in zip(xa, xb):
                    h1, i1, d1, t1, u1 = render(segs(ca, cb, lang))
                    ni += i1; nd += d1; it.append(t1); dt.append(u1); cells.append(h1)
                rows.append(cells)
            return table_html(rows), ni, nd, " ".join(x for x in it if x), " ".join(x for x in dt if x)
    return render(segs(a, b, lang))


def wrap(t, body, same=False):
    cls = " same" if same else ""
    if t == "table" and body.startswith("<div class='tablewrap'>"):
        return body.replace("<div class='tablewrap'>", f"<div class='tablewrap{cls}'>", 1)
    if t in ("fence", "table"):
        return f"<p class='para small mono{cls}'>{body}</p>"
    return f"<p class='para{cls}'>" + body.replace("\n", "<br>") + "</p>"


def label_of(s, lang, counter):
    L, t = LAB[lang], btype(s)
    if t == "head":
        return L["head"]
    if t == "table":
        return L["table"]
    if t == "fence":
        m = re.search(r"\\tag\{([^}]*)\}", s)
        if m:
            return L["eqn"].format(t=m.group(1))
        return L["eq"] if re.match(r"```\s*(latex|math|tex|katex)\b", s) else L["code"]
    m = re.match(r"^\[embedded content: (图|Figure) ?(\d+)", s)
    if m:
        return ("图 " if m.group(1) == "图" else "Figure ") + m.group(2)
    m = re.match(r"^\[image: ((?:图|Figure) ?\d+)", s)
    if m:
        return m.group(1)
    m = re.match(r"^!\[([^\]\n]{0,24})", s)
    if m:
        return (L["fig"] + " " + m.group(1)).strip()
    m = re.match(r"^(图 ?\d+|表 ?\d+|Figure \d+|Table \d+)[：:.]", s)
    if m:
        return m.group(1) + L["cap"]
    m = re.match(r"^\[(\d+)\]\s", s)
    if m:
        return f"[{m.group(1)}]"
    m = re.match(r"^（(\d{1,2})）(.{2,24}?)[。：]", s)
    if m:
        return f"（{m.group(1)}）{m.group(2)}"
    m = re.match(r"^\(([ivx]+|\d{1,2})\) (.{2,60}?)[.:]", s)
    if m:
        return f"({m.group(1)}) {m.group(2)}"
    m = re.match(r"^([A-Z]\.\d+) (.{2,90}?)[。.]", s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    if t == "list":
        return L["list"]
    if counter is None:
        return L["deleted"]
    counter[0] += 1
    return L["para"].format(n=counter[0])


def lis_keep(seq):
    """最长递增子序列，返回保留的位置；不在其中的视为挪了位置"""
    tails, idx, prev = [], [], [-1] * len(seq)
    for k, v in enumerate(seq):
        p = bisect.bisect_left(tails, v)
        if p == len(tails):
            tails.append(v); idx.append(k)
        else:
            tails[p] = v; idx[p] = k
        prev[k] = idx[p - 1] if p > 0 else -1
    keep, k = set(), idx[-1] if idx else -1
    while k >= 0:
        keep.add(k); k = prev[k]
    return keep


# ---------- 批注 ----------
FIELDS = {"ins": "ins", "del": "dele", "text": "new", "old": "old", "label": "label", "doc": "doc"}


def rule_hit(r, ctx):
    tested = False
    for f, key in FIELDS.items():
        if f in r:
            vals = r[f] if isinstance(r[f], list) else [r[f]]
            if not any(v in ctx[key] for v in vals):
                return False
            tested = True
        if f + "_all" in r:
            if not all(v in ctx[key] for v in r[f + "_all"]):
                return False
            tested = True
        if f + "_re" in r:
            if not re.search(r[f + "_re"], ctx[key]):
                return False
            tested = True
        if "not_" + f in r:
            vals = r["not_" + f] if isinstance(r["not_" + f], list) else [r["not_" + f]]
            if any(v in ctx[key] for v in vals):
                return False
    if "kind" in r:
        kinds = r["kind"] if isinstance(r["kind"], list) else [r["kind"]]
        if ctx["kind"] not in kinds:
            return False
        tested = True
    return tested


class Doc:
    def __init__(self, spec, d, base):
        self.name = d.get("name") or os.path.basename(d["after"])
        rd = lambda p: open(p if os.path.isabs(p) else os.path.join(base, p), encoding="utf-8-sig").read()
        self.A, self.B = blocks(rd(d["before"])), blocks(rd(d["after"]))
        lang = d.get("lang", spec.get("lang", "auto"))
        self.lang = detect_lang("\n".join(self.B or self.A)) if lang == "auto" else lang
        self.src = (d["before"], d["after"])


def build_doc(doc, spec, ui):
    A, B, lang = doc.A, doc.B, doc.lang
    thr = float(spec.get("pair_threshold", 0.35))
    cite_re = re.compile(spec.get("cite_pattern", DEFAULT_CITE)) if spec.get("cite_pattern", DEFAULT_CITE) else None
    rules, moved_note, cite_note = spec.get("rules", []), spec.get("moved_note"), spec.get("cite_note")

    # 先按整段对齐，再把剩下的段落按相似度全局配对（能认出挪了位置的段落）
    pair, ua, ub = {}, [], []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=A, b=B, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                pair[j1 + k] = i1 + k
        else:
            ua += range(i1, i2); ub += range(j1, j2)
    # 相似度按词（英文）或按字（中文）算；英文按字符算会把两段无关的话也配成一对
    tk = lambda s: [x for x in toks(s, lang) if not x.isspace()]
    ta = {i: tk(A[i]) for i in ua}
    cand, sm = [], SequenceMatcher(autojunk=False)
    for j in ub:
        sm.set_seq2(tk(B[j])); hj = btype(B[j]) == "head"
        for i in ua:
            if (btype(A[i]) == "head") != hj:
                continue
            sm.set_seq1(ta[i])
            if sm.real_quick_ratio() >= thr and sm.quick_ratio() >= thr:
                r = sm.ratio()
                if r >= thr:
                    cand.append((r, i, j))
    cand.sort(key=lambda x: (-x[0], x[1], x[2]))
    used_a = set(pair.values())
    for r, i, j in cand:
        if i in used_a or j in pair:
            continue
        used_a.add(i); pair[j] = i
    order = sorted(pair)
    keep = lis_keep([pair[j] for j in order])
    moved = {order[k] for k in range(len(order)) if k not in keep}

    # 删去的段落放在它原来前一段（没挪过位置的那段）后面
    b_of_a = {pair[j]: j for j in pair if j not in moved}
    dels = {}
    for i in range(len(A)):
        if i in used_a:
            continue
        anchor = next((b_of_a[k] for k in range(i - 1, -1, -1) if k in b_of_a), -1)
        dels.setdefault(anchor, []).append(i)

    hl = lambda s: len(HEAD.match(s).group(1)) if HEAD.match(s) else 0
    levels = [hl(x) for x in B + A if hl(x)]
    top = min(levels) if levels else 2
    chap_lv = top if sum(1 for x in B if hl(x) == top) >= 2 else top + 1
    band = lambda h: 1 if h < chap_lv else (2 if h == chap_lv else 3)

    T = UI[ui]
    items, counter, chap = [], [0], T["front"]
    st = {"ins": 0, "del": 0, "nins": 0, "ndel": 0, "rows": 0, "chap": {}}

    def note_ids(kind, a, b, it, dt, lab, is_moved):
        if kind == "cite":
            return [cite_note] if cite_note else []
        ctx = {"ins": it, "dele": dt, "new": b or "", "old": a or "", "label": lab, "doc": doc.name, "kind": kind}
        ids = [r["note"] for r in rules if rule_hit(r, ctx)]
        if is_moved and moved_note:
            ids.append(moved_note)
        # 段内还动了引用编号时补上顺移批注；已有批注本身就交代了引用变化的（如新增文献）不补
        unless = {str(x) for x in spec.get("cite_note_unless", [])}
        if (cite_note and cite_re and a and b and cite_re.findall(a) != cite_re.findall(b)
                and not unless.intersection(ids)):
            ids.append(cite_note)
        return [str(x) for x in dict.fromkeys(ids)]

    def count(it, dt, ni, nd, rows=1):
        ci, cd = size(it, lang), size(dt, lang)
        st["ins"] += ci; st["del"] += cd; st["nins"] += ni; st["ndel"] += nd; st["rows"] += rows
        c = st["chap"].setdefault(chap, [0, 0, 0]); c[0] += ci; c[1] += cd; c[2] += rows

    def emit_deleted(i):
        a = A[i]; t = btype(a)
        lab = label_of(a, ui if ui in LAB else lang, None)   # 通用标签（第几段、表、公式）跟界面语言走，不跟正文语言
        body, ni, nd, it, dt = diff_block(prep(HEAD.sub(r"\2", a) if t == "head" else a, t), None, t, lang)
        count(it, dt, ni, nd)
        items.append({"t": "row", "kind": "del", "label": lab, "body": wrap(t, body),
                      "tag": f"<span class='tag tag-del'>{T['tag_del']}</span>",
                      "ids": note_ids("del", a, None, it, dt, lab, False), "ins": it, "del": dt})

    for i in dels.get(-1, []):
        emit_deleted(i)
    for j, b in enumerate(B):
        a = A[pair[j]] if j in pair else None
        t = btype(b)
        if t == "head":
            h, tb = hl(b), HEAD.match(b).group(2)
            if h == chap_lv:
                chap = tb
            counter[0] = 0
            it_ = {"t": "sect", "lvl": band(h), "html": esc(tb), "text": tb, "chg": None}
            if a != b or j in moved:
                ta = HEAD.match(a).group(2) if a else None
                body, ni, nd, it, dt = diff_block(ta, tb, "para", lang)
                kind = "new" if a is None else ("move" if a == b else "edit")
                count(it, dt, ni, nd)
                it_["html"] = body
                it_["chg"] = {"kind": kind, "label": LAB[ui if ui in LAB else lang]["head"],
                              "ids": note_ids(kind, a, b, it, dt, LAB[ui if ui in LAB else lang]["head"], j in moved), "ins": it, "del": dt}
            items.append(it_)
        else:
            lab = label_of(b, ui if ui in LAB else lang, counter)
            if a == b and j not in moved:
                body = (f"<p class='para small fixed'>{esc(T['fig_same'].format(lab=lab))}</p>" if is_fig(b)
                        else wrap(t, table_html([[esc(c) for c in r] for r in table_cells(b)]) if t == "table"
                                  else esc(prep(b, t)), same=True))
                items.append({"t": "row", "kind": "same", "label": lab, "body": body, "ids": [],
                              "tag": f"<span class='tag'>{T['tag_same']}</span>"})
            elif a == b:
                body = (f"<p class='para small fixed'>{esc(T['fig_move'].format(lab=lab))}</p>" if is_fig(b)
                        else wrap(t, table_html([[esc(c) for c in r] for r in table_cells(b)]) if t == "table"
                                  else esc(prep(b, t))))
                count("", "", 0, 0)
                items.append({"t": "row", "kind": "move", "label": lab, "body": body, "ins": "", "del": "",
                              "tag": f"<span class='tag tag-move'>{T['tag_move']}</span>",
                              "ids": note_ids("move", a, b, "", "", lab, True)})
            else:
                body, ni, nd, it, dt = diff_block(prep(a, t), prep(b, t), t, lang)
                count(it, dt, ni, nd)
                cite_only = a is not None and cite_re is not None and cite_re.sub("[]", a) == cite_re.sub("[]", b)
                kind = "cite" if cite_only else ("new" if a is None else "edit")
                tag = (f"<span class='tag tag-edit'>{T['tag_new']}</span>" if a is None else
                       f"<span class='tag tag-edit'>{T['tag_edit'].format(i=ni, d=nd)}</span>")
                if j in moved:
                    tag += f"<span class='tag tag-move'>{T['tag_move']}</span>"
                items.append({"t": "row", "kind": kind, "label": lab, "body": wrap(t, body), "tag": tag,
                              "ids": note_ids(kind, a, b, it, dt, lab, j in moved), "ins": it, "del": dt})
        for i in dels.get(j, []):
            emit_deleted(i)

    # 按章字数：原稿的章按配对映射到现稿的章名
    a_to_b = {pair[j]: j for j in pair}

    def sizes(X, name_of):
        s, cur = {}, T["front"]
        for k, x in enumerate(X):
            if hl(x) == chap_lv:
                cur = name_of(k, x)
            s[cur] = s.get(cur, 0) + size(x, lang)
        return s
    sb = sizes(B, lambda k, x: HEAD.match(x).group(2))
    sa = sizes(A, lambda k, x: HEAD.match(B[a_to_b[k]]).group(2) if k in a_to_b and btype(B[a_to_b[k]]) == "head"
               else HEAD.match(x).group(2))
    chap_order = list(dict.fromkeys(list(sb) + list(sa)))
    npara = (sum(1 for x in A if btype(x) != "head"), sum(1 for x in B if btype(x) != "head"))
    return items, st, {"sa": sa, "sb": sb, "order": chap_order, "npara": npara}


# ---------- 拼页面 ----------
def note_li(n, notes):
    t, p = notes.get(n, (n, ""))
    return (f"<li><span class='num'>{esc(n)}</span><div><b>{esc(t)}</b>" + (f"<p>{esc(p)}</p>" if p else "") +
            "</div></li>")


def assemble(items, start, notes, T, multi, docname):
    """改动段落编号并写入目录；未改动和只改了引用编号的段落折叠"""
    out, nav, run, no = [], [], [], start
    if multi:
        out.append(f"<div class='sect lv1 docname'>{esc(docname)}</div>")
        nav.append(("doc", docname))

    def flush():
        if not run:
            return
        n_same = sum(1 for r in run if r["kind"] == "same")
        n_cite = len(run) - n_same
        parts = ([T["fold_same"].format(n=n_same)] if n_same else []) + ([T["fold_cite"].format(n=n_cite)] if n_cite else [])
        rows = "".join(row_html(r, None, notes) for r in run)
        out.append(f"<details class='fold'><summary><span>{T['fold_sep'].join(parts)}</span>"
                   f"<span class='fold-hint'>{T['fold_open']}</span></summary>{rows}</details>")
        run.clear()

    for it in items:
        if it["t"] == "sect":
            flush()
            lvl, c = it["lvl"], it["chg"]
            if c:
                no += 1
                rid = f"c{no}"
                nums = " ".join(x.replace(" ", "_") for x in c["ids"])
                out.append(f"<div class='sect lv{lvl} chg' id='{rid}' data-no='{no}' data-kind='{c['kind']}' "
                           f"data-notes='{esc(nums)}'>{it['html']}" +
                           (f"<ol class='notes inline'>{''.join(note_li(n, notes) for n in c['ids'])}</ol>" if c["ids"] else "") +
                           "</div>")
            else:
                out.append(f"<div class='sect lv{lvl}'>{it['html']}</div>")
            if lvl >= 2:
                nav.append(("h2" if lvl == 2 else "h3", it["text"]))
            if c:
                nav.append(("row", rid, no, c["label"], c["kind"], c["ids"]))
            continue
        if it["kind"] in ("same", "cite"):
            run.append(it)
            continue
        flush()
        no += 1
        out.append(row_html(it, (f"c{no}", no), notes))
        nav.append(("row", f"c{no}", no, it["label"], it["kind"], it["ids"]))
    flush()
    rank = {"doc": 0, "h2": 1, "h3": 2}
    clean = []
    for k, x in enumerate(nav):
        if x[0] in rank:
            has = False
            for y in nav[k + 1:]:
                if y[0] == "row":
                    has = True; break
                if rank[y[0]] <= rank[x[0]]:
                    break
            if not has:
                continue
        clean.append(x)
    return "\n".join(out), clean, no


def row_html(r, idno, notes):
    nums = " ".join(x.replace(" ", "_") for x in r["ids"])
    attrs = f" id='{idno[0]}' data-no='{idno[1]}' data-kind='{r['kind']}' data-notes='{esc(nums)}'" if idno else ""
    return (f"<section class='row{' chg' if idno else ''}'{attrs}><div class='text'><div class='rowhead'>"
            f"<span class='label'>{esc(r['label'])}</span>{r['tag']}</div>{r['body']}</div>"
            f"<ol class='notes'>{''.join(note_li(n, notes) for n in r['ids'])}</ol></section>")


def nav_html(nav):
    out = []
    for x in nav:
        if x[0] == "doc":
            out.append(f"<div class='nvd'>{esc(x[1])}</div>")
        elif x[0] == "h2":
            out.append(f"<div class='nv2'>{esc(x[1])}</div>")
        elif x[0] == "h3":
            out.append(f"<div class='nv3'>{esc(x[1])}</div>")
        else:
            _, rid, no, lab, kind, ids = x
            chips = "".join(f"<i>{esc(n)}</i>" for n in ids)
            out.append(f"<a class='nvi k-{kind}' href='#{rid}' data-target='{rid}'><span class='nvn'>{no}</span>"
                       f"<span class='nvl'>{esc(lab)}</span><span class='nvt'>{chips}</span></a>")
    return "\n".join(out)


def summary_html(res, T, multi):
    fmt = lambda v: f"{v:,}"
    parts, lines = [f"<h2>{T['sum_h']}</h2>"], []
    if multi:
        rows = []
        for doc, items, st, sz in res:
            u = T["unit"][doc.lang]
            a_, b_ = sum(sz["sa"].values()), sum(sz["sb"].values())
            rows.append(f"<tr><td>{esc(doc.name)}</td><td>{fmt(a_)} <small>{u}</small></td><td>{fmt(b_)}</td>"
                        f"<td>{fmt(st['ins'])}</td><td>{fmt(st['del'])}</td><td>{st['rows']}</td><td>{sz['npara'][0]} → {sz['npara'][1]}</td></tr>")
        parts.append(f"<div class='tablewrap'><table class='chk chap'><thead><tr><th>{T['col_doc']}</th><th>{T['col_a']}</th>"
                     f"<th>{T['col_b']}</th><th>{T['col_ins']}</th><th>{T['col_del']}</th><th>{T['col_rows']}</th><th>{T['col_paras']}</th></tr></thead>"
                     f"<tbody>{''.join(rows)}</tbody></table></div>")
    for doc, items, st, sz in res:
        u = T["unit"][doc.lang]
        real = [c for c in sz["order"] if c != T["front"] or sz["sa"].get(c) or sz["sb"].get(c)]
        if len(real) >= 2:
            rows, tot = [], [0, 0, 0, 0, 0]
            for c in real:
                g = st["chap"].get(c, [0, 0, 0])
                a_, b_ = sz["sa"].get(c, 0), sz["sb"].get(c, 0)
                rows.append(f"<tr><td>{esc(c)}</td><td>{fmt(a_)}</td><td>{fmt(b_)}</td><td>{fmt(g[0])}</td>"
                            f"<td>{fmt(g[1])}</td><td>{g[2]}</td></tr>")
                tot = [tot[0] + a_, tot[1] + b_, tot[2] + g[0], tot[3] + g[1], tot[4] + g[2]]
            rows.append(f"<tr class='tot'><td>{T['total']}</td><td>{fmt(tot[0])}</td><td>{fmt(tot[1])}</td>"
                        f"<td>{fmt(tot[2])}</td><td>{fmt(tot[3])}</td><td>{tot[4]}</td></tr>")
            cap = (esc(doc.name) + " · ") if multi else ""
            parts.append(f"<div class='tablewrap'><table class='chk chap'><thead><tr><th>{cap}{T['col_chap']}</th>"
                         f"<th>{T['col_a']}（{u}）</th><th>{T['col_b']}</th><th>{T['col_ins']}</th><th>{T['col_del']}</th>"
                         f"<th>{T['col_rows']}</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>")
        refs = [c for c in sz["order"] if REFS.match(c)]
        base = sum(v for c, v in sz["sa"].items() if c not in refs) or 1
        ins_ = st["ins"] - sum(st["chap"].get(c, [0])[0] for c in refs)
        del_ = st["del"] - sum(st["chap"].get(c, [0, 0])[1] for c in refs)
        lines.append(T["ratio"].format(name=(doc.name + "：" if T is UI["zh"] else doc.name + ": ") if multi else "",
                                       i=fmt(ins_), d=fmt(del_), u=u, p=f"{100 * (ins_ + del_) / base:.1f}",
                                       x=T["ratio_x"] if refs else ""))
    parts.append("<p>" + "".join(lines) + T["count_note"] + "</p>")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="把同一文稿的两个版本比成一页修订对比 HTML")
    ap.add_argument("inputs", nargs="+", help="spec.json，或者 before after 两个文件")
    ap.add_argument("-o", "--out")
    ap.add_argument("--title")
    ap.add_argument("--lang", default="auto", choices=["auto", "zh", "en"])
    ap.add_argument("--ui", choices=["zh", "en"])
    ap.add_argument("--dump", help="把每处改动另存成 JSON，写批注规则时对照")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(args.inputs) == 1 and args.inputs[0].lower().endswith(".json"):
        sp = os.path.abspath(args.inputs[0])
        spec, base = json.load(open(sp, encoding="utf-8-sig")), os.path.dirname(sp)
    elif len(args.inputs) == 2:
        spec, base = {"docs": [{"before": os.path.abspath(args.inputs[0]), "after": os.path.abspath(args.inputs[1])}],
                      "lang": args.lang}, os.getcwd()
    else:
        ap.error("给一个 spec.json，或者 before、after 两个文件")
    if args.title:
        spec["title"] = args.title
    if args.ui:
        spec["ui"] = args.ui
    out = args.out or spec.get("out") or "diff.html"
    out = out if os.path.isabs(out) else os.path.join(base, out)
    ui = spec.get("ui", "zh")
    T = UI[ui]
    notes = {}
    for k, v in (spec.get("notes") or {}).items():
        notes[str(k)] = (v, "") if isinstance(v, str) else ((v.get("title", ""), v.get("text", "")) if isinstance(v, dict) else (v[0], v[1] if len(v) > 1 else ""))
    for r in spec.get("rules", []):
        r["note"] = str(r["note"])
        if r["note"] not in notes:
            print(f"警告：规则指向不存在的批注 {r['note']}")
    for k in ("moved_note", "cite_note"):
        if spec.get(k) is not None:
            spec[k] = str(spec[k])

    prog = None
    try:
        sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".claude", "progress"))
        from claude_progress import Progress
        prog = Progress("html_diff", total=len(spec["docs"]))
    except Exception:
        pass
    docs, res = [Doc(spec, d, base) for d in spec["docs"]], []
    for doc in docs:
        items, st, sz = build_doc(doc, spec, ui)
        res.append((doc, items, st, sz))
        if prog:
            prog.tick()
    if prog:
        prog.done()

    multi = len(docs) > 1
    mains, navs, no = [], [], 0
    for doc, items, st, sz in res:
        h, nv, no = assemble(items, no, notes, T, multi, doc.name)
        mains.append(f"<main class='doc' aria-label='{esc(doc.name)}'>\n{h}\n</main>")
        navs.append(nav_html(nv))
    total = no

    # 只列页面上实际用到的批注条目，顺序按 spec 里写的
    used = []
    for doc, items, st, sz in res:
        for it in items:
            ids = it["chg"]["ids"] if it["t"] == "sect" and it["chg"] else (it["ids"] if it["t"] == "row" and it["kind"] not in ("same", "cite") else [])
            used += ids
    used = set(used)
    opts = "".join(f"<option value='{esc(k.replace(' ', '_'))}'>{esc(k)}　{esc(notes[k][0])}</option>"
                   for k in notes if k in used)
    nfold = sum(1 for m in mains if "<details class='fold'>" in m)

    title = spec.get("title") or ("修订对比" if ui == "zh" else "Revision comparison")
    srcs = lambda k: "、".join(os.path.basename(d.src[k]) for d in docs)
    intro = spec.get("intro") or esc(T["intro"].format(a=srcs(0), b=srcs(1)))
    nins = sum(st["nins"] for _, _, st, _ in res)
    ndel = sum(st["ndel"] for _, _, st, _ in res)
    stats = (f"<div class='stats'><span><b>{total}</b> {T['st_rows']}</span><span><b>{nins}</b> {T['st_ins']}</span>"
             f"<span><b>{ndel}</b> {T['st_del']}</span>" +
             (f"<span><b>{len([k for k in notes if k in used])}</b> {T['st_notes']}</span>" if used else "") + "</div>")
    filt = (f"<label class='filter'>{T['filter']} <select id='flt' autocomplete='off'><option value=''>{T['filter_all']}</option>{opts}</select></label>"
            if opts else "")
    unfold = f"<button type='button' class='tool' id='unfold'>{T['unfold']}</button>" if nfold else ""
    toc = "\n".join(navs) if total else f"<p class='empty'>{T['nochange']}</p>"
    closing = spec.get("closing_html")
    css = open(os.path.join(ASSETS, "review.css"), encoding="utf-8").read()
    js = open(os.path.join(ASSETS, "nav.js"), encoding="utf-8").read()
    key = "htmldiff-view-" + re.sub(r"\W", "", title)[:24]
    cfg = {"key": key, "unfold": T["unfold"], "fold": T["fold"], "rail": T["rail"]}

    head = f"""<title>{esc(title)}</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600&family=Noto+Sans+SC:wght@400;500;700&display=swap">
<style>
{css}
</style>"""
    body = f"""<div class="wrap">
  <header>
    <h1>{esc(title)}</h1>
    <p class="meta">{intro}</p>
  </header>

  <section class="summary">
{summary_html(res, T, multi)}
{spec.get("summary_html", "")}
  </section>

  <div class="bar" role="toolbar" aria-label="{T['toolbar']}">
    <button type="button" class="tool sidebtn" id="sidebtn" aria-expanded="false" aria-controls="side">{T['toc_btn']}</button>
    <div class="navctl">
      <button type="button" id="prev" aria-label="{T['prev_aria']}">{T['prev']}</button>
      <span class="pos" id="pos" aria-live="polite"><b>–</b> / {total}</span>
      <button type="button" id="next" aria-label="{T['next_aria']}">{T['next']}</button>
    </div>
    {filt}
    {unfold}
    <span class="kbd"><kbd>J</kbd> <kbd>K</kbd> {T['keys']}</span>
    {stats}
    <div class="legend"><span><ins>{T['lg_ins']}</ins></span><span><del>{T['lg_del']}</del></span></div>
    <div class="views">
      <button type="button" data-v="mark" aria-pressed="true">{T['v_mark']}</button>
      <button type="button" data-v="new" aria-pressed="false">{T['v_new']}</button>
      <button type="button" data-v="old" aria-pressed="false">{T['v_old']}</button>
    </div>
  </div>

  <div class="layout">
    <nav class="side" id="side" aria-label="{T['toc']}">
      <h2>{T['toc']} <span class="kbd">{T['toc_total'].format(n=total)}</span></h2>
{toc}
    </nav>
    <div class="docs">
{chr(10).join(mains)}
    </div>
  </div>
{f'''
  <section class="rest">
{closing}
  </section>''' if closing else ""}
</div>
<div class="rail" id="rail" aria-hidden="true"></div>

<script>
var HD = {json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")};
{js}
</script>
"""
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w", encoding="utf-8").write(head + "\n\n" + body)
    sa_out = re.sub(r"(\.html?)?$", ".standalone.html", out, count=1)
    lang_attr = "zh-CN" if ui == "zh" else "en"
    open(sa_out, "w", encoding="utf-8").write(
        f"<!doctype html>\n<html lang=\"{lang_attr}\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n{head}\n</head>\n<body>\n{body}</body>\n</html>\n")

    # 报告：每份文档的改动量；配了批注时列出没有批注的改动
    changes = []
    no = 0
    for doc, items, st, sz in res:
        print(f"[{doc.name}] lang={doc.lang} 改动 {st['rows']} 处，段落 {sz['npara'][0]} → {sz['npara'][1]}，增 {st['ins']} 删 {st['del']} {T['unit'][doc.lang]}")
        for it in items:
            c = it["chg"] if it["t"] == "sect" else (it if it["kind"] not in ("same", "cite") else None)
            if not c:
                continue
            no += 1
            changes.append({"no": no, "doc": doc.name, "label": c["label"], "kind": c["kind"], "notes": c["ids"],
                            "ins": c.get("ins", ""), "del": c.get("del", "")})
    if notes:
        miss = [c for c in changes if not c["notes"]]
        for c in miss:
            print(f"  没有批注 #{c['no']} {c['doc']} {c['label']} [{c['kind']}] +{c['ins'][:50]!r} -{c['del'][:50]!r}")
        print(f"没有批注的改动 {len(miss)} 处")
    if args.dump:
        dp = args.dump if os.path.isabs(args.dump) else os.path.join(os.getcwd(), args.dump)
        json.dump(changes, open(dp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("改动清单", dp)
    print(f"共 {total} 处改动")
    print("页面", out)
    print("单文件", sa_out)


if __name__ == "__main__":
    main()
