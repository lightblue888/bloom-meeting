# -*- coding: utf-8 -*-
"""2026-11-21 晚上那场会暴露的几处（内容都是编的）：
  · 开会说的总纲是一段小节（"这次会议的大纲就是把五点三一直到五点五全部都改掉"）：认成总纲，不补记成改稿；
  · 快嘴已经当这场的范围收下了（"好，这场会的范围是 5.3 到 5.5"）：补漏检查不再补；
  · 大厅里移除了论文：遗留事项照样列出最近几场会的，不是一条都没有。

    python -X utf8 -m unittest tests.test_meeting_1121d -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-1121d-")
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("不在真实状态目录里跑")

from zuhui import dialog_rules as r  # noqa: E402
from zuhui import meetings as mt  # noqa: E402
from zuhui import past  # noqa: E402
from zuhui.app import _SCOPE_ACK  # noqa: E402


class AgendaRange(unittest.TestCase):
    def test_spoken_range_is_agenda(self):
        s = "呃这次会议呢，就把。会议的大纲呢，就是把四点。二全部就四点二，一直到四点四，全部都改掉。"
        self.assertEqual(r.agenda_text(s), "4.2 到 4.4 全部改")
        self.assertTrue(r.agenda_only(s))
        self.assertEqual(r.agenda_scope(s), "4.2 到 4.4")

    def test_written_range_and_chapters(self):
        self.assertTrue(r.agenda_only("今天把3.1到3.3都过一遍。"))
        self.assertTrue(r.agenda_only("这次会议主要改第二章和第四章。"))

    def test_specific_edits_are_not_agenda(self):
        for s in ("把3.1第二段改成两句。", "这次把 3.1 到 3.3 的样本都改成样本期。", "你把三点一到三点三都看一下有没有错别字？",
                  "3.1 到 3.3 全部都改掉。", "把 2.2 改一下。"):
            self.assertFalse(r.agenda_only(s), s)

    def test_sec_digits(self):
        self.assertEqual(r.sec_digits("五点。三和十二点五"), "5.3和12.5")
        self.assertEqual(r.sec_digits("下午两点钟"), "下午两点钟")

    def test_scope_ack(self):
        self.assertTrue(_SCOPE_ACK.search("好，这场会的范围是 4.2 到 4.4 全部，我知道了。"))
        self.assertTrue(_SCOPE_ACK.search("明白，这次的大纲是第二章。"))
        self.assertFalse(_SCOPE_ACK.search("好，我把 4.2 第一段改成两句，排队去改。"))


class CarryWithoutPaper(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zuhui-1121d-"))
        self.lib = mt.Meetings(root=self.tmp / "meetings")
        past._INDEX.__init__()

    def make(self, mid, paper, tasks):
        d = self.lib.root / mid
        d.mkdir(parents=True)
        started = f"{mid[:4]}-{mid[4:6]}-{mid[6:8]}T{mid[9:11]}:{mid[11:13]}:{mid[13:15]}"
        (d / "meta.json").write_text(json.dumps({"id": mid, "title": paper["name"] if paper else "组会", "started": started, "ended": started,
                                                 "state": "ended", "papers": [paper] if paper else [],
                                                 "active_paper": paper["id"] if paper else None}, ensure_ascii=False), encoding="utf-8")
        (d / "board.json").write_text(json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")

    def test_no_paper_lists_recent_meetings(self):
        pa = {"id": "aaaa1111", "name": "城市单车调度", "path": "C:/papers/a/a.md"}
        pb = {"id": "bbbb2222", "name": "雨量站插值", "path": "C:/papers/b/b.md"}
        self.make("20261120-100000", pa, [{"id": "T1", "title": "4.2 补一组对照", "note": True, "status": "done"}])
        self.make("20261121-100000", pb, [{"id": "T1", "title": "3.1 换掉旧数据", "note": True, "status": "done"}])
        self.make("20261121-120000", None, [])                       # 没开论文的会（只记逐字稿）：没有遗留
        rows = past.carry(self.lib, None)
        self.assertEqual([x["text"] for x in rows], ["3.1 换掉旧数据", "4.2 补一组对照"])   # 新的在前，两篇都有
        only_a = past.carry(self.lib, pa)
        self.assertEqual([x["text"] for x in only_a], ["4.2 补一组对照"])                  # 选了论文：照旧只列这篇的


if __name__ == "__main__":
    unittest.main()
