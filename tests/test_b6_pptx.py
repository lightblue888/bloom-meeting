# -*- coding: utf-8 -*-
"""第 6 批：加载 PPT（zuhui/pptx_conv.py、papers.import_file）。
用 python-pptx 造一份像样的组会汇报：封面、多级项目符号、表格、图、组合里的文字框、备注、隐藏页。
看转出来的讲稿：每页一节（第 N 页 · 标题）、正文按级别缩进、表格成管道表、图存进 figures/ 并在原位写占位行、
备注跟在那一页后面、隐藏页不进；导入后标成只读（readonly），原 PPT 一个字节都不动。
跑：python -X utf8 -m unittest tests.test_b6_pptx -v（没有 python-pptx 时跳过）"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ZUHUI_HOME"] = os.environ.get("ZUHUI_HOME") or tempfile.mkdtemp(prefix="zuhui-test-home-")

from zuhui import pptx_conv  # noqa: E402

try:
    import pptx  # noqa: F401
    from pptx.util import Inches
    HAVE = True
except ImportError:
    HAVE = False


def png_bytes() -> bytes:
    import struct
    import zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\xff\x80\x40" * 4 for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_deck(path: Path) -> None:
    from pptx import Presentation
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[0])               # 封面
    s.shapes.title.text = "组会进展汇报"
    s.placeholders[1].text = "张三 · 2026 年 11 月"
    s = prs.slides.add_slide(prs.slide_layouts[1])               # 标题 + 多级项目符号 + 备注
    s.shapes.title.text = "本周进展"
    tf = s.placeholders[1].text_frame
    tf.text = "完成了转写模块"
    p = tf.add_paragraph()
    p.text = "延迟降到 310 毫秒"
    p.level = 1
    p = tf.add_paragraph()
    p.text = "下一步：接上改稿"
    s.notes_slide.notes_text_frame.text = "这里要说清楚为什么延迟下降。"
    s = prs.slides.add_slide(prs.slide_layouts[5])               # 标题 + 表格
    s.shapes.title.text = "实验结果"
    tbl = s.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(6), Inches(1.5)).table
    for i, row in enumerate([("方法", "准确率", "延迟"), ("基线", "75.1%", "820 ms"), ("Bloom", "94.9%", "310 ms")]):
        for j, v in enumerate(row):
            tbl.cell(i, j).text = v
    s = prs.slides.add_slide(prs.slide_layouts[5])               # 标题 + 图 + 组合里的文字框
    s.shapes.title.text = "系统结构"
    s.shapes.add_picture(io.BytesIO(png_bytes()), Inches(1), Inches(2), Inches(2), Inches(2))
    grp = s.shapes.add_group_shape()
    tb = grp.shapes.add_textbox(Inches(5), Inches(2), Inches(3), Inches(1))
    tb.text_frame.text = "前端 → 后端 → 模型"
    s = prs.slides.add_slide(prs.slide_layouts[1])               # 隐藏页
    s.shapes.title.text = "备用页"
    s.placeholders[1].text = "不该出现"
    s._element.set("show", "0")
    prs.save(path)


@unittest.skipUnless(HAVE, "没有 python-pptx")
class PptxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pptx-case-"))
        self.src = self.tmp / "汇报.pptx"
        make_deck(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_convert(self):
        stage = self.tmp / "stage"
        stage.mkdir()
        md, st = pptx_conv.convert_pptx(self.src, stage)
        self.assertEqual(st["slides"], 4)
        self.assertEqual(st["figures"], 1)
        self.assertEqual(st["tables"], 1)
        self.assertIn("## 第 1 页 · 组会进展汇报", md)
        self.assertIn("张三 · 2026 年 11 月", md)
        self.assertIn("## 第 2 页 · 本周进展", md)
        self.assertIn("- 完成了转写模块", md)
        self.assertIn("  - 延迟降到 310 毫秒", md)          # 第二级缩进
        self.assertIn("备注：这里要说清楚为什么延迟下降。", md)
        self.assertIn("| 方法 | 准确率 | 延迟 |", md)
        self.assertIn("| Bloom | 94.9% | 310 ms |", md)
        self.assertIn("[image: 图 1]", md)
        self.assertIn("前端 → 后端 → 模型", md)
        self.assertNotIn("不该出现", md)                    # 隐藏页
        self.assertNotIn("备用页", md)
        figs = json.loads((stage / "figures" / "figures.json").read_text(encoding="utf-8"))
        self.assertEqual(figs["1"]["source"], "pptx")
        self.assertTrue((stage / "figures" / figs["1"]["file"]).is_file())
        # 第 2 页的正文在备注前面、第 3 页前面
        self.assertLess(md.index("下一步：接上改稿"), md.index("备注："))
        self.assertLess(md.index("备注："), md.index("## 第 3 页"))

    def test_import_marks_readonly(self):
        from zuhui import papers
        before = hashlib.sha1(self.src.read_bytes()).hexdigest()
        info = papers.import_file(self.src)
        self.assertEqual(info["kind"], "pptx")
        md = Path(info["path"])
        self.assertEqual(md.parent.name, "汇报-bloom")
        self.assertTrue(pptx_conv.readonly(md))
        self.assertTrue((md.parent / "figures" / "fig1.png").is_file())
        self.assertEqual(hashlib.sha1(self.src.read_bytes()).hexdigest(), before)
        meta = json.loads((md.parent / ".bloom-convert.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["engine"], "pptx")
        # 普通的 Markdown 不是只读
        other = self.tmp / "笔记.md"
        other.write_text("# 笔记\n\n正文。\n", encoding="utf-8")
        self.assertFalse(pptx_conv.readonly(other))

    def test_bad_file(self):
        bad = self.tmp / "坏的.pptx"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(pptx_conv.PptxError):
            pptx_conv.convert_pptx(bad, self.tmp)

    def test_viewer_refuses_edit(self):
        from zuhui import papers, view
        info = papers.import_file(self.src)
        v = view.Viewer.__new__(view.Viewer)
        v.mirror = Path(info["path"])
        r = v.para_source("完成了转写模块")
        self.assertFalse(r["ok"])
        self.assertEqual(r.get("error"), pptx_conv.READONLY_SAY[0])


if __name__ == "__main__":
    unittest.main()
