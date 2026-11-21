# -*- coding: utf-8 -*-
"""改稿工人正在改一篇时，有人在对比页里手改了另一段：这处手改并进那条任务的"开工时全文"（Hands.note_hand_edit），
记下的改动（STATE/changes/<id>.json）里只有工人自己改的；之后撤回那条任务，手改不会被一起撤掉。

全离线：不起 claude，_run_claude 换成直接改文件的替身；ZUHUI_HOME 指到临时目录。

    python -X utf8 -m unittest tests.test_hand_edit_during_task -v
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
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-hand-edit-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

HOME = Path(zconfig.STATE)
if os.path.normcase(str(HOME.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import hands as handsmod  # noqa: E402

PAPER = ("# 2 方法\n\n我们用三个城市的雨量站数据训练模型。\n\n"
         "# 3 实验\n\n误差在汛期明显变大，原因是降雨更集中。\n\n"
         "# 4 结论\n\n模型在多数月份可用。\n")


class _Board:
    def __init__(self):
        self.done = {}

    def finish(self, tid, **kw):
        self.done[tid] = kw

    def set_progress(self, *a, **k):
        pass

    def get(self, tid):
        return None


class HandEditDuringTask(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="hand-edit-doc-"))
        self.paper = self.dir / "测试稿.md"
        self.paper.write_text(PAPER, encoding="utf-8")
        self.hands = handsmod.Hands({"hands": {}, "meeting": {"lang": "zh"}}, _Board(), lambda *_: None, self.paper)

    def _changes(self, tid):
        return json.loads((HOME / "changes" / f"{tid}.json").read_text(encoding="utf-8"))

    def _run(self, tid, during):
        """跑一条任务：工人把"训练模型"改成"训练回归模型"；改的过程中执行 during()（模拟有人在对比页手改）。"""
        def fake_run_claude(task):
            t = self.paper.read_text(encoding="utf-8")
            self.paper.write_text(t.replace("训练模型", "训练回归模型"), encoding="utf-8")
            during()
        self.hands._run_claude = fake_run_claude
        self.hands._run_task({"id": tid, "idx": 1, "title": "改方法那句"})

    def test_hand_edit_is_not_part_of_the_task(self):
        old, new = "误差在汛期明显变大，原因是降雨更集中。", "误差在汛期明显变大。"

        def hand():
            t = self.paper.read_text(encoding="utf-8")
            self.paper.write_text(t.replace(old, new), encoding="utf-8")
            self.assertEqual(self.hands.note_hand_edit(str(self.paper), old, new), 1)

        self._run("T901", hand)
        ch = self._changes("T901")
        self.assertIn(new, ch["before"])              # 开工时全文里已经并进了手改
        self.assertNotIn(old, ch["before"])
        self.assertIn("训练模型", ch["before"])        # 工人自己的改动照样记着
        self.assertIn("训练回归模型", ch["after"])
        self.assertNotIn("降雨更集中", " ".join(ch["del"]))

    def test_other_paper_or_missing_paragraph_left_alone(self):
        def hand():
            self.assertEqual(self.hands.note_hand_edit(str(self.dir / "别的稿.md"), "模型在多数月份可用。", "可用。"), 0)
            self.assertEqual(self.hands.note_hand_edit(str(self.paper), "论文里没有这一段。", "随便"), 0)
            self.assertEqual(self.hands.note_hand_edit(str(self.paper), "", "随便"), 0)

        self._run("T902", hand)
        self.assertEqual(self._changes("T902")["before"], PAPER)

    def test_no_running_task_nothing_to_do(self):
        self.assertEqual(self.hands.note_hand_edit(str(self.paper), "模型在多数月份可用。", "可用。"), 0)
        self.assertEqual(self.hands._bases, {})


if __name__ == "__main__":
    unittest.main()
