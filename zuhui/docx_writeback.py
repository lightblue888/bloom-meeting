# -*- coding: utf-8 -*-
"""会上改完的 Markdown 写回 Word：在原 .docx 旁边另存一份「<原名>（会上修订）.docx」，原文件不动。

拖进来的 .docx 由 papers.py / pdfconv.py 转成 <名字>-bloom/<名字>.md，会上改的都是这份 Markdown。散会时（或点"写回 Word"）
拿三样东西算出要改哪里：
    原 .docx                    会上没动过的 Word
    base                        转换刚完成时的 Markdown（.bloom-convert.json 里 md_sha1 指纹对得上的那一份）
    cur                         会上改完的 Markdown
做法：
  1. Word 里每一段（正文、表格格子里的、套着的表里的）抽出文字，记下每个字在哪个 run 的哪个位置；
     base 的每一行去掉 Markdown 标记（# ** | 转义 链接 HTML 标签……）成纯文字。两边去掉空白后按字对齐（difflib），
     于是 base 里的每个字知道自己是 Word 里哪一段的第几个字。
  2. base → cur 先按行比（段落），成对的行再按字（中文）/ 按词（英文）比，得到一处处"把 base 第 i..j 个字换成 xx"。
  3. 每一处落到 Word：要删的字在 Word 里必须一个不落地对上、连在一起、在同一段里，才动手；插入的字用旁边 run 的格式
     （改的是哪个 run 就抄哪个 run 的 rPr）。新加的行 → 照着附近同类段落（标题照标题、正文照正文）的段落格式新起一段；
     删掉的行 → 整段删（格子里最后一段、分节符所在段只清字不删段）。表格、图片、页眉页脚、封面、分节设置都不碰。
  4. 对不上的不猜：跳过，连原因一起列出来（skipped）。
输出：document.xml 改完，其余部件原样拷贝。目标文件被 Word 打开着（写不进去）、或者是用户自己改过的同名文件，
就另起一个名字「<原名>（会上修订 2）.docx」……，不覆盖。

两种写法（设置 → 通用 → 论文，zuhui/paper_prefs.py）：
    track（默认）  Word 修订：删的字包进 w:del（w:t 换成 w:delText，格式照原 run），加的字包进 w:ins（格式抄旁边的 run），
                   新加的段落、删掉的段落连段落标记一起记成修订。作者写 AI 的名字，带日期。在 Word 里能逐条接受、拒绝：
                   全部接受 = 会上改完的样子，全部拒绝 = 原来的 Word。
    direct         直接改字，不留修订痕迹（以前的做法）。
"""
from __future__ import annotations

import copy
import difflib
import glob
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

from lxml import etree

from .pdfconv import FILL

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
CONV_META = ".bloom-convert.json"          # papers.py 写的转换记录（source、md_sha1 指纹）
WB_STATE = ".bloom-writeback.json"         # 这里记：上次写出去的文件和它的指纹（下次只覆盖自己写的、没被人改过的）
SUFFIX = "（会上修订）"


def q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


class Skip(Exception):
    """这一处在 Word 里定不准：跳过，原因写在 args[0]。"""


class _Track:
    """修订模式：作者、日期、下一个修订编号（从文档里已有的 w:id 往后排，不和书签、批注、旧修订撞号）；数着加了几处、删了几处。"""

    def __init__(self, author: str, date: str, start_id: int):
        self.author, self.date, self._id = (author or "AI")[:60], date, start_id
        self.ins = self.dele = 0

    def mark(self, kind: str):
        """一个 w:ins / w:del 元素（还没挂东西）。"""
        self._id += 1
        el = etree.Element(q(kind))
        el.set(q("id"), str(self._id))
        el.set(q("author"), self.author)
        el.set(q("date"), self.date)
        if kind == "ins":
            self.ins += 1
        else:
            self.dele += 1
        return el

    def wrap(self, run, kind: str):
        w = self.mark(kind)
        w.append(run)
        return w


def _max_id(z: zipfile.ZipFile) -> int:
    """包里所有 XML 部件上出现过的最大 w:id（修订、书签、批注共用编号的地方都算进去）。"""
    top = 0
    pat = re.compile(rb'w:id="(-?\d+)"')
    for n in z.namelist():
        if not n.endswith(".xml"):
            continue
        try:
            for m in pat.finditer(z.read(n)):
                top = max(top, int(m.group(1)))
        except (KeyError, ValueError):
            continue
    return top


def _word_date() -> str:
    """Word 自己写的修订日期就是本机时间加个 Z（不换算成 UTC）：照它写，Word 里显示的时间才对。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:00Z")


def _to_deleted(run) -> None:
    """一个 run 里的字换成"删掉的字"：w:t → w:delText，w:instrText → w:delInstrText（格式 rPr 不动）。"""
    for ch in run:
        if ch.tag == q("t"):
            ch.tag = q("delText")
            ch.set(XML_SPACE, "preserve")
        elif ch.tag == q("instrText"):
            ch.tag = q("delInstrText")


def _mark_para(p, kind: str, track: "_Track") -> None:
    """段落标记记成插入 / 删除（pPr/rPr 里最前面放 w:ins / w:del）。"""
    ppr = p.find(q("pPr"))
    if ppr is None:
        ppr = etree.Element(q("pPr"))
        p.insert(0, ppr)
    rpr = ppr.find(q("rPr"))
    if rpr is None:
        rpr = etree.Element(q("rPr"))
        tail = [x for x in ppr if x.tag in (q("sectPr"), q("pPrChange"))]
        (tail[0].addprevious(rpr) if tail else ppr.append(rpr))
    for x in list(rpr):
        if x.tag in (q("ins"), q("del")):
            rpr.remove(x)
    rpr.insert(0, track.mark(kind))


# ======================================================================
# Markdown 一行 → 纯文字
# ======================================================================
_TABLE_SEP = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST = re.compile(r"^(?:[-*+]|\d{1,3}[.)])\s+")


def line_kind(raw: str) -> tuple[str, int]:
    """(种类, 标题级别)：heading / table / sep / image / list / body。"""
    s = raw.strip().lstrip("\u2060")
    if _TABLE_SEP.match(s) and "-" in s:
        return "sep", 0
    if s.startswith("|"):
        return "table", 0
    m = _HEAD.match(s)
    if m:
        return "heading", len(m.group(1))
    if re.fullmatch(r"\[image:[^\]]*\]", s) or re.fullmatch(r"(?:!\[[^\]]*\]\([^)]*\)\s*)+", s):
        return "image", 0
    if _LIST.match(s):
        return "list", 0
    return "body", 0


def _split_row(s: str) -> list[str]:
    s = s.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, in_math, i = [], [], False, 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2])
            i += 2
            continue
        if c == "$":
            in_math = not in_math
        if c == "|" and not in_math:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    cells.append("".join(cur))
    return [c.strip() for c in cells]


def _inline_plain(s: str) -> str:
    s = re.sub(r"<!--.*?-->", "", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)                     # 图
    s = re.sub(r"\[image:[^\]]*\]", "", s)
    s = re.sub(r"(?<!\\)\[([^\]\n]*)\]\([^)\s]*(?:\s+\"[^\"]*\")?\)", r"\1", s)   # 链接只留字
    s = re.sub(r"<((?:https?|mailto|ftp):[^>\s]+)>", r"\1", s)      # 自动链接
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>", "", s)   # HTML 标签
    s = re.sub(r"(?<!\\)(?:\*+|~~|`+)", "", s)                      # 粗体 / 斜体 / 删除线 / 行内代码
    s = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", s)                    # Markdown 转义
    s = html.unescape(s)
    return s.replace("\u2060", "").replace("\u200b", "").replace("\ufeff", "")


def md_plain(raw: str) -> str:
    """一行 Markdown 的纯文字；表格行的格子之间用 \\x1f 隔开（插入、删除都不跨格子）。"""
    kind, _ = line_kind(raw)
    s = raw.strip().lstrip("\u2060")
    if kind == "sep":
        return ""
    if kind == "table":
        return "\x1f".join(_inline_plain(c) for c in _split_row(s))
    if kind == "heading":
        s = _HEAD.match(s).group(2)
    return _inline_plain(s)


def md_lines(text: str) -> list[str]:
    """非空行（CRLF / LF 都行）。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    return [ln.rstrip() for ln in text.split("\n") if ln.strip()]


def _norm(c: str) -> str:
    n = unicodedata.normalize("NFKC", c)
    return n if len(n) == 1 else c


def _ws(c: str) -> bool:
    # FILL（"＿"）：转换器把封面填空线（Word 里是带下划线的空格 / 制表符）画成的一串"＿"（pdfconv._docx_fill_lines）。
    # Word 里那儿是空白，对齐时和空白一样不算字；不然"指导教师：＿＿＿＿＿＿＿＿"这一行对不齐，会上把空填上写不回去（2026-11-15 联调）
    return c.isspace() or c == "\x1f" or c == FILL


def _gap(c: str) -> bool:
    """Word 段里能被填空覆盖的空白：空格、制表符，和字面上的"＿"。"""
    return c.isspace() or c == FILL


# ======================================================================
# Word 段落模型
# ======================================================================
_HINT = {q("lastRenderedPageBreak"), q("proofErr"), q("noProof")}


class _Para:
    __slots__ = ("el", "runs", "pieces", "atom_pi", "idx")

    def __init__(self, el, idx):
        self.el, self.idx = el, idx
        self.runs: list = []
        self.pieces: list[dict] = []     # {"run","kind": c/tab/br/op/hint,"ch","el"}
        self.atom_pi: list[int] = []     # 第 k 个字 → pieces 里的下标

    def atom(self, a: int) -> dict:
        return self.pieces[self.atom_pi[a]]

    def text(self) -> str:
        return "".join(self.atom(a)["ch"] for a in range(len(self.atom_pi)))


def _runs_of(p) -> list:
    out = []

    def walk(el):
        for ch in el:
            tag = ch.tag
            if not isinstance(tag, str):
                continue
            if tag == q("r"):
                out.append(ch)
            elif tag in (q("del"), q("moveFrom"), q("p"), q("tbl"), q("pPr"), q("txbxContent")) \
                    or tag.startswith(f"{{{M_NS}}}") or tag.startswith(f"{{{MC_NS}}}"):
                continue
            else:
                walk(ch)              # hyperlink、ins、smartTag、sdt/sdtContent、fldSimple、customXml……
    walk(p)
    return out


def _build_para(p, idx: int) -> _Para:
    para = _Para(p, idx)
    para.runs = _runs_of(p)
    for r in para.runs:
        for ch in r:
            tag = ch.tag
            if tag == q("rPr"):
                continue
            if not isinstance(tag, str):
                para.pieces.append({"run": r, "kind": "hint", "ch": "", "el": ch})
            elif tag == q("t"):
                for c in ch.text or "":
                    para.atom_pi.append(len(para.pieces))
                    para.pieces.append({"run": r, "kind": "c", "ch": c, "el": None})
            elif tag in (q("tab"), q("ptab")):
                para.atom_pi.append(len(para.pieces))
                para.pieces.append({"run": r, "kind": "tab", "ch": "\t", "el": ch})
            elif tag in (q("br"), q("cr")) and ch.get(q("type")) in (None, "textWrapping"):
                para.atom_pi.append(len(para.pieces))
                para.pieces.append({"run": r, "kind": "br", "ch": "\n", "el": ch})
            elif tag in _HINT:
                para.pieces.append({"run": r, "kind": "hint", "ch": "", "el": ch})
            else:                     # 图、域、脚注号、分页符、符号……：不能删，也不能被一处修改夹在中间
                para.pieces.append({"run": r, "kind": "op", "ch": "", "el": ch})
    return para


def _body_paras(body) -> list[_Para]:
    out = []
    for p in body.iter(q("p")):
        if any(a.tag in (q("txbxContent"), f"{{{MC_NS}}}Fallback") for a in p.iterancestors()):
            continue                  # 文本框、兼容备份里的段落：不在正文流里
        out.append(_build_para(p, len(out)))
    return out


# ======================================================================
# 对齐：base 的每个字 ↔ Word 里的每个字
# ======================================================================
class _Model:
    def __init__(self, root, base_lines: list[str]):
        self.root = root
        self.body = root.find(q("body"))
        self.paras = _body_paras(self.body)
        self.lines = base_lines
        self.plain = [md_plain(x) for x in base_lines]
        self.kinds = [line_kind(x) for x in base_lines]
        D, M = [], []
        for pi, para in enumerate(self.paras):
            for a, ai in enumerate(para.atom_pi):
                c = para.pieces[ai]["ch"]
                if not _ws(c):
                    D.append((pi, a, _norm(c)))
        for li, P in enumerate(self.plain):
            for ci, c in enumerate(P):
                if not _ws(c):
                    M.append((li, ci, _norm(c)))
        self.D = D
        self.lmap: list[dict] = [dict() for _ in base_lines]
        sm = difflib.SequenceMatcher(None, [m[2] for m in M], [d[2] for d in D], autojunk=False)
        for a, b, n in sm.get_matching_blocks():
            for k in range(n):
                li, ci, _ = M[a + k]
                self.lmap[li][ci] = b + k
        self.para_of_el = {id(p.el): p for p in self.paras}
        # 每一行对得好不好：大部分字对上、对上的字在 Word 里挨得紧
        self.solid = []
        for li, P in enumerate(self.plain):
            n = sum(1 for c in P if not _ws(c))
            got = sorted(self.lmap[li].values())
            ok = n > 0 and len(got) >= max(1, 0.6 * n) and (got[-1] - got[0] + 1) <= 1.5 * len(got) + 2
            self.solid.append(ok)

    # ---- 一行在 Word 里的位置 ----
    def line_paras(self, li: int) -> list[_Para]:
        got = sorted(self.lmap[li].values())
        if not got:
            return []
        out = []
        for d in got:
            p = self.paras[self.D[d][0]]
            if not out or out[-1] is not p:
                out.append(p)
        return out

    def _ws_gap_right(self, para: _Para, a: int) -> int:
        """第 a 个字后面连着的空白（空格、制表符）到哪为止（返回第一个非空白的位置）。"""
        n = len(para.atom_pi)
        e = a
        while e < n and para.atom(e)["kind"] in ("c", "tab") and _gap(para.atom(e)["ch"]):
            e += 1
        return e

    def _ws_gap_left(self, para: _Para, a: int) -> int:
        s = a
        while s > 0 and para.atom(s - 1)["kind"] in ("c", "tab") and _gap(para.atom(s - 1)["ch"]):
            s -= 1
        return s

    def locate(self, li: int, i1: int, i2: int) -> tuple[_Para, int, int, str, int]:
        """base 第 li 行纯文字的 [i1, i2) → (段落, 起, 止, 'L'/'R'/'D', 抄格式的字)。定不准就 raise Skip。"""
        P = self.plain[li]
        mp = self.lmap[li]
        if "\x1f" in P[i1:i2]:
            raise Skip("跨了表格的格子")
        if not self.solid[li]:
            raise Skip("这一行在 Word 里对不齐")
        lo = P.rfind("\x1f", 0, i1) + 1
        hi = P.find("\x1f", i2)
        hi = len(P) if hi < 0 else hi
        nonws = [c for c in range(i1, i2) if not _ws(P[c])]
        if nonws:
            dis = [mp.get(c) for c in nonws]
            if any(d is None for d in dis):
                raise Skip("要改的字在 Word 里没对上")
            if dis != list(range(dis[0], dis[0] + len(dis))):
                raise Skip("要改的字在 Word 里不连在一起")
            p0, a0, _ = self.D[dis[0]]
            p1, a1, _ = self.D[dis[-1]]
            if p0 != p1:
                raise Skip("要改的字在 Word 里跨了段落")
            para = self.paras[p0]
            s, e = a0, a1 + 1
            if P[i1].isspace() and (i1 == lo or not P[i1 - 1].isspace()):
                s = self._ws_gap_left(para, s)
            if P[i2 - 1].isspace() and (i2 == hi or not P[i2].isspace()):
                e = self._ws_gap_right(para, e)
            return para, s, e, "D", a0
        # 纯插入，或者只删空白（含填空线"＿"）：靠前后的字定位
        j = next((x for x in range(i1 - 1, lo - 1, -1) if not _ws(P[x])), None)
        # 填空行的冒号是转换器补的（Word 里是"指导教师⇥⇥⇥"，转出来"指导教师：＿＿＿"）：Word 里没有，往左再找一个对得上的字，
        # 冒号算进它和插入点之间的空当
        while j is not None and mp.get(j) is None and P[j] in "：:":
            j2 = next((x for x in range(j - 1, lo - 1, -1) if not _ws(P[x])), None)
            if j2 is None or mp.get(j2) is None:
                break
            j = j2
        k = next((x for x in range(i2, hi) if not _ws(P[x])), None)
        dj = mp.get(j) if j is not None else None
        dk = mp.get(k) if k is not None else None
        # 填上的是一段填空线：写上去的字抄那段空白的格式（线上的字照样带下划线），不抄左边标签的
        fill = i2 > i1 and FILL in P[i1:i2] and all(_ws(c) for c in P[i1:i2])
        if j is not None and dj is None and (k is None or dk is None):
            raise Skip("插入点前后的字在 Word 里都没对上")
        if j is None and dk is None:
            raise Skip("插入点后面的字在 Word 里没对上")
        before = (i1 - j - 1) if j is not None else 0          # 插入点和左边那个字之间的空白（不删）
        after = (k - i2) if k is not None else 0               # 插入点和右边那个字之间的空白
        dele = i2 - i1                                         # 要删的空白
        if dj is not None and dk is not None:
            pj, aj, _ = self.D[dj]
            pk, ak, _ = self.D[dk]
            if pj == pk and dk != dj + 1:
                raise Skip("插入点前后在 Word 里夹着 Markdown 里没有的内容，位置拿不准")
        if dj is not None:
            pj, aj, _ = self.D[dj]
            para = self.paras[pj]
            g0, g1 = aj + 1, self._ws_gap_right(para, aj + 1)
            if before + after + dele == 0 or g1 == g0:          # 两边都没空白 / Word 这里没空白
                return para, g0, g0, "L", aj
            if before == 0 and after == 0:                      # 整段空白都删
                return para, g0, g1, "L", (g1 - 1 if fill else aj)
            s = min(g0 + before, g1)
            e = min(s + dele, g1) if dele else s
            return para, s, e, "L", (e - 1 if fill and e > s else aj)
        pk, ak, _ = self.D[dk]
        para = self.paras[pk]
        g0, g1 = self._ws_gap_left(para, ak), ak
        if before + after + dele == 0 or g1 == g0:
            return para, g1, g1, "R", ak
        if before == 0 and after == 0:
            return para, g0, g1, "R", (g1 - 1 if fill else ak)
        e = max(g1 - after, g0)
        s = max(e - dele, g0) if dele else e
        return para, s, e, "R", (e - 1 if fill and e > s else ak)


# ======================================================================
# 段内改字：重建 run
# ======================================================================
def _mk_run(owner, pcs: list[dict], tmpl, reuse: bool, deleted: bool = False):
    """deleted：这一截是修订模式里删掉的字（写成 w:delText）。"""
    if reuse:
        r = owner
        for ch in list(r):
            if ch.tag != q("rPr"):
                r.remove(ch)
    else:
        src = owner if owner is not None else tmpl
        r = etree.Element(q("r"))
        if owner is not None:
            for k, v in owner.attrib.items():
                r.set(k, v)
        rpr = src.find(q("rPr")) if src is not None else None
        if rpr is not None:
            rpr = copy.deepcopy(rpr)
            for x in list(rpr):
                if x.tag in (q("rPrChange"), q("ins"), q("del")):
                    rpr.remove(x)
            r.append(rpr)
    buf: list[str] = []

    def flush():
        if buf:
            t = etree.SubElement(r, q("delText" if deleted else "t"))
            t.text = "".join(buf)
            t.set(XML_SPACE, "preserve")
            buf.clear()
    for pc in pcs:
        if pc["kind"] == "c":
            buf.append(pc["ch"])
            continue
        flush()
        if pc["el"] is not None:
            r.append(pc["el"])
        elif pc["kind"] == "tab":
            etree.SubElement(r, q("tab"))
        elif pc["kind"] == "br":
            etree.SubElement(r, q("br"))
    flush()
    return r


def _apply_para(para: _Para, edits: list[dict], track: "_Track | None" = None) -> list[dict]:
    """一段里的几处修改一起做。edits: {"s","e","text","side","tmpl","order","ref"}。返回做不了的那几处（带原因）。
    track：修订模式——要删的字留着、记成删除（w:del），加的字记成插入（w:ins）；不给就直接改字。"""
    pieces = list(para.pieces)
    api = para.atom_pi
    n = len(api)
    plan, bad = [], []
    for ed in edits:
        s, e = ed["s"], ed["e"]
        if s < e:
            ps, pe = api[s], api[e - 1] + 1
        elif ed["side"] == "L" and s > 0:
            ps = pe = api[s - 1] + 1
        else:
            ps = pe = api[s] if s < n else len(pieces)
        if any(pieces[x]["kind"] == "op" for x in range(ps, pe)):
            bad.append({**ed, "reason": "要删的字中间夹着图、域或脚注号"})
            continue
        tmpl = para.pieces[api[ed["tmpl"]]]["run"] if 0 <= ed["tmpl"] < n else (para.runs[0] if para.runs else None)
        plan.append((ps, pe, ed, tmpl))
    plan.sort(key=lambda x: (x[0], x[2]["order"]))
    kept, last_end = [], -1
    for item in plan:
        if item[0] < last_end:
            bad.append({**item[2], "reason": "和另一处修改重叠"})
            continue
        kept.append(item)
        last_end = max(last_end, item[1])
    if not kept:
        return bad
    for ps, pe, ed, tmpl in reversed(kept):
        syn = {"tmpl": tmpl}
        new = [{"run": syn, "kind": "tab" if c == "\t" else "c", "ch": c, "el": None, "mk": "ins" if track else None}
               for c in ed["text"]]
        if track:                                          # 删的字留在原 run 里，记成删除；加的字跟在后面
            old = [dict(pc, mk="del") if pc["kind"] in ("c", "tab", "br") else pc for pc in pieces[ps:pe]]
            pieces[ps:pe] = old + new
        else:
            pieces[ps:pe] = new
    # 按 owner 分段（修订模式下删的、加的、没动的各成一段），合成的段挂到前一个原 run 后面（段首的挂到后一个原 run 前面）
    segs: list[tuple] = []
    for pc in pieces:
        if segs and segs[-1][0] is pc["run"] and segs[-1][2] == pc.get("mk"):
            segs[-1][1].append(pc)
        else:
            segs.append((pc["run"], [pc], pc.get("mk")))
    groups = {id(r): [] for r in para.runs}
    last, pending = None, []
    for seg in segs:
        owner = seg[0]
        if isinstance(owner, dict):
            (groups[id(last)].append(seg) if last is not None else pending.append(seg))
        else:
            if pending:
                groups[id(owner)].extend(pending)
                pending = []
            groups[id(owner)].append(seg)
            last = owner

    def built(owner, pcs, mk, src, reuse):
        run = _mk_run(src, pcs, owner["tmpl"] if isinstance(owner, dict) else None, reuse=reuse, deleted=mk == "del")
        return track.wrap(run, mk) if (track and mk) else run

    made: list = []
    if pending:
        host = pending[0][0]["tmpl"]
        host = host if host is not None and id(host) in groups else (para.runs[0] if para.runs else None)
        if host is None:                                   # 一个 run 都没有的空段：直接在段尾加
            for owner, pcs, mk in pending:
                para.el.append(built(owner, pcs, mk, None, False))
        else:
            groups[id(host)].extend(pending)
    orig = {id(r): [pc for pc in para.pieces if pc["run"] is r] for r in para.runs}
    for r in para.runs:
        grp = groups[id(r)]
        if len(grp) == 1 and grp[0][0] is r and len(grp[0][1]) == len(orig[id(r)]) \
                and all(a is b for a, b in zip(grp[0][1], orig[id(r)])):
            continue                                        # 没动的 run 原样留着
        parent = r.getparent()
        at = parent.index(r)
        els, reused = [], False
        for owner, pcs, mk in grp:
            if owner is r and not reused and not track:
                els.append(built(owner, pcs, mk, r, True))
                reused = True
            elif owner is r:
                els.append(built(owner, pcs, mk, r, False))
            else:
                els.append(built(owner, pcs, mk, None, False))
        if r.getparent() is parent:
            parent.remove(r)
        for k, el in enumerate(els):
            parent.insert(at + k, el)
        made += [el for el in els if el.tag == q("r")]
    _merge_runs(made)
    return bad


def _plain_run(r) -> bool:
    return all(ch.tag in (q("rPr"), q("t")) for ch in r)


def _merge_runs(made: list) -> None:
    """改字时拆出来的、格式一模一样又挨着的 run 并回一个（XML 干净些，Word 里没区别）。"""
    ids = {id(x) for x in made}
    for r in made:
        parent = r.getparent()
        if parent is None or not _plain_run(r):
            continue
        nxt = r.getnext()
        while nxt is not None and id(nxt) in ids and nxt.tag == q("r") and _plain_run(nxt) \
                and dict(nxt.attrib) == dict(r.attrib):
            a, b = r.find(q("rPr")), nxt.find(q("rPr"))
            if (a is None) != (b is None) or (a is not None and etree.tostring(a) != etree.tostring(b)):
                break
            ts = r.findall(q("t"))
            add = "".join(t.text or "" for t in nxt.findall(q("t")))
            if ts:
                ts[-1].text = (ts[-1].text or "") + add
            else:
                t = etree.SubElement(r, q("t"))
                t.text = add
                t.set(XML_SPACE, "preserve")
            parent.remove(nxt)
            nxt = r.getnext()


# ======================================================================
# 整段：新起一段 / 删一段
# ======================================================================
def _main_run(p) -> object | None:
    best, n = None, -1
    for r in _runs_of(p):
        k = sum(len((t.text or "").strip()) for t in r.findall(q("t")))
        if k > n:
            best, n = r, k
    return best


def _new_para(tmpl_p, text: str, track: "_Track | None" = None):
    """新起一段（段落格式抄 tmpl_p，字的格式抄它字最多的那个 run）。修订模式：字和段落标记都记成插入。"""
    p = etree.Element(q("p"))
    ppr = tmpl_p.find(q("pPr")) if tmpl_p is not None else None
    if ppr is not None:
        ppr = copy.deepcopy(ppr)
        for x in list(ppr):
            if x.tag in (q("sectPr"), q("pPrChange")):
                ppr.remove(x)
        rp = ppr.find(q("rPr"))
        if rp is not None:
            for x in list(rp):
                if x.tag in (q("ins"), q("del"), q("moveFrom"), q("moveTo"), q("rPrChange")):
                    rp.remove(x)
        p.append(ppr)
    tr = _main_run(tmpl_p) if tmpl_p is not None else None
    pcs = [{"run": None, "kind": "tab" if c == "\t" else "c", "ch": c, "el": None} for c in text]
    run = _mk_run(None, pcs, tr, reuse=False)
    if track:
        p.append(track.wrap(run, "ins"))
        _mark_para(p, "ins", track)
    else:
        p.append(run)
    return p


def _track_remove_para(p, track: "_Track") -> None:
    """修订模式下"删整段"：段里每个 run 记成删除（字换成 w:delText），段落标记也记成删除；接受修订时整段才真没了。"""
    for r in _runs_of(p):
        if not any(ch.tag in (q("t"), q("tab"), q("br"), q("cr"), q("instrText"), q("footnoteReference"),
                              q("endnoteReference"), q("sym")) for ch in r):
            continue                                  # 只有格式、书签挂着的空 run：不用记
        _to_deleted(r)
        parent = r.getparent()
        at = parent.index(r)
        w = track.mark("del")
        parent.remove(r)
        w.append(r)
        parent.insert(at, w)
    _mark_para(p, "del", track)


_BLOCK = (q("p"), q("tbl"), q("sdt"))


def _sib_kind(el, forward: bool) -> str:
    x = el.getnext() if forward else el.getprevious()
    while x is not None:
        if x.tag in _BLOCK:
            return "p" if x.tag != q("tbl") else "tbl"
        if x.tag == q("sectPr"):
            return "end"
        if x.tag in (q("tcPr"),):
            return "start"
        x = x.getnext() if forward else x.getprevious()
    return "end" if forward else "start"


def _can_remove(p) -> bool:
    if p.find(q("pPr") + "/" + q("sectPr")) is not None:
        return False                                  # 分节符所在的段：删了会把两节并起来
    prev, nxt = _sib_kind(p, False), _sib_kind(p, True)
    if prev == "tbl" and nxt in ("tbl", "end"):
        return False                                  # 两张表会粘在一起 / 格子、正文不能以表结尾
    if prev == "start" and nxt == "end":
        return False                                  # 格子里只剩这一段
    if any(x.tag in (q("drawing"), q("pict"), q("object")) for x in p.iter()):
        return False
    return True


_MARKS = (q("bookmarkStart"), q("bookmarkEnd"), q("commentRangeStart"), q("commentRangeEnd"),
          q("permStart"), q("permEnd"))


def _remove_para(p) -> None:
    """删整段；段里的书签、批注范围挪到相邻段落里，免得落单。"""
    marks = [x for x in p.iter() if x.tag in _MARKS]
    if marks:
        nxt = p.getnext()
        while nxt is not None and nxt.tag != q("p"):
            nxt = nxt.getnext()
        prv = p.getprevious()
        while prv is not None and prv.tag != q("p"):
            prv = prv.getprevious()
        for m in marks:
            m.getparent().remove(m)
            if nxt is not None:
                ppr = nxt.find(q("pPr"))
                (ppr.addnext(m) if ppr is not None else nxt.insert(0, m))
            elif prv is not None:
                prv.append(m)
    p.getparent().remove(p)


# ======================================================================
# base → cur 的改动
# ======================================================================
_TOK = re.compile(r"[A-Za-z0-9_\u00C0-\u024F]+|[ \t\u3000\xa0]+|.", re.S)


def _tok_ops(a: str, b: str) -> list[tuple[int, int, str]]:
    """纯文字 a → b 的改动：中文按字、英文按词。返回 [(a 起, a 止, 换成的字)]。"""
    ta, tb = _TOK.findall(a), _TOK.findall(b)
    oa = [0]
    for t in ta:
        oa.append(oa[-1] + len(t))
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    return [(oa[i1], oa[i2], "".join(tb[j1:j2])) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _pair_lines(old: list[int], new: list[int], op: list[str], np_: list[str], ok, ko, nk) -> list[tuple]:
    """replace 块里旧行和新行配对（单调、按相似度）：返回 [(旧下标 or None, 新下标 or None)]，按文中顺序。"""
    n, m = len(old), len(new)

    def sim(i, j):
        a, b = op[old[i]], np_[new[j]]
        if (ko[old[i]][0] == "table") != (nk[new[j]][0] == "table"):
            return 0.0
        if not a or not b:
            return 0.0
        s = difflib.SequenceMatcher(None, a, b, autojunk=False)
        if s.real_quick_ratio() < 0.3 or s.quick_ratio() < 0.3:
            return 0.0
        r = s.ratio()
        return r if r >= 0.3 else 0.0
    if n * m > 4000:                                   # 太大：按顺序一一配
        S = {(i, i): 1.0 for i in range(min(n, m))}
    else:
        S = {(i, j): sim(i, j) for i in range(n) for j in range(m)}
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            v = max(best[i + 1][j], best[i][j + 1])
            s = S.get((i, j), 0.0)
            if s > 0:
                v = max(v, s + best[i + 1][j + 1])
            best[i][j] = v
    out, i, j = [], 0, 0
    while i < n and j < m:
        s = S.get((i, j), 0.0)
        if s > 0 and abs(best[i][j] - (s + best[i + 1][j + 1])) < 1e-9:
            out.append((old[i], new[j]))
            i += 1
            j += 1
        elif best[i + 1][j] >= best[i][j + 1]:
            out.append((old[i], None))
            i += 1
        else:
            out.append((None, new[j]))
            j += 1
    out += [(old[x], None) for x in range(i, n)] + [(None, new[x]) for x in range(j, m)]
    return out


def _excerpt(s: str, n: int = 40) -> str:
    s = re.sub(r"\s+", " ", s.replace("\x1f", " | ")).strip()
    return s if len(s) <= n else s[:n] + "…"


# ======================================================================
# 主流程
# ======================================================================
MODES = ("track", "direct")


def apply(docx_bytes: bytes, base_md: str, cur_md: str, mode: str = "direct", author: str = "AI",
          date: str | None = None) -> tuple[bytes | None, dict]:
    """把 base → cur 的改动写进 docx。mode="track"：记成 Word 修订（作者 author、日期 date，默认现在）；
    "direct"：直接改文字，不留修订痕迹。返回 (新 docx 的字节 or None, 报告)；修订模式的报告里 revisions = {ins, del}。"""
    zin = zipfile.ZipFile(_BytesIO(docx_bytes))
    part = _main_part(zin)
    root = etree.fromstring(zin.read(part))
    if root.find(q("body")) is None:
        raise ValueError("Word 正文部件里没有 w:body（不是常见的 Word 文档格式）")
    track = _Track(author, date or _word_date(), _max_id(zin) + 100) if mode == "track" else None
    bl, cl = md_lines(base_md), md_lines(cur_md)
    model = _Model(root, bl)
    cp = [md_plain(x) for x in cl]
    ck = [line_kind(x) for x in cl]
    report = {"applied": 0, "skipped": [], "paras_added": 0, "paras_removed": 0, "mode": "track" if track else "direct",
              "lines_total": len(bl), "lines_solid": sum(model.solid)}
    skipped = report["skipped"]

    def skip(where: str, old: str, new: str, reason: str):
        skipped.append({"where": _excerpt(where), "old": _excerpt(old, 80), "new": _excerpt(new, 80), "reason": reason})

    sm = difflib.SequenceMatcher(None, [x.strip() for x in bl], [x.strip() for x in cl], autojunk=False)
    seq: list[tuple] = []                     # 文中顺序的 (旧行, 新行)：equal 也放进来给插入定位
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            seq += [(i1 + k, j1 + k, "eq") for k in range(i2 - i1)]
        elif tag == "delete":
            seq += [(i, None, "x") for i in range(i1, i2)]
        elif tag == "insert":
            seq += [(None, j, "x") for j in range(j1, j2)]
        else:
            seq += [(a, b, "x") for a, b in _pair_lines(list(range(i1, i2)), list(range(j1, j2)),
                                                        model.plain, cp, None, model.kinds, ck)]

    para_edits: dict[int, list[dict]] = {}
    inserts: list[tuple] = []                 # (锚段落 el, 'after'/'before', [新段 el], 行号)
    removes: list = []
    line_state: dict = {}                     # 新/旧行 → applied / skipped

    def mark(key, ok: bool):
        st = line_state.setdefault(key, [0, 0])
        st[0 if ok else 1] += 1

    # 1) 成对的行：段内按字/词改
    for a, b, tag in seq:
        if tag == "eq" or a is None or b is None:
            continue
        old, new = model.plain[a], cp[b]
        key = ("pair", a)
        if old == new:
            if bl[a].strip() != cl[b].strip():
                skip(new or cl[b], bl[a], cl[b], "只改了格式（加粗、标题级别、标记等），Word 里没动")
                mark(key, False)
            continue
        if model.kinds[a][0] == "table" and old.count("\x1f") != new.count("\x1f"):
            skip(new, old, new, "表格这一行的格子数变了（加删列、合并格子），没写")
            mark(key, False)
            continue
        for order, (i1, i2, text) in enumerate(_tok_ops(old, new)):
            try:
                if "\x1f" in text:
                    raise Skip("跨了表格的格子")
                para, s, e, side, tmpl = model.locate(a, i1, i2)
            except Skip as ex:
                skip(new, old[i1:i2], text, ex.args[0])
                mark(key, False)
                continue
            para_edits.setdefault(para.idx, []).append(
                {"s": s, "e": e, "text": text, "side": side, "tmpl": tmpl, "order": (a, order),
                 "key": key, "line": new, "old": old[i1:i2]})

    # 2) 删掉的行
    for a, b, tag in seq:
        if tag != "x" or a is None or b is not None:
            continue
        key = ("del", a)
        kind = model.kinds[a][0]
        P = model.plain[a]
        if kind == "sep" or not P.strip("\x1f").strip():
            continue
        if kind == "table":
            skip(P, P, "", "删表格行没写（Word 表格结构不动）")
            mark(key, False)
            continue
        if kind == "image":
            skip(bl[a], bl[a], "", "删图没写")
            mark(key, False)
            continue
        got = sorted(model.lmap[a].values())
        n = sum(1 for c in P if not _ws(c))
        if not model.solid[a] or not got or len(got) < n or got != list(range(got[0], got[0] + len(got))):
            skip(P, P, "", "整段删除：这段在 Word 里对不齐")
            mark(key, False)
            continue
        first, last = model.D[got[0]], model.D[got[-1]]
        for pi in range(first[0], last[0] + 1):
            para = model.paras[pi]
            s = first[1] if pi == first[0] else 0
            e = last[1] + 1 if pi == last[0] else len(para.atom_pi)
            rest = [x for x in range(len(para.atom_pi)) if (x < s or x >= e) and not _ws(para.atom(x)["ch"])]
            if not rest and _can_remove(para.el):
                removes.append((para.el, key))
                continue
            if not rest:
                s, e = 0, len(para.atom_pi)
            else:
                s = model._ws_gap_left(para, s)
            para_edits.setdefault(para.idx, []).append(
                {"s": s, "e": e, "text": "", "side": "D", "tmpl": s, "order": (a, 0), "key": key,
                 "line": P, "old": P})

    # 3) 新加的行
    def nearest(li: int, want: str, level: int = 0) -> object | None:
        """离第 li 行最近、同类、在 Word 里对得好的那一段：抄它的段落格式。"""
        cands = []
        for x in range(len(bl)):
            k, lv = model.kinds[x]
            if not model.solid[x]:
                continue
            if want == "heading" and (k != "heading" or (level and lv != level)):
                continue
            if want == "body" and k not in ("body", "list"):
                continue
            ps = model.line_paras(x)
            if len(ps) == 1 and not any(not _ws(c) and ci not in model.lmap[x] for ci, c in enumerate(model.plain[x])):
                cands.append((abs(x - li), x, ps[0].el))
        cands.sort(key=lambda t: (t[0], t[1]))
        return cands[0][2] if cands else None

    prev_old = None
    pend: list[int] = []

    def flush_new(next_old):
        nonlocal pend
        if not pend:
            return
        anchor, how = None, None
        if prev_old is not None and model.kinds[prev_old][0] not in ("table", "sep", "image") and model.solid[prev_old]:
            ps = model.line_paras(prev_old)
            if ps:
                anchor, how = ps[-1].el, "after"
        if anchor is None and next_old is not None and model.kinds[next_old][0] not in ("table", "sep", "image") \
                and model.solid[next_old]:
            ps = model.line_paras(next_old)
            if ps:
                anchor, how = ps[0].el, "before"
        for j in pend:
            kind, lv = ck[j]
            if kind == "sep":
                continue
            key = ("new", j)
            if kind == "table":
                skip(cp[j], "", cp[j], "新增表格行没写（Word 表格结构不动）")
                mark(key, False)
                continue
            if kind == "image":
                skip(cl[j], "", cl[j], "新增的图没写")
                mark(key, False)
                continue
            if anchor is None:
                skip(cp[j], "", cp[j], "新段落前后是表格、图或对不齐的段落，放哪拿不准")
                mark(key, False)
                continue
            text = cp[j]
            if kind == "list":
                text = _LIST.sub("", text, count=1)
            ref = nearest(prev_old if prev_old is not None else (next_old or 0),
                          "heading" if kind == "heading" else "body", lv)
            if ref is None and kind == "heading":
                ref = nearest(prev_old if prev_old is not None else (next_old or 0), "heading")
            if ref is None:
                ref = anchor
            if kind == "list" and ref.find(q("pPr") + "/" + q("numPr")) is None:
                text = cp[j]                     # 抄的那段不是列表：编号留在字里
            inserts.append((anchor, how, _new_para(ref, text, track), key))
            mark(key, True)
        pend = []

    for a, b, tag in seq:
        if a is None:
            pend.append(b)
            continue
        flush_new(a)
        prev_old = a
    flush_new(None)

    # 4) 动手：先改段内的字，再插段、删段（插入都挂在原来的段落元素上，删段放最后）
    for pi, eds in para_edits.items():
        bad = _apply_para(model.paras[pi], eds, track)
        for x in bad:
            skip(x["line"], x["old"], x["text"], x["reason"])
            mark(x["key"], False)
        badset = {(x["order"], x["s"], x["e"]) for x in bad}
        for ed in eds:
            if (ed["order"], ed["s"], ed["e"]) not in badset:
                mark(ed["key"], True)
    last_after: dict = {}
    for anchor, how, el, key in inserts:
        if how == "after":
            ref = last_after.get(id(anchor), anchor)
            ref.addnext(el)
            last_after[id(anchor)] = el
        else:
            anchor.addprevious(el)
        report["paras_added"] += 1
    for el, key in removes:
        if el.getparent() is not None:
            (_track_remove_para(el, track) if track else _remove_para(el))
            report["paras_removed"] += 1
            mark(key, True)

    applied = sum(1 for v in line_state.values() if v[0])
    report["applied"] = applied
    report["lines_skipped"] = sum(1 for v in line_state.values() if v[1])
    if track:
        report["revisions"] = {"ins": track.ins, "del": track.dele}
    if not applied:
        return None, report
    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    out = _BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            data = xml if info.filename == part else zin.read(info.filename)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zout.writestr(zi, data)
    return out.getvalue(), report


def _BytesIO(b: bytes = b""):
    import io
    return io.BytesIO(b)


def _main_part(z: zipfile.ZipFile) -> str:
    """正文部件的路径：一般是 word/document.xml，按 _rels/.rels 里 officeDocument 那条找。"""
    try:
        rels = etree.fromstring(z.read("_rels/.rels"))
        for r in rels:
            if str(r.get("Type") or "").endswith("/officeDocument"):
                t = str(r.get("Target") or "").lstrip("/")
                if t in z.namelist():
                    return t
    except (KeyError, etree.XMLSyntaxError):
        pass
    return "word/document.xml"


# ======================================================================
# 找 base（转换刚完成时那份 Markdown）
# ======================================================================
def _sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _text_shas(text: str) -> set[str]:
    lf = text.replace("\r\n", "\n")
    return {_sha1(text.encode("utf-8")), _sha1(lf.encode("utf-8")), _sha1(lf.replace("\n", "\r\n").encode("utf-8"))}


def conv_meta(md_path: Path) -> dict | None:
    """md 是从 Word 转出来的：返回 .bloom-convert.json（带 source）；不是就 None。"""
    try:
        meta = json.loads((Path(md_path).parent / CONV_META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    src = str(meta.get("source") or "")
    if not src.lower().endswith(".docx"):
        return None
    if Path(src).stem != Path(md_path).stem:
        return None
    return meta


def find_base(md_path: Path, meta: dict, state_dir: Path | None = None) -> tuple[str | None, str]:
    """转换刚完成时的 Markdown：按 md_sha1 指纹在导入快照、对比页原文、改动记录里找；都没有就用 pandoc 重新转一遍。
    返回 (文字 or None, 从哪来的)。"""
    want = str(meta.get("md_sha1") or "")
    if state_dir is None:
        try:
            from .config import STATE as state_dir
        except Exception:
            state_dir = Path.home() / ".zuhui"
    state_dir = Path(state_dir)
    stem = Path(md_path).stem
    if want:
        cands: list[Path] = []
        for pat in ("papers/*/original.md", f"base/*{glob.escape(stem)}*.orig"):
            cands += sorted(state_dir.glob(pat), key=lambda p: -p.stat().st_mtime)
        for p in cands:
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            if _sha1(raw) == want:
                return raw.decode("utf-8-sig"), str(p)
            try:
                t = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            if want in _text_shas(t):
                return t, str(p)
        for p in sorted((state_dir / "changes").glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            t = d.get("before")
            if isinstance(t, str) and want in _text_shas(t):
                return t, f"{p}#before"
    # 重新转：转换器版本要和当时一样，结果的指纹对得上才算数
    try:
        from . import pdfconv
        from .components import pandoc as _find_pandoc
        pandoc = _find_pandoc()
        if not pandoc:
            return None, "没有 pandoc，也找不到转换时的原样"
        stage = Path(tempfile.mkdtemp(prefix="bloom-wb-"))
        try:
            md, _ = pdfconv.convert_docx(Path(meta["source"]), stage, pandoc)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        if not want or want in _text_shas(md):
            return md, "重新转换（指纹对得上）"
        return None, "找不到转换时的原样（重新转出来的和当时的指纹对不上，转换规则或 Word 文件变过）"
    except Exception as e:
        return None, f"找不到转换时的原样：{e!r}"[:200]


# ======================================================================
# 写文件
# ======================================================================
def _out_candidates(src: Path):
    yield src.with_name(f"{src.stem}{SUFFIX}.docx")
    for k in range(2, 50):
        yield src.with_name(f"{src.stem}（会上修订 {k}）.docx")


def _write_out(src: Path, data: bytes, state_p: Path) -> tuple[Path, list[str]]:
    """写到「<原名>（会上修订）.docx」：那个文件是上次我们写的、没被人改过才覆盖；被 Word 打开着、或者是别人的文件，就换个名字。"""
    notes = []
    try:
        st = json.loads(state_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    ours = st.get("written") or {}
    for dst in _out_candidates(src):
        if dst.exists():
            try:
                same = _sha1(dst.read_bytes()) == ours.get(dst.name)
            except OSError:
                same = False
            if not same:
                notes.append(f"{dst.name} 已经有了、不是上次写的那份（可能改过），没覆盖")
                continue
        tmp = dst.with_name(f".{dst.stem}.{os.getpid()}.part")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, dst)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            notes.append(f"{dst.name} 写不进去（多半正被 Word 打开）：{e.__class__.__name__}")
            continue
        ours[dst.name] = _sha1(data)
        st["written"] = ours
        st["last"] = {"file": dst.name, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        try:
            state_p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return dst, notes
    raise OSError("；".join(notes[-3:]) or "写不出文件")


def writeback(md_path, out_dir_state: Path | None = None, base_text: str | None = None,
              state_dir: Path | None = None, mode: str | None = None, author: str | None = None) -> dict:
    """md_path 这篇（从 Word 转来的）会上改完的，写成原 .docx 旁边的「<原名>（会上修订）.docx」。
    mode：track（Word 修订，默认按设置）/ direct（直接改）；author：修订的作者（默认 AI 的名字）。
    返回 {"ok","out","applied","skipped":[…],"note","base_from",…}；不是从 Word 转来的：{"ok": False, "not_docx": True}。"""
    t0 = time.time()
    if mode not in MODES:
        try:
            from .paper_prefs import load as _prefs
            mode = _prefs().get("docx_mode")
        except Exception:
            mode = None
        mode = mode if mode in MODES else "track"
    if not author:
        try:
            from .config import assistant_name
            author = assistant_name()
        except Exception:
            author = "AI"
    md_path = Path(md_path)
    meta = conv_meta(md_path)
    if not meta:
        return {"ok": False, "not_docx": True, "error": "这篇不是从 Word 转来的"}
    src = Path(meta["source"])
    if not src.is_file():
        return {"ok": False, "error": f"找不到原来的 Word 文件：{src}"}
    try:
        cur = md_path.read_text(encoding="utf-8-sig")
    except OSError as e:
        return {"ok": False, "error": f"读不了会上改的稿子：{e}"}
    base_from = "调用方给的"
    if base_text is None:
        base_text, base_from = find_base(md_path, meta, state_dir)
        if base_text is None:
            return {"ok": False, "error": base_from, "source": str(src)}
    if md_lines(base_text) == md_lines(cur):
        return {"ok": True, "out": None, "applied": 0, "skipped": [], "note": "会上没改这篇", "source": str(src)}
    warn = []
    try:
        if abs(src.stat().st_mtime - float(meta.get("src_mtime") or 0)) > 2:
            warn.append("Word 文件在转换以后改过：按现在的内容对齐，对不上的跳过")
    except (OSError, ValueError, TypeError):
        pass
    try:
        data, rep = apply(src.read_bytes(), base_text, cur, mode=mode, author=author)
    except Exception as e:
        return {"ok": False, "error": f"写回出错：{e!r}"[:300], "source": str(src)}
    res = {"ok": True, "source": str(src), "base_from": base_from, "warnings": warn, **rep,
           "secs": round(time.time() - t0, 2)}
    if data is None:
        res.update(out=None, note="一处都没写进去（都对不上）")
        return res
    state_p = (out_dir_state or md_path.parent) / WB_STATE
    try:
        dst, notes = _write_out(src, data, state_p)
    except OSError as e:
        try:                     # Word 所在的文件夹写不进去（只读共享、光盘）：写到 Markdown 旁边
            dst, notes = _write_out(md_path.parent / src.name, data, state_p)
            notes = [f"Word 旁边写不进去（{e}），写到了 {md_path.parent}"] + notes
        except OSError as e2:
            res.update(ok=False, out=None, error=f"写文件失败：{e}；{e2}")
            return res
    res.update(out=str(dst), notes=notes, secs=round(time.time() - t0, 2))
    return res


def summary(res: dict, name: str = "") -> str:
    """一行人话（日志、页面提示用）。"""
    who = f"「{name}」" if name else ""
    if not res.get("ok"):
        return f"写回 Word 没成{who}：{res.get('error') or '未知原因'}"
    if not res.get("out"):
        return f"写回 Word{who}：{res.get('note') or '没有要写回的'}"
    sk = res.get("skipped") or []
    how = "（修订模式）" if res.get("mode") == "track" else ""
    s = f"已写回 Word {res.get('applied', 0)} 处{how}，跳过 {len(sk)} 处{who} → {Path(res['out']).name}"
    if sk:
        s += "；跳过的：" + "；".join(f"「{x['where']}」{x['reason']}" for x in sk[:5]) + ("……" if len(sk) > 5 else "")
    return s


# ======================================================================
# 接到 app 上（散会自动跑、/api/paper/writeback 手动跑）
# ======================================================================
_lock = threading.Lock()


def _papers_for(app, meeting: dict | None) -> list[tuple[Path, str]]:
    out = []
    try:
        from .meetings import papers_of
        for p in papers_of(meeting):
            out.append((Path(p["path"]), str(p.get("name") or Path(p["path"]).stem)))
    except Exception:
        pass
    try:
        cur = Path(app.paper.path)
        if not any(_same(cur, p) for p, _ in out):
            out.append((cur, str(getattr(app.paper, "title", "") or cur.stem)))
    except Exception:
        pass
    return out


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return str(a) == str(b)


def tex_summary(res: dict, name: str = "") -> str:
    who = f"「{name}」" if name else ""
    if not res.get("ok"):
        return f"LaTeX 修订版没写成{who}：{res.get('error') or '未知原因'}"
    if not res.get("out"):
        return f"LaTeX 修订版{who}：{res.get('note') or '没有要写的'}"
    s = f"已写 LaTeX 修订版{who}：加 {res.get('added', 0)} 处、删 {res.get('deleted', 0)} 处 → {Path(res['out']).name}"
    if res.get("unmarked"):
        s += f"，{res['unmarked']} 处在公式、标题、命令里，照改了没标"
    return s


def run_for_app(app, meeting: dict | None = None, why: str = "手动") -> list[dict]:
    """这场会（或屏幕上这篇）里从 Word 转来的论文，各写一份「（会上修订）.docx」；LaTeX 论文另写一份带修订标记的
    「（会上修订）.tex」（zuhui/tex_changes.py）。每篇在日志里记一行。"""
    results = []
    with _lock:
        for md, name in _papers_for(app, meeting):
            if conv_meta(md):
                res = writeback(md)
                res["name"] = name
                line, state = summary(res, name), "写回 Word"
            elif md.suffix.lower() == ".tex":
                from . import tex_changes
                try:
                    res = tex_changes.for_paper(md)
                except Exception as e:
                    res = {"ok": False, "error": repr(e)[:200]}
                res.update(name=name, kind="tex")
                line, state = tex_summary(res, name), "LaTeX 修订"
            else:
                continue
            try:
                app.log("meeting", state=state, text=f"{line}（{why}）",
                        skipped=res.get("skipped") or [], out=res.get("out"))
            except Exception:
                pass
            results.append(res)
    return results


def snapshot_after(app, meeting: dict | None) -> None:
    """散会后（改稿都做完了）每篇论文存一份"散会后"的版本（zuhui/paper_versions.py）。"""
    try:
        from . import paper_versions
        title = re.sub(r"\s*·\s*\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*$", "", str((meeting or {}).get("title") or "")).strip()
        for md, name in _papers_for(app, meeting):
            paper_versions.snapshot(md, name, "after", (meeting or {}).get("id"), title)
    except Exception as e:
        try:
            app.log("fast_error", error=f"散会后的论文版本没存上：{e!r}"[:300])
        except Exception:
            pass


def after_meeting(app, meeting: dict | None, wait_s: float = 1200) -> None:
    """散会后：等后台还在改的、排着队的改稿任务做完（最多 wait_s 秒），再写回。在后台线程里跑。"""
    def go():
        t_end = time.time() + wait_s
        while time.time() < t_end:
            try:
                c = app.board.counts()
                if not c.get("working") and not c.get("queued"):
                    break
            except Exception:
                break
            time.sleep(3)
        snapshot_after(app, meeting)
        try:
            run_for_app(app, meeting, why="散会")
        except Exception as e:
            try:
                app.log("fast_error", error=f"散会写回 Word 出错：{e!r}"[:300])
            except Exception:
                pass
    threading.Thread(target=go, name="docx-writeback", daemon=True).start()


def on_demand(app) -> dict:
    """POST /api/paper/writeback：屏幕上这篇现在就写一份。"""
    try:
        md = Path(app.paper.path)
    except Exception:
        return {"ok": False, "error": "没有打开的文档"}
    if not conv_meta(md) and md.suffix.lower() != ".tex":
        return {"ok": False, "error": "这篇不是从 Word 或 LaTeX 来的，没有可写回的"}
    res = run_for_app(app, None, why="手动")          # 不在开会：只有屏幕上这篇
    r = res[0] if res else {"ok": False, "error": "没有写回"}
    return {"ok": bool(r.get("ok")), "result": {k: r.get(k) for k in ("out", "applied", "skipped", "note", "error",
                                                                       "warnings", "notes", "source", "mode", "revisions",
                                                                       "added", "deleted", "unmarked", "kind")},
            "text": tex_summary(r, r.get("name") or "") if r.get("kind") == "tex" else summary(r, r.get("name") or "")}
