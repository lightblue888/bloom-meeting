# -*- coding: utf-8 -*-
"""第 6 批：LaTeX 论文另出一份带修订标记的 .tex（zuhui/tex_changes.py）。
样例自己造：英文 article（表格、脚注、公式、列表、\\section 标题、\\cite）和中文 ctexart。每条：
  - 把 \\BloomDel{…} 去掉、\\BloomAdd{…} 拆开（去掉补的导言区），一字不差等于会上改完的那份
  - 加了几处、删了几处、没标上的几处对得上
  - 装了 TeX（pdflatex / xelatex）的机器上真编一遍：不报错、出得了 PDF
跑：python -X utf8 -m unittest tests.test_b6_tex -v"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ZUHUI_HOME"] = os.environ.get("ZUHUI_HOME") or tempfile.mkdtemp(prefix="zuhui-test-home-")

from zuhui import tex_changes as tc  # noqa: E402

PDFLATEX, XELATEX = shutil.which("pdflatex"), shutil.which("xelatex")

EN = r"""\documentclass{article}
\usepackage{amsmath}
\usepackage{booktabs}
\title{A Meeting Assistant}
\begin{document}
\maketitle
\section{Introduction}
Advisors give feedback \textbf{orally} during group meetings, and students
often forget parts of it.\footnote{We surveyed 12 labs in 2026.}
We present Bloom, which edits the paper live~\cite{knuth84}.

\section{Method}\label{sec:method}
The turn detector scores each pause with
\begin{equation}
  p = \sigma(w^\top x + b),
\end{equation}
and speaks when $p > 0.5$ holds.
\begin{itemize}
  \item Streaming ASR on device.
  \item Decisions become edits.
\end{itemize}
\begin{table}[h]
\centering
\begin{tabular}{lrr}
\toprule
Method & Accuracy & Latency \\
\midrule
Baseline & 75.1\% & 820 ms \\
Bloom & 94.9\% & 310 ms \\
\bottomrule
\end{tabular}
\caption{Results on the public set.}
\end{table}
The last paragraph stays the same.
\begin{thebibliography}{1}
\bibitem{knuth84} D. Knuth. The \TeX book. 1984.
\end{thebibliography}
\end{document}
"""

ZH = r"""\documentclass{ctexart}
\usepackage{amsmath}
\begin{document}
\section{引言}
组会上老师的意见常常口头给出，学生会后凭记忆修改论文，容易遗漏。
本文提出 Bloom，一个会上实时改稿的助手。\footnote{代码已开源。}

\section{实验}
判断准确率由 $75.1\%$ 提高到 94.9\%，延迟是 \textbf{310 毫秒}。
\begin{tabular}{ll}
方法 & 准确率 \\
基线 & 75.1\% \\
\end{tabular}

最后一段保持不变。
\end{document}
"""


def accept(text: str) -> str:
    """接受全部修订：去掉补的导言区和删的，拆开加的。"""
    text = text.replace(tc.PREAMBLE, "").replace(tc.FRAGMENT, "")
    text = re.sub(r"\\BloomDel\{[^{}]*\}", "", text)
    return re.sub(r"\\BloomAdd\{([^{}]*)\}", r"\1", text)


def reject(text: str) -> str:
    """拒绝全部修订（只看字）：去掉加的，拆开删的。"""
    text = text.replace(tc.PREAMBLE, "").replace(tc.FRAGMENT, "")
    text = re.sub(r"\\BloomAdd\{[^{}]*\}", "", text)
    return re.sub(r"\\BloomDel\{([^{}]*)\}", r"\1", text)


def compile_ok(engine: str, tex: Path) -> tuple[bool, str]:
    r = subprocess.run([engine, "-interaction=nonstopmode", "-halt-on-error", tex.name], cwd=str(tex.parent),
                       capture_output=True, timeout=240, stdin=subprocess.DEVNULL)
    log = r.stdout.decode("utf-8", "replace")
    return r.returncode == 0 and tex.with_suffix(".pdf").exists(), log[-3000:]


class TexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tex-case-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tokens_lossless(self):
        for s in (EN, ZH, "a % c\n\\verb|x{|y \\\\[2pt] $$x$$ \\{ é—ü"):
            self.assertEqual("".join(tc.tokens(s)), s)

    def test_english_marks(self):
        cur = (EN.replace("often forget parts of it", "often forget much of it")
               .replace(r"\textbf{orally}", r"\textbf{orally and quickly}")
               .replace("which edits the paper live", "which edits the paper during the meeting")
               .replace(r"\section{Method}", r"\section{Our Method}")          # 标题：不标
               .replace(r"p = \sigma(w^\top x + b)", r"p = \sigma(w^\top x + c)")  # 公式：不标
               .replace("Decisions become edits.", "Spoken decisions become edits.")
               .replace("94.9\\%", "95.2\\%")
               .replace("Results on the public set.", "Results on the public benchmark."))  # 图注：不标
        out, rep = tc.make(EN, cur)
        self.assertEqual(accept(out), cur)
        self.assertIn(r"\section{Our Method}", out)
        self.assertNotIn(r"\section{\Bloom", out)
        self.assertIn(r"\textbf{orally\BloomAdd{and quickly}}", out.replace(" \\BloomAdd", "\\BloomAdd"))
        self.assertIn(r"\BloomDel{parts}", out)
        self.assertIn(r"\BloomAdd{much}", out)
        self.assertIn(r"\item \BloomDel{Decisions}\BloomAdd{Spoken decisions}", out)
        self.assertIn(r"\BloomDel{94.9}\BloomAdd{95.2}\%", out)
        self.assertEqual(rep["unmarked"], 3)                  # 标题、公式、图注里的三处
        self.assertEqual(rep["added"], 5)                     # and quickly / much / during the meeting / Spoken decisions / 95.2
        self.assertEqual(rep["deleted"], 4)                   # parts / live / Decisions / 94.9
        self.assertEqual(out.count("\\BloomAdd{"), rep["added"])
        self.assertEqual(out.count("\\BloomDel{"), rep["deleted"])
        self.assertTrue(out.index(tc.PREAMBLE) < out.index("\\begin{document}"))

    def test_chinese_marks(self):
        cur = (ZH.replace("凭记忆", "靠回忆").replace("容易遗漏。", "容易遗漏。\n\n我们在三个课题组里试用了一学期。")
               .replace("310 毫秒", "290 毫秒").replace("$75.1\\%$", "$76.0\\%$"))
        out, rep = tc.make(ZH, cur)
        self.assertEqual(accept(out), cur)
        self.assertIn("\\BloomAdd{我们在三个课题组里试用了一学期", out)
        self.assertIn("\\textbf{\\BloomDel{310}\\BloomAdd{290}", out)
        self.assertEqual(rep["unmarked"], 1)                  # 行内公式里那一处

    def test_new_paragraph_and_deleted_paragraph(self):
        cur = EN.replace("The last paragraph stays the same.\n", "").replace(
            "often forget parts of it.\\footnote{We surveyed 12 labs in 2026.}\n",
            "often forget parts of it.\\footnote{We surveyed 12 labs in 2026.}\n\nA second new paragraph here.\n")
        out, rep = tc.make(EN, cur)
        self.assertEqual(accept(out), cur)
        self.assertIn("\\BloomDel{The last paragraph stays the same.}", out)
        self.assertIn("\\BloomAdd{A second new paragraph here.}", out)

    def test_fragment_without_document(self):
        base = "\\section{Intro}\nOld words here.\n"
        cur = "\\section{Intro}\nNew words here.\n"
        out, rep = tc.make(base, cur)
        self.assertTrue(out.startswith(tc.FRAGMENT))
        self.assertEqual(accept(out), cur)
        self.assertEqual((rep["added"], rep["deleted"]), (1, 1))

    def test_write_next_to_source(self):
        src = self.tmp / "main.tex"
        src.write_text(EN, encoding="utf-8")
        r = tc.write(src, EN, EN)
        self.assertTrue(r["ok"])
        self.assertIsNone(r["out"])
        r = tc.write(src, EN, EN.replace("parts of it", "much of it"))
        self.assertEqual(Path(r["out"]).name, "main（会上修订）.tex")
        self.assertEqual(src.read_text(encoding="utf-8"), EN)         # 原文件不动

    def _compile(self, engine, base, cur, name="main"):
        src = self.tmp / f"{name}.tex"
        src.write_text(cur, encoding="utf-8")
        r = tc.write(src, base, cur)
        out = Path(r["out"])
        ok, log = compile_ok(engine, out)
        self.assertTrue(ok, log)

    @unittest.skipUnless(PDFLATEX, "没有 pdflatex")
    def test_compile_pdflatex(self):
        cur = (EN.replace("often forget parts of it", "often forget much of it")
               .replace("Decisions become edits.", "Spoken decisions become edits.")
               .replace("The last paragraph stays the same.\n", "")
               .replace(r"\section{Method}", r"\section{Our Method}")
               .replace(r"\textbf{orally}", r"\textbf{orally and quickly}")
               .replace(r"p = \sigma(w^\top x + b)", r"p = \sigma(w^\top x + c)")
               .replace("94.9\\%", "95.2\\%").replace("820 ms", "800 ms")
               .replace("Results on the public set.", "Results on the public benchmark.")
               .replace("We surveyed 12 labs", "We surveyed 14 labs")
               .replace("when $p > 0.5$ holds", "when $p > 0.6$ clearly holds"))
        self._compile(PDFLATEX, EN, cur)

    @unittest.skipUnless(XELATEX, "没有 xelatex")
    def test_compile_xelatex_chinese(self):
        cur = (ZH.replace("凭记忆", "靠回忆").replace("容易遗漏。", "容易遗漏。\n\n我们在三个课题组里试用了一学期。")
               .replace("最后一段保持不变。\n", "").replace("310 毫秒", "290 毫秒"))
        self._compile(XELATEX, ZH, cur, name="论文")


if __name__ == "__main__":
    unittest.main()
