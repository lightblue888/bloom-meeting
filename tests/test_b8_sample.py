# -*- coding: utf-8 -*-
"""第 8 批：自带的示例会议（zuhui/sample.py、samples/demo-meeting/）。

原件齐不齐、拷进会议库旁边时 AI 名字和论文路径换没换、Meetings 能不能照常打开 / 导出 / 给修订对比、
会议库的列表和保留期看不看得到它、删掉以后还会不会回来、语言和 AI 名字变了会不会重拷。全在临时目录里。

    python -X utf8 -m unittest tests.test_b8_sample -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-sample-")      # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import sample  # noqa: E402
from zuhui.meetings import Meetings  # noqa: E402

FILES = ("meta.json", "participants.json", "transcript.jsonl", "board.json", "summary.json", "summary.md", "diff.html",
         "paper.md", "paper.before.md")


class SampleSourceTest(unittest.TestCase):
    def test_both_languages_are_complete(self):
        for lang in sample.LANGS:
            d = sample.SRC / lang
            for n in FILES:
                self.assertTrue((d / n).is_file(), f"{lang}/{n}")
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["lang"], lang)
            self.assertTrue(meta["sample"])
            board = json.loads((d / "board.json").read_text(encoding="utf-8"))["tasks"]
            self.assertGreaterEqual(sum(1 for t in board if t["status"] == "done" and not t.get("note")), 3)
            self.assertTrue(any(t.get("note") for t in board))
            s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(s["abstract"] and s["conclusions"] and s["sections"] and s["people"] and s["todos"])
            self.assertTrue(all(c.get("t") for c in s["conclusions"]), "纪要每条结论都能点回原话")
            html = (d / "diff.html").read_text(encoding="utf-8")
            self.assertIn("<html", html.lower())

    def test_no_model_names(self):
        """AI 的名字用 {AI}，不写死型号"""
        for p in sample.SRC.rglob("*"):
            if p.is_file() and p.suffix in (".json", ".jsonl", ".md"):
                s = p.read_text(encoding="utf-8")
                for bad in ("Claude", "Opus", "Haiku", "DeepSeek"):
                    self.assertNotIn(bad, s, f"{p.name}: {bad}")


class SampleCopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zuhui-sample-case-"))
        self.root = self.tmp / "meetings"
        self.mt = Meetings(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copy_outside_the_library_with_names_filled_in(self):
        d = sample.ensure(self.root, "zh", '小"花"', "小周")
        self.assertEqual(d, self.tmp / "sample-meeting")
        self.assertFalse((self.root / "sample").exists())
        self.assertEqual(self.mt.list(), [])                       # 会议库里看不到它
        g = self.mt.get(sample.SAMPLE_ID)
        self.assertIsNotNone(g)
        m = g["meeting"]
        self.assertEqual(m["id"], "sample")
        self.assertTrue(m["sample"])
        self.assertEqual(Path(m["papers"][0]["path"]), d / "paper.md")
        self.assertEqual(m["active_paper"], m["papers"][0]["id"])
        ai_lines = [l for l in g["transcript_raw"] if l["who"] == "student"]
        self.assertTrue(ai_lines)
        said = "".join(l["text"] for l in g["transcript_raw"])
        self.assertNotIn("{AI}", said)
        self.assertIn('小"花"', said)                              # 名字里有引号，JSON 照样能读
        self.assertIn('小"花"', [p["name"] for p in g["participants"]])
        self.assertNotIn("{AI}", (d / "summary.md").read_text(encoding="utf-8"))
        self.assertNotIn("{AI}", (d / "diff.html").read_text(encoding="utf-8"))
        self.assertTrue(all(t["paper_path"] == str(d / "paper.md") for t in g["board"]["tasks"]))
        self.assertEqual(g["diff_url"], "/meetings/sample/diff.html")
        self.assertEqual(self.mt.diff_path("sample"), d / "diff.html")
        self.assertNotIn("{ME}", said + (d / "summary.md").read_text(encoding="utf-8") + (d / "diff.html").read_text(encoding="utf-8"))
        host = next(p for p in g["participants"] if p.get("rank") == "host")
        self.assertEqual((host["name"], host.get("you")), ("小周", True))       # 主持人就是用 Bloom 的人
        self.assertIn("小周", [p["name"] for p in g["summary"]["people"]])
        self.assertTrue(all(l.get("name") == "小周" for l in g["transcript_raw"] if l["who"] == "teacher"))
        ex = self.mt.export("sample", "md")
        self.assertTrue(ex["ok"])
        self.assertIn("王老师", ex["text"])

    def test_brief_and_delete(self):
        b = sample.brief(self.mt, "en", {"assistant": {"name": "Ava"}, "meeting": {"me": "Sam"}})
        self.assertIn("Sam", "".join(l.get("name") or "" for l in self.mt.get("sample")["transcript_raw"]))
        self.assertEqual(b["id"], "sample")
        self.assertTrue(b["sample"] and b["has_summary"] and b["has_diff"])
        self.assertGreater(b["n_tasks"], 3)
        self.assertEqual(b["title"], "Using Bloom")
        self.assertEqual(b["state"], "ended")
        out = sample.delete(self.mt)
        self.assertTrue(out["ok"])
        self.assertTrue(sample.deleted(self.root))
        self.assertFalse(sample.folder(self.root).exists())
        self.assertTrue(list((self.tmp / "meetings-trash").glob("sample-deleted-*")))
        self.assertIsNone(sample.ensure(self.root, "en", "Ava"))      # 删过：不再回来
        self.assertIsNone(sample.brief(self.mt, "en", {"assistant": {"name": "Ava"}}))
        self.assertIsNone(self.mt.get("sample"))

    def test_recopied_when_language_or_name_changes(self):
        d = sample.ensure(self.root, "zh", "小助手")
        self.mt.rename("sample", "改过的名字")
        self.assertEqual(self.mt.get("sample")["meeting"]["title"], "改过的名字")
        sample.ensure(self.root, "zh", "小助手")                        # 什么都没变：不重拷
        self.assertEqual(self.mt.get("sample")["meeting"]["title"], "改过的名字")
        sample.ensure(self.root, "en", "小助手")
        g = self.mt.get("sample")
        self.assertEqual(g["meeting"]["lang"], "en")
        self.assertEqual(g["meeting"]["title"], "Using Bloom")
        sample.ensure(self.root, "en", "Robin")
        self.assertIn("Robin", "".join(l["text"] for l in self.mt.get("sample")["transcript_raw"]))
        sample.ensure(self.root, "en", "Robin", "Kim")                  # 主持人改了名字：也重拷
        self.assertIn("Kim", [p["name"] for p in self.mt.get("sample")["participants"]])
        self.assertEqual(d, sample.folder(self.root))

    def test_unknown_language_falls_back_to_chinese(self):
        sample.ensure(self.root, "fr", "AI")
        self.assertEqual(self.mt.get("sample")["meeting"]["lang"], "zh")

    def test_retention_and_storage_do_not_see_it(self):
        from zuhui import storage
        sample.ensure(self.root, "zh", "AI")

        class V:
            class meetings:
                root = self.root

                @staticmethod
                def current():
                    return None

        self.assertEqual(storage.record_rows(V()), [])

    def test_cannot_be_live_or_resumed(self):
        sample.ensure(self.root, "zh", "AI")
        mt = Meetings(self.root)                       # 起来时看"进行中"的会：示例会议不在库里，不会被接上
        self.assertIsNone(getattr(mt, "_orphan", None))
        self.assertIsNone(mt.current())


if __name__ == "__main__":
    unittest.main()
