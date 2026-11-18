# -*- coding: utf-8 -*-
"""Word（pandoc）转 Markdown：表单表拆开、格子里套表不截断外层表、表头合并、编号题头、转换器版本号。
跑：python -m unittest tests.test_pdfconv_docx -v （在仓库根目录；不需要 pytest，带 pandoc 的那条没有 pandoc 时跳过）"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-home-")    # 别碰真的 ~/.zuhui

from zuhui import papers, pdfconv  # noqa: E402
from zuhui.paper import Paper  # noqa: E402

# pandoc 对申报书这类 Word 表单给的样子：一张 9 列大表，键值行 + 整行通栏的正文格，正文格里套着数据表和图
FORM = """**项目申报书**

<table style="width:100%;">
<colgroup>
<col style="width: 50%" />
<col style="width: 50%" />
</colgroup>
<tbody>
<tr>
<td colspan="2"><strong><br />
项目名称</strong></td>
<td colspan="7">组会助手</td>
</tr>
<tr>
<td rowspan="2"><strong>负责人情况</strong></td>
<td colspan="2"><strong>姓 名</strong></td>
<td>张三</td>
<td><strong>性 别</strong></td>
<td colspan="4">男</td>
</tr>
<tr>
<td colspan="2"><strong>学 院</strong></td>
<td>信息学院</td>
<td><strong>所在单位</strong></td>
<td colspan="4"><blockquote>
&#10;</blockquote></td>
</tr>
<tr>
<td colspan="9"><p><strong>一、项目简介</strong></p>
<p>本项目开发面向组会的桌面软件，实时转写多人发言。</p></td>
</tr>
<tr>
<td colspan="9"><p><strong>二、项目方案（研究内容、计划等）</strong></p>
<p><strong>（一）研究目标</strong></p>
<p>（1）在多人组会中准确判断发言是否需要作答。</p>
<p><strong>1. 多人组会中的发言判断</strong></p>
<p>现有方法由规则和对话模型组成。</p>
<p><img src="media/image1.png" style="width:5in" /></p>
<p>图 1　技术路线</p>
<p>系统实现方式如下表。</p>
<table>
<colgroup>
<col style="width: 50%" />
<col style="width: 50%" />
</colgroup>
<thead>
<tr>
<th><strong>模块</strong></th>
<th><strong>实现方式</strong></th>
</tr>
</thead>
<tbody>
<tr>
<td>桌面端</td>
<td>Tauri 与 TypeScript</td>
</tr>
<tr>
<td>后端</td>
<td>Python，本机运行</td>
</tr>
</tbody>
</table>
<p><strong>（二）人员分工</strong></p>
<p>本项目由负责人个人完成。</p></td>
</tr>
<tr>
<td colspan="9"><p><strong>三、项目进度安排</strong></p>
<table>
<thead>
<tr>
<th>时间</th>
<th>工作内容</th>
</tr>
</thead>
<tbody>
<tr>
<td>1 月至 2 月</td>
<td>完成论文初稿</td>
</tr>
</tbody>
</table></td>
</tr>
</tbody>
</table>

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p><strong>四、项目团队承诺</strong></p>
<p>本项目团队承诺对填写内容的真实性负责。</p>
<p>项目负责人（签名）：</p></td>
</tr>
</tbody>
</table>
"""


def post(md: str) -> str:
    work = Path(tempfile.mkdtemp(prefix="docx-work-"))
    (work / "media").mkdir()
    (work / "media" / "image1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    figs = work / "figs"
    try:
        out, _ = pdfconv._docx_post(md, work, figs)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def blocks(md: str) -> list[str]:
    return [b.strip() for b in re.split(r"\n\s*\n", md) if b.strip()]


class FormTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = post(FORM)
        cls.blocks = blocks(cls.md)

    def test_no_html_left(self):
        self.assertIsNone(re.search(r"</?(table|tr|td|th|tbody|thead|colgroup|blockquote)\b", self.md), self.md)

    def test_headings_promoted_with_levels(self):
        heads = [b for b in self.blocks if b.startswith("#")]
        self.assertEqual(heads, ["## 一、项目简介", "## 二、项目方案（研究内容、计划等）", "### （一）研究目标",
                                 "#### 1. 多人组会中的发言判断", "### （二）人员分工", "## 三、项目进度安排",
                                 "## 四、项目团队承诺"])

    def test_sections_for_paper(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.md"
            f.write_text(self.md, encoding="utf-8")
            p = Paper(f)
            self.assertEqual(len(p.sections), 7)
            self.assertIn("现有方法由规则和对话模型组成。", p.sections[3].text)
            self.assertIs(p.find("一点一"), p.current)             # 不报错就行：小节号查找能跑
            self.assertEqual(p.by_title("（二）人员分工").title, "（二）人员分工")

    def test_prose_is_paragraphs(self):
        for s in ("本项目开发面向组会的桌面软件，实时转写多人发言。", "（1）在多人组会中准确判断发言是否需要作答。",
                  "本项目由负责人个人完成。", "项目负责人（签名）："):
            self.assertIn(s, self.blocks)

    def test_kv_table_compact(self):
        kv = [b for b in self.blocks if b.startswith("| **项目名称**")]
        self.assertEqual(len(kv), 1)
        rows = kv[0].split("\n")
        self.assertEqual(rows[0], "| **项目名称** | 组会助手 |  |  |  |")
        self.assertEqual(rows[2], "| **负责人情况** | **姓名** | 张三 | **性别** | 男 |")
        self.assertEqual(rows[3], "|  | **学院** | 信息学院 | **所在单位** |  |")     # 竖着合并的那格留空位，空引用块不留标签

    def test_nested_tables_are_whole_pipe_tables(self):
        tables = [b for b in self.blocks if b.startswith("|")]
        self.assertEqual(len(tables), 3)
        self.assertIn("| **模块** | **实现方式** |\n| --- | --- |\n| 桌面端 | Tauri 与 TypeScript |\n| 后端 | Python，本机运行 |",
                      tables)
        self.assertIn("| 时间 | 工作内容 |\n| --- | --- |\n| 1 月至 2 月 | 完成论文初稿 |", tables)
        for t in tables:                                          # 每张表一块：中间没有空行、每行都是表行
            self.assertTrue(all(ln.startswith("|") for ln in t.split("\n")))

    def test_figure_kept_in_place(self):
        i = self.blocks.index("[image: 图 1]")
        self.assertEqual(self.blocks[i + 1], "图 1　技术路线")
        self.assertEqual(self.blocks[i - 1], "现有方法由规则和对话模型组成。")

    def test_template_heading_only_rows(self):
        md = post('<table>\n<tbody>\n<tr>\n<td colspan="2"><strong>项目名称</strong></td>\n<td colspan="7"></td>\n</tr>\n'
                  '<tr>\n<td colspan="9"><p><strong>一、项目简介</strong></p></td>\n</tr>\n<tr>\n'
                  '<td colspan="9"><strong>四、项目特色与创新点</strong></td>\n</tr>\n</tbody>\n</table>\n')
        self.assertEqual(blocks(md), ["| **项目名称** |\n| --- |", "## 一、项目简介", "## 四、项目特色与创新点"])


class DataTables(unittest.TestCase):
    MERGED_HEAD = ("<table>\n<thead>\n<tr>\n<th rowspan=\"2\">模型</th>\n<th colspan=\"2\">Dev</th>\n</tr>\n<tr>\n<th>EM</th>\n"
                   "<th>F1</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>BERT</td>\n<td>80.1</td>\n<td>85.2</td>\n</tr>\n</tbody>\n</table>")

    def test_two_thead_rows_merge(self):
        self.assertEqual(post(self.MERGED_HEAD).strip(), "| 模型 | Dev EM | Dev F1 |\n| --- | --- | --- |\n| BERT | 80.1 | 85.2 |")

    def test_one_thead_row_not_merged_into_body(self):
        md = ("<table>\n<thead>\n<tr>\n<th>模型</th>\n<th colspan=\"2\">结果</th>\n</tr>\n</thead>\n<tbody>\n"
              "<tr>\n<td>A</td>\n<td>1.0</td>\n<td>2.0</td>\n</tr>\n<tr>\n<td>B</td>\n<td>3.0</td>\n<td>4.0</td>\n</tr>\n</tbody>\n</table>")
        self.assertEqual(post(md).strip().split("\n")[2], "| A | 1.0 | 2.0 |")   # 第一行数据还在表身，没被并进表头

    def test_no_thead_still_guesses(self):
        md = self.MERGED_HEAD.replace("<thead>\n", "").replace("</thead>\n", "").replace("<tbody>\n", "")
        self.assertEqual(post(md).strip().split("\n")[0], "| 模型 | Dev EM | Dev F1 |")

    def test_data_table_with_note_and_long_cells_not_unwrapped(self):
        long = "这一格是变量定义，写得比较长。" * 12
        md = (f"<table>\n<tbody>\n<tr>\n<td>变量</td>\n<td>定义</td>\n</tr>\n<tr>\n<td>ILLIQ</td>\n<td>{long}</td>\n</tr>\n"
              "<tr>\n<td>IVOL</td>\n<td><p>特质波动率。</p>\n<p>用过去一个月的日度数据估计。</p></td>\n</tr>\n"
              "<tr>\n<td colspan=\"2\">注：括号内为 t 值。</td>\n</tr>\n</tbody>\n</table>")
        out = blocks(post(md))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("| 变量 | 定义 |"))
        self.assertIn("| 注：括号内为 t 值。 |  |", out[0])
        self.assertEqual(out[0], pdfconv.table_to_md(md, True, head="thead"))

    def test_bold_numbered_row_labels_stay_a_table(self):
        md = ("<table>\n<tbody>\n<tr>\n<td>模型</td>\n<td>系数</td>\n</tr>\n<tr>\n<td><strong>1. OLS</strong></td>\n<td>0.35</td>\n</tr>\n"
              "<tr>\n<td><p><strong>2. FE</strong></p>\n<p>(3.1)</p></td>\n<td>0.21</td>\n</tr>\n</tbody>\n</table>")
        out = blocks(post(md))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], pdfconv.table_to_md(md, True, head="thead"))

    def test_pipe_table_untouched(self):
        md = "正文。\n\n| a | b |\n|----|----|\n| 1 | 2 |\n"
        self.assertEqual(blocks(post(md)), ["正文。", "| a | b |\n| --- | --- |\n| 1 | 2 |"])

    def test_mineru_auto_guess_unchanged(self):
        html = "<table><tr><td>m</td><td colspan=\"2\">Dev</td></tr><tr><td></td><td>EM</td><td>F1</td></tr><tr><td>x</td><td>1</td><td>2</td></tr></table>"
        self.assertEqual(pdfconv.table_to_md(html, False).split("\n")[0], "| m | Dev EM | Dev F1 |")


class Headings(unittest.TestCase):
    def test_real_headings_win(self):
        md = "# 1 引言\n\n正文。\n\n# 2 方法\n\n**一、不是标题**\n"
        self.assertIn("**一、不是标题**", blocks(post(md)))

    def test_sentences_not_promoted(self):
        out = blocks(post("**一、这是一句完整的话。**\n\n**（一）研究意义**\n"))
        self.assertEqual(out, ["**一、这是一句完整的话。**", "## （一）研究意义"])

    def test_decimal_numbering(self):
        out = blocks(post("**1. 引言**\n\n正文。\n\n**1.1 背景**\n\n正文。\n\n**1.1.1 细节**\n"))
        self.assertEqual([b for b in out if b.startswith("#")], ["## 1. 引言", "### 1.1 背景", "#### 1.1.1 细节"])


class ConvertVersion(unittest.TestCase):
    """papers._convert：旧版本转的、没人改过的 Markdown 重转；改过的、没有指纹的不动。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="docx-ver-"))
        self.src = self.dir / "申报书.docx"
        self.src.write_bytes(b"fake")
        old = time.time() - 100
        os.utime(self.src, (old, old))
        self.out = self.dir / "申报书-bloom"
        self.out.mkdir()
        self.dst = self.out / "申报书.md"
        self.dst.write_text("旧版转出来的\n", encoding="utf-8")
        self.calls = 0
        from zuhui import components
        self.components = components
        self._orig = (pdfconv.convert_docx, components.pandoc)

        def fake_convert(src, stage, pandoc):
            self.calls += 1
            return "# 新版转出来的\n", {"engine": "pandoc", "tables": 0}
        pdfconv.convert_docx = fake_convert
        components.pandoc = lambda: "pandoc"                     # 没装 pandoc 的机器上也能测

    def tearDown(self):
        pdfconv.convert_docx, self.components.pandoc = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def meta(self, **kw):
        m = {"engine": "pandoc", "source": str(self.src), "md_sha1": papers._sha1(self.dst)}
        m.update(kw)
        (self.out / papers.CONV_META).write_text(json.dumps(m), encoding="utf-8")

    def run_convert(self):
        return papers._convert(self.src, ".docx", self.dir, self.dir, True, None)

    def test_old_version_unedited_reconverts(self):
        self.meta()                                               # 没有 conv：加版本号之前转的
        self.run_convert()
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "# 新版转出来的\n")
        meta = json.loads((self.out / papers.CONV_META).read_text(encoding="utf-8"))
        self.assertEqual(meta["conv"], pdfconv.DOCX_VERSION)
        self.assertEqual(meta["md_sha1"], papers._sha1(self.dst))
        self.run_convert()                                        # 再导入一次：已是新版本，不再转
        self.assertEqual(self.calls, 1)

    def test_old_version_edited_kept(self):
        self.meta()
        self.dst.write_text("旧版转出来的\n会上改过一句\n", encoding="utf-8")
        self.run_convert()
        self.assertEqual(self.calls, 0)
        self.assertIn("会上改过一句", self.dst.read_text(encoding="utf-8"))

    def test_no_fingerprint_kept(self):
        self.meta(md_sha1="")
        self.run_convert()
        self.assertEqual(self.calls, 0)

    def test_current_version_kept(self):
        self.meta(conv=pdfconv.DOCX_VERSION)
        self.run_convert()
        self.assertEqual(self.calls, 0)


@unittest.skipUnless(shutil.which("pandoc"), "没有 pandoc")
class RealPandoc(unittest.TestCase):
    def test_docx_roundtrip_nested_form(self):
        """pandoc 从 HTML 造一份套表的 docx，再走真实的 convert_docx：外层表不被截断、没有 HTML 残留。"""
        d = Path(tempfile.mkdtemp(prefix="docx-real-"))
        try:
            html_src = d / "f.html"
            html_src.write_text(
                "<html><body><table><tr><td><b>项目名称</b></td><td>组会助手</td></tr>"
                "<tr><td colspan='2'><p><b>一、项目简介</b></p><p>正文第一段。</p>"
                "<table><tr><th>模块</th><th>实现</th></tr><tr><td>后端</td><td>Python</td></tr></table>"
                "<p><b>（一）研究意义</b></p><p>正文第二段。</p></td></tr></table></body></html>", encoding="utf-8")
            subprocess.run([shutil.which("pandoc"), str(html_src), "-o", str(d / "f.docx")], check=True)
            md, stats = pdfconv.convert_docx(d / "f.docx", d / "stage", shutil.which("pandoc"))
            self.assertIsNone(re.search(r"</?(table|tr|td|th)\b", md), md)
            bs = blocks(md)
            self.assertIn("## 一、项目简介", bs)
            self.assertIn("### （一）研究意义", bs)
            self.assertLess(bs.index("正文第一段。"), bs.index("正文第二段。"))
            self.assertTrue(any(b.startswith("| 模块 | 实现 |") for b in bs), md)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
