# -*- coding: utf-8 -*-
"""LaTeX 论文：会上改完的另出一份带修订标记的 .tex（照 latexdiff 的做法，宏包只用 ulem 和 xcolor），能直接编译。

会上改的就是用户自己的 .tex（就地改，见 papers.py）。散会时（或点"写回"）拿开会那一刻的原文（view.base_for(...).orig）
和现在的稿子比，在原 .tex 旁边写「<原名>（会上修订）.tex」：
    加的字  \\BloomAdd{...}   蓝色下划线
    删的字  \\BloomDel{...}   红色删除线
做法：
  1. 两份都切成记号（命令、花括号、$、注释、空白、英文词、单个汉字、标点……），切完拼回去和原文一字不差；
  2. 按记号比（difflib），输出 = 现在这份的全部记号（结构、命令一个不少，所以现在这份能编译、输出就能编译），
     只在"安全的地方"给加的字包上 \\BloomAdd、在删的位置补上 \\BloomDel{删掉的字}；
  3. 安全的地方 = 正文里（\\begin{document} 之后）、不在公式里、不在 verbatim 一类环境里、不在 \\section \\caption \\label
     \\cite 这类命令的参数里；包的只是一截"纯文字"（不含命令、花括号、$、&、空行……），命令照原样输出、不包；
  4. 不安全的地方：加的照原样写上（不标），删的不写（不标），数进 unmarked。
导言区加几行定义（\\RequirePackage{ulem}、{xcolor} 和两个宏）；没有 \\begin{document} 的分章文件，定义写在文件头，
只用 \\providecommand，主文件没载 ulem / xcolor 时退成不画线、不上色。"""
from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

SUFFIX = "（会上修订）"

_TOK = re.compile(
    r"%[^\n]*"                                  # 注释（不含换行）
    r"|\\verb\*?(.)(?:(?!\1).)*\1"              # \verb|...|
    r"|\\[A-Za-z@]+\*?"                         # 命令
    r"|\\\\\*?"                                 # \\ 换行
    r"|\\[^A-Za-z@\s]"                          # \% \& \$ \_ \{ \} \, \; ……
    r"|\$\$|\$"
    r"|[{}\[\]&#^_~]"
    r"|\n[ \t]*\n\s*"                           # 空行（分段）
    r"|[ \t\r\n]+"
    r"|[0-9]+(?:[.,][0-9]+)*"                   # 数字（94.9、1,000 算一个）
    r"|[A-Za-z0-9\u00C0-\u024F'’-]+"            # 英文词、数字
    r"|.", re.S)

_TEXT_ESC = {"\\%", "\\&", "\\$", "\\#", "\\_", "\\{", "\\}", "\\,"}
# 参数里可以画线的命令（字体、强调、脚注正文）；别的命令后面的 {…} 一律不碰
_SAFE_ARG = {"\\textbf", "\\textit", "\\emph", "\\textrm", "\\textsf", "\\texttt", "\\textsc", "\\textup", "\\textsl",
             "\\textmd", "\\underline", "\\footnote", "\\mbox", "\\text"}
_MATH_ENVS = {"equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "eqnarray",
              "eqnarray*", "displaymath", "math", "flalign", "flalign*", "alignat", "alignat*", "split", "cases",
              "array", "matrix", "pmatrix", "bmatrix", "vmatrix"}
_RAW_ENVS = {"verbatim", "verbatim*", "lstlisting", "minted", "comment", "Verbatim", "alltt", "tikzpicture", "filecontents"}

# 导言区最后补的几行（插在 \begin{document} 前面，用户的宏包都已经载好了）：没载的 ulem / xcolor 补上；
# 用 xeCJK（ctex + XeLaTeX）的换成 xeCJKfntef 的 \CJKunderline / \CJKsout（中文能断行）；pdfLaTeX + CJK 包的
# ulem 对中文不灵，退成只上色（删的字再缩小一号，照 latexdiff 的 CFONT 样式）
PREAMBLE = (
    "% Bloom 会上的修订：蓝色下划线是加的，红色删除线是删的\n"
    "\\makeatletter\n"
    "\\@ifpackageloaded{ulem}{}{\\RequirePackage[normalem]{ulem}}\n"
    "\\@ifpackageloaded{xcolor}{}{\\RequirePackage{xcolor}}\n"
    "\\@ifpackageloaded{xeCJK}{\\IfFileExists{xeCJKfntef.sty}{\\RequirePackage{xeCJKfntef}}{}}{}\n"
    "\\@ifpackageloaded{CJK}{%\n"
    "  \\providecommand{\\BloomAdd}[1]{{\\color{blue}#1}}%\n"
    "  \\providecommand{\\BloomDel}[1]{{\\color{red}\\scriptsize #1}}}{%\n"
    "\\@ifpackageloaded{xeCJKfntef}{%\n"
    "  \\providecommand{\\BloomAdd}[1]{{\\color{blue}\\CJKunderline{#1}}}%\n"
    "  \\providecommand{\\BloomDel}[1]{{\\color{red}\\CJKsout{#1}}}}{%\n"
    "  \\providecommand{\\BloomAdd}[1]{{\\color{blue}\\uline{#1}}}%\n"
    "  \\providecommand{\\BloomDel}[1]{{\\color{red}\\sout{#1}}}}}\n"
    "\\makeatother\n")
FRAGMENT = (
    "% Bloom 会上的修订：蓝色下划线是加的，红色删除线是删的（主文件里载了 ulem、xcolor 才画线、上色）\n"
    "\\providecommand{\\BloomAdd}[1]{{\\ifdefined\\color\\color{blue}\\fi\\ifdefined\\uline\\uline{#1}\\else#1\\fi}}\n"
    "\\providecommand{\\BloomDel}[1]{{\\ifdefined\\color\\color{red}\\fi\\ifdefined\\sout\\sout{#1}\\else#1\\fi}}\n")


def tokens(s: str) -> list[str]:
    out = [m.group(0) for m in _TOK.finditer(s)]
    assert "".join(out) == s
    return out


def _is_text(t: str) -> bool:
    """能放进 \\uline / \\sout 里的一个记号：字、标点、空格（不含空行）、几个转义符号。"""
    if t in _TEXT_ESC:
        return True
    if t[0] in "\\%${}[]&#^_~":
        return False
    if t[0] in " \t\r\n":
        return t.count("\n") < 2
    return True


def _contexts(toks: list[str]) -> tuple[list[bool], list[bool]]:
    """(tok, pos)：tok[i] = 第 i 个记号要是纯文字，能不能包进修订标记；pos[j] = 第 j 个记号前面这个位置能不能插一段
    \\BloomDel{…}。能 = 在正文里、不在公式里、不在 verbatim 一类环境里、不在 \\section{…} \\label{…} \\cite{…} 这类参数里
    （\\textbf \\emph \\footnote 这些的参数里可以）、不在命令的 [可选参数] 里；插删除的位置还不能夹在命令和它的参数中间。"""
    n = len(toks)
    tok, pos = [False] * n, [False] * (n + 1)
    text = "".join(toks)
    body = not re.search(r"\\begin\s*\{document\}", text)      # 分章文件（没有 \begin{document}）：整个都算正文
    math = None            # None / "$" / "$$" / "\\(" / "\\[" / 公式环境名
    raw = None             # verbatim 一类环境名
    stack: list[bool] = []  # 每层花括号：安全不安全
    pending = None          # 上一个命令（还在等它的参数）
    opt = 0                 # 在命令的 [可选参数] 里几层

    def ok_here() -> bool:
        return body and math is None and raw is None and opt == 0 and (stack[-1] if stack else True)

    def next_real(i: int) -> str:
        while i < n and toks[i].isspace() and toks[i].count("\n") < 2:
            i += 1
        return toks[i] if i < n else ""
    i = 0
    while i < n:
        t = toks[i]
        pos[i] = ok_here() and not (pending is not None and next_real(i) in ("{", "["))
        if raw is not None and t != "\\end":
            i += 1
            continue
        if t in ("\\begin", "\\end"):
            j = i + 1
            while j < n and toks[j].isspace():
                j += 1
            name, end = "", i
            if j < n and toks[j] == "{":
                k = j + 1
                while k < n and toks[k] != "}":
                    name += toks[k]
                    k += 1
                end = k
            name = name.strip()
            if t == "\\begin":
                if raw is None and math is None:
                    if name == "document":
                        body = True
                    elif name in _RAW_ENVS:
                        raw = name
                    elif name in _MATH_ENVS:
                        math = name
            elif raw is not None:
                if name == raw:
                    raw = None
                else:
                    i += 1
                    continue
            elif name == math:
                math = None
            elif name == "document" and math is None:
                body = False
            for k in range(i + 1, min(end + 1, n)):
                pos[k] = False
            i = end + 1
            pending = None
            continue
        if t in ("$", "$$"):
            math = None if math == t else (t if math is None else math)
            pending = None
        elif t in ("\\(", "\\["):
            math = math or t
        elif t == "\\)" and math == "\\(":
            math = None
        elif t == "\\]" and math == "\\[":
            math = None
        elif t == "[" and pending is not None:
            opt += 1
        elif t == "]" and opt:
            opt -= 1
        elif opt:
            pass
        elif t == "{":
            parent = stack[-1] if stack else True
            stack.append(parent and (pending is None or pending in _SAFE_ARG))
            pending = None
        elif t == "}":
            if stack:
                stack.pop()
            pending = None
        elif t in ("\\\\", "\\\\*") or (t.startswith("\\") and len(t) > 1 and (t[1].isalpha() or t[1] == "@")):
            pending = t
        elif t.isspace() and t.count("\n") < 2:
            tok[i] = ok_here()                # 命令和参数之间的空格：还算"那个命令后面"
        else:
            tok[i] = ok_here() and not t.isspace()
            pending = None
        i += 1
    pos[n] = ok_here()
    return tok, pos


def _wrap(chunk: list[str], ok: list[bool], macro: str) -> tuple[str, int]:
    """一截记号：连着的、能画的纯文字包进 macro（首尾空白留在外面），别的原样；返回 (文字, 包了几处)。"""
    out, run, marks = [], [], 0

    def flush():
        nonlocal marks
        body = "".join(run)
        if body.strip():
            lead = body[:len(body) - len(body.lstrip())]
            tail = body[len(body.rstrip()):]
            out.append(lead + macro + "{" + body.strip() + "}" + tail)
            marks += 1
        else:
            out.append(body)
        run.clear()
    for t, good in zip(chunk, ok):
        if good and _is_text(t):
            run.append(t)
        else:
            flush()
            out.append(t)
    flush()
    return "".join(out), marks


def make(base: str, cur: str) -> tuple[str, dict]:
    """base → cur 的带修订标记的 LaTeX 源码；报告 {"added": 包了几处加的, "deleted": 包了几处删的,
    "unmarked": 改了却没标上的几处（公式里、命令参数里……）, "changes": 一共几处改动}。"""
    a, b = tokens(base.replace("\r\n", "\n")), tokens(cur.replace("\r\n", "\n"))
    tok_a, _ = _contexts(a)
    tok_b, pos_b = _contexts(b)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: list[str] = []
    rep = {"added": 0, "deleted": 0, "unmarked": 0, "changes": 0}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(b[j1:j2])
            continue
        rep["changes"] += 1
        marked = False
        if i2 > i1:                           # 删掉的：只补能画的纯文字，命令、花括号、$ 都不写回去
            text = "".join(t for t, s in zip(a[i1:i2], tok_a[i1:i2]) if s and _is_text(t))
            if pos_b[j1] and text.strip():
                out.append("\\BloomDel{" + re.sub(r"\s+", " ", text).strip() + "}")
                rep["deleted"] += 1
                marked = True
        if j2 > j1:
            s, k = _wrap(b[j1:j2], tok_b[j1:j2], "\\BloomAdd")
            out.append(s)
            rep["added"] += k
            marked = marked or k > 0
        if not marked and ("".join(a[i1:i2]).strip() or "".join(b[j1:j2]).strip()):
            rep["unmarked"] += 1
    text = "".join(out)
    m = re.search(r"\\begin\s*\{document\}", text)
    if m:
        text = text[:m.start()] + PREAMBLE + text[m.start():]
    else:
        text = FRAGMENT + text
    return text, rep


def out_path(src: Path) -> Path:
    return src.with_name(f"{src.stem}{SUFFIX}{src.suffix}")


def write(src: Path, base: str, cur: str) -> dict:
    """写「<原名>（会上修订）.tex」。没改：{"ok": True, "out": None}。"""
    if base.replace("\r\n", "\n") == cur.replace("\r\n", "\n"):
        return {"ok": True, "out": None, "note": "会上没改这篇"}
    text, rep = make(base, cur)
    dst = out_path(src)
    tmp = dst.with_name(f".{dst.stem}.{os.getpid()}.part")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, dst)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": f"写文件失败：{e}"}
    return {"ok": True, "out": str(dst), **rep}


def for_paper(path) -> dict:
    """一篇 .tex：拿开会那一刻的原文（对比页的 .orig 基线）和现在的稿子比，写修订版。"""
    src = Path(str(path))
    if src.suffix.lower() != ".tex" or not src.is_file():
        return {"ok": False, "not_tex": True}
    try:
        from .view import base_for
        b0 = Path(str(base_for(src)) + ".orig")
        base = b0.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, ImportError) as e:
        return {"ok": False, "error": f"找不到开会时的原文：{e}"}
    cur = src.read_text(encoding="utf-8-sig", errors="replace")
    return write(src, base, cur)
