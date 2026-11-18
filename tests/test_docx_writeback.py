# -*- coding: utf-8 -*-
"""会上改完的 Markdown 写回 Word（zuhui/docx_writeback.py）。
每条：python-docx 造一份 Word → 用 Bloom 自己的转换器（pdfconv.convert_docx）转出 base → 在 base 上改出 cur →
写回 → 把写出来的 Word 再转一遍，去掉空白后逐行等于 cur；改到的字保留原 run 的格式；没改的部件原样。
跑：python -m unittest tests.test_docx_writeback -v （在仓库根目录；没有 pandoc 时跳过）"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-home-")    # 别碰真的 ~/.zuhui

from zuhui import docx_writeback as wb, pdfconv  # noqa: E402

PANDOC = shutil.which("pandoc")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def plain_lines(text: str) -> list[str]:
    return [re.sub(r"\s", "", wb.md_plain(x)) for x in wb.md_lines(text) if wb.line_kind(x)[0] != "sep"]


def convert(docx: Path) -> str:
    stage = Path(tempfile.mkdtemp(prefix="wbt-"))
    try:
        md, _ = pdfconv.convert_docx(docx, stage, PANDOC)
        return md
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def runs_with(docx: Path, needle: str) -> list:
    from lxml import etree
    root = etree.fromstring(zipfile.ZipFile(docx).read("word/document.xml"))
    out = []
    for r in root.iter(W + "r"):
        t = "".join(x.text or "" for x in r.findall(W + "t"))
        if needle in t:
            out.append(r)
    return out


@unittest.skipUnless(PANDOC, "没有 pandoc")
class WritebackTest(unittest.TestCase):
    OUT_DIR = None              # 设了的话（环境变量 WB_KEEP）把写出来的 Word 留在这里，给 Word 打开检查

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-case-"))
        from zuhui import paper_prefs     # 这里测的是"直接改"（修订模式见 tests/test_b6_track.py）
        paper_prefs.save({"docx_mode": "direct"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 造一篇：Word + 旁边 <名字>-bloom/ 里转好的 md 和 .bloom-convert.json ----
    def make(self, build, name="论文") -> tuple[Path, Path, str]:
        import docx
        d = docx.Document()
        build(d)
        src = self.tmp / f"{name}.docx"
        d.save(src)
        base = convert(src)
        folder = self.tmp / f"{name}-bloom"
        folder.mkdir()
        md = folder / f"{name}.md"
        md.write_text(base, encoding="utf-8")
        meta = {"engine": "pandoc", "source": str(src), "src_mtime": src.stat().st_mtime,
                "md_sha1": wb._sha1(md.read_bytes()), "conv": pdfconv.DOCX_VERSION}
        (folder / ".bloom-convert.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return src, md, base

    def run_case(self, src: Path, md: Path, base: str, cur: str, crlf: bool = False, expect_skips: int = 0):
        self.assertNotEqual(plain_lines(cur), plain_lines(base))      # 用例真改了字
        md.write_bytes((cur.replace("\n", "\r\n") if crlf else cur).encode("utf-8"))
        res = wb.writeback(md, base_text=base)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(res["skipped"]), expect_skips, res["skipped"])
        out = Path(res["out"])
        self.assertEqual(out.name, f"{src.stem}（会上修订）.docx")
        self.assertTrue(src.exists())
        # 写出来的 Word 再转一遍：去掉空白后逐行等于会上改完的 md
        if not expect_skips:
            self.assertEqual(plain_lines(convert(out)), plain_lines(cur))
        # 除了 document.xml，别的部件一个字节都不变
        zs, zo = zipfile.ZipFile(src), zipfile.ZipFile(out)
        self.assertEqual(zs.namelist(), zo.namelist())
        for n in zs.namelist():
            if n != "word/document.xml":
                self.assertEqual(zs.read(n), zo.read(n), n)
        # 分节设置、表格个数不变
        from lxml import etree
        rs = etree.fromstring(zs.read("word/document.xml"))
        ro = etree.fromstring(zo.read("word/document.xml"))
        self.assertEqual(etree.tostring(rs.find(f"{W}body/{W}sectPr")), etree.tostring(ro.find(f"{W}body/{W}sectPr")))
        self.assertEqual(len(list(rs.iter(W + "tbl"))), len(list(ro.iter(W + "tbl"))))
        keep = os.environ.get("WB_KEEP")
        if keep:
            Path(keep).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(out, Path(keep) / f"{self._testMethodName}.docx")
        return res, out

    # ---------------- 用例 ----------------
    def test_replace_word_keeps_run_format(self):
        def build(d):
            d.add_heading("Introduction", level=1)
            p = d.add_paragraph("The quick ")
            r = p.add_run("brown")
            r.bold = True
            p.add_run(" fox jumps over the lazy dog.").italic = True
            d.add_paragraph("Second paragraph stays the same.")
        src, md, base = self.make(build)
        cur = base.replace("brown", "red")
        self.assertNotEqual(cur, base)
        res, out = self.run_case(src, md, base, cur)
        rs = runs_with(out, "red")
        self.assertEqual(len(rs), 1)
        self.assertIsNotNone(rs[0].find(f"{W}rPr/{W}b"))           # 换掉的词还是粗体
        self.assertIsNotNone(runs_with(out, "fox")[0].find(f"{W}rPr/{W}i"))
        self.assertEqual(res["applied"], 1)

    def test_chinese_char_level(self):
        def build(d):
            d.add_paragraph("本项目开发面向组会的桌面软件，实时转写多人发言。")
            d.add_paragraph("第二段不动。")
        src, md, base = self.make(build)
        cur = base.replace("面向组会的", "面向课题组组会的").replace("实时转写", "实时识别并转写")
        self.run_case(src, md, base, cur)

    def test_insert_and_delete_sentence(self):
        def build(d):
            d.add_paragraph("Bloom listens to the meeting. It edits the paper. It keeps every change.")
            d.add_paragraph("会上的意见多为口头表达。会后依靠笔记整理。容易漏改。")
        src, md, base = self.make(build)
        cur = (base.replace(" It edits the paper.", "")                     # 删一句（英文）
               .replace("It keeps every change.", "It keeps every change. Each change can be undone.")   # 加一句
               .replace("会后依靠笔记整理。", "")                            # 删一句（中文）
               .replace("容易漏改。", "容易漏改，也可能错改。"))
        self.run_case(src, md, base, cur)

    def test_insert_and_delete_paragraph(self):
        def build(d):
            d.add_heading("一、项目简介", level=1)
            d.add_paragraph("第一段：研究背景。")
            d.add_paragraph("第二段：会被删掉的一段。")
            d.add_paragraph("第三段：研究目标。")
            d.add_heading("二、研究内容", level=1)
            d.add_paragraph("内容段落。")
        src, md, base = self.make(build)
        lines = base.split("\n\n")
        i2 = next(i for i, x in enumerate(lines) if "第二段" in x)
        lines.pop(i2)
        i3 = next(i for i, x in enumerate(lines) if "第三段" in x)
        lines.insert(i3 + 1, "新加的一段：研究方法和技术路线。")
        ih = next(i for i, x in enumerate(lines) if "二、研究内容" in x)
        lines.insert(ih, "# 三、新加的一节")
        cur = "\n\n".join(lines)
        res, out = self.run_case(src, md, base, cur)
        self.assertEqual(res["paras_removed"], 1)
        self.assertEqual(res["paras_added"], 2)
        from lxml import etree
        root = etree.fromstring(zipfile.ZipFile(out).read("word/document.xml"))
        styles = {"".join(t.text or "" for t in p.iter(W + "t")): (p.find(f"{W}pPr/{W}pStyle").get(W + "val")
                  if p.find(f"{W}pPr/{W}pStyle") is not None else None) for p in root.iter(W + "p")}
        self.assertEqual(styles["三、新加的一节"], styles["二、研究内容"])     # 新标题照着同级标题的样式
        self.assertEqual(styles["新加的一段：研究方法和技术路线。"], styles["第三段：研究目标。"])

    def test_nested_table_cells(self):
        def build(d):
            d.add_paragraph("表格前的一段。")
            t = d.add_table(rows=2, cols=2)
            t.style = "Table Grid"
            t.cell(0, 0).text = "项目名称"
            t.cell(0, 1).text = "组会论文实时修订助手"
            t.cell(1, 0).text = "说明"
            inner = t.cell(1, 1).add_table(rows=1, cols=2)
            inner.cell(0, 0).text = "内层左格 alpha"
            inner.cell(0, 1).text = "内层右格 beta"
            d.add_paragraph("表格后的一段。")
        src, md, base = self.make(build)
        self.assertIn("内层右格", base)
        cur = base.replace("内层右格 beta", "内层右格 gamma 已改").replace("实时修订助手", "实时修订系统")
        self.run_case(src, md, base, cur)

    def test_form_cell_body_and_new_paragraph_in_cell(self):
        long1 = "课题组组会上，导师和同学提出的论文修改意见多为口头表达，会后依靠笔记和记忆整理，容易出现漏改、错改。" * 2
        long2 = "本项目开发面向组会的桌面软件 Bloom，实时转写多人发言，并由后台模型直接写入论文原稿。" * 2

        def build(d):
            t = d.add_table(rows=3, cols=2)
            t.style = "Table Grid"
            t.cell(0, 0).text = "项目名称"
            t.cell(0, 1).text = "组会助手"
            c = t.cell(1, 0).merge(t.cell(1, 1))
            c.paragraphs[0].add_run("一、项目简介").bold = True
            c.add_paragraph(long1)
            c.add_paragraph(long2)
            c2 = t.cell(2, 0).merge(t.cell(2, 1))
            c2.paragraphs[0].add_run("二、立项依据").bold = True
            c2.add_paragraph("组会是课题组开展科研训练的主要形式。" * 6)
        src, md, base = self.make(build)
        self.assertIn("一、项目简介", base)
        lines = base.split("\n\n")
        i = next(k for k, x in enumerate(lines) if x.startswith("课题组组会上"))
        lines[i] = lines[i].replace("容易出现漏改", "很容易出现漏改")
        lines.insert(i + 1, "新插入格子里的一段：说明三个难点。")
        cur = "\n\n".join(lines)
        res, out = self.run_case(src, md, base, cur)
        from lxml import etree
        root = etree.fromstring(zipfile.ZipFile(out).read("word/document.xml"))
        newp = next(p for p in root.iter(W + "p") if "新插入格子里的一段" in "".join(t.text or "" for t in p.iter(W + "t")))
        self.assertTrue(any(a.tag == W + "tc" for a in newp.iterancestors()))   # 新段落在格子里

    def test_crlf_md(self):
        def build(d):
            d.add_paragraph("Windows 上写出来的 Markdown 是 CRLF 换行。")
            d.add_paragraph("第二段。")
        src, md, base = self.make(build)
        base_crlf = base.replace("\n", "\r\n")
        cur = base.replace("CRLF 换行", "CRLF（\\r\\n）换行") + "\n新加一段在最后。\n"
        md.write_bytes(cur.replace("\n", "\r\n").encode("utf-8"))
        res = wb.writeback(md, base_text=base_crlf)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["skipped"], [])
        self.assertEqual(plain_lines(convert(Path(res["out"]))), plain_lines(cur))

    def test_fill_in_blank_line(self):
        """封面填空线（DOCX_VERSION 3 转成"指导教师：＿＿＿"，Word 里是"指导教师⇥⇥⇥"、后两个制表符带下划线，冒号是转换器补的）
        和正文里的"□其他：____"（带下划线的空格）：会上把空填上，写得回去，填的字落在原来那段线上、带下划线。
        2026-11-15 联调：这种行以前整行"在 Word 里对不齐"，填空一处都写不回去。"""
        def build(d):
            p = d.add_paragraph()
            p.add_run("指导教师")
            r = p.add_run("\t")
            r.underline = False
            r = p.add_run("\t\t")
            r.underline = True
            p = d.add_paragraph()
            p.add_run("□其他：")
            r = p.add_run("          ")
            r.underline = True
            d.add_paragraph("正文一段，没有填空。")
        src, md, base = self.make(build)
        self.assertRegex(base, "指导教师：" + pdfconv.FILL + "{3,}")
        self.assertRegex(base, "其他：" + pdfconv.FILL + "{3,}")
        cur = re.sub("指导教师：" + pdfconv.FILL + "+", "指导教师：周老师", base)
        cur = re.sub("其他：" + pdfconv.FILL + "+", "其他：开源社区需求", cur).replace("没有填空", "没有填空线")
        res, out = self.run_case(src, md, base, cur)
        self.assertEqual(res["applied"], 3, res)
        for needle in ("周老师", "开源社区需求"):
            rs = runs_with(out, needle)
            self.assertEqual(len(rs), 1, needle)
            u = rs[0].find(f"{W}rPr/{W}u")
            self.assertIsNotNone(u, needle)
            self.assertEqual(u.get(f"{W}val"), "single", needle)
        lab = runs_with(out, "指导教师")[0].find(f"{W}rPr/{W}u")
        self.assertTrue(lab is None or lab.get(f"{W}val") == "none")    # 标签还是不带下划线

    def test_mixed_chinese_english(self):
        def build(d):
            d.add_paragraph("Bloom 使用 Whisper large-v3 做转写，判断准确率由 92.1% 提高到 96.1%。")
            d.add_paragraph("The classifier runs in 15 至 34 毫秒 per sentence.")
        src, md, base = self.make(build)
        cur = (base.replace("Whisper large-v3", "SenseVoice small").replace("96.1%", "96.5%")
               .replace("runs in", "runs locally in"))
        self.run_case(src, md, base, cur)

    def test_unlocatable_change_is_skipped(self):
        def build(d):
            t = d.add_table(rows=2, cols=2)
            t.style = "Table Grid"
            for i, (a, b) in enumerate([("指标", "数值"), ("准确率", "92.1%")]):
                t.cell(i, 0).text, t.cell(i, 1).text = a, b
            d.add_paragraph("表后正文。")
        src, md, base = self.make(build)
        lines = base.split("\n")
        k = next(i for i, x in enumerate(lines) if "准确率" in x)
        lines.insert(k + 1, "| 误答率 | 6.6% |")                     # 新增表格行：不动 Word 表格结构，跳过
        cur = "\n".join(lines).replace("表后正文。", "表后正文，改了。")
        res, out = self.run_case(src, md, base, cur, expect_skips=1)
        self.assertIn("表格行", res["skipped"][0]["reason"])
        self.assertTrue(runs_with(out, "表后正文，改了。"))

    def test_locked_target_falls_back_to_new_name(self):
        def build(d):
            d.add_paragraph("目标文件正被 Word 打开。")
        src, md, base = self.make(build)
        cur = base.replace("正被 Word 打开", "正被 Word 打开着")
        res, out = self.run_case(src, md, base, cur)
        with open(out, "rb"):                                            # 占着（Word 打开时一样写不进去）
            md.write_text(cur.replace("打开着", "打开着，换个名字写"), encoding="utf-8")
            r2 = wb.writeback(md, base_text=base)
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(Path(r2["out"]).name, "论文（会上修订 2）.docx")
        self.assertTrue(any("写不进去" in n for n in r2["notes"]))

    def test_no_change_writes_nothing(self):
        def build(d):
            d.add_paragraph("没改。")
        src, md, base = self.make(build)
        res = wb.writeback(md, base_text=base)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["out"])
        self.assertFalse((self.tmp / "论文（会上修订）.docx").exists())

    def test_find_base_by_fingerprint_and_no_overwrite_of_user_file(self):
        def build(d):
            d.add_paragraph("指纹找原样。")
        src, md, base = self.make(build)
        cur = base.replace("指纹找原样", "按指纹找到转换时的原样")
        md.write_text(cur, encoding="utf-8")
        home = Path(os.environ["ZUHUI_HOME"])
        snap = home / "papers" / "20261114-000000-论文-abcd"
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "original.md").write_bytes(base.replace("\n", "\r\n").encode("utf-8"))
        meta_p = md.parent / ".bloom-convert.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        meta["md_sha1"] = wb._sha1(base.replace("\n", "\r\n").encode("utf-8"))   # 转换时写的是 CRLF
        meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        r1 = wb.writeback(md, state_dir=home)
        self.assertTrue(r1["ok"], r1)
        self.assertTrue(r1["base_from"].endswith("original.md"))
        out1 = Path(r1["out"])
        r2 = wb.writeback(md, state_dir=home)                         # 自己上次写的：覆盖
        self.assertEqual(Path(r2["out"]), out1)
        out1.write_bytes(out1.read_bytes() + b"")                      # 内容没变：仍算自己写的
        with zipfile.ZipFile(out1, "a") as z:                          # 用户改过（这里往包里加个东西模拟）
            z.writestr("customXml/x.xml", "<x/>")
        r3 = wb.writeback(md, state_dir=home)
        self.assertEqual(Path(r3["out"]).name, "论文（会上修订 2）.docx")
        self.assertTrue(any("没覆盖" in n for n in r3["notes"]))


if __name__ == "__main__":
    unittest.main()
