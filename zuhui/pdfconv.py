# -*- coding: utf-8 -*-
"""论文 PDF / Word → 学生改的 Markdown，按对比页能显示的样子写：
    公式    独立公式写成 ```latex 围栏（对比页把它排成公式，\\tag{N} 当式号）；
            行内公式简单的（x_t、α、𝐱_i）直接写成文字，复杂的保留 $...$ LaTeX
    表格    Markdown 管道表（合并单元格摊平，单元格里的 | 换成全角或 \\vert）
    图      存进论文文件夹的 figures/figN.png，figures.json 记 {"N": {"file": ...}}，
            正文原位写占位行 [image: 图 N] 加图注，对比页按图号把图放回去
    页眉页脚页码、边注（arXiv 编号、行号）丢掉
PDF 用 MinerU（单独的 venv：仓库根下 .venv-mineru，子进程调用，模型在 ~/.mineru/models）；
没装、失败或超时时抛 ConvertError，papers.py 退回 PyMuPDF 抽纯文本。
Word 用 pandoc 转 gfm，图用 --extract-media 抽出来，同样的图号规则。
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIER = os.environ.get("ZUHUI_PDF_TIER", "standard")        # standard：版面 + VLM 认公式/表格；basic 更省显存但更慢
TIMEOUT = int(os.environ.get("ZUHUI_PDF_TIMEOUT", "300"))   # MinerU 一篇最多跑这么久（秒；平时 30 页约 1 分钟，
                                                            # 显卡被别的训练占满时会慢十几倍），超时退回 PyMuPDF，下次导入再试 MinerU
# Word 转换器的版本：转换规则改了就加一。papers.py 记进 .bloom-convert.json，旧版本转的、没人改过的 Markdown 重新转一遍
# 2：申报书这类表单表拆成标题、段落和键值表；格子里套着的表不再把外层表截断；整段加粗的编号题头写成标题
# 3：封面的填空线（下划线的空格、画上去的直线）写成"标签：值"，空着的写成"＿＿＿"（_docx_fill_lines）
DOCX_VERSION = 3
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ConvertError(RuntimeError):
    pass


def mineru_kit() -> Path | None:
    env = os.environ.get("ZUHUI_MINERU")
    if env and env.strip().lower() in ("0", "off", "no", "none"):     # 关掉 MinerU，只用 PyMuPDF
        return None
    cands = [Path(env)] if env else []
    cands += [ROOT / ".venv-mineru" / "Scripts" / "mineru-kit.exe", ROOT / ".venv-mineru" / "bin" / "mineru-kit"]
    for c in cands:
        if c.is_file():
            return c
    return None


# ======================================================================
# LaTeX / 行内文字
# ======================================================================
GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "ϕ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
    "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
SYMS = {
    "cdot": "·", "times": "×", "pm": "±", "mp": "∓", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "sim": "∼", "simeq": "≃", "equiv": "≡", "propto": "∝",
    "infty": "∞", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←", "Rightarrow": "⇒", "leftrightarrow": "↔",
    "ldots": "…", "dots": "…", "cdots": "⋯", "partial": "∂", "nabla": "∇", "forall": "∀", "exists": "∃",
    "ell": "ℓ", "prime": "′", "circ": "∘", "degree": "°", "ast": "*", "star": "⋆", "mid": "|",
    "times": "×", "div": "÷", "%": "%", "percent": "%", "dagger": "†", "top": "⊤", "emptyset": "∅",
}
def _alpha(start_upper, start_lower, digits=None, holes=None):
    m = {}
    for i in range(26):
        m[chr(65 + i)] = chr(start_upper + i)
        m[chr(97 + i)] = chr(start_lower + i)
    if digits:
        for i in range(10):
            m[str(i)] = chr(digits + i)
    m.update(holes or {})
    return m


BOLD = _alpha(0x1D400, 0x1D41A, 0x1D7CE)
BOLD.update({c: chr(0x1D6A8 + i) for i, c in enumerate("ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡϴΣΤΥΦΧΨΩ")})
BOLD.update({c: chr(0x1D6C2 + i) for i, c in enumerate("αβγδεζηθικλμνξοπρςστυφχψω")})
BOLD.update({"ϵ": "\U0001D6DC", "ϑ": "\U0001D6DD", "ϕ": "\U0001D6DF", "ϱ": "\U0001D6E0"})
CAL = {chr(65 + i): chr(0x1D49C + i) for i in range(26)}
CAL.update({"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ"})
BB = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "E": "𝔼", "P": "ℙ"}
# 对比页把 x_t、p_{t,1}、𝐱_i 这种排成上下标（view.py 里的 SCRIPT），规则照抄过来判断"能不能写成文字"
SCRIPT = re.compile(r"(?<![A-Za-z0-9_\\])([A-Za-z\u00C0-\u024F\u0370-\u03FF\U0001D400-\U0001D7FF])"
                    r"((?:[_^](?:\{[^{}]{1,16}\}|[A-Za-z0-9*](?![A-Za-z0-9])))+)")
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _close_delims(t: str) -> str:
    """VLM 偶尔多认出一根竖线：整式套 \\left| … \\right|，或开头孤零零一个 \\left\\rvert。配平，免得 MathJax 报错。"""
    tag = ""
    m = re.search(r"\s*(\\tag\{[^{}]*\})\s*$", t)
    if m:
        tag, t = m.group(1), t[:m.start()]
    m = re.fullmatch(r"\\left\s*(?:\||\\vert|\\lvert|\\rvert)\s*(.*?)\s*\\right\s*(?:\||\\vert|\\rvert|\\lvert)", t, re.S)
    if m:
        inner = m.group(1)
        if (len(re.findall(r"\\left(?![A-Za-z])", inner)) == len(re.findall(r"\\right(?![A-Za-z])", inner))
                and re.search(r"(?<![<>!:\\])=|\\leftarrow|\\gets|\\approx|\\leq?\b|\\geq?\b|:=|\\sim\b", inner)):
            t = inner
    nl = len(re.findall(r"\\left(?![A-Za-z])", t))
    nr = len(re.findall(r"\\right(?![A-Za-z])", t))
    if nl > nr:
        t2 = re.sub(r"^\s*\\left\s*(?:\\rvert|\\vert|\||\\Vert|\\\|)\s*", "", t)
        if t2 != t:
            t, nl = t2, nl - 1
    if nl > nr:
        t = t + " \\right." * (nl - nr)
    elif nr > nl:
        t = "\\left. " * (nr - nl) + t
    return (t + " " + tag).strip() if tag else t


def tex_tidy(t: str, display: bool = False) -> str:
    """MinerU 的 LaTeX 是一个记号一个空格（\\mathbf {x} _ {i}、1 3 . 3）。收紧成人写的样子，意思不变。"""
    t = (t or "").strip()
    t = re.sub(r"^\$+|\$+$", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(?<!\\)\s*([_^])\s*", r"\1", t)
    t = re.sub(r"(\\[A-Za-z]+\*?)\s+(?=[{\[])", r"\1", t)
    t = re.sub(r"\{\s+", "{", t)
    t = re.sub(r"(?<!\\)\s+\}", "}", t)
    # \boldsymbol 要 MathJax 现场去拉扩展文件（对比页只给了 mathjax.js 本体，拉不到整页公式都排不出来），\bm MathJax 没有：都换成内置的 \pmb
    t = re.sub(r"\\(?:boldsymbol|bm)(?![A-Za-z])", r"\\pmb", t)
    t = re.sub(r"(?<=\d)\s+(?=\d)", "", t)
    t = re.sub(r"(?<=\d)\s+(?=\.\s?\d)", "", t)
    t = re.sub(r"(?<=\d\.)\s+(?=\d)", "", t)
    # \operatorname* {m i n}、\mathrm {B N}：一个字母一个空格是 MinerU 的记号切分，收回去
    t = re.sub(r"(\\(?:operatorname\*?|mathrm|text|textrm|mathbf|mathit|mathsf)\{)((?:\S ){1,30}\S)\}",
               lambda m: m.group(1) + m.group(2).replace(" ", "") + "}", t)
    # * 写成 \ast：对比页去 Markdown 斜体时会把两个 * 之间的东西当斜体吃掉
    t = re.sub(r"(\\[A-Za-z]+\*)|\*", lambda m: m.group(1) or r"\ast ", t)
    t = re.sub(r"\\ast\s+\}", r"\\ast}", t)
    if display:
        t = re.sub(r"\\begin\{(align|eqnarray|gather|multline)\*?\}", r"\\begin{aligned}", t)
        t = re.sub(r"\\end\{(align|eqnarray|gather|multline)\*?\}", r"\\end{aligned}", t)
        t = re.sub(r"\\(?:begin|end)\{(?:equation|displaymath)\*?\}", "", t).strip()
        t = _close_delims(t)
    return t.strip()


def _simple_inline(tex: str) -> str | None:
    """简单的行内公式（单个符号、x_t、α、𝐱_i、L_{1}）→ 文字；对比页能排上下标。复杂的返回 None（保留 $...$）。"""
    t = tex_tidy(tex)
    if not t or len(t) > 48:
        return None

    def unwrap(m):
        cmd, body = m.group(1), m.group(2)
        if cmd in ("mathrm", "text", "textrm", "mathit", "operatorname", "textit", "mathsf", "textsf", "rm", "mathnormal"):
            return body
        if cmd in ("mathbf", "boldsymbol", "bm", "textbf", "mathbfit", "pmb"):
            return "".join(BOLD.get(c, c) for c in body)
        if cmd == "mathcal" and all(c in CAL for c in body):
            return "".join(CAL[c] for c in body)
        if cmd == "mathbb" and all(c in BB for c in body):
            return "".join(BB[c] for c in body)
        return m.group(0)

    t = re.sub(r"\\(mathbf|boldsymbol|bm|pmb|mathbfit)\{\\([A-Za-z]+)\}",       # \pmb{\epsilon} → 𝝐
               lambda m: BOLD.get(GREEK.get(m.group(2), ""), m.group(0)), t)
    for _ in range(3):
        t2 = re.sub(r"\\(mathrm|text|textrm|mathit|operatorname|textit|mathsf|textsf|rm|mathnormal|mathbf|boldsymbol|bm|textbf|mathbfit|pmb|mathcal|mathbb)\{([^{}\\]*)\}", unwrap, t)
        if t2 == t:
            break
        t = t2
    m = re.fullmatch(r"\^\{?([^{}]+?)\}?", t)        # 只有上标：$^{[5]}$ 是引用号，$^{2}$ 写成 ²
    if m:
        body = m.group(1).strip()
        if re.fullmatch(r"[\[［][\d,，\-–\s]+[\]］]", body):
            return body
        if re.fullmatch(r"[\d+\-=()]+", body):
            return body.translate(SUP_DIGITS)
    t = re.sub(r"\\[,;:! ]|~", " ", t)
    t = re.sub(r"\\(max|min|log|ln|exp|sin|cos|tan|arg|lim|sup|inf|det|dim|Pr|deg)(?![A-Za-z])\s*", r"\1", t)
    t = re.sub(r"\\%", "%", t)
    t = re.sub(r"\\([A-Za-z]+)", lambda m: GREEK.get(m.group(1)) or SYMS.get(m.group(1)) or m.group(0), t)
    if "\\" in t:
        return None
    rest = SCRIPT.sub("", t)
    if re.search(r"[_^{}]", rest):
        return None
    t = re.sub(r"\s+([,.;:)])", r"\1", t)
    t = re.sub(r"([(])\s+", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def inline_math(tex: str) -> str:
    s = _simple_inline(tex)
    if s is not None:
        return s
    return "$" + tex_tidy(tex) + "$"


SUP_DIGITS = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")


def _html_inline(s: str) -> str:
    """MinerU 文字里夹的 HTML：上下标、粗体、链接 → 纯文字。"""
    def sup(m):
        body = re.sub(r"\\(?=[*_~#])", "", re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if re.fullmatch(r"[\[［【]?[\d,，\-–\s]+[\]］】]?", body) and re.search(r"[\[［【]", body):
            return body                                # <sup>［1］</sup>：参考文献号，原样
        if re.fullmatch(r"[\d,，\-–+()=\s*∗†‡§]+", body):
            return body.replace("，", ",").translate(SUP_DIGITS).replace(" ", "")
        return "^{" + body + "}" if len(body) <= 16 else body

    def sub(m):
        body = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        base = m.group(1)
        if not body or not base:
            return base + body
        if len(base) == 1:
            return base + "_{" + body + "}" if len(body) <= 16 else base + body
        return base + "_" + body                      # BERT<sub>BASE</sub> → BERT_BASE
    s = re.sub(r"<sup>(.*?)</sup>", sup, s, flags=re.S | re.I)
    s = re.sub(r"(\b[A-Za-z\u0370-\u03FF]{0,24})<sub>(.*?)</sub>", sub, s, flags=re.S | re.I)
    s = re.sub(r"</?(strong|b)>", "**", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<a\s[^>]*>(.*?)</a>", r"\1", s, flags=re.S | re.I)
    s = re.sub(r"</?(u|i|em|span|p|div|font|small|big|s|strike|mark)(\s[^>]*)?>", "", s, flags=re.I)
    s = re.sub(r"\*\*\s*\*\*", "", s)
    return s


MATH_SPAN = re.compile(r"\$`(.+?)`\$|(?<![\\$])\$(?!\$)(.+?)(?<![\\$])\$(?!\$)", re.S)


def text_inline(s: str, cjk: bool = False) -> str:
    """一段正文：HTML 小标签、行内公式、Markdown 转义、全角/空格整理。"""
    if not s:
        return ""
    s = s.replace("\u200b", "").replace("\ufeff", "").replace("\ufffdC", "–")
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "\ufffd", s)   # PDF 字体没给 Unicode 的字形（如 \x0f 当 ε）：标出来，别悄悄吞掉
    parts, last = [], 0
    for m in MATH_SPAN.finditer(s):
        parts.append(("t", s[last:m.start()]))
        parts.append(("m", m.group(1) if m.group(1) is not None else m.group(2)))
        last = m.end()
    parts.append(("t", s[last:]))
    out = []
    for k, v in parts:
        if k == "m":
            out.append(inline_math(v))
            continue
        v = _html_inline(v)
        v = html.unescape(v)
        v = re.sub(r"\\([~*_#`|<>])", r"\1", v)
        v = re.sub(r"\[([^\[\]\n]{1,300})\]\(\1\)", r"\1", v)       # [url](url) → url
        if cjk:
            v = re.sub(r"(?<=\d)\. (?=\d)", ".", v)                 # 方正排版的 "95. 5%"
        out.append(v)
    s = "".join(out)
    s = re.sub(r"[ \t]*\n[ \t]*", " " if not cjk else "", s) if "\n" in s else s
    s = re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])", "", s) if cjk else s
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _guard_start(s: str) -> str:
    """段落开头别被对比页当成标题 / 表格 / 代码块。"""
    if re.match(r"^(#{1,6}\s|\||```)", s):
        return "\u2060" + s
    return s


# ======================================================================
# 表格
# ======================================================================
def _split_md_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, in_math, i = [], [], False, 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2]); i += 2; continue
        if c == "$":
            in_math = not in_math
        if c == "|" and not in_math:
            cells.append("".join(cur)); cur = []
        else:
            cur.append(c)
        i += 1
    cells.append("".join(cur))
    return [c.strip() for c in cells]


def _cell(s: str, cjk: bool) -> str:
    s = s.replace("\\\\\\|", "\\|")
    s = re.sub(r"(?<!\\)\\\|", "|", s) if "$" not in s else s
    # Word 表格里空的引用块、列表这类块级标签（pandoc 原样留在格子里）：只要字，不要标签
    s = re.sub(r"</?(?:blockquote|ul|ol|li|h[1-6])\b[^>]*>", " ", s, flags=re.I)
    s = text_inline(s, cjk)

    def guard_math(m):
        body = m.group(0)
        return body.replace("\\|", "\\Vert ").replace("|", "\\vert ")
    s = re.sub(r"\$[^$]+\$", guard_math, s)
    parts = re.split(r"(\$[^$]+\$)", s)
    s = "".join(p if p.startswith("$") else p.replace("|", "｜") for p in parts)
    return s.replace("\n", " ").strip()


def _html_table_rows(tbl: str) -> list[list[tuple[str, int, int]]]:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S | re.I):
        cells = []
        for m in re.finditer(r"<t([dh])((?:\s[^>]*)?)>(.*?)</t[dh]>", tr, re.S | re.I):
            attrs = m.group(2) or ""
            rs = re.search(r"rowspan\s*=\s*[\"']?(\d+)", attrs)
            cs = re.search(r"colspan\s*=\s*[\"']?(\d+)", attrs)
            body = re.sub(r"</p>\s*<p[^>]*>", " ", m.group(3), flags=re.I)
            cells.append((body.strip(), int(rs.group(1)) if rs else 1, int(cs.group(1)) if cs else 1))
        rows.append(cells)
    return rows


def _grid(rows, merge: bool | None = None) -> list[list[str]]:
    """HTML 表 → 方格。合并单元格的字只留在左上角那格；表头两行（上行横跨几列）合成一行："Dev EM | Dev F1"。
    merge：None 按样子猜（MinerU 的表）；True 头两行就是表头（pandoc 给了两行 <thead>），照合；False 不合。"""
    origin: dict = {}
    grid: dict = {}
    ncol = 0
    for r, cells in enumerate(rows):
        c = 0
        for body, rs, cs in cells:
            while (r, c) in origin:
                c += 1
            rs, cs = max(1, min(rs, 200)), max(1, min(cs, 60))
            for dr in range(rs):
                for dc in range(cs):
                    origin[(r + dr, c + dc)] = (r, c, rs, cs)
                    grid[(r + dr, c + dc)] = body if (dr, dc) == (0, 0) else ""
            c += cs
            ncol = max(ncol, c)
    nrow = max([k[0] for k in grid] or [-1]) + 1
    out = [[grid.get((r, c), "") for c in range(ncol)] for r in range(nrow)]
    plain = lambda s: re.sub(r"<[^>]+>", "", s or "").strip()       # noqa: E731
    if merge is not False and nrow >= (2 if merge else 3) and ncol >= 2:
        wide0 = any(origin.get((0, c), (0, 0, 1, 1))[3] > 1 and origin[(0, c)][0] == 0 for c in range(ncol))
        row1 = [plain(out[1][c]) for c in range(ncol) if origin.get((1, c), (1,))[0] == 1]
        if merge or (wide0 and len({x for x in row1 if x}) >= 2):
            head = []
            for c in range(ncol):
                o = origin.get((0, c))
                a = (grid.get((o[0], o[1]), "") if o else "").strip()
                o1 = origin.get((1, c))
                b = (out[1][c] if o1 and o1[0] == 1 else "").strip()
                head.append(a + " " + b if plain(a) and plain(b) and plain(a) != plain(b) else (a if plain(a) else b))
            out = [head] + out[2:]
    return out


def table_to_md(content: str, cjk: bool, head: str = "auto") -> str:
    """MinerU / pandoc 给的表（HTML 或管道表）→ 对比页认的管道表。
    head：auto 按样子猜表头要不要两行合一行（MinerU）；thead（Word）：pandoc 给了 <thead> 就照它，两行以上才合、
    一行不合；没给（Word 里表格没勾"标题行"，pandoc 就不分表头）仍按样子猜——表单表在这之前已经拆掉，到不了这里。"""
    content = (content or "").strip()
    if not content:
        return ""
    if re.search(r"<table", content, re.I):
        merge = None
        if head == "thead":
            th = re.search(r"<thead\b(.*?)</thead>", content, re.S | re.I)
            if th:
                merge = len(re.findall(r"<tr\b", th.group(1), re.I)) >= 2
        grid = _grid(_html_table_rows(content), merge)
    else:
        lines = [ln for ln in content.splitlines() if ln.strip().startswith("|")]
        grid = []
        for ln in lines:
            cells = _split_md_row(ln)
            if cells and all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in cells if c.strip()) and any(c.strip() for c in cells):
                continue
            grid.append(cells)
        n = max((len(r) for r in grid), default=0)
        grid = [r + [""] * (n - len(r)) for r in grid]
    grid = [[_cell(c, cjk) for c in row] for row in grid]
    grid = [row for row in grid if any(c.strip() for c in row)]
    if not grid:
        return ""
    n = max(len(r) for r in grid)
    keep = [j for j in range(n) if any(j < len(r) and r[j].strip() for r in grid)]
    grid = [[r[j] if j < len(r) else "" for j in keep] for r in grid]
    out = ["| " + " | ".join(grid[0]) + " |", "| " + " | ".join("---" for _ in keep) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in grid[1:]]
    return "\n".join(out)


# ----------------------------------------------------------------------
# Word 的表单表（申报书、开题报告：整篇正文写在一张大表的格子里，格子里还套着表）
# ----------------------------------------------------------------------
class _MD(str):
    """已经是写好的 Markdown（表单表拆出来的键值表）：_docx_post 原样输出，不再过一遍。"""


_TBL_TAG = re.compile(r"<(/?)(table|thead|tbody|tfoot|tr|td|th)\b([^>]*)>", re.I)
_BLK_TAG = re.compile(r"<(/?)(p|table|blockquote|ul|ol|figure|div|h[1-6])\b[^>]*>", re.I)
# 题头的编号：一、 （一） 1. 1.1 1.1.1 第一章（按层级从高到低）
_HEAD_KINDS = (("chap", re.compile(r"^第[一二三四五六七八九十百\d]{1,3}[章篇部]")),
               ("cn1", re.compile(r"^[一二三四五六七八九十]{1,3}[、.．]\s*\S")),
               ("cn2", re.compile(r"^[（(][一二三四五六七八九十]{1,3}[)）]\s*\S")),
               ("d3", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}(?![\d.])\s*\S")),
               ("d2", re.compile(r"^\d{1,2}\.\d{1,2}(?![\d.])\s*\S")),
               ("d1", re.compile(r"^\d{1,2}[.．、](?![\d.])\s*\S")))
_HEAD_ORDER = ("chap", "cn1", "cn2", "d1", "d2", "d3")


def _head_kind(text: str) -> str | None:
    """一行整段加粗的短字是不是带编号的题头（“一、项目简介”“（二）研究内容”“1. 多人……”），是哪一级。"""
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t or len(re.sub(r"\s", "", t)) > 90 or re.search(r"[。！？!?；;]", t):
        return None
    for kind, rx in _HEAD_KINDS:
        if rx.match(t):
            return kind
    return None


def _plain(s: str) -> str:
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", s or "")))


def _html_table_parse(tbl: str) -> tuple[list[list[dict]], int]:
    """最外层那张表 → (行, <thead> 里的行数)。每格 {"html": 格子里的 HTML（套着的表原样留着）, "rs", "cs"}。
    一层层数 <table>：套在格子里的表，它的 <tr> <td> 不算外层的。"""
    rows: list[list[dict]] = []
    nhead, depth, in_head, cell = 0, 0, False, None
    for m in _TBL_TAG.finditer(tbl):
        close, name, attrs = m.group(1) == "/", m.group(2).lower(), m.group(3) or ""
        if name == "table":
            depth += -1 if close else 1
            if depth == 0:
                break
            continue
        if depth != 1:
            continue
        if name == "thead":
            in_head = not close
        elif name == "tr":
            if not close:
                rows.append([])
                nhead += 1 if in_head else 0
        elif not close:
            rs = re.search(r"rowspan\s*=\s*[\"']?(\d+)", attrs)
            cs = re.search(r"colspan\s*=\s*[\"']?(\d+)", attrs)
            cell = {"at": m.end(), "rs": max(1, min(int(rs.group(1)) if rs else 1, 200)),
                    "cs": max(1, min(int(cs.group(1)) if cs else 1, 60))}
        elif cell is not None:
            cell["html"] = tbl[cell.pop("at"):m.start()]
            if not rows:
                rows.append([])
            rows[-1].append(cell)
            cell = None
    return rows, nhead


def _place(rows: list[list[dict]]):
    """格子落到方格上 → (每行从这一行开始的 [(列, 格)], {(行, 列): 盖住它的格子从哪行哪列开始}, 列数)。"""
    taken: dict = {}
    starts, ncol = [], 0
    for r, cells in enumerate(rows):
        c, row = 0, []
        for cell in cells:
            while (r, c) in taken:
                c += 1
            for dr in range(cell["rs"]):
                for dc in range(cell["cs"]):
                    taken[(r + dr, c + dc)] = (r, c)
            row.append((c, cell))
            c += cell["cs"]
            ncol = max(ncol, c)
        starts.append(row)
    return starts, taken, ncol


def _cell_pieces(h: str) -> list[str]:
    """一格的 HTML → 一块一块：顶层的 <p> 各算一段，套着的表、列表、引用块整个算一块，块外面散着的字也算一段。"""
    out, depth, start, last = [], 0, 0, 0
    for m in _BLK_TAG.finditer(h or ""):
        if m.group(0).endswith("/>"):
            continue
        if m.group(1) != "/":
            if depth == 0:
                if h[last:m.start()].strip():
                    out.append(h[last:m.start()])
                start = m.start()
            depth += 1
        elif depth > 0:
            depth -= 1
            if depth == 0:
                out.append(h[start:m.end()])
                last = m.end()
    if (h or "")[last:].strip():
        out.append(h[last:])
    return [p for p in out if _plain(p) or re.search(r"<(img|table)\b", p, re.I)]


def _piece_blocks(piece: str) -> list[str]:
    """格子里的一块 → _docx_post 认的原始块（段落是带行内 HTML 的一行字，图是 <img>，套着的表原样）。"""
    s = piece.strip()
    m = re.match(r"<(table|figure)\b", s, re.I)
    if m:
        return [s]
    m = re.match(r"<(blockquote|div)\b[^>]*>(.*)</\1>$", s, re.S | re.I)
    if m:
        return [b for p in _cell_pieces(m.group(2)) for b in _piece_blocks(p)]
    m = re.match(r"<(ul|ol)\b[^>]*>(.*)</\1>$", s, re.S | re.I)
    if m:
        items = re.findall(r"<li\b[^>]*>(.*?)</li>", m.group(2), re.S | re.I)
        lines = [("- " if m.group(1).lower() == "ul" else f"{i}. ") + re.sub(r"\s*\n\s*", " ", re.sub(r"</?p\b[^>]*>", " ", x)).strip()
                 for i, x in enumerate(items, 1)]
        return ["\n".join(ln for ln in lines if _plain(ln[2:]))] if lines else []
    m = re.match(r"<(h[1-6])\b[^>]*>(.*)</\1>$", s, re.S | re.I)
    if m:
        return ["#" * int(m.group(1)[1]) + " " + re.sub(r"\s*\n\s*", " ", m.group(2)).strip()]
    m = re.match(r"<p\b[^>]*>(.*)</p>$", s, re.S | re.I)
    inner = (m.group(1) if m else s).strip()
    if re.fullmatch(r"(?:<img\b[^>]*>\s*)+", inner, re.I):
        return [inner]
    inner = re.sub(r"\s*\n\s*", " ", inner).strip()
    return [inner] if _plain(inner) or "<img" in inner else []


def _bold_head(piece: str) -> str | None:
    """这一块是不是整段加粗、带编号的题头（“<p><strong>一、项目简介</strong></p>”）。"""
    m = re.fullmatch(r"\s*(?:<p\b[^>]*>)?\s*<(strong|b)>(.*?)</\1>\s*(?:</p>)?\s*", piece or "", re.S | re.I)
    return _head_kind(_plain(m.group(2))) if m else None


def _cell_is_block(cell: dict, ncol: int) -> bool:
    """这一格装的是正文（套着表、有图、以带编号的题头开头、好几段长字），不是表单里的一个空。
    带编号的加粗题头只在整行通栏、或者后面还跟着正文时才算：数据表里加粗的行名（“1. OLS”）不算。"""
    h = cell["html"]
    if re.search(r"<(table|img)\b", h, re.I):
        return True
    pieces = _cell_pieces(h)
    if pieces and _bold_head(pieces[0]) and (cell["cs"] >= ncol or (len(pieces) >= 2 and len(_plain(h)) >= 40)):
        return True
    return len(pieces) >= 2 and len(_plain(h)) >= 150


def _is_layout(rows: list[list[dict]]) -> bool:
    ncol = _place(rows)[2]
    return any(_cell_is_block(c, ncol) for row in rows for c in row)


def _kv_md(rows: list[list[str]], cjk: bool) -> _MD | None:
    """表单里的一串短格子（键值行）→ 一张紧凑的管道表：每行从左往右只排有格子开头的位置，被横向合并的空位不占列；
    被上面竖着合并盖住的位置留一个空格，下面的行才对得齐。"""
    def one(x):                                   # 格子里分段、换行（<br />）都并成一行，中间留空格
        x = re.sub(r"</p>\s*<p[^>]*>|<br\s*/?>", " ", x, flags=re.I)
        x = re.sub(r"[ \t]*\n[ \t]*", " ", x)
        x = re.sub(r"(<(strong|b)>)\s+", r"\1", x, flags=re.I)      # “<strong><br />项目名称</strong>”：粗体里头不留空格
        return re.sub(r"\s+(</(?:strong|b)>)", r"\1", x, flags=re.I)
    grid = [[_cell(one(x), cjk) for x in row] for row in rows]
    grid = [r for r in grid if any(c.strip() for c in r)]
    if not grid:
        return None
    n = max(len(r) for r in grid)
    grid = [r + [""] * (n - len(r)) for r in grid]
    keep = [j for j in range(n) if any(r[j].strip() for r in grid)]
    grid = [[r[j] for j in keep] for r in grid]
    out = ["| " + " | ".join(grid[0]) + " |", "| " + " | ".join("---" for _ in keep) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in grid[1:]]
    return _MD("\n".join(out))


def _layout_blocks(rows: list[list[dict]], cjk: bool) -> list[str]:
    """表单表拆开：写正文的那几行（整行通栏的长字、套着表或图、带编号的题头）拆成一段段，
    中间连着的短键值行合成一张紧凑的管道表。"""
    starts, taken, ncol = _place(rows)
    out: list[str] = []
    run: list[list[str]] = []

    def flush():
        if run:
            t = _kv_md(run, cjk)
            if t:
                out.append(t)
            run.clear()

    for r, row in enumerate(starts):
        prose = any(_cell_is_block(cell, ncol) or (cell["cs"] >= ncol > 1 and len(_plain(cell["html"])) >= 60)
                    for _, cell in row)
        if not prose:
            at = {c: cell for c, cell in row}
            packed = []
            for c in range(ncol):
                if c in at:
                    packed.append(at[c]["html"])
                elif taken.get((r, c), (r, -1))[0] < r and taken[(r, c)][1] == c:
                    packed.append("")                 # 上面竖着合并下来的：留个空位
            run.append(packed)
            continue
        flush()
        texts = [cell for _, cell in row if _plain(cell["html"]) or re.search(r"<(img|table)\b", cell["html"], re.I)]
        for cell in texts:
            pieces = _cell_pieces(cell["html"])
            if len(texts) > 1 and not _cell_is_block(cell, ncol) and len(_plain(cell["html"])) <= 30 \
                    and len(pieces) == 1 and not _bold_head(pieces[0]):
                out.append("<strong>" + re.sub(r"</?(?:p|strong|b)\b[^>]*>", "", pieces[0]).strip() + "</strong>")
                continue                              # 正文左边那格短标签（“项目简介”）：加粗单独一行
            for p in pieces:
                out += _piece_blocks(p)
    flush()
    return out


def _unwrap_tables(blocks: list[str], cjk: bool) -> list[str]:
    """把表单表换成拆开后的那些块（拆出来的块里还套着表单表的，接着拆）；数据表原样留着。"""
    out: list[str] = []
    for b in blocks:
        s = b.strip()
        if not isinstance(b, _MD) and re.match(r"<table\b", s, re.I):
            rows, _ = _html_table_parse(s)
            if rows and _is_layout(rows):
                out += _unwrap_tables(_layout_blocks(rows, cjk), cjk)
                continue
        out.append(b)
    return out


def _promote_heads(out: list[str]) -> list[str]:
    """整段加粗、带编号的短行（“**一、项目简介**”“**（二）研究内容**”“**1. 多人……**”）写成标题，小节定位、目录才有东西。
    Word 里本来就用了标题样式（pandoc 给了两个以上 # 标题）的不动。级别按出现过的编号种类从高到低排成 ## / ### / ####。"""
    if sum(1 for x in out if re.match(r"#{1,6}\s", x)) >= 2:
        return out
    cand = {}
    for i, x in enumerate(out):
        if isinstance(x, _MD):
            continue
        m = re.fullmatch(r"\*\*([^*\n]+?)\*\*", x.strip())
        k = _head_kind(m.group(1)) if m else None
        if k:
            cand[i] = (k, m.group(1).strip())
    kinds = [k for k in _HEAD_ORDER if any(v[0] == k for v in cand.values())]
    level = {k: min(2 + j, 4) for j, k in enumerate(kinds)}
    return [("#" * level[cand[i][0]] + " " + cand[i][1]) if i in cand else x for i, x in enumerate(out)]


# ======================================================================
# 图注 / 表注
# ======================================================================
FIG_CAP = re.compile(r"^\s*(?:图|Figure|FIGURE|Fig\.?|FIG\.?)\s*(\d+)(?=\s*[:：.．、\s　]|$|(?=[^\d(（a-z]))")
TAB_CAP = re.compile(r"^\s*(?:表|Table|TABLE|Tab\.?|TAB\.?)\s*(\d+)(?=\s*[:：.．、\s　]|$|(?=[^\d(（a-z]))")


def _cap_kind(text: str):
    t = re.sub(r"<[^>]+>|\*\*", "", text or "")
    m = FIG_CAP.match(t)
    if m:
        return "fig", int(m.group(1))
    m = TAB_CAP.match(t)
    if m:
        return "tab", int(m.group(1))
    return None, None


# ======================================================================
# MinerU
# ======================================================================
STAGES = {"layout": "版面分析", "vlm": "识别公式、表格和文字", "ocr-det": "找文字", "ocr-rec": "认文字",
          "mfr": "认公式", "mfd": "找公式", "table": "认表格"}
PROG = re.compile(r"([A-Za-z][A-Za-z \-]{1,40}?)\s*:\s*(\d+)%\|[^|]*\|\s*(\d+)/(\d+)")


def _kill_tree(proc) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=20,
                           creationflags=NO_WINDOW)
        else:
            proc.kill()
    except Exception:
        pass


def run_mineru(pdf: Path, out_dir: Path, pages: int, progress=None, tier: str | None = None,
               timeout: int | None = None) -> Path:
    """mineru-kit parse → out_dir/res（structured_content.json + images/）。失败抛 ConvertError。"""
    kit = mineru_kit()
    if not kit:
        raise ConvertError("没装 MinerU（.venv-mineru）")
    tier = tier or TIER
    timeout = timeout or TIMEOUT
    src = out_dir / "input.pdf"                     # 拷一份到 ASCII 路径下：中文名、空格、长路径都不怕
    shutil.copyfile(pdf, src)
    z = out_dir / "out.zip"
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"))}
    env.update({"MINERU_TELEMETRY_DISABLED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                "COLUMNS": "160", "NO_COLOR": "1"})
    env.setdefault("MINERU_MODEL_SOURCE", "modelscope")
    cmd = [str(kit), "parse", str(src), "-o", str(z), "-f", "zip", "--tier", tier]
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=str(out_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW)
    tail: list[str] = []
    state = {"last": 0.0}

    def reader():
        buf = b""
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-40]
                m = PROG.search(line)
                if m and progress and time.time() - state["last"] > 0.5:
                    name = m.group(1).strip().lower()
                    key = next((k for k in STAGES if name.startswith(k)), None)
                    label = STAGES.get(key, m.group(1).strip())
                    state["last"] = time.time()
                    try:
                        progress(stage=label, done=int(m.group(3)), total=int(m.group(4)),
                                 text=f"MinerU {label} {m.group(3)}/{m.group(4)}（共 {pages} 页，已用 {time.time() - t0:.0f} 秒）")
                    except Exception:
                        pass

    th = threading.Thread(target=reader, name="mineru-out", daemon=True)
    th.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.wait(timeout=15)        # 等它真退出、放开临时文件，外面才删得掉
        except subprocess.TimeoutExpired:
            pass
        raise ConvertError(f"MinerU 超过 {timeout} 秒没转完")
    th.join(timeout=5)
    if proc.returncode != 0 or not z.exists():
        msg = " | ".join(x for x in tail[-6:] if "DEBUG" not in x)[-600:]
        raise ConvertError(f"MinerU 失败（退出码 {proc.returncode}）：{msg}")
    res = out_dir / "res"
    with zipfile.ZipFile(z) as zf:
        for n in zf.namelist():                       # 只解出正常的相对路径
            if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                continue
            zf.extract(n, res)
    if not (res / "structured_content.json").exists():
        raise ConvertError("MinerU 的结果里没有 structured_content.json")
    return res


# ---------- structured_content.json → Markdown ----------
def _overlap_x(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def _vgap(cap, blk) -> float:
    """图注到图块的竖直距离（图注在下方为正常，在上方也算），水平不重叠时加罚。"""
    if cap[1] >= blk[3] - 0.01:
        d = cap[1] - blk[3]
    elif cap[3] <= blk[1] + 0.01:
        d = blk[1] - cap[3]
    else:
        d = 0.0
    if _overlap_x(cap, blk) <= 0:
        d += 0.5
    return max(d, 0.0)


def _heading_level(t: str, lvl) -> int:
    s = t.strip()
    m = re.match(r"^(\d+(?:\s*\.\s*\d+)*)\.?(?:\s|　|$)", s)
    if m:
        return min(1 + len(re.findall(r"\d+", m.group(1))), 6)
    m = re.match(r"^([A-Z])((?:\s*\.\s*\d+)*)\.?(?:\s|　)", s)
    if m:
        return min(2 + len(re.findall(r"\d+", m.group(2))), 6)
    if re.match(r"^[一二三四五六七八九十]+\s*[、.．]", s):
        return 2
    if re.match(r"^[（(][一二三四五六七八九十]+[)）]", s):
        return 3
    try:
        return max(2, min(int(lvl or 2), 6))
    except (TypeError, ValueError):
        return 2


def _norm_heading(t: str) -> str:
    t = re.sub(r"^(\d+(?:\s*\.\s*\d+)+)", lambda m: re.sub(r"\s+", "", m.group(1)), t.strip())
    return re.sub(r"[\s　]+", " ", t).strip()


def _render_clip(doc, page_idx: int, box, dst_noext: Path) -> tuple[str, int, int] | None:
    """从 PDF 原页面把图的区域截出来（矢量图也清楚）；照片多、PNG 太大就存 JPEG。"""
    import pymupdf as fitz  # PyMuPDF（新版不再用 fitz 这个名字）
    try:
        page = doc[page_idx]
    except Exception:
        return None
    r = page.rect
    x0, y0, x1, y1 = box
    pad = 3
    clip = fitz.Rect(r.x0 + x0 * r.width - pad, r.y0 + y0 * r.height - pad,
                     r.x0 + x1 * r.width + pad, r.y0 + y1 * r.height + pad) & r
    if clip.is_empty or clip.width < 8 or clip.height < 8:
        return None
    zoom = min(2.5, 2000 / max(clip.width, 1))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    data, ext = pix.tobytes("png"), ".png"
    if len(data) > 1_500_000:
        data, ext = pix.tobytes("jpg", jpg_quality=85), ".jpg"
    out = dst_noext.with_suffix(ext)
    out.write_bytes(data)
    return out.name, pix.width, pix.height


SUBCAP = re.compile(r"^\s*[(（]\s*[a-zA-Z]|^\s*[(（]\s*(?:[ivx]{1,4}|\d{1,2})\s*[)）]")
TEXT_FIG_CAP = re.compile(r"^\s*(?:图|Figure|FIGURE|Fig\.?|FIG\.?)\s*\d+\s*(?:[:：.．]|　|\s{2,})")
TEXT_TAB_CAP = re.compile(r"^\s*(?:表|Table|TABLE|Tab\.?|TAB\.?)\s*\d+\s*(?:[:：.．]|　|\s{2,})")


def _caption_like(s: str, kind: str) -> bool:
    """一段正文像不像图注 / 表注："图 3：…" "Figure 3." "图3　…"，或很短的中文 "图 3 系统结构"；
    "图 9 展示了……，……" 这种正文不算。"""
    s = re.sub(r"<[^>]+>|\*\*", "", s or "").strip()
    if (TEXT_FIG_CAP if kind == "fig" else TEXT_TAB_CAP).match(s):
        return True
    return bool(len(s) <= 50 and CJK.search(s) and not re.search(r"[，。；]", s[:-1])
                and not re.search(r"^\S+\s*\d+\s*(所示|展示|给出|表明|显示|可以|中|的|是|为|和|与)", s))


def _direction(cap, blk) -> str:
    if cap[1] >= blk[3] - 0.01:
        return "below"
    if cap[3] <= blk[1] + 0.01:
        return "above"
    return "inside"


def _dominant_dirs(pages) -> dict:
    """这篇论文的图注 / 表注习惯写在图表的下方还是上方（按 MinerU 自己挂的统计）。"""
    cnt = {"fig": {"below": 0, "above": 0}, "tab": {"below": 0, "above": 0}}
    for p in pages:
        for b in p.get("blocks") or []:
            t = b.get("type")
            if t not in ("image", "chart", "table") or not b.get("bbox"):
                continue
            for c in b.get("captions") or []:
                k, _ = _cap_kind(c.get("content") or "")
                want = "tab" if t == "table" else "fig"
                if k and c.get("bbox"):
                    d = _direction(c["bbox"], b["bbox"])
                    if d in ("below", "above") and (k == want or (t == "table" and k == "fig")):
                        cnt[want][d] += 1
    return {k: ("above" if v["above"] > v["below"] else "below") if (v["above"] or v["below"])
            else ("below" if k == "fig" else "above") for k, v in cnt.items()}


def _page_captions(blocks, figblocks, tabblocks):
    """这一页所有的图注 / 表注：[{kind,num,bbox,text,blk,textblock}]，以及每个图块的子图注区域（截图时一起截进去）。
    同一块下没号的行：跟在带号图注后面的是图注的续行，前面的（"(a) …"）是子图注。"""
    caps, subboxes = [], {}
    for i, b in enumerate(blocks):
        t = b.get("type")
        if t in ("image", "chart", "table", "code"):
            cur = None
            for c in b.get("captions") or []:
                text = c.get("content") or ""
                bb = c.get("bbox") or b.get("bbox")
                k, n = _cap_kind(text)
                if k == "fig" and t == "table":        # 有的论文表也叫 Figure：挂在表上就当表注
                    k = "tab"
                if k:
                    cur = {"kind": k, "num": n, "bbox": list(bb), "text": text, "blk": i, "textblock": False}
                    caps.append(cur)
                elif cur is not None and not SUBCAP.match(text):
                    cur["text"] += ("" if CJK.match(text[:1]) else " ") + text
                    cur["bbox"] = [min(cur["bbox"][0], bb[0]), min(cur["bbox"][1], bb[1]),
                                   max(cur["bbox"][2], bb[2]), max(cur["bbox"][3], bb[3])]
                elif t in ("image", "chart") and bb:
                    subboxes.setdefault(i, []).append(bb)
                elif t == "table":
                    caps.append({"kind": None, "num": None, "bbox": bb, "text": text, "blk": i, "textblock": False})
        elif t == "text" and isinstance(b.get("content"), str) and len(b["content"]) < 1200 and b.get("bbox"):
            s = re.sub(r"<[^>]+>|\*\*", "", b["content"])
            for kind, targets in (("fig", figblocks), ("tab", tabblocks)):
                k, n = _cap_kind(s)
                if k != kind or not targets or not _caption_like(s, kind):
                    continue
                if min(_vgap(b["bbox"], blocks[j]["bbox"]) for j in targets) < 0.08:
                    caps.append({"kind": k, "num": n, "bbox": b["bbox"], "text": b["content"], "blk": i, "textblock": True})
    return caps, subboxes


def _box_dist(a, b) -> float:
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    return dx + dy


def _assign_captions(blocks, caps, figblocks, tabblocks, dominant):
    """带号的图注 / 表注各认一个块：代价 = 竖直距离 + 方向不合本文习惯的罚分；贪心配对，一块一个号。
    剩下没号的图块是子图，挂到同页最近的带号图上（太远的算孤图，不放）。"""
    numcaps: dict = {}
    for c in caps:
        if c["kind"]:
            numcaps.setdefault((c["kind"], c["num"]), []).append(c)
    owner_fig: dict = {}
    owner_tab: dict = {}
    for kind, targets in (("fig", figblocks), ("tab", tabblocks)):
        pairs = []
        for (k, num), cl in numcaps.items():
            if k != kind or not targets:
                continue
            prim = next((c for c in cl if not c["textblock"]), cl[0])
            for j in targets:
                bb = blocks[j]["bbox"]
                cost = _vgap(prim["bbox"], bb)
                d = _direction(prim["bbox"], bb)
                if d != "inside" and d != dominant.get(kind):
                    cost += 0.06
                if j == prim["blk"]:
                    cost -= 0.004
                pairs.append((cost, num, j))
        taken_num, taken_blk = set(), set()
        for cost, num, j in sorted(pairs):
            if num in taken_num or j in taken_blk or cost > 0.45:
                continue
            taken_num.add(num)
            taken_blk.add(j)
            if kind == "fig":
                owner_fig[j] = num
            else:
                owner_tab[j] = numcaps[(kind, num)]
    changed = True
    while changed:                     # 一排子图：挨着已归属的那张就跟它走（可以一张接一张传过去）
        changed = False
        for j in figblocks:
            if j in owner_fig or not owner_fig:
                continue
            near = min(owner_fig, key=lambda a: _box_dist(blocks[j]["bbox"], blocks[a]["bbox"]))
            if _box_dist(blocks[j]["bbox"], blocks[near]["bbox"]) < 0.12:
                owner_fig[j] = owner_fig[near]
                changed = True
    return owner_fig, owner_tab, numcaps


def structured_to_md(sc: dict, pdf: Path, figdir: Path, title: str = "") -> tuple[str, dict]:
    """MinerU 的 structured_content → (Markdown, 统计)。图截好存进 figdir，figures.json 也写好。"""
    import pymupdf as fitz  # PyMuPDF（新版不再用 fitz 这个名字）
    pages = sc.get("pages") or []
    alltext = "".join(b.get("content") or "" for p in pages for b in p.get("blocks") or [] if isinstance(b.get("content"), str))
    cjk = len(CJK.findall(alltext)) / max(1, len(re.sub(r"\s", "", alltext))) > 0.1
    stats = {"pages": len(pages), "figures": 0, "tables": 0, "equations": 0, "inline_latex": 0,
             "orphan_images": 0, "empty_tables": 0, "lang": "zh" if cjk else "en"}
    figdir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf))
    figmeta: dict = {}
    used_fig: set = set()
    items: list[dict] = []                 # {"k": text|head|eq|fig|tab|note|code, "s": str}

    def emit(kind, s, **kw):
        items.append(dict(k=kind, s=s, **kw))

    def last_text_idx():
        for i in range(len(items) - 1, -1, -1):
            if items[i]["k"] in ("text", "ref"):
                return i
            if items[i]["k"] not in ("fig", "tab", "note"):
                return None
        return None

    def join(a: str, b: str) -> str:
        if not a:
            return b
        if CJK.match(a[-1:]) or CJK.match(b[:1]) or re.match(r"[，。；：、）」』》]", b[:1]):
            return a + b
        if a.endswith("-") and b[:1].islower():
            return a + b
        return a + " " + b

    seen_title = False
    dominant = _dominant_dirs(pages)
    for p in pages:
        pi = p.get("page_idx", 0)
        blocks = [b for b in (p.get("blocks") or []) if isinstance(b, dict)]
        # ---- 这一页的图注 / 表注，归到图块 / 表块上 ----
        figblocks = [i for i, b in enumerate(blocks) if b.get("type") in ("image", "chart")]
        tabblocks = [i for i, b in enumerate(blocks) if b.get("type") == "table"]
        caps, subboxes = _page_captions(blocks, figblocks, tabblocks)
        owner_fig, owner_tab, numcaps = _assign_captions(blocks, caps, figblocks, tabblocks, dominant)
        if os.environ.get("ZUHUI_PDFCONV_DEBUG"):
            print(f"[p{pi}] figblocks={figblocks} tabblocks={tabblocks} owner_fig={owner_fig} "
                  f"owner_tab={ {k: [(c['kind'], c['num']) for c in v] for k, v in owner_tab.items()} } "
                  f"numcaps={ {k: [c['blk'] for c in v] for k, v in numcaps.items()} }", file=sys.stderr)
        owned_caps = [c for v in owner_tab.values() for c in v]
        owned_caps += [c for (k, n), cl in numcaps.items() if k == "fig" and n in owner_fig.values() for c in cl]
        used_textcaps = {c["blk"] for c in owned_caps if c["textblock"]}
        # ---- 截图：同一个图号的块（含子图注）合成一个区域 ----
        groups: dict = {}
        for j, num in owner_fig.items():
            groups.setdefault(num, []).append(j)
        fig_first = {}
        for num, js in groups.items():
            if num in used_fig:
                stats["orphan_images"] += len(js)
                continue
            boxes = [blocks[j]["bbox"] for j in js] + [bb for j in js for bb in subboxes.get(j, [])]
            box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
            got = _render_clip(doc, pi, box, figdir / f"fig{num}")
            if not got:
                continue
            used_fig.add(num)
            capl = numcaps.get(("fig", num), [])
            captext = " ".join(re.sub(r"\s+", " ", c["text"]).strip() for c in capl)
            figmeta[str(num)] = {"file": got[0], "kind": "image", "caption": text_inline(captext, cjk),
                                 "page": pi + 1, "width": got[1], "height": got[2], "source": "mineru"}
            fig_first[min(js)] = (num, capl)
        stats["orphan_images"] += len([j for j in figblocks if j not in owner_fig])

        # ---- 按阅读顺序输出 ----
        for i, b in enumerate(blocks):
            t = b.get("type")
            c = b.get("content")
            if isinstance(c, list):
                c = "\n".join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in c)
            c = c if isinstance(c, str) else ""
            if t in ("page_number", "header", "footer", "page_header", "page_footer", "aside_text", "discarded"):
                continue
            if i in used_textcaps:
                continue
            if t in ("image", "chart"):
                if i in fig_first:
                    num, capl = fig_first[i]
                    emit("fig", f"[image: 图 {num}]")
                    for cc in capl:
                        emit("cap", _guard_start(text_inline(cc["text"], cjk)))
                    stats["figures"] += 1
                continue
            if t == "table":
                capl = owner_tab.get(i, [])
                if not capl:   # MinerU 自己挂的、没号的表注也留着
                    capl = [x for x in caps if x["blk"] == i and x["kind"] is None]
                for cc in capl:
                    emit("cap", _guard_start(text_inline(cc["text"], cjk)))
                md = table_to_md(c, cjk)
                if md:
                    emit("tab", md)
                    stats["tables"] += 1
                else:
                    stats["empty_tables"] += 1
                    emit("text", f"（这里有一张表没认出来，见原 PDF 第 {pi + 1} 页）")
                for fn in b.get("footnotes") or []:
                    emit("text", _guard_start(text_inline(fn.get("content") or "", cjk)))
                continue
            if t == "equation":
                tex = tex_tidy(c, display=True)
                if tex:
                    emit("eq", "```latex\n" + tex + "\n```", tagged="\\tag{" in tex)
                    stats["equations"] += 1
                continue
            if t == "code":
                for cc in b.get("captions") or []:
                    emit("cap", _guard_start(text_inline(cc.get("content") or "", cjk)))
                if (b.get("sub_type") or "") == "algorithm" or "$" in c:
                    lines = [text_inline(ln, cjk) for ln in c.splitlines() if ln.strip()]
                    lines = [("\u2060" + ln if re.match(r"^(#{1,6}\s|\||```)", ln) else ln) for ln in lines]
                    if lines:
                        emit("text", "\n".join(lines))
                else:
                    body = c.strip()
                    if not body.startswith("```"):
                        body = "```text\n" + body + "\n```"
                    emit("code", body)
                continue
            if t == "doc_title":
                s = text_inline(c, cjk)
                if not s:
                    continue
                if not seen_title:
                    emit("head", "# " + s)
                    seen_title = True
                else:
                    emit("text", "**" + s + "**")
                continue
            if t in ("paragraph_title", "title"):
                s = _norm_heading(text_inline(c, cjk))
                if s:
                    emit("head", "#" * _heading_level(s, b.get("level")) + " " + s)
                continue
            if t == "page_footnote":
                s = text_inline(c, cjk)
                if not s:
                    continue
                m = re.match(r"^\s*(\d{1,3})(?=\D)", s)
                if m and not re.match(r"^\s*\d{1,3}[.,]\d", s):
                    s = m.group(1).translate(SUP_DIGITS) + " " + s[m.end():].lstrip()
                elif items and items[-1]["k"] == "note" and not re.match(r"^[⁰¹²³⁴⁵⁶⁷⁸⁹*∗†]", s):
                    items[-1]["s"] = join(items[-1]["s"], s)
                    continue
                emit("note", _guard_start(s))
                continue
            s = text_inline(c, cjk)
            if not s:
                continue
            if t == "text" and re.fullmatch(r"[(（]\s*\d{1,3}\s*[)）]", s):     # 单独一行的式号 "(2)"
                for it in reversed(items):
                    if it["k"] == "eq":
                        if not it.get("tagged"):
                            num = re.sub(r"\D", "", s)
                            it["s"] = it["s"][:-4] + f" \\tag{{{num}}}" + it["s"][-4:]
                            it["tagged"] = True
                        break
                    if it["k"] in ("text", "head"):
                        break
                continue
            kind = "ref" if t == "ref_text" else "text"
            if b.get("continues_prev"):
                j = last_text_idx()
                if j is not None:
                    items[j]["s"] = join(items[j]["s"], s)
                    continue
            emit(kind, _guard_start(s))
    doc.close()
    if not seen_title and title:
        items.insert(0, {"k": "head", "s": "# " + title})
    for it in items:
        if it["k"] in ("text", "ref", "cap", "note", "head"):
            stats["inline_latex"] += len(re.findall(r"(?<!\\)\$[^$]+\$", it["s"]))
    md = "\n\n".join(it["s"] for it in items if it["s"].strip()).rstrip() + "\n"
    _write_figmeta(figdir, figmeta)
    return md, stats


def _write_figmeta(figdir: Path, meta: dict) -> None:
    if meta:
        (figdir / "figures.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def pdf_page_count(pdf: Path) -> int:
    import pymupdf as fitz  # PyMuPDF（新版不再用 fitz 这个名字）
    with fitz.open(str(pdf)) as d:
        return d.page_count


def convert_pdf(pdf: Path, stage: Path, progress=None) -> tuple[str, dict]:
    """PDF → (Markdown, 统计)，图写进 stage/figures。失败抛 ConvertError。"""
    t0 = time.time()
    pages = pdf_page_count(pdf)
    work = Path(tempfile.mkdtemp(prefix="bloom-mineru-"))
    try:
        if progress:
            progress(stage="启动 MinerU", done=0, total=pages, text=f"MinerU 正在加载模型（共 {pages} 页）")
        res = run_mineru(pdf, work, pages, progress=progress)
        sc = json.loads((res / "structured_content.json").read_text(encoding="utf-8"))
        if progress:
            progress(stage="写 Markdown", done=pages, total=pages, text="整理公式、表格和图")
        md, stats = structured_to_md(sc, pdf, stage / "figures", title=pdf.stem)
        stats.update(engine="mineru", tier=TIER, secs=round(time.time() - t0, 1))
        return md, stats
    except ConvertError:
        raise
    except Exception as e:      # MinerU 的结果格式变了之类：也退回 PyMuPDF
        raise ConvertError(f"MinerU 结果整理失败：{type(e).__name__}: {e}") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ======================================================================
# Word（pandoc）
# ======================================================================
# 填空线（2026-11-15）：申报书封面"项目名称 ______"这种，Word 里是下划线的空格（或画上去的一条直线），值写在线上。
# pandoc 把只有空格的下划线丢掉、画的线（VML / DrawingML 直线）也丢掉，转出来只剩"项目名称"，或者标签和值粘在一起
# （"项目名称组会论文实时修订助手"）。转之前先在 document.xml 里改写：
#   · 一行一个"标签 + 线上的值"（段落标记带下划线、有画的直线，或者下划线的值跟在标签后面）：写成"标签：值"，
#     线上是空的写成"标签：＿＿＿＿＿＿＿＿"；
#   · 正文里只有空格的下划线（"签名______"）：换成同样宽的一串"＿"，看得见。
FILL = "＿"
_W_P = re.compile(r"<w:p\b[^>]*>(?:(?!<w:p\b).)*?</w:p>", re.S)          # 最里层的段落（文本框里的段落单独算）
_W_R = re.compile(r"<w:r\b[^>]*>.*?</w:r>", re.S)
_W_U = re.compile(r'<w:u\b[^>]*\bw:val="([^"]+)"')
_LINE_SHAPE = re.compile(r'<v:line\b|o:connectortype="|type="#_x0000_t3[2-9]"|o:spt="(?:20|3[2-9])"|prst="(?:line|straightConnector1)"')
_CJK_GAP = re.compile(r"(?<=[　-〿一-鿿＀-￯])\s+(?=[　-〿一-鿿＀-￯])")


def _run_text(r: str) -> str:
    """一个 run 里的字（XML 转义原样留着）：<w:t> 的字，<w:tab/> 当 \\t。"""
    out = []
    for m in re.finditer(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:tab/>|<w:br/>", r, re.S):
        out.append(m.group(1) if m.group(1) is not None else ("\t" if m.group(0) == "<w:tab/>" else " "))
    return "".join(out)


def _run_underlined(r: str) -> bool:
    rp = re.search(r"<w:rPr>.*?</w:rPr>", r, re.S)
    m = _W_U.search(rp.group(0)) if rp else None
    return bool(m and m.group(1) not in ("none", "0", "false"))


def _blank_width(s: str) -> int:
    """一段空白有多宽（半角空格 1、全角空格 2、制表符 4）→ 画几个"＿"（一个"＿"两个半角宽，至少 3 个）。"""
    w = sum(4 if c == "\t" else 2 if c == "　" else 1 for c in s)
    return max(3, (w + 1) // 2)


def _tidy_label(s: str) -> str:
    s = html.unescape(s).replace("\t", " ").strip()
    s = _CJK_GAP.sub("", s)                     # "学    号" → "学号"（Word 里靠空格撑宽的标签）
    return html.escape(re.sub(r"\s+", " ", s), quote=False)


def _field_line(ppr: str, runs: list[str]) -> str | None:
    """一行"标签 + 线上的值"改写成一个 run 的"标签：值"（值是空的画一串"＿"）。不像填空行就返回 None。"""
    texts = [_run_text(r) for r in runs]
    full = html.unescape("".join(texts))
    if not full.strip() and not any(_LINE_SHAPE.search(r) for r in runs):
        return None
    if len(full.strip()) > 80 or re.search(r"[。！？；]", full):
        return None                             # 是一句话、一段正文，不是填空行
    has_line = any(_LINE_SHAPE.search(r) for r in runs)
    para_u = bool(_W_U.search(ppr)) and _W_U.search(ppr).group(1) not in ("none", "0", "false")
    under = [_run_underlined(r) for r in runs]
    first_u = next((i for i, u in enumerate(under) if u and (texts[i].strip() or "\t" in texts[i])), None)
    if first_u is not None:
        # 下划线的值得是这一行的尾巴：值后面不能再有没下划线的字（那是正文里划了几个字，不是填空行）
        if any(not under[i] and texts[i].strip() for i in range(first_u + 1, len(runs))):
            return None
        label = "".join(texts[:first_u])
        value = "".join(texts[first_u:])
        gap = bool(re.search(r"\s$", html.unescape(label))) or "\t" in "".join(texts[first_u:first_u + 1])
        if not (para_u or has_line or gap):
            return None
    elif has_line or (para_u and re.search(r"\S[ \t　]{4,}$", full)):
        # 画的线（封面模板；线有时锚在相邻那一段上，这一段只剩段落标记的下划线和一串空格）：标签是第一截字，
        # 隔开一段空白后的是值。两个字靠空格撑宽的标签（"学    号"）先并起来
        joined = re.sub(r"^(\s*[一-鿿])[ \t　]+([一-鿿])(?=[ \t　]|$)", r"\1\2", "".join(texts))
        m = re.match(r"\s*(\S(?:.*?\S)??)(?:\s{2,}|\t|$)(.*)$", joined, re.S)
        if not m:
            return None
        label, value = m.group(1), m.group(2)
    else:
        return None
    label, value = _tidy_label(label), html.unescape(value).replace("\t", " ").strip()
    value = html.escape(value, quote=False)
    if not value:
        value = FILL * 8
    if label and not re.search(r"[:：]$", html.unescape(label)):
        label += "："
    rpr = next((m.group(0) for r in runs for m in [re.search(r"<w:rPr>.*?</w:rPr>", r, re.S)] if m), "")
    rpr = _W_U.sub('<w:u w:val="none"', rpr)
    return f'{ppr}<w:r>{rpr}<w:t xml:space="preserve">{label}{value}</w:t></w:r>'


def _fill_blanks(runs: list[str]) -> tuple[list[str], int]:
    """正文里只有空格的下划线 run：字换成同样宽的一串"＿"。"""
    out, n = [], 0
    for r in runs:
        t = _run_text(r)
        if _run_underlined(r) and t and not html.unescape(t).strip():
            body = FILL * _blank_width(html.unescape(t))
            r = re.sub(r"(<w:r\b[^>]*>(?:\s*<w:rPr>.*?</w:rPr>)?).*?(</w:r>)",
                       lambda m: f'{m.group(1)}<w:t xml:space="preserve">{body}</w:t>{m.group(2)}', r, count=1, flags=re.S)
            n += 1
        out.append(r)
    return out, n


def _docx_fill_lines(src: Path, dst: Path) -> int:
    """docx 里的填空线改写成看得见的字（见上面 FILL 的说明），写到 dst。改了几处；一处没改返回 0（dst 不写）。"""
    with zipfile.ZipFile(src) as z:
        xml = z.read("word/document.xml").decode("utf-8")
        count = [0]

        def para(m):
            p = m.group(0)
            if "<w:u " not in p and not _LINE_SHAPE.search(p):
                return p
            head = re.match(r"<w:p\b[^>]*>", p).group(0)
            ppr_m = re.search(r"<w:pPr>.*?</w:pPr>", p, re.S)
            ppr = ppr_m.group(0) if ppr_m else ""
            runs = _W_R.findall(p)
            if not runs:
                return p
            line = _field_line(ppr, runs)
            if line is not None:
                count[0] += 1
                return head + line + "</w:p>"
            new_runs, n = _fill_blanks(runs)
            if not n:
                return p
            count[0] += n
            it = iter(new_runs)
            return _W_R.sub(lambda _m: next(it), p)

        new = _W_P.sub(para, xml)
        if not count[0]:
            return 0
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
            for item in z.infolist():
                data = new.encode("utf-8") if item.filename == "word/document.xml" else z.read(item.filename)
                out.writestr(item, data)
        return count[0]


def convert_docx(src: Path, stage: Path, pandoc: str) -> tuple[str, dict]:
    """docx → (Markdown, 统计)。公式 pandoc 已经转成 TeX；图抽进 stage/figures，按图号写 figures.json。
    封面的填空线先改写成看得见的字（_docx_fill_lines），改写出错就照原文件转。"""
    t0 = time.time()
    src = Path(src).resolve()                  # pandoc 在临时目录里跑：相对路径会找不到
    work = Path(tempfile.mkdtemp(prefix="bloom-docx-"))
    try:
        use, filled = src, 0
        try:
            filled = _docx_fill_lines(Path(src), work / "filled.docx")
            if filled:
                use = work / "filled.docx"
        except Exception:
            use, filled = src, 0
        r = subprocess.run([pandoc, str(use), "-t", "gfm", "-s", "--wrap=none", "--extract-media=media"],
                           cwd=str(work), capture_output=True, timeout=180, creationflags=NO_WINDOW)
        if r.returncode != 0 and use != src:          # 改写过的那份 pandoc 不认：照原文件再转一次
            use, filled = src, 0
            r = subprocess.run([pandoc, str(src), "-t", "gfm", "-s", "--wrap=none", "--extract-media=media"],
                               cwd=str(work), capture_output=True, timeout=180, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise ConvertError("pandoc 转换失败：" + r.stderr.decode("utf-8", "replace")[-400:])
        md = r.stdout.decode("utf-8", "replace")
        out, stats = _docx_post(md, work, stage / "figures")
        stats.update(engine="pandoc", secs=round(time.time() - t0, 1), fill_lines=filled)
        return out, stats
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _docx_post(md: str, work: Path, figdir: Path) -> tuple[str, dict]:
    md = md.replace("\r\n", "\n").replace("\u200b", "").replace("\ufeff", "")
    head = []
    m = re.match(r"^---\n(.*?)\n---\n", md, re.S)
    if m:
        meta = m.group(1)
        md = md[m.end():]
        tm = re.search(r"^title:\s*(.+)$", meta, re.M)
        am = re.search(r"^author:\s*(.+)$", meta, re.M)
        if tm:
            head.append("# " + tm.group(1).strip().strip("'\""))
        if am:
            head.append(am.group(1).strip().strip("'\""))
    cjk = len(CJK.findall(md)) / max(1, len(re.sub(r"\s", "", md))) > 0.1
    stats = {"figures": 0, "tables": 0, "equations": 0, "inline_latex": 0, "unsupported_images": 0,
             "lang": "zh" if cjk else "en"}
    # 段落切开：围栏、HTML 块（<figure>、<table>）各算一块。HTML 块按层数收：格子里套着的表先收尾，外层表还没完
    blocks, cur, fence, htmlblk, depth = [], [], False, None, 0

    def nest(tag, s):
        return len(re.findall(rf"<{tag}\b", s, re.I)) - len(re.findall(rf"</{tag}\s*>", s, re.I))
    for line in md.split("\n"):
        s = line.strip()
        if fence:
            cur.append(line)
            if s.startswith("```"):
                blocks.append("\n".join(cur)); cur = []; fence = False
            continue
        if htmlblk:
            cur.append(line)
            depth += nest(htmlblk, s)
            if depth <= 0:
                blocks.append("\n".join(cur)); cur = []; htmlblk = None
            continue
        if s.startswith("```"):
            if cur:
                blocks.append("\n".join(cur)); cur = []
            cur.append(line); fence = True
            continue
        hm = re.match(r"<(figure|table|div)\b", s, re.I)
        if hm:
            if cur:
                blocks.append("\n".join(cur)); cur = []
            cur.append(line)
            depth = nest(hm.group(1), s)
            if depth <= 0:
                blocks.append("\n".join(cur)); cur = []
            else:
                htmlblk = hm.group(1)
            continue
        if not s:
            if cur:
                blocks.append("\n".join(cur)); cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    blocks = _unwrap_tables(blocks, cjk)          # 申报书这类表单表：拆成标题、段落和紧凑的键值表

    figdir.mkdir(parents=True, exist_ok=True)
    figmeta: dict = {}
    used: set = set()
    caps_used: set = set()
    out: list[str] = []

    def find_media(ref: str) -> Path | None:
        ref = html.unescape(ref).split("?")[0]
        p = (work / ref)
        if p.is_file():
            return p
        hits = list(work.rglob(Path(ref).name))
        return hits[0] if hits else None

    def save_fig(num: int, srcp: Path | None, caption: str) -> None:
        if srcp is None:
            return
        ext = srcp.suffix.lower()
        dst = None
        if ext in IMG_EXT:
            dst = figdir / f"fig{num}{'.jpg' if ext == '.jpeg' else ext}"
            shutil.copyfile(srcp, dst)
        elif ext in (".tif", ".tiff", ".bmp"):
            try:
                import pymupdf as fitz  # PyMuPDF（新版不再用 fitz 这个名字）
                pix = fitz.Pixmap(str(srcp))
                if pix.alpha or pix.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                dst = figdir / f"fig{num}.png"
                pix.save(str(dst))
            except Exception:
                dst = None
        if dst is None:
            stats["unsupported_images"] += 1        # emf / wmf 之类浏览器显示不了
            return
        figmeta[str(num)] = {"file": dst.name, "kind": "image", "caption": caption, "source": "docx"}

    def next_num() -> int:
        n = 1
        while n in used:
            n += 1
        return n

    for bi, blk in enumerate(blocks):
        s = blk.strip()
        if bi in caps_used:
            continue
        if isinstance(blk, _MD):                  # 表单表拆出来的键值表：已经是管道表
            out.append(s); stats["tables"] += 1
            continue
        if s.startswith("```"):
            lang = re.match(r"```\s*([A-Za-z]*)", s).group(1).lower()
            body = re.sub(r"^```[^\n]*\n?", "", s)
            body = re.sub(r"\n?```\s*$", "", body)
            if lang in ("math", "latex", "tex"):
                out.append("```latex\n" + tex_tidy(body, display=True) + "\n```")
                stats["equations"] += 1
            else:
                out.append(s)
            continue
        imgs = re.findall(r"<img\b[^>]*\bsrc=\"([^\"]+)\"", s, re.I) + re.findall(r"!\[[^\]]*\]\(([^)\s]+)", s)
        if imgs and (s.startswith(("<figure", "<img", "![")) or re.fullmatch(r"(?:\s*!\[[^\]]*\]\([^)]+\)\s*)+", s)):
            cap = ""
            fm = re.search(r"<figcaption>(.*?)</figcaption>", s, re.S | re.I)
            if fm:
                cap = re.sub(r"<[^>]+>", " ", fm.group(1))
            else:
                am = re.search(r"!\[([^\]]+)\]", s)
                cap = am.group(1) if am else ""
            cap = re.sub(r"\s+", " ", html.unescape(cap)).strip()
            k, num = _cap_kind(cap)
            cap_in_next = False
            if k != "fig":
                for nb in (bi + 1, bi - 1):
                    if 0 <= nb < len(blocks) and nb not in caps_used:
                        k2, n2 = _cap_kind(blocks[nb])
                        if k2 == "fig" and len(blocks[nb]) < 600 and _caption_like(blocks[nb], "fig"):
                            num, cap_in_next = n2, nb == bi + 1
                            if nb == bi - 1 and out:
                                pass          # 图注在图上方：它已经输出过了
                            break
                else:
                    num = None
            if not num or num in used:
                num = next_num()
                if cap:
                    cap = (f"图 {num}：" if cjk else f"Figure {num}: ") + cap
            used.add(num)
            save_fig(num, find_media(imgs[0]), text_inline(cap, cjk))
            out.append(f"[image: 图 {num}]")
            if cap and not cap_in_next:
                out.append(_guard_start(text_inline(cap, cjk)))
            stats["figures"] += 1
            continue
        if re.match(r"<table\b", s, re.I):
            t = table_to_md(s, cjk, head="thead")
            if t:
                out.append(t); stats["tables"] += 1
            continue
        if s.startswith("|"):
            t = table_to_md(s, cjk)
            if t:
                out.append(t); stats["tables"] += 1
            continue
        if re.match(r"^#{1,6}\s", s):
            hm = re.match(r"^(#{1,6})\s+(.*)$", s, re.S)
            out.append(hm.group(1) + " " + text_inline(hm.group(2), cjk))
            continue
        lines = [re.sub(r"\\$", "", ln) for ln in s.split("\n")]
        out.append(_guard_start("\n".join(text_inline(ln, cjk) for ln in lines if ln.strip())))
    out = _promote_heads(out)
    if head and not (out and out[0].strip() == head[0]):
        out = head + out
    _write_figmeta(figdir, figmeta)
    text = "\n\n".join(x for x in out if x.strip()).rstrip() + "\n"
    stats["inline_latex"] = len(re.findall(r"(?<!\\)\$[^$\n]+\$", text))
    return text, stats


# ======================================================================
# 把暂存目录的结果搬到论文文件夹
# ======================================================================
def install(stage: Path, md: str, dst: Path) -> None:
    """stage/figures → dst.parent/figures（先删掉上次我们写的图），最后写 Markdown（mtime 最新）。"""
    out = dst.parent
    figs_new = stage / "figures"
    figs_old = out / "figures"
    try:
        old = json.loads((figs_old / "figures.json").read_text(encoding="utf-8"))
        for v in (old or {}).values():
            f = figs_old / Path(str((v or {}).get("file") or "")).name
            if f.is_file() and (v or {}).get("source") in ("mineru", "docx"):
                f.unlink()
        if all((v or {}).get("source") in ("mineru", "docx") for v in (old or {}).values()):
            (figs_old / "figures.json").unlink()
    except (OSError, ValueError, AttributeError):
        pass
    if figs_new.is_dir() and any(figs_new.iterdir()):
        figs_old.mkdir(parents=True, exist_ok=True)
        for f in figs_new.iterdir():
            shutil.copyfile(f, figs_old / f.name)
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, dst)
