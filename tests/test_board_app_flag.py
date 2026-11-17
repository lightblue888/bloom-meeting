# -*- coding: utf-8 -*-
"""任务板上的软件意见：计数（counts()["app"]）和发给界面的每一条（tasks()，修改清单按 app / kind 分"软件"那一组）认的是同一批。
2026-11-15 联调：工人看过原文才说"这条是在反馈软件"的那条（#edit 派下去、做成标注、正文没动），计数算软件，
发给界面的却没有 app 标记，修改清单把它放进了论文那一节。
跑：python -X utf8 -m unittest tests.test_board_app_flag -v （在仓库根目录；任务板写在临时文件里）"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules:
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-board-")

from zuhui.board import Board, is_app_task  # noqa: E402


class BoardAppFlag(unittest.TestCase):
    def setUp(self):
        self.b = Board(Path(tempfile.mkdtemp(prefix="board-")) / "board.json")

    def test_worker_said_app_is_flagged_everywhere(self):
        t = self.b.add("逐字稿老是不往下滚，记一下。另外第一段压短。", "逐字稿不往下滚")
        self.b.annotate(t["id"], status="done", note=True, unchanged=True,
                        result="这条是在反馈软件（逐字稿滚动），论文没动。")
        p = self.b.add_note("经费那段散会后核一下", "六 经费核对", plan_job=False)
        own = self.b.add_note("对比页打勾颜色乱跳", "对比页打勾颜色乱跳", plan_job=False)
        self.b.annotate(own["id"], app=True, kind="app")
        ts = {x["id"]: x for x in self.b.tasks()}
        self.assertTrue(is_app_task(ts[t["id"]]))
        self.assertIs(ts[t["id"]].get("app"), True)          # 界面（TodoList 的 isApp）认得
        self.assertIs(ts[own["id"]].get("app"), True)
        self.assertFalse(ts[p["id"]].get("app"))               # 论文的标注不受影响
        self.assertEqual(self.b.counts()["app"], sum(1 for x in ts.values() if x.get("app") or x.get("kind") == "app"))
        # 只是读出来带上标记，不写回任务板文件（下次工人改它时照旧）
        raw = Board(self.b.path)._tasks
        self.assertFalse(next(x for x in raw if x["id"] == t["id"]).get("app"))


if __name__ == "__main__":
    unittest.main()
