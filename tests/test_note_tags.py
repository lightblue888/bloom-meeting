# -*- coding: utf-8 -*-
"""标注（会上改不了的那几条）身上的标签：按标题、怎么改、要补归类（board.note_tags），发给界面的每一条（tasks()）、
往期会议（meetings.get）、修改清单导出（export）都带上。小旗卡片底下画成"会后改 · 要重新跑实验"这样一排（2026-11-19）。
跑：python -X utf8 -m unittest tests.test_note_tags -v （在仓库根目录；任务板写在临时文件里）"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-tags-")

from zuhui.board import NOTE_TAG_KEYS, Board, note_tags, with_tags  # noqa: E402


def _note(title="", plan="", need="", **kw):
    return dict({"id": "x", "idx": 1, "note": True, "status": "done", "title": title, "plan": plan, "need": need}, **kw)


class NoteTags(unittest.TestCase):
    def test_kinds_from_plan_and_need(self):
        self.assertEqual(note_tags(_note("第 4 节消融实验", "加一组去掉注意力模块的对照，重跑表 3", "重跑消融实验")),
                         ["rerun", "figure"])
        self.assertEqual(note_tags(_note("稳健性", "表 5 加两列，样本期换成 2015–2023", "重跑 2 次回归")),
                         ["rerun", "data", "figure"])
        self.assertEqual(note_tags(_note("补引用", "第 2 节补两篇相关工作", "查文献")), ["lit"])
        self.assertEqual(note_tags(_note("样本", "换一批新样本", "补 2023 年的数据")), ["data"])
        self.assertEqual(note_tags(_note("贡献点", "第三个贡献要不要保留，散会后跟老师确认", "无")), ["discuss"])
        self.assertEqual(note_tags(_note("Robustness", "Add two columns to Table 5", "rerun 2 regressions")),
                         ["rerun", "figure"])
        self.assertEqual(note_tags(_note("措辞", "引言第一段再压短一点", "")), [])

    def test_common_words_are_not_tags(self):
        # "刻画""试图""代表""讨论部分"不算画图、不算要再讨论
        self.assertEqual(note_tags(_note("讨论部分", "讨论部分刻画一下机制，试图说明它代表什么", "")), [])

    def test_only_notes_and_at_most_three(self):
        self.assertEqual(note_tags({"title": "重跑表 3", "status": "done"}), [])          # 改过的不是标注
        self.assertEqual(note_tags(_note("软件：逐字稿不滚动", "重跑一下", "", app=True)), [])  # 软件意见
        many = note_tags(_note("重跑实验", "补数据、重画图 2、查文献、再讨论", ""))
        self.assertEqual(len(many), 3)
        self.assertEqual(many, [k for k in NOTE_TAG_KEYS if k in many])                 # 顺序固定

    def test_stored_tags_win(self):
        self.assertEqual(note_tags(_note("重跑实验", tags=["lit", "bogus"])), ["lit"])
        self.assertEqual(note_tags(_note("重跑实验", tags=[])), [])

    def test_board_tasks_carry_tags(self):
        b = Board(Path(tempfile.mkdtemp(prefix="board-")) / "board.json")
        t = b.add("消融不够，补一组对照", "第 4 节消融实验")
        b.to_note(t["id"], plan="加一组去掉注意力模块的对照，重跑表 3", need="重跑消融实验")
        e = b.add("引言压短", "引言压短")
        b.finish(e["id"], result="改好了")
        ts = {x["id"]: x for x in b.tasks()}
        self.assertEqual(ts[t["id"]]["tags"], ["rerun", "figure"])
        self.assertNotIn("tags", ts[e["id"]])
        self.assertNotIn("tags", next(x for x in Board(b.path)._tasks if x["id"] == t["id"]))   # 只是读出来带上
        self.assertEqual(with_tags({"title": "x"}), {"title": "x"})

    def test_export_lists_tags(self):
        from zuhui import export
        g = {"board": {"tasks": [_note("第 4 节消融实验", "加一组去掉注意力模块的对照，重跑表 3", "重跑消融实验")]}}
        for lang, want in (("zh", "需重跑实验、需重画图表"), ("en", "Rerun experiments, Redo figures or tables")):
            rows = next(b[1] for b in export.todos_blocks(g, lang) if b[0] == "check")
            self.assertIn(want, rows[0]["sub"])


if __name__ == "__main__":
    unittest.main()
