# -*- coding: utf-8 -*-
"""改完就是改完了（2026-11-21 组会 T6、T8）：要改的那一处前一条已经做掉了，工人什么都没动，以前按"修改失败"记进会后修改清单，
汇报说"这条没改成……我记进散会后的修改清单了"。现在：
- 工人写 #already（API 那一路交 "already": true），或者说明里明摆着"已经删掉了""已经改成……"：按已是要的样子收场，
  任务板上算完成，不进修改清单（不是标注），对比页上不打勾；
- 真没做成的（找不到他引的那句、问回去"你指的是哪句"）照旧进修改清单；
- 汇报只说一句，不说"没改成""修改清单"。

句子、论文都是编的。全离线、不花钱：ZUHUI_HOME / BLURT_HOME 指到临时目录，claude 调用和接口都换成假的，中继指到 https://127.0.0.1:1/。

    python -X utf8 -m unittest tests.test_hands_already -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-already-")     # 必须在 import zuhui 之前
    os.environ.setdefault("BLURT_HOME", tempfile.mkdtemp(prefix="blurt-test-already-"))
from zuhui import config as zconfig  # noqa: E402

HOME = Path(zconfig.STATE)
if os.path.normcase(str(HOME.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import hands as handsmod  # noqa: E402
from zuhui import hands_api, share, view  # noqa: E402
from zuhui.board import Board  # noqa: E402

share.PUBLIC_RELAY = "https://127.0.0.1:1/"

PAPER = ("# 测试稿\n\n## 2 方法\n\n### 2.3 基线\n\n本节回答两个问题：阈值法能否发现偏差，查表法能否替代它。\n\n"
         "只有基于规则的方法，即阈值法、查表法和人工打分法会出现。\n\n## 3 结论\n\n本文提出了一个基准。\n")


class Rules(unittest.TestCase):
    def test_parse_already(self):
        self.assertEqual(handsmod.parse_already("#already\n2.3 开头那句前面已经删了"), "2.3 开头那句前面已经删了")
        self.assertEqual(handsmod.parse_already("#already 2.3 开头那句前面已经删了"), "2.3 开头那句前面已经删了")
        self.assertEqual(handsmod.parse_already("#already"), "")
        self.assertIsNone(handsmod.parse_already("我把 2.3 开头那句删了。无。"))
        self.assertIsNone(handsmod.parse_already("#note\n位置：无"))

    def test_said_already(self):
        for yes in ("我没改稿子，因为 2.3 开头那句上一条已经删掉了，全文也找不到这句。拿不准的点：无。",
                    "这条没改：2.3 开头那段前面已经改成只提问题了，原来那句结论也已经删掉。",
                    "3.3 那段本来就有偏差的定义，所以没动。",
                    "I left it: the sentence in 2.3 was already removed by the previous edit."):
            self.assertTrue(handsmod.said_already(yes), yes)
        for no in ("论文里没找到“阈值法能否替代”这句，你指的是哪句？",
                   "定位不到这一处，读不到截图。",
                   "原文写的是 230，所以没改，他可能指的是表 2 那个数。",
                   "这条要新的数据才能做，已经记下。"):
            self.assertFalse(handsmod.said_already(no), no)
        self.assertEqual(handsmod.tidy_already("我没改稿子，因为 2.3 开头那句上一条已经删掉了，全文也找不到这句。拿不准的点：无。"),
                         "2.3 开头那句上一条已经删掉了，全文也找不到这句。")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="already-paper-"))
        self.paper = self.dir / "paper.md"
        self.paper.write_text(PAPER, encoding="utf-8")
        self.board = Board(self.dir / "board.json")
        self.hands = handsmod.Hands({"hands": {"timeout_s": 30}, "meeting": {"lang": "zh"}}, self.board, lambda t: None,
                                    self.paper)
        self.hands.RETRY_WAITS = (0, 0)
        self.ids = []

    def tearDown(self):
        for i in self.ids:
            (HOME / "changes" / f"{i}.json").unlink(missing_ok=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_claude(self, result: str, write: str | None = None) -> dict:
        """模型来源是 Claude Code 账号：claude -p 换成假的，交回 result；write 给了就先把论文改成它（工人动过正文）。"""
        def fake_call(args, **kw):
            if write is not None:
                self.paper.write_text(write, encoding="utf-8")
            return {"type": "result", "result": result, "is_error": False}, "", 0
        self.hands.call = fake_call
        with mock.patch("zuhui.provider.mode", return_value="claude"):
            return self.run_task()

    def run_task(self) -> dict:
        pp = {"id": "p1", "name": self.paper.name, "path": str(self.paper)}
        t = self.board.add("把 2.3 开头那句删掉", "2.3 开头那句删掉", section="2.3 基线", paper=pp)
        self.ids.append(t["id"])
        self.hands._run(self.board.claim_next())
        return self.board.get(t["id"])

    def changed(self, tid: str) -> bool:
        return json.loads((HOME / "changes" / f"{tid}.json").read_text(encoding="utf-8"))["changed"]


class ClaudePath(Base):
    def test_marker(self):
        t = self.run_claude("#already\n2.3 开头那句前面已经删了")
        self.assertEqual(t["status"], "done")
        self.assertTrue(t.get("already"))
        self.assertFalse(t.get("note"))                                     # 不是标注：不进修改清单
        self.assertFalse(t.get("unchanged"))
        self.assertEqual(t["result"], "2.3 开头那句前面已经删了")
        self.assertEqual(t["progress"]["step"], "已完成")
        self.assertFalse(self.changed(t["id"]))
        self.assertEqual(self.paper.read_text(encoding="utf-8"), PAPER)

    def test_no_marker_but_said_so(self):
        t = self.run_claude("我没改稿子，因为 2.3 开头那句上一条已经删掉了，全文也找不到这句。拿不准的点：无。")
        self.assertTrue(t.get("already"))
        self.assertFalse(t.get("note"))
        self.assertNotIn("修改失败", t["result"])
        self.assertTrue(t["result"].startswith("2.3 开头那句上一条已经删掉了"), t["result"])

    def test_real_failure_still_noted(self):
        t = self.run_claude("论文里没找到“阈值法能否替代”这句，所以没改。你指的是不是 2.3 开头那句？")
        self.assertTrue(t.get("note") and t.get("unchanged"))
        self.assertFalse(t.get("already"))
        self.assertTrue(t["result"].startswith("修改失败，已加入修改清单："), t["result"])

    def test_marker_but_edited_is_normal_done(self):
        t = self.run_claude("#already\n2.3 开头那句删了", write=PAPER.replace("本节回答两个问题：阈值法能否发现偏差，查表法能否替代它。\n\n", ""))
        self.assertEqual(t["status"], "done")
        self.assertFalse(t.get("already"))
        self.assertTrue(self.changed(t["id"]))
        self.assertEqual(t["result"], "2.3 开头那句删了")

    def test_prompt_tells_worker(self):
        for f in ("worker.md", "worker_api.md"):
            s = (ROOT / "prompts" / f).read_text(encoding="utf-8")
            self.assertIn("已经是要的样子", s, f)
        self.assertIn("#already", (ROOT / "prompts" / "worker.md").read_text(encoding="utf-8"))
        self.assertIn('"already"', (ROOT / "prompts" / "worker_api.md").read_text(encoding="utf-8"))


class ApiPath(Base):
    def run_api(self, *replies: str) -> dict:
        got = list(replies)
        with mock.patch("zuhui.provider.mode", return_value="api"), \
                mock.patch.object(hands_api, "lit_on", return_value=False), \
                mock.patch.object(hands_api, "_ask", side_effect=lambda *a, **k: (got.pop(0), None)):
            return self.run_task()

    def test_already_flag(self):
        t = self.run_api(json.dumps({"edits": [], "report": "2.3 开头那句前面已经删了", "unsure": "", "note": None,
                                     "already": True}, ensure_ascii=False))
        self.assertEqual(t["status"], "done")
        self.assertTrue(t.get("already"))
        self.assertFalse(t.get("note"))
        self.assertEqual(t["result"], "2.3 开头那句前面已经删了")
        self.assertEqual(self.paper.read_text(encoding="utf-8"), PAPER)

    def test_parse_reads_flag(self):
        p, _ = hands_api._parse('{"edits": [], "report": "a", "unsure": "", "note": null, "already": true}')
        self.assertTrue(p["already"])
        p, _ = hands_api._parse('{"edits": [], "report": "a", "unsure": "", "note": null}')
        self.assertFalse(p["already"])

    def test_no_flag_not_found_still_noted(self):
        t = self.run_api(json.dumps({"edits": [], "report": "论文里没找到这句，所以没改", "unsure": "你指的是哪句",
                                     "note": None}, ensure_ascii=False))
        self.assertTrue(t.get("note") and t.get("unchanged"))
        self.assertFalse(t.get("already"))


class PaperPage(unittest.TestCase):
    def test_no_tick_for_already(self):
        tasks = [{"id": "T1", "idx": 1, "title": "2.3 开头那句删掉", "status": "done", "result": "删了"},
                 {"id": "T2", "idx": 2, "title": "2.3 开头那句删掉", "status": "done", "already": True,
                  "result": "前面已经删了"}]
        self.assertEqual([b["id"] for b in view._briefs(tasks, "我")], ["T1"])


# ---------------------------------------------------------------- 汇报只说一句
class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = Path(tempfile.mkdtemp(prefix="paper-already-"))
        cls.paper_file = d / "测试稿.md"
        cls.paper_file.write_text(PAPER, encoding="utf-8")
        cls._saved_cur = zconfig.CURRENT_PAPER.read_bytes() if zconfig.CURRENT_PAPER.exists() else None
        zconfig.CURRENT_PAPER.write_text(json.dumps({"path": str(cls.paper_file), "name": "测试稿"}, ensure_ascii=False),
                                         encoding="utf-8")
        from zuhui import app
        from zuhui.config import load
        cls.m = app.Meeting(load(), dry=True, mute=True)

    @classmethod
    def tearDownClass(cls):
        if cls.m.meetings.current():
            cls.m.meetings.end()
        if cls._saved_cur is None:
            zconfig.CURRENT_PAPER.unlink(missing_ok=True)
        else:
            zconfig.CURRENT_PAPER.write_bytes(cls._saved_cur)

    def setUp(self):
        m = self.m
        if m.meetings.current():
            m.meetings.end()
        m.board.reset()
        m.fast.history.clear()
        m.pending_reports.clear()
        m._last_reply = ""
        m.meetings.start(session=f"s-already-{time.time()}", title="组会")
        self.calls, self.replies = [], []
        m.fast.reply = self._fake

    def _fake(self, said, light=False, remember=True):
        self.calls.append(said)
        tag, *sents = self.replies.pop(0) if self.replies else ("#status",)
        yield ("tag", tag)
        for s in sents:
            yield ("sentence", s)
        yield ("done", "".join(sents))

    def task(self) -> dict:
        m = self.m
        t = m.board.add(remark="把 2.3 开头那句删掉", title="2.3 开头那句删掉", paper=m._paper_now())
        m.board.claim(t["id"])
        m.board.annotate(t["id"], already=True)
        m.board.finish(t["id"], result="2.3 开头那句前面已经删了")
        return m.board.get(t["id"])

    def ai_text(self) -> str:
        return [l for l in self.m.meetings.current_lines() if l["who"] == "student"][-1]["text"]

    def test_one_sentence(self):
        self.replies = [("#status", "2.3 开头那句前面已经删了，不用再改。", "另外还有一条在改。")]
        self.m._report(self.task())
        self.assertIn("已经是要的样子", self.calls[-1])
        self.assertIn("不说“没改成”", self.calls[-1])
        self.assertNotIn("记进散会后的修改清单", self.calls[-1])
        self.assertEqual(self.ai_text(), "2.3 开头那句前面已经删了，不用再改。")
        self.assertNotIn("另外还有一条", self.m.fast.history[-1]["content"])

    def test_fallback_one_sentence(self):
        self.replies = [("#status",)]
        self.m._report(self.task())
        self.assertEqual(self.ai_text(), "2.3 开头那句前面已经删了，不用再改。")


if __name__ == "__main__":
    unittest.main()
