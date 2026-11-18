# -*- coding: utf-8 -*-
"""第 6 批：会上改完的写回 Word 用修订模式（w:ins / w:del）。
样例是自己造的真实结构 Word：多 run 的段落（加粗、斜体、字体）、表格、脚注、标题、列表。每条：
  - 全部接受（pandoc --track-changes=accept）= 会上改完的；全部拒绝（reject）= 原来的 Word
  - 修订数对得上（报告里的 ins/del 和 XML 里数出来的一样）、编号不重复、作者和日期都在
  - 删掉的字保住原 run 的格式（加粗、字体），加的字抄旁边 run 的格式
  - 除了 document.xml 别的部件一个字节都不变（脚注、样式都不动）；python-docx 打得开
跑：python -X utf8 -m unittest tests.test_b6_track -v（没有 pandoc 时跳过）"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ZUHUI_HOME"] = os.environ.get("ZUHUI_HOME") or tempfile.mkdtemp(prefix="zuhui-test-home-")

from lxml import etree  # noqa: E402

from zuhui import docx_writeback as wb, paper_prefs, pdfconv  # noqa: E402

PANDOC = shutil.which("pandoc")
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS


def plain(text: str) -> list[str]:
    return [re.sub(r"\s", "", wb.md_plain(x)) for x in wb.md_lines(text) if wb.line_kind(x)[0] != "sep"]


def convert(docx: Path) -> str:
    stage = Path(tempfile.mkdtemp(prefix="trk-"))
    try:
        md, _ = pdfconv.convert_docx(docx, stage, PANDOC)
        return md
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def pandoc_text(docx: Path, how: str) -> str:
    r = subprocess.run([PANDOC, str(docx), "-t", "plain", "--wrap=none", f"--track-changes={how}"],
                       capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return re.sub(r"\s+", "", r.stdout.decode("utf-8", "replace"))


def add_footnote(docx: Path, needle: str, note: str) -> None:
    """给 python-docx 造的 Word 加一条真正的脚注：footnotes.xml 部件、关系、内容类型，正文里那一段末尾放脚注号。"""
    zin = zipfile.ZipFile(docx)
    items = {n: zin.read(n) for n in zin.namelist()}
    zin.close()
    foot = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:footnotes xmlns:w="{W_NS}">'
            '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
            '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
            f'<w:footnote w:id="1"><w:p><w:r><w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> {note}</w:t></w:r></w:p></w:footnote>'
            '</w:footnotes>').encode("utf-8")
    items["word/footnotes.xml"] = foot
    rels = items["word/_rels/document.xml.rels"].decode("utf-8")
    rels = rels.replace("</Relationships>", '<Relationship Id="rIdFoot1" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" '
                        'Target="footnotes.xml"/></Relationships>')
    items["word/_rels/document.xml.rels"] = rels.encode("utf-8")
    ct = items["[Content_Types].xml"].decode("utf-8")
    ct = ct.replace("</Types>", '<Override PartName="/word/footnotes.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>')
    items["[Content_Types].xml"] = ct.encode("utf-8")
    root = etree.fromstring(items["word/document.xml"])
    for p in root.iter(W + "p"):
        if needle in "".join(t.text or "" for t in p.iter(W + "t")):
            r = etree.SubElement(p, W + "r")
            rpr = etree.SubElement(r, W + "rPr")
            etree.SubElement(rpr, W + "vertAlign").set(W + "val", "superscript")
            etree.SubElement(r, W + "footnoteReference").set(W + "id", "1")
            break
    items["word/document.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in items.items():
            z.writestr(n, data)


def build_paper(d):
    """一篇像样的中文论文：标题、多 run 正文（加粗、斜体、换字体）、列表、表格、带脚注的段落、英文段。"""
    from docx.shared import Pt
    d.add_heading("基于实时转写的组会助手", level=1)
    d.add_heading("1 引言", level=2)
    p = d.add_paragraph("组会上老师的意见")
    r = p.add_run("常常口头给出")
    r.bold = True
    p.add_run("，学生会后")
    r = p.add_run("凭记忆")
    r.italic = True
    r.font.name = "Times New Roman"
    r.font.size = Pt(12)
    p.add_run("修改论文，容易遗漏。")
    d.add_paragraph("本文提出 Bloom，一个会上实时改稿的助手。")
    d.add_paragraph("贡献一：实时转写与判断何时发言。", style="List Bullet")
    d.add_paragraph("贡献二：会上拍板的改动直接落到论文上。", style="List Bullet")
    d.add_heading("2 实验", level=2)
    t = d.add_table(rows=3, cols=3)
    t.style = "Table Grid"
    for i, row in enumerate([("方法", "准确率", "延迟"), ("基线", "75.1%", "820 ms"), ("Bloom", "94.9%", "310 ms")]):
        for j, v in enumerate(row):
            t.cell(i, j).text = v
    d.add_paragraph("表 1 给出了在公开数据集上的结果，Bloom 的准确率明显更高。")
    d.add_paragraph("The classifier runs in 15 ms per sentence on a laptop GPU.")
    d.add_paragraph("最后一段保持不变。")


@unittest.skipUnless(PANDOC, "没有 pandoc")
class TrackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trk-case-"))
        paper_prefs.save({"docx_mode": "track"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, build=build_paper, foot: tuple | None = ("表 1 给出了", "数据集来自 2026 年的公开评测。")):
        import docx
        d = docx.Document()
        build(d)
        src = self.tmp / "论文.docx"
        d.save(src)
        if foot:
            add_footnote(src, *foot)
        base = convert(src)
        folder = self.tmp / "论文-bloom"
        folder.mkdir()
        md = folder / "论文.md"
        md.write_text(base, encoding="utf-8")
        meta = {"engine": "pandoc", "source": str(src), "src_mtime": src.stat().st_mtime,
                "md_sha1": wb._sha1(md.read_bytes()), "conv": pdfconv.DOCX_VERSION}
        (folder / ".bloom-convert.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return src, md, base

    def check(self, src: Path, md: Path, base: str, cur: str, author="小助手"):
        md.write_text(cur, encoding="utf-8")
        res = wb.writeback(md, base_text=base, author=author)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["mode"], "track")
        self.assertEqual(res["skipped"], [])
        out = Path(res["out"])
        # 全部接受 = 会上改完的；全部拒绝 = 原来的
        self.assertEqual(plain(convert(out)), plain(cur))
        self.assertEqual(pandoc_text(out, "reject"), pandoc_text(src, "accept"))
        self.assertEqual(pandoc_text(out, "accept"), re.sub(r"\s+", "", self._accept_plain(cur)))
        # 修订数、编号、作者、日期
        root = etree.fromstring(zipfile.ZipFile(out).read("word/document.xml"))
        ins, dels = list(root.iter(W + "ins")), list(root.iter(W + "del"))
        self.assertEqual(res["revisions"], {"ins": len(ins), "del": len(dels)})
        ids = [x.get(W + "id") for x in ins + dels]
        self.assertEqual(len(ids), len(set(ids)))
        for x in ins + dels:
            self.assertEqual(x.get(W + "author"), author)
            self.assertRegex(x.get(W + "date") or "", r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        for x in dels:                       # 删的字只在 w:delText 里
            if x.getparent().tag == W + "rPr":
                continue                     # 段落标记
            self.assertFalse(list(x.iter(W + "t")))
            self.assertTrue(list(x.iter(W + "delText")) or list(x.iter(W + "footnoteReference")))
        for x in ins:
            if x.getparent().tag == W + "rPr":
                continue
            self.assertFalse(list(x.iter(W + "delText")))
        # 别的部件一字节不差（脚注、样式、编号）
        zs, zo = zipfile.ZipFile(src), zipfile.ZipFile(out)
        self.assertEqual(zs.namelist(), zo.namelist())
        for n in zs.namelist():
            if n != "word/document.xml":
                self.assertEqual(zs.read(n), zo.read(n), n)
        import docx
        docx.Document(str(out))              # 打得开
        keep = os.environ.get("WB_KEEP")
        if keep:
            Path(keep).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(out, Path(keep) / f"{self._testMethodName}.docx")
        return res, out, root

    def _accept_plain(self, cur: str) -> str:
        """会上改完的 md 转成 pandoc plain 的样子：再造一份直接改的 Word 转一遍最省事（避开 md 和 plain 的写法差别）。"""
        return self._direct_text

    def _direct(self, src, md, base, cur):
        md.write_text(cur, encoding="utf-8")
        r = wb.writeback(md, base_text=base, mode="direct")
        self.assertTrue(r.get("ok"), r)
        txt = pandoc_text(Path(r["out"]), "accept")
        Path(r["out"]).unlink()
        (md.parent / wb.WB_STATE).unlink(missing_ok=True)
        self._direct_text = txt

    def run_both(self, src, md, base, cur, **kw):
        self._direct(src, md, base, cur)
        return self.check(src, md, base, cur, **kw)

    # ---------------- 用例 ----------------
    def test_multi_run_paragraph_keeps_format(self):
        src, md, base = self.make()
        cur = base.replace("常常口头给出", "常常只在口头给出").replace("凭记忆", "靠回忆")
        res, out, root = self.run_both(src, md, base, cur)
        # "靠回忆"换掉"凭记忆"：删的那截还是斜体 + Times New Roman，加的那截抄同一个 run 的格式
        dels = [x for x in root.iter(W + "del") if "凭" in "".join(t.text or "" for t in x.iter(W + "delText"))]
        self.assertTrue(dels)
        rpr = dels[0].find(f"{W}r/{W}rPr")
        self.assertIsNotNone(rpr.find(W + "i"))
        self.assertEqual(rpr.find(W + "rFonts").get(W + "ascii"), "Times New Roman")
        ins = [x for x in root.iter(W + "ins") if "靠" in "".join(t.text or "" for t in x.iter(W + "t"))]
        self.assertTrue(ins)
        self.assertIsNotNone(ins[0].find(f"{W}r/{W}rPr/{W}i"))
        bold_ins = [x for x in root.iter(W + "ins") if "只在" in "".join(t.text or "" for t in x.iter(W + "t"))]
        self.assertTrue(bold_ins)
        self.assertIsNotNone(bold_ins[0].find(f"{W}r/{W}rPr/{W}b"))
        self.assertGreaterEqual(res["revisions"]["ins"], 2)

    def test_table_cell_and_footnote_paragraph(self):
        src, md, base = self.make()
        cur = (base.replace("94.9%", "95.2%").replace("明显更高", "高出二十个百分点")
               .replace("15 ms", "12 ms"))
        res, out, root = self.run_both(src, md, base, cur)
        tbl = root.find(f"{W}body/{W}tbl")
        self.assertTrue(list(tbl.iter(W + "ins")) and list(tbl.iter(W + "del")))     # 表格格子里也是修订
        self.assertEqual(len(list(root.iter(W + "footnoteReference"))), 1)           # 脚注号还在，没被夹进修订
        for x in root.iter(W + "del"):
            self.assertFalse(list(x.iter(W + "footnoteReference")))

    def test_new_and_removed_paragraphs(self):
        src, md, base = self.make()
        lines = base.split("\n")
        k = next(i for i, x in enumerate(lines) if "本文提出 Bloom" in x)
        lines.insert(k + 1, "")
        lines.insert(k + 2, "我们在三个课题组里用了一个学期。")
        cur = "\n".join(lines)
        cur = re.sub(r"\n最后一段保持不变。\n?", "\n", cur)
        self.assertNotIn("最后一段", cur)
        res, out, root = self.run_both(src, md, base, cur)
        self.assertEqual(res["paras_added"], 1)
        self.assertEqual(res["paras_removed"], 1)
        marks = [x for x in root.iter(W + "ins", W + "del") if x.getparent().tag == W + "rPr"
                 and x.getparent().getparent().tag == W + "pPr"]
        self.assertEqual(sorted(x.tag for x in marks), sorted([W + "ins", W + "del"]))   # 两个段落标记各记一笔

    def test_heading_list_and_english(self):
        src, md, base = self.make(foot=None)
        cur = (base.replace("1 引言", "1 研究背景").replace("贡献二：会上拍板的改动直接落到论文上。",
                                                          "贡献二：会上拍板的改动当场落到论文上。")
               .replace("runs in 15 ms", "runs locally in 15 ms"))
        res, out, root = self.run_both(src, md, base, cur)
        self.assertGreaterEqual(res["revisions"]["ins"], 3)

    def test_ids_above_existing(self):
        src, md, base = self.make()
        z = zipfile.ZipFile(src)
        top = wb._max_id(z)
        cur = base.replace("容易遗漏", "很容易遗漏")
        res, out, root = self.run_both(src, md, base, cur)
        ids = [int(x.get(W + "id")) for x in root.iter(W + "ins", W + "del")]
        self.assertTrue(all(i > top for i in ids))

    def test_direct_mode_still_available(self):
        src, md, base = self.make()
        paper_prefs.save({"docx_mode": "direct"})
        md.write_text(base.replace("容易遗漏", "很容易遗漏"), encoding="utf-8")
        res = wb.writeback(md, base_text=base)
        self.assertEqual(res["mode"], "direct")
        root = etree.fromstring(zipfile.ZipFile(res["out"]).read("word/document.xml"))
        self.assertFalse(list(root.iter(W + "ins")) or list(root.iter(W + "del")))

    def test_prefs(self):
        self.assertEqual(paper_prefs.save({"docx_mode": "direct"})["docx_mode"], "direct")
        self.assertEqual(paper_prefs.load()["docx_mode"], "direct")
        with self.assertRaises(ValueError):
            paper_prefs.save({"docx_mode": "nope"})
        paper_prefs.PREFS_FILE.write_text("{坏的", encoding="utf-8")
        self.assertEqual(paper_prefs.load()["docx_mode"], "track")


if __name__ == "__main__":
    unittest.main()
