# -*- coding: utf-8 -*-
"""导出和分享（第 5 批）：纪要排版（每个人、时间、上次遗留、议程、发言时长都在）、带格式复制、Word（pandoc）、PDF（Edge
无界面打印）、会议包（单个 HTML，图内嵌、离线、深浅色）、散会后自动保存（设置、限长校验、文件名、失败记录）、散会收尾里
调到它、HTTP 接口。

全离线、不花钱：不调任何模型（散会收尾那一组把转写、整理、纪要都换成假的）；ZUHUI_HOME 指到临时目录，不碰真实的
~/.zuhui、不碰正在跑的 Bloom。本机没有 pandoc / Edge 时，对应的几条跳过。

    python -X utf8 -m unittest tests.test_b5_export -v
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-b5-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import autosave, components  # noqa: E402
from zuhui import export as ex  # noqa: E402
from zuhui import meetings as mt  # noqa: E402

CFG = {"meeting": {"me": "林晓"}, "assistant": {"name": "小B"}}
MID = "20261118-143000"


def png(w=40, h=20, rgb=(80, 140, 220)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def ch(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")


SUMMARY = {
    "title": "进展汇报", "template": "default", "speakers": ["林晓", "王一凡"],
    "abstract": "王一凡汇报进展，定了下周补实验。<b>不是标签</b>",
    "conclusions": [{"text": "TimeGAN 下周补上", "t": "02:10", "sec": 130}, {"text": "表 2 更新"}],
    "sections": [{"title": "数据划分", "t": "03:30", "sec": 210,
                  "cards": [{"title": "按时间切", "bullets": [{"text": "训练集到 2020 年", "t": "03:31", "sec": 211}]}]}],
    "people": [{"name": "王一凡", "said": [{"text": "基线跑完了", "t": "01:05", "sec": 65}],
                "feedback": [{"text": "下周补上", "by": "林晓", "t": "02:10", "sec": 130}], "asks": [{"text": "更新表 2", "by": "林晓"}]}],
    "todos": [{"text": "王一凡补 TimeGAN", "who": "王一凡", "t": "02:10", "sec": 130}],
    "carried": [{"id": "c2", "item": "画图 3", "done": False, "text": ""},
                {"id": "c1", "item": "补充相关工作", "done": True, "text": "加了三篇", "t": "05:00", "sec": 300}],
    "agenda": [{"id": "a1", "title": "王一凡进展", "who": "王一凡", "covered": True, "text": "讲完了"},
               {"id": "a2", "title": "讨论第三章", "covered": False, "text": ""}],
}
LINES = [
    {"t": "00:12", "sec": 12, "who": "teacher", "text": "我们开始吧"},
    {"t": "00:30", "sec": 30, "who": "system", "text": "切到 paper.md"},
    {"t": "01:05", "sec": 65, "who": "guest", "name": "王一凡", "text": "我这周把基线跑完了"},
    {"t": "01:30", "sec": 90, "who": "student", "text": "好的，记下了"},
]
TASKS = [
    {"id": "T1", "idx": 1, "title": "补 TimeGAN 实验", "note": True, "remark": "参会者王一凡说：这个要补", "plan": "加一行到表 2",
     "need": "实验数据", "section": "3.2 实验", "status": "done"},
    {"id": "T2", "idx": 2, "title": "改摘要第一句", "note": False, "status": "done", "section": "摘要"},
    {"id": "T3", "idx": 3, "title": "图 3 坐标轴", "note": True, "remark": "", "plan": "", "section": "4 结果", "status": "queued"},
    {"id": "T4", "idx": 4, "title": "摘要太长", "note": True, "remark": "", "plan": "删两句", "section": "摘要", "status": "done"},
    {"id": "T5", "idx": 5, "title": "转写卡了一下", "note": True, "app": True, "remark": "", "plan": "", "status": "done"},
    {"id": "T6", "idx": 6, "title": "撤回的改动", "note": False, "status": "done", "rejected": True},
]
DIFF = ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<link rel=\"stylesheet\" href=\"https://fonts.googleapis.com/css2?family=Noto+Serif+SC\">"
        "<style>:root{--ink:#111}@media (prefers-color-scheme: dark){:root{--ink:#eee}}</style></head><body>"
        "<div class='docs'><section class='row chg'><div class='text'><div class='rowhead'><span class='label'>第 1 段</span></div>"
        "<p class='para'>改了 <ins>一句</ins></p></div></section>"
        "<section class='row'><div class='text'><div class='rowhead'><span class='label'>图 1</span></div>"
        "<p class='para small fixed'>图 1未改动。</p></div></section></div></body></html>")


def make_meeting(root: Path, mid: str = MID, summary: dict | None = SUMMARY, diff: str | None = DIFF, title: str = "第三章 实验设计") -> Path:
    """一场散会了的会：逐字稿、纪要、任务板、勾掉的、修订对比、参会人、论文文件夹里的图。"""
    d = root / mid
    d.mkdir(parents=True, exist_ok=True)
    paper = root.parent / f"paper-{mid}"
    (paper / "figures").mkdir(parents=True, exist_ok=True)
    (paper / "paper.md").write_text("# 论文\n", encoding="utf-8")
    (paper / "figures" / "fig1.png").write_bytes(png())
    (d / "meta.json").write_text(json.dumps({"id": mid, "title": title, "started": "2026-11-18T14:30:00", "ended": "2026-11-18T15:15:00",
                                             "state": "ended", "duration_s": 2712,
                                             "papers": [{"id": "p1", "name": "paper", "path": str(paper / "paper.md")}]},
                                            ensure_ascii=False), encoding="utf-8")
    (d / "transcript.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in LINES), encoding="utf-8")
    (d / "board.json").write_text(json.dumps({"tasks": TASKS}, ensure_ascii=False), encoding="utf-8")
    (d / "carry.json").write_text(json.dumps({"done": {"note:T1": True}}), encoding="utf-8")
    (d / "participants.json").write_text(json.dumps([{"id": "me", "name": "林晓", "role": "teacher", "you": True},
                                                     {"id": "claude", "name": "小B", "role": "student"},
                                                     {"id": "g1", "name": "王一凡", "role": "guest"}], ensure_ascii=False), encoding="utf-8")
    if diff:
        (d / "diff.html").write_text(diff, encoding="utf-8")
    lib = mt.Meetings(root)
    if summary:
        lib.set_summary(mid, summary)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="b5-"))
        self.root = self.tmp / "meetings"
        make_meeting(self.root)
        self.lib = mt.Meetings(self.root)
        self.g = self.lib.get(MID)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class MinutesTest(Base):
    def test_everything_is_in(self):
        text = ex.blocks_text(ex.minutes_blocks(self.g, CFG, "zh"))
        for s in ("进展汇报", "2026-11-18 14:30 · 45:12 · 第三章 实验设计", "参会者：林晓、小B、王一凡", "主题摘要", "核心结论",
                  "1. TimeGAN 下周补上（02:10）", "2. 表 2 更新", "01 数据划分（03:30）", "按时间切", "- 训练集到 2020 年（03:31）",
                  "\n参会者\n", "王一凡", "发言要点", "- 基线跑完了（01:05）", "意见", "- 林晓：下周补上（02:10）", "待办",
                  "- 林晓：更新表 2", "后续工作安排", "- 王一凡补 TimeGAN（02:10）", "遗留事项", "- [已完成] 补充相关工作：加了三篇（05:00）",
                  "- [未完成] 画图 3", "发言时长"):
            self.assertIn(s, text)
        # 议程功能删了：旧纪要里带着 agenda 字段也不导出、不报错
        for s in ("议程", "讲到了", "讨论第三章"):
            self.assertNotIn(s, text)
        # 遗留：落实了的在前；主题在每个人前面（默认格式）
        self.assertLess(text.index("[已完成]"), text.index("[未完成]"))
        self.assertLess(text.index("01 数据划分"), text.index("\n参会者\n"))
        # 发言时长：三个人都在，名字是参会人里的
        talk = text[text.index("发言时长"):]
        for n in ("林晓", "小B", "王一凡"):
            self.assertIn(n, talk)

    def test_progress_puts_people_first_and_renames(self):
        self.g["summary"] = dict(SUMMARY, template="progress")
        text = ex.blocks_text(ex.minutes_blocks(self.g, CFG, "zh"))
        self.assertLess(text.index("\n参会者\n"), text.index("01 数据划分"))
        self.assertIn("进展", text)
        self.assertIn("下周任务", text)

    def test_english_headings(self):
        text = ex.blocks_text(ex.minutes_blocks(self.g, CFG, "en"))
        self.assertNotIn("Agenda", text)
        for s in ("Key conclusions", "By person", "Feedback", "Follow-ups", "Left from last time", "Talk time",
                  "Participants: 林晓, 小B, 王一凡", "TimeGAN 下周补上 (02:10)", "林晓: 下周补上 (02:10)", "[Done] 补充相关工作: 加了三篇 (05:00)"):
            self.assertIn(s, text)
        self.assertNotIn("核心结论", text)

    def test_no_summary(self):
        self.g["summary"] = None
        with self.assertRaises(ex.ExportError) as c:
            ex.minutes_blocks(self.g, CFG, "zh")
        self.assertEqual(c.exception.code, "no_summary")

    def test_old_summary_plain_strings(self):
        self.g["summary"] = {"title": "旧", "speakers": [], "abstract": "a", "conclusions": ["c1", "c2"], "sections": [], "todos": ["t1"]}
        self.g["talk"] = []
        text = ex.blocks_text(ex.minutes_blocks(self.g, CFG, "zh"))
        self.assertIn("1. c1", text)
        self.assertIn("- t1", text)
        self.assertNotIn("\n参会者\n", text)

    def test_html_escaped_and_clip(self):
        clip = ex.minutes_clip(self.g, CFG, "zh")
        self.assertIn("&lt;b&gt;不是标签&lt;/b&gt;", clip["html"])
        self.assertNotIn("<b>不是标签", clip["html"])
        for tag in ("<h1>", "<h2>主题摘要</h2>", "<ol>", "<ul>", "<li>", "<strong>已完成</strong>"):
            self.assertIn(tag, clip["html"])
        self.assertNotIn("<", clip["text"].replace("<b>不是标签</b>", ""))
        self.assertIn("主题摘要", clip["text"])

    def test_markdown(self):
        md = ex.minutes_md(self.g, CFG, "zh")
        self.assertTrue(md.startswith("# 进展汇报"))
        self.assertIn("## 参会者", md)
        self.assertIn("- **已完成** 补充相关工作：加了三篇（05:00）", md)
        self.assertIn("1. TimeGAN 下周补上（02:10）", md)


class TodosTest(Base):
    def test_groups_ticks_edits(self):
        b = ex.todos_blocks(self.g, "zh")
        heads = [v for k, v in b if k == "h3"]
        self.assertEqual(heads, ["摘要", "3.2 实验", "4 结果", "软件", "会中已修改"])
        checks = [v for k, v in b if k == "check"]
        t1 = checks[1][0]
        self.assertTrue(t1["done"])                                    # carry.json 里勾掉了
        self.assertEqual(t1["text"], "补 TimeGAN 实验（实验数据）")
        # 末尾那行是标签（board.note_tags：标题里有"实验"、要补里有"数据"、怎么改里有"表 2"）
        self.assertEqual(t1["sub"], ["王一凡 这个要补", "修改方案：加一行到表 2", "需重跑实验、需补数据、需重画图表"])
        self.assertFalse(checks[2][0]["done"])
        self.assertEqual(checks[2][0]["sub"], ["整理中", "需重画图表"])      # 标题"图 3 坐标轴"
        self.assertEqual([x["text"] for x in checks[4]], ["改摘要第一句"])  # 撤回的不算
        md = ex.blocks_md(b)
        self.assertIn("- [x] 补 TimeGAN 实验（实验数据）", md)
        self.assertIn("- [ ] 图 3 坐标轴", md)

    def test_nothing(self):
        self.g["board"] = {"tasks": []}
        self.assertEqual(ex.todos_blocks(self.g, "zh"), [])


@unittest.skipUnless(components.pandoc(), "没有 pandoc")
class DocxTest(Base):
    def test_word(self):
        out = ex.save(self.lib, MID, "docx", self.tmp / "纪要 测试", lang="zh", cfg=CFG)
        self.assertEqual(out.suffix, ".docx")                       # 没写扩展名：补上
        with zipfile.ZipFile(out) as z:
            doc = z.read("word/document.xml").decode("utf-8")
        for s in ("主题摘要", "参会者", "遗留事项", "林晓：下周补上", "（02:10）"):
            self.assertIn(s, doc)
        self.assertIn('w:val="Heading1"', doc)
        self.assertIn('w:val="Heading2"', doc)
        self.assertFalse(list(self.tmp.glob(".*.part")))           # 临时文件收走了

    def test_word_needs_summary(self):
        make_meeting(self.root, "20261118-160000", summary=None)
        with self.assertRaises(ex.ExportError) as c:
            ex.save(self.lib, "20261118-160000", "docx", self.tmp / "a.docx", cfg=CFG)
        self.assertEqual(c.exception.code, "no_summary")


@unittest.skipUnless(ex.find_browser(), "没有 Edge / Chrome")
class PdfTest(Base):
    def test_pdf(self):
        d = self.tmp / "中文 文件夹"
        d.mkdir()
        out = ex.save(self.lib, MID, "pdf", d / "纪要.pdf", lang="zh", cfg=CFG)
        data = out.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreater(len(data), 5000)

    def test_page_is_a4_and_light(self):
        page = ex.minutes_page(self.g, CFG, "zh")
        self.assertIn("size: A4", page)
        self.assertNotIn("http", page.replace("http-equiv", ""))


class PackTest(Base):
    def test_pack(self):
        out = ex.save(self.lib, MID, "pack", self.tmp / "包.html", lang="zh", cfg=CFG)
        s = out.read_text(encoding="utf-8")
        for k in ('href="#notes"', 'href="#todos"', 'href="#diff"', "<iframe", "srcdoc=", "prefers-color-scheme: dark",
                  "主题摘要", "补 TimeGAN 实验", "data:image/png;base64,"):
            self.assertIn(k, s)
        self.assertNotIn("fonts.googleapis.com", s)                 # 离线：不链网上的字体
        self.assertNotIn('href="#transcript"', s)                   # 没要逐字稿
        self.assertNotIn("<script src", s)

    def test_pack_with_transcript(self):
        out = ex.save(self.lib, MID, "pack", self.tmp / "包.html", lang="zh", transcript=True, cfg=CFG)
        s = out.read_text(encoding="utf-8")
        self.assertIn('href="#transcript"', s)
        self.assertIn("我这周把基线跑完了", s)
        self.assertNotIn("切到 paper.md", s)                        # 系统行不要

    def test_pack_without_summary_or_diff(self):
        make_meeting(self.root, "20261118-170000", summary=None, diff=None)
        out = ex.save(self.lib, "20261118-170000", "pack", self.tmp / "b.html", lang="en", cfg=CFG)
        s = out.read_text(encoding="utf-8")
        self.assertIn("No notes", s)
        self.assertNotIn("<iframe", s)
        self.assertIn("To-dos", s)

    def test_offline_diff(self):
        figs = ex.figures_of(self.g["meeting"])
        self.assertIn("1", figs)
        page = ex.offline_diff(DIFF, figs)
        self.assertNotIn("fonts.googleapis.com", page)
        self.assertIn("data:image/png;base64,", page)
        self.assertNotIn("MathJax={tex", page.split("window.MathJax=")[0])
        if ex.mathjax_file():                                       # 有公式才把 MathJax 带上
            self.assertLess(len(page), 200_000)
            maths = DIFF.replace("改了 <ins>一句</ins>", "公式 $\\alpha + \\beta$")
            self.assertGreater(len(ex.offline_diff(maths, figs)), 1_000_000)

    def test_bad_requests(self):
        with self.assertRaises(ex.ExportError) as c:
            ex.save(self.lib, MID, "pack", "relative.html")
        self.assertEqual(c.exception.code, "bad_path")
        with self.assertRaises(ex.ExportError) as c:
            ex.save(self.lib, MID, "pack", self.tmp / "nope" / "x.html")
        self.assertEqual(c.exception.code, "bad_path")
        with self.assertRaises(ex.ExportError) as c:
            ex.save(self.lib, MID, "exe", self.tmp / "x.exe")
        self.assertEqual(c.exception.code, "kind")
        with self.assertRaises(ex.ExportError) as c:
            ex.save(self.lib, "../../etc", "pack", self.tmp / "x.html")
        self.assertEqual(c.exception.code, "not_found")

    def test_names(self):
        self.assertEqual(ex.file_name(self.g, "docx", "zh", CFG), "2026-11-18 第三章 实验设计 纪要.docx")
        self.assertEqual(ex.file_name(self.g, "pack", "en", CFG), "2026-11-18 第三章 实验设计 Meeting pack.html")
        self.g["meeting"]["title"] = 'a/b:c*?"<>| · 11月18日 14:30'
        self.assertEqual(ex.info(self.g, CFG, "zh")["stem"], "2026-11-18 a b c")


class AutosavePrefsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="b5-prefs-"))
        for f in (autosave.PREFS_FILE, autosave.LAST_FILE):
            try:
                f.unlink()
            except OSError:
                pass

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_off(self):
        p = autosave.load()
        self.assertEqual((p["on"], p["dir"], p["items"]), (False, "", ["minutes_docx", "todos", "diff"]))
        self.assertEqual(autosave.public()["dir_max"], autosave.DIR_MAX)

    def test_check_dir(self):
        for bad, code in (("", "empty"), ("   ", "empty"), ("relative\\dir", "not_abs"), (str(self.tmp / "nope"), "not_dir"),
                          ("C:\\" + "a" * 200, "too_long"), (str(self.tmp / "f.txt"), "not_dir")):
            if bad.endswith("f.txt"):
                Path(bad).write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError) as c:
                autosave.check_dir(bad)
            self.assertEqual(str(c.exception), code, bad)
        self.assertEqual(autosave.check_dir(f'"{self.tmp}"'), str(self.tmp))
        self.assertEqual(list(self.tmp.glob(".bloom-*")), [])        # 试写的文件删了

    def test_save(self):
        with self.assertRaises(ValueError) as c:
            autosave.save({"on": True})
        self.assertEqual(str(c.exception), "empty")
        with self.assertRaises(ValueError) as c:
            autosave.save({"items": ["exe"]})
        self.assertEqual(str(c.exception), "items")
        p = autosave.save({"dir": str(self.tmp), "on": True, "items": ["transcript", "minutes_md"]})
        self.assertEqual((p["on"], p["dir"], p["items"]), (True, str(self.tmp), ["minutes_md", "transcript"]))
        with self.assertRaises(ValueError) as c:
            autosave.save({"items": []})
        self.assertEqual(str(c.exception), "items")
        p = autosave.save({"dir": ""})                               # 文件夹清掉：跟着关
        self.assertEqual((p["on"], p["dir"]), (False, ""))
        self.assertEqual(autosave.load()["items"], ["minutes_md", "transcript"])


class AutosaveRunTest(Base):
    def setUp(self):
        super().setUp()
        self.out = self.tmp / "存这里"
        self.out.mkdir()
        self.prefs = {"on": True, "dir": str(self.out), "items": list(autosave.ITEMS)}

    def test_saves_everything(self):
        with mock.patch.object(ex, "html_to_docx", side_effect=lambda frag, to, lang="zh": (to.write_bytes(b"PK"), to)[1]):
            r = autosave.run(self.lib, MID, self.prefs, lang="zh", cfg=CFG)
        self.assertTrue(r["ok"], r)
        names = sorted(Path(f).name for f in r["files"])
        self.assertEqual(names, sorted(["2026-11-18 第三章 实验设计 纪要.docx", "2026-11-18 第三章 实验设计 纪要.md",
                                        "2026-11-18 第三章 实验设计 修改清单.md", "2026-11-18 第三章 实验设计 修订对比.html",
                                        "2026-11-18 第三章 实验设计 逐字稿.txt"]))
        tr = (self.out / "2026-11-18 第三章 实验设计 逐字稿.txt").read_bytes()
        self.assertTrue(tr.startswith(b"\xef\xbb\xbf"))
        self.assertIn("[01:05] 王一凡：我这周把基线跑完了\r\n", tr.decode("utf-8-sig"))
        self.assertIn("- [x] 补 TimeGAN 实验", (self.out / "2026-11-18 第三章 实验设计 修改清单.md").read_text(encoding="utf-8"))
        self.assertNotIn("fonts.googleapis.com", (self.out / "2026-11-18 第三章 实验设计 修订对比.html").read_text(encoding="utf-8"))
        last = autosave.last_result()
        self.assertTrue(last["ok"])
        self.assertEqual(last["meeting"], MID)
        # 再存一次：不盖掉，加 (2)
        r2 = autosave.run(self.lib, MID, dict(self.prefs, items=["minutes_md"]), lang="zh", cfg=CFG)
        self.assertEqual(Path(r2["files"][0]).name, "2026-11-18 第三章 实验设计 纪要 (2).md")

    @unittest.skipUnless(components.pandoc(), "没有 pandoc")
    def test_real_word(self):
        r = autosave.run(self.lib, MID, dict(self.prefs, items=["minutes_docx"]), lang="zh", cfg=CFG)
        self.assertTrue(r["ok"], r)
        self.assertTrue(zipfile.is_zipfile(r["files"][0]))

    def test_folder_gone(self):
        shutil.rmtree(self.out)
        r = autosave.run(self.lib, MID, self.prefs, lang="zh", cfg=CFG)
        self.assertEqual((r["ok"], r["error"], r["files"]), (False, "not_dir", []))
        self.assertFalse(autosave.last_result()["ok"])

    def test_no_summary_saves_the_rest(self):
        make_meeting(self.root, "20261118-180000", summary=None)
        r = autosave.run(self.lib, "20261118-180000", self.prefs, lang="zh", cfg=CFG)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "no_summary")
        self.assertEqual(r["failed"], ["minutes_docx", "minutes_md"])
        self.assertEqual(len(r["files"]), 3)                         # 修改清单、修订对比、逐字稿照样存了

    def test_skips_what_the_meeting_lacks(self):
        make_meeting(self.root, "20261118-190000", diff=None)
        d = self.root / "20261118-190000"
        (d / "board.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
        r = autosave.run(self.lib, "20261118-190000", dict(self.prefs, items=["todos", "diff", "minutes_md"]), lang="zh", cfg=CFG)
        self.assertTrue(r["ok"])
        self.assertEqual([Path(f).name for f in r["files"]], ["2026-11-18 第三章 实验设计 纪要.md"])

    def test_long_names_fit(self):
        long_title = "很长的会议名" * 10
        make_meeting(self.root, "20261118-200000", title=long_title)
        deep = self.out / ("子目录" * 20)
        deep.mkdir()
        r = autosave.run(self.lib, "20261118-200000", {"on": True, "dir": str(deep), "items": ["minutes_md"]}, lang="zh", cfg=CFG)
        self.assertTrue(r["ok"], r)
        self.assertLessEqual(len(r["files"][0]), autosave.PATH_MAX + 2)

    def test_after_meeting_only_when_on(self):
        for f in (autosave.PREFS_FILE, autosave.LAST_FILE):
            try:
                f.unlink()
            except OSError:
                pass
        self.assertIsNone(autosave.after_meeting(self.lib, MID, cfg=CFG))
        autosave.save({"dir": str(self.out), "on": True, "items": ["minutes_md"]})
        r = autosave.after_meeting(self.lib, MID, cfg=CFG)
        self.assertTrue(r["ok"])
        self.assertEqual(len(list(self.out.glob("*.md"))), 1)
        autosave.save({"on": False})


class PipelineHookTest(Base):
    """散会收尾（aftermeeting._pipeline）纪要写完之后调到自动保存：转写、整理、纪要都换成假的，不调模型。"""

    def test_pipeline_calls_autosave(self):
        from zuhui import aftermeeting
        out = self.tmp / "auto"
        out.mkdir()
        autosave.save({"dir": str(out), "on": True, "items": ["minutes_md", "transcript"]})
        logs = []
        app = SimpleNamespace(cfg=CFG, meetings=self.lib, board=SimpleNamespace(tasks=lambda: []), viewer=SimpleNamespace(ui_lang="zh"))
        post = aftermeeting.PostMeeting(MID, None, "t")
        m = self.g["meeting"]
        with mock.patch("zuhui.retranscribe.run", return_value={"skipped": "测试"}), \
                mock.patch("zuhui.polish.run", return_value={"items": [], "meta": {"lines": 3}}), \
                mock.patch("zuhui.lexicon.learn_meeting", return_value=[]), \
                mock.patch("zuhui.lexicon.learn_cleanup", return_value={}), \
                mock.patch("zuhui.meetings.summarize", return_value=dict(SUMMARY, title="新纪要")), \
                mock.patch("zuhui.past.carry_in", return_value=[]), \
                mock.patch("zuhui.past.settle", return_value=None), \
                mock.patch("zuhui.asr_train.after_meeting", return_value=None):
            aftermeeting._pipeline(app, post, m, None, time.time(), lambda *a, **k: logs.append((a, k)))
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(names, ["2026-11-18 第三章 实验设计 纪要.md", "2026-11-18 第三章 实验设计 逐字稿.txt"])
        self.assertTrue((out / names[0]).read_text(encoding="utf-8").startswith("# 新纪要"))   # 用的是刚写好的纪要
        self.assertTrue(any(a and a[0] == "meeting" and k.get("state") == "自动保存" for a, k in logs), logs)
        autosave.save({"on": False})


SERVER = r"""
import json, os, sys, socket, time, http.client, tempfile
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, os.path.join(sys.argv[1], "tests"))
from pathlib import Path
from zuhui import relay_cfg, share
share.PUBLIC_RELAY = "https://127.0.0.1:1/"
relay_cfg.set_mode("off")
from zuhui.config import STATE
from test_b5_export import make_meeting, MID
make_meeting(STATE / "meetings")
from zuhui.view import Viewer
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = Viewer(port=port)
v.cfg.setdefault("meeting", {})["me"] = "林晓"
v.serve(open_browser=False)
def req(method, path, body=None, origin=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    h = {"Content-Type": "application/json"}
    if origin: h["Origin"] = origin
    c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
    r = c.getresponse(); data = r.read()
    return [r.status, json.loads(data.decode("utf-8"))]
out = {}
d = Path(sys.argv[2])
out["prefs0"] = req("GET", "/api/autosave")
out["bad_on"] = req("POST", "/api/autosave", {"on": True})
out["bad_long"] = req("POST", "/api/autosave", {"dir": "C:\\" + "a" * 200})
out["bad_rel"] = req("POST", "/api/autosave", {"dir": "rel"})
out["set"] = req("POST", "/api/autosave", {"dir": str(d), "on": True, "items": ["minutes_md"]})
out["evil"] = req("POST", "/api/autosave", {"dir": str(d)}, origin="https://evil.example")
out["minutes"] = req("GET", "/api/meetings/" + MID + "/minutes?ui=zh")
out["minutes_en"] = req("GET", "/api/meetings/" + MID + "/minutes?ui=en")
out["minutes_404"] = req("GET", "/api/meetings/20000101-000000/minutes")
out["save_pack"] = req("POST", "/api/meetings/" + MID + "/save", {"kind": "pack", "to": str(d / "包.html"), "transcript": True})
out["save_bad"] = req("POST", "/api/meetings/" + MID + "/save", {"kind": "pack", "to": "x.html"})
out["save_kind"] = req("POST", "/api/meetings/" + MID + "/save", {"kind": "exe", "to": str(d / "x.exe")})
out["save_evil"] = req("POST", "/api/meetings/" + MID + "/save", {"kind": "pack", "to": str(d / "e.html")}, origin="https://evil.example")
(STATE / "autosave_last.json").write_text(json.dumps({"meeting": MID, "ok": False, "error": "not_dir", "at": time.time()}), encoding="utf-8")
out["current"] = req("GET", "/api/current")[1].get("autosave")
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class HttpTest(unittest.TestCase):
    def test_http(self):
        d = Path(tempfile.mkdtemp(prefix="b5-http-out-"))
        env = dict(os.environ, ZUHUI_HOME=tempfile.mkdtemp(prefix="zuhui_home_b5_"), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", SERVER, str(ROOT), str(d)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=180)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        self.assertEqual((r["prefs0"][0], r["prefs0"][1]["on"]), (200, False))
        self.assertEqual(r["bad_on"], [400, {"error": "empty"}])
        self.assertEqual(r["bad_long"], [400, {"error": "too_long"}])
        self.assertEqual(r["bad_rel"], [400, {"error": "not_abs"}])
        self.assertEqual((r["set"][0], r["set"][1]["on"], r["set"][1]["items"]), (200, True, ["minutes_md"]))
        self.assertEqual(r["evil"][0], 403)
        self.assertEqual(r["minutes"][0], 200)
        self.assertIn("<h2>主题摘要</h2>", r["minutes"][1]["html"])
        self.assertIn("主题摘要", r["minutes"][1]["text"])
        self.assertIn("<h2>Summary</h2>", r["minutes_en"][1]["html"])
        self.assertEqual(r["minutes_404"][0], 404)
        self.assertEqual((r["save_pack"][0], Path(r["save_pack"][1]["path"]).name), (200, "包.html"))
        self.assertIn("我这周把基线跑完了", (d / "包.html").read_text(encoding="utf-8"))
        self.assertEqual((r["save_bad"][0], r["save_bad"][1]["error"]), (400, "bad_path"))
        self.assertEqual((r["save_kind"][0], r["save_kind"][1]["error"]), (400, "kind"))
        self.assertEqual(r["save_evil"][0], 403)
        self.assertFalse((d / "e.html").exists())
        self.assertEqual((r["current"]["ok"], r["current"]["error"]), (False, "not_dir"))
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
