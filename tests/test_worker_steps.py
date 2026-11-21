# -*- coding: utf-8 -*-
"""改稿 / 深想步骤在逐字稿和状态条上怎么写（zuhui/hands.py：grep_label、ask_step、WorkerProgress、progress_steps）。

2026-11-21 组会：`查找"^#+"`、`搜索FooBench论文_合稿第1249版-bloom`、`读 .bloom-convert.json` 看不懂；大任务只剩
"第 51 处"往后的 40 条，几十条"在 D 节改第 N 处"一条一条列；汇报每行都是同一个时间、都挂着"修改"，失败说两遍。

全离线：假任务板，论文写在临时目录；ZUHUI_HOME 指到临时目录，不碰真实的 ~/.zuhui。

    python -X utf8 -m unittest tests.test_worker_steps -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-worker-steps-")
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import hands  # noqa: E402
from zuhui.hands import WorkerProgress, ask_step, grep_label, progress_steps  # noqa: E402

PAPER = ("# 论文\n\n## 3.1 数据\n\n第一句在三点一。第二句也在三点一。\n\n"
         "## 3.2 任务\n\n甲句在三点二。乙句在三点二。丙句在三点二。\n\n"
         "## D 附录\n\n" + "".join(f"附录第{i}句。" for i in range(1, 120)) + "\n")


class FakeBoard:
    def __init__(self):
        self.snaps = []

    def set_progress(self, task_id, snap):
        self.snaps.append(snap)


def tool(name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


class GrepLabel(unittest.TestCase):
    def test_regex_becomes_words(self):
        cases = {
            "^#+": ("看目录", ""),
            "^#": ("看目录", ""),
            "^#{1,3} ": ("看目录", ""),
            r"3\.1": ("查找", "3.1"),
            r"3\.1\b": ("查找", "3.1"),
            "唯一变量|测量工具": ("查找", "唯一变量、测量工具"),
            "^#|一笔数据是一条带时": ("查找", "一笔数据是一条带时"),
            r"(?:3\.1|3\.2)": ("查找", "3.1、3.2"),
            "测量工具.*误差": ("查找", "测量工具 误差"),
            "冒号[：:]": ("查找", "冒号"),
            r"\d+": ("搜索文档", ""),
            "": ("搜索文档", ""),
            None: ("搜索文档", ""),
        }
        for pat, want in cases.items():
            with self.subTest(pat=pat):
                self.assertEqual(grep_label(pat), want)

    def test_many_alternatives_are_cut(self):
        k, words = grep_label("(?i)experiment|实验|result|outperform|baseline")
        self.assertEqual((k, words), ("查找", "experiment、实验、result、outperform 等"))

    def test_long_word_is_clipped(self):
        _, words = grep_label("这是一个特别特别特别特别特别特别长的搜索词组合")
        self.assertTrue(words.endswith("…") and len(words) == 21, words)


class AskStep(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="worker-steps-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "paper.md").write_text(PAPER, encoding="utf-8")

    def test_grep_in_the_document_folder_is_searching_the_document(self):
        self.assertEqual(ask_step("Grep", {"pattern": "唯一变量|测量工具", "path": str(self.dir)}, "paper.md", folder=self.dir),
                         ("搜索文档", "唯一变量、测量工具"))
        self.assertEqual(ask_step("Grep", {"pattern": "唯一变量", "path": "."}, "paper.md", folder=self.dir), ("搜索文档", "唯一变量"))
        # 文件夹名（没有扩展名、磁盘上也看不到）不当成"搜索 X"
        self.assertEqual(ask_step("Grep", {"pattern": "^#+", "path": "FooBench论文_合稿第1249版-bloom"}, "paper.md"), ("看目录", ""))
        self.assertEqual(ask_step("Grep", {"pattern": "^#+"}, "paper.md"), ("看目录", ""))
        self.assertEqual(ask_step("Grep", {"pattern": "^#+", "path": "paper.md"}, "paper.md"), ("看目录", ""))
        self.assertEqual(ask_step("Grep", {"pattern": r"\s+"}, "paper.md"), ("搜索文档", ""))

    def test_grep_in_another_file_names_it(self):
        self.assertEqual(ask_step("Grep", {"pattern": "基线", "path": "notes.md"}, "paper.md", folder=self.dir), ("搜索notes.md", "基线"))

    def test_internal_files_are_not_a_step(self):
        for name in ("Read", "Grep", "Glob"):
            with self.subTest(name=name):
                self.assertIsNone(ask_step(name, {"file_path": str(self.dir / ".bloom-convert.json"), "pattern": "x"}, "paper.md"))
        self.assertEqual(ask_step("Read", {"file_path": str(self.dir / "paper.md")}, "paper.md"), ("读文档", "全文"))

    def test_glob_pattern_is_not_shown(self):
        self.assertEqual(ask_step("Glob", {"pattern": "**/*.md"}, "paper.md"), ("查找文件", ""))


class Progress(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="worker-steps-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.paper = self.dir / "paper.md"
        self.paper.write_text(PAPER, encoding="utf-8")
        self.board = FakeBoard()
        self.prog = WorkerProgress(self.board, "T1", self.paper)

    def edit(self, old):
        self.prog.on_event(tool("Edit", file_path=str(self.paper), old_string=old, new_string="x"))

    def test_labels_merge_and_times(self):
        p = self.prog
        p.on_event(tool("Grep", pattern="^#+", path=str(self.dir)))
        p.on_event(tool("Read", file_path=str(self.paper)))
        p.on_event(tool("Read", file_path=str(self.paper), offset=10, limit=20))     # 连着读两次：一条
        p.on_event(tool("Read", file_path=str(self.dir / ".bloom-convert.json")))   # 软件自己的文件：不算
        p.on_event(tool("Grep", pattern=r"3\.1", path=str(self.dir)))
        self.edit("甲句在三点二。")
        self.assertEqual(p.step, "在 3.2 节改第 1 处")
        self.edit("乙句在三点二。")
        p.on_event(tool("Grep", pattern="丙句", path=str(self.dir)))                # 改完一处查一下，接着改同一节
        self.edit("丙句在三点二。")
        self.assertEqual(p.step, "在 3.2 节改第 3 处")                               # 当前这一步照旧
        self.edit("附录第1句。")
        p.on_event(tool("MultiEdit", file_path=str(self.paper), edits=[
            {"old_string": "附录第2句。", "new_string": "x"}, {"old_string": "附录第3句。", "new_string": "y"}]))
        p.on_event(tool("Read", file_path=str(self.paper)))
        p.on_event(tool("Grep", pattern="附录", path=str(self.dir)))
        p.done("已完成")
        snap = p.snapshot()
        self.assertEqual(snap["log"], ["开始", "看目录", "读文档", "查找“3.1”", "在 3.2 节改第 1–3 处",
                                       "在 D 节改第 4–6 处", "检查修改", "已完成"])
        self.assertEqual(len(snap["log_t"]), len(snap["log"]))
        self.assertEqual(snap["log_t"], sorted(snap["log_t"]))
        self.assertEqual(snap["edits"], 6)
        self.assertEqual(self.board.snaps[-1]["log"], snap["log"])                    # 冻结前最后写进任务板的就是这份
        self.assertNotIn("bloom-convert", " ".join(snap["log"]))

    def test_edits_in_another_section_are_not_merged(self):
        self.edit("甲句在三点二。")
        self.edit("第一句在三点一。")
        self.edit("乙句在三点二。")
        self.assertEqual(self.prog.log[1:], ["在 3.2 节改第 1 处", "在 3.1 节改第 2 处", "在 3.2 节改第 3 处"])

    def test_long_task_keeps_its_beginning(self):
        p = self.prog
        p.on_event(tool("Grep", pattern="^#+", path=str(self.dir)))
        p.on_event(tool("Read", file_path=str(self.paper)))
        for i in range(1, 100):                       # 两节来回改：一处一条，超过上限
            self.edit("附录第1句。" if i % 2 else "甲句在三点二。")
        log = p.log
        self.assertLessEqual(len(log), WorkerProgress.HEAD + WorkerProgress.TAIL)
        self.assertEqual(log[:3], ["开始", "看目录", "读文档"])                        # 开头读了什么还在
        self.assertEqual(log[-1], "在 D 节改第 99 处")                                 # 最近的也在
        self.assertEqual(len(p.snapshot()["log_t"]), len(log))

    def test_failure_is_one_step(self):
        p = self.prog
        p.on_event(tool("Read", file_path=str(self.paper)))
        p.done("修改失败")
        steps = progress_steps(p.snapshot(), clock=lambda ts: "01:00", error="修改超时 420 秒")
        self.assertEqual(steps, [{"t": "01:00", "k": "读文档"}, {"t": "01:00", "k": "失败", "v": "修改超时 420 秒"}])


class Steps(unittest.TestCase):
    def test_report_steps_have_their_own_time_and_no_repeated_kind(self):
        prog = {"log": ["开始", "看目录", "读文档", "在 D 节改第 66–78 处", "检查修改", "已完成"],
                "log_t": [100.0, 160.0, 175.0, 400.0, 830.0, 840.0]}
        steps = progress_steps(prog, clock=lambda ts: f"{int(ts) // 60:02d}:{int(ts) % 60:02d}")
        self.assertEqual(steps, [{"t": "02:40", "k": "看目录"}, {"t": "02:55", "k": "读文档"},
                                 {"t": "06:40", "k": "在 D 节改第 66–78 处"}, {"t": "13:50", "k": "检查修改"}])
        self.assertFalse(any(x["k"] == "修改" for x in steps))

    def test_live_steps_keep_start_and_end(self):
        prog = {"log": ["开始", "读文档"], "log_t": [100.0, 101.0]}
        self.assertEqual([x["k"] for x in progress_steps(prog, clock=str, ends=True)], ["开始", "读文档"])

    def test_failure_once(self):
        prog = {"log": ["开始", "查找文件", "读文档", "修改失败"], "log_t": [1.0, 2.0, 3.0, 9.0]}
        steps = progress_steps(prog, clock=lambda ts: str(int(ts)), error="修改超时 420 秒")
        self.assertEqual([x["k"] for x in steps], ["查找文件", "读文档", "失败"])
        self.assertEqual(steps[-1], {"t": "9", "k": "失败", "v": "修改超时 420 秒"})

    def test_old_progress_without_times(self):
        steps = progress_steps({"log": ["开始", "读文档", "已完成"]}, clock=lambda ts: "x")
        self.assertEqual(steps, [{"k": "读文档"}])
        self.assertEqual(progress_steps(None), [])


class AppReport(unittest.TestCase):
    """app.py 汇报（Meeting._report）写进逐字稿的 steps、状态推送（brain_state）里的 steps：带时间、不挂"修改"、失败一行。"""

    def setUp(self):
        import types
        from zuhui.app import Meeting
        from zuhui.board import Board
        self.dir = Path(tempfile.mkdtemp(prefix="worker-steps-app-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.board = Board(self.dir / "board.json")
        app = Meeting.__new__(Meeting)
        app.cfg = {"fast": {}, "hands": {}, "deep": {}, "meeting": {"lang": "zh"}}
        app.board = self.board
        app.meetings = types.SimpleNamespace(clock=lambda ts=None: "--:--" if ts is None else f"t{int(ts)}")
        app.fast = types.SimpleNamespace(reply=lambda s: iter([("sentence", "好了。")]), remember=lambda *a: None)
        app.mouth = types.SimpleNamespace(say=lambda *a, **k: None, speaking=False)
        app.log = lambda *a, **k: None
        app._multi_party = lambda: False
        self.extra = []
        app._speech_line = lambda tag=None, heard=None, extra=None: (self.extra.append(extra), (lambda *a: None))[1]
        app.pending_reports = []
        self.app = app

    def task(self, status, progress, **kw):
        t = self.board.add("原话", "改 3.2 节第一段")
        self.board.set_progress(t["id"], progress)
        return dict(self.board.get(t["id"]), status=status, **kw)

    def test_report_steps(self):
        t = self.task("done", {"log": ["开始", "看目录", "在 D 节改第 66–78 处", "检查修改", "已完成"],
                               "log_t": [10.0, 20.0, 30.0, 40.0, 50.0]}, result="改好了")
        self.app._report(t)
        steps = self.extra[-1]["steps"]
        self.assertEqual(steps, [{"t": "t20", "k": "看目录"}, {"t": "t30", "k": "在 D 节改第 66–78 处"},
                                 {"t": "t40", "k": "检查修改"}])

    def test_failed_report_says_it_once(self):
        t = self.task("failed", {"log": ["开始", "读文档", "修改失败"], "log_t": [10.0, 20.0, 90.0]}, error="修改超时 420 秒")
        self.app._report(t)
        steps = self.extra[-1]["steps"]
        self.assertEqual(steps, [{"t": "t20", "k": "读文档"}, {"t": "t90", "k": "失败", "v": "修改超时 420 秒"}])

    def test_brain_state_pushes_timed_steps(self):
        import types
        t = self.board.add("原话", "改 3.2 节")
        self.board.claim_next()
        self.board.set_progress(t["id"], {"step": "在 D 节改第 87 处", "edits": 22,
                                          "log": ["开始", "读文档", "在 D 节改第 66–87 处"], "log_t": [1.0, 2.0, 3.0]})
        app = self.app
        app._mute, app._talk, app._deep_jobs, app.pending_answers = False, None, [], []
        app.hands = types.SimpleNamespace()
        ed = app.brain_state()["edits"][0]
        self.assertEqual(ed["step"], "在 D 节改第 87 处")                            # 当前这一步照旧
        self.assertEqual(ed["steps"], [{"t": "t1", "k": "开始"}, {"t": "t2", "k": "读文档"}, {"t": "t3", "k": "在 D 节改第 66–87 处"}])
        self.assertIs(hands.progress_steps, progress_steps)


if __name__ == "__main__":
    unittest.main()
