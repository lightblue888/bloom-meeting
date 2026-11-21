# -*- coding: utf-8 -*-
"""2026-11-21 下午那场组会暴露的快嘴对话毛病（句子都是编的，句式、长短、识别错字和当时一样）：
1. 让后台深想时，题目和交过去的问题是"你让后台的模型去想个答案"这句原话，不是他前面问的那个问题；
2. 嘴上说"先不动，等你确认"，兜底却按上一句记了一条改稿开改；一句话被停顿切成两半（"……人工打分法。" + "会出现。"）没并起来；
3. 点名不要某条任务（"这个顿号任务不用改了"）：没认出来，快嘴又记了一条"撤销……的任务"排在后面，被点名的那条照样改完；
4. 含糊的"把顿号符号改一下"被派成全文任务；"我把这个，会出现。的后面那个顿号删掉了"被兜底记成标题是"的后面……"的任务；
5. AI 刚问"要统一的话你点个头"，"统一，统一。"被当成口头禅跳过；
6. 说出口的话里报了任务编号（"T2 还在排队收尾"）；
7. 快嘴已经答过的问题，后台补查的答案又念了一遍。

全离线、不花钱：快嘴换成假的生成器，补漏检查、深想都换掉；会议对象用哑嘴（mute=True）、dry=True 不起工人。
ZUHUI_HOME / BLURT_HOME 指到临时目录，论文是临时目录里的假论文；中继指到 https://127.0.0.1:1/。

    python -X utf8 -m unittest tests.test_dialog_1121b -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-1121b-")     # 必须在 import zuhui 之前
    os.environ.setdefault("BLURT_HOME", tempfile.mkdtemp(prefix="blurt-test-1121b-"))
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import dialog_rules as R  # noqa: E402
from zuhui import share  # noqa: E402

share.PUBLIC_RELAY = "https://127.0.0.1:1/"

# ---------------------------------------------------------------- 编的句子（和当时的句式一样）
ASK_TABLE = ("你要不要把这个表一里面的几类方法全部讲一遍啊？甲方法，乙方法丙方法丁方法。你觉得要不要把它全部在这个？"
             "第二章里面讲一遍啊。同样的位置，你怎么看？")
DEEPER = "我想问个啥来着，我先啥？你让后台的模型去想个答案，想好了跟我说。"
HALF1 = "你补一个。呃，我想想啊。你补一个说明吧。把二点三第一段改成只有基于规则的方法，即阈值法查表法和人工打分法。"
HALF2 = "会出现。"
HOLD = "好，那我先不动，等你确认要补的写法再记。"
CANCEL = "OK，这个顿号任务就不用改了，不用改了，就这样吧。"
VAGUE = "把顿号符号改一下。"
OWN = "我把这个，会出现。的后面那个顿号删掉了。"
OWN_REPLY = "好，你是说 2.3 第一段末句“会出现”后面那个顿号你已经自己删了，对吧？那我这边的顿号任务还在排队，不会再动这一处。"
NOD_Q = "这条改好了：2.4 节末尾加了一句说明。正文里还有几处单位写法没统一，我这次没动，要统一的话你点个头。"
T_DUN = "全文顿号按语气改，别再拆成句号"

PAPER = """# 测试稿

## 2 方法

### 2.3 基线

只有基于规则的方法，即阈值法、查表法和人工打分法会出现。表 1 列了几类方法。

### 2.4 指标

ABC 指标是判错的比例，越小越好。

## 3 结论

本文提出了一个基准。
"""


class RulesTest(unittest.TestCase):
    def test_question_of(self):
        q = R.question_of(ASK_TABLE)
        self.assertTrue(q.startswith("第二章 "), q)
        self.assertIn("要不要把这个表一里面的几类方法全部讲一遍", q)
        self.assertEqual(R.deep_question(DEEPER), "")                     # 叫后台想、没内容：从上文找
        self.assertEqual(R.deep_question("你仔细想一想，二点三第二段要不要删掉？"), "二点三第二段要不要删掉")
        for empty in ("你怎么看？", "是吧？", "我先说个啥，我先啥？", "好的。"):
            self.assertEqual(R.question_of(empty), "", empty)
        self.assertIn("ABC", R.question_of("然后你这个ABC确是什么东西呢？不是不是雀，就是ABC是什么东西？"))

    def test_same_question(self):
        self.assertTrue(R.same_question("评测指标里的 ABC 是什么意思", "然后你这个ABC确是什么东西呢？"))
        self.assertTrue(R.same_question("阈值法和查表法的区别", "查表法跟阈值法到底有什么区别"))
        self.assertFalse(R.same_question("评测指标里的 ABC 是什么意思", "表一里面有几类方法？"))

    def test_holds_off(self):
        self.assertTrue(R.holds_off(HOLD))
        self.assertTrue(R.holds_off(OWN_REPLY))
        self.assertTrue(R.holds_off("好，那这条你自己改，我这边不动。"))
        for no in ("好，2.3 那句改成只列三种，别的地方先不动，我记下了，交给后台排队改。", "我记下了，交给后台排队改。",
                   "先不改成复数，保留原来的单数，我记下了。", "好，2.3 删掉阈值法那句，其余我先不动，记下了，交给后台排队改。"):
            self.assertFalse(R.holds_off(no), no)

    def test_join_tail(self):
        self.assertTrue(R.join_tail(HALF1, HALF2).endswith("人工打分法，会出现。"))
        self.assertEqual(R.join_tail("把二点三第一段删掉。", "好的。"), "把二点三第一段删掉。好的。")
        self.assertEqual(R.join_tail("把二点三第一段删掉。", "你觉得呢？"), "把二点三第一段删掉。你觉得呢？")

    def test_named_cancel(self):
        self.assertIn("顿号", R.named_cancel(CANCEL))
        self.assertEqual(R.named_cancel("顿号那条撤掉吧"), "顿号")
        self.assertIsNotNone(R.named_cancel("T2 不要了"))
        for no in ("这个任务改好了吗", "不用改了，就这样吧", "那条不用改成甲，改成乙", "算了，先往下看吧", "我自己改吧",
                   "顿号那条撤掉吧，然后把2.3第二段的阈值法删掉。"):
            self.assertIsNone(R.named_cancel(no), no)
        self.assertTrue(R.retracts(CANCEL))                               # 叫停之外的字再多，点名撤的也算
        self.assertTrue(R.cancel_title("撤销全文顿号按语气改的任务"))
        self.assertTrue(R.cancel_title("取消顿号那条"))
        self.assertFalse(R.cancel_title("2.3 第一段改成只列三种方法"))
        self.assertFalse(R.cancel_title("取消第二段的分号"))                # 改论文，不是撤任务
        self.assertEqual(R.cancel_head("撤销全文顿号按语气改"), "全文顿号按语气改")

    def test_scrub_ids(self):
        names = {"T2": R.task_short_name(T_DUN), "T7": R.task_short_name("撤销全文顿号按语气改的任务")}
        self.assertEqual(names["T2"], "全文顿号按语气改")
        out = R.scrub_task_ids("顿号那条我已经撤掉了，T2 还在排队收尾，T99 不在，KX-T 不动。", names)
        self.assertEqual(out, "顿号那条我已经撤掉了，「全文顿号按语气改」那条还在排队收尾，T99 不在，KX-T 不动。")
        self.assertEqual(R.scrub_task_ids("T2 这条好了。", names), "「全文顿号按语气改」那条好了。")

    def test_vague_and_own_done(self):
        self.assertEqual(R.vague_edit(VAGUE), "顿号符号")
        for no in ("把2.3第二段改一下", "把“会出现”后面的顿号删掉", "那你改一下", "把这个改一下", "把顿号都改成逗号"):
            self.assertIsNone(R.vague_edit(no), no)
        self.assertFalse(R.directive(OWN))                               # 他说的是自己已经删了
        title = R.title_from_words(OWN)
        self.assertFalse(title.startswith("的"), title)
        self.assertTrue(R.directive("把会出现后面那个顿号删掉。"))
        self.assertTrue(R.asked_back(OWN_REPLY))                          # 反问在中间也算

    def test_answers(self):
        self.assertTrue(R.invites_answer(NOD_Q))
        self.assertTrue(R.invites_answer("2.4 那句加好了。要不要也挪进表一，你定。"))
        self.assertFalse(R.invites_answer("好，记下了，交给后台排队改。"))
        self.assertTrue(R.answers_question("统一，统一。", NOD_Q))
        self.assertTrue(R.answers_question("改吧。", NOD_Q))
        self.assertTrue(R.answers_question("单位写法", NOD_Q))
        self.assertFalse(R.answers_question("我们接着看第三章吧", NOD_Q))


def _meeting():
    from zuhui import app
    from zuhui.config import load
    return app.Meeting(load(), dry=True, mute=True)


class FlowBase(unittest.TestCase):
    """会议对象（哑嘴、dry、不起耳朵和工人），论文是临时目录里的假论文。快嘴换成假的：每一轮按 self.replies 依次回。"""

    @classmethod
    def setUpClass(cls):
        d = Path(tempfile.mkdtemp(prefix="paper-1121b-"))
        cls.paper_file = d / "测试稿.md"
        cls.paper_file.write_text(PAPER, encoding="utf-8")
        cls._saved_cur = zconfig.CURRENT_PAPER.read_bytes() if zconfig.CURRENT_PAPER.exists() else None
        zconfig.CURRENT_PAPER.write_text(json.dumps({"path": str(cls.paper_file), "name": "测试稿"}, ensure_ascii=False),
                                         encoding="utf-8")
        cls.m = _meeting()

    @classmethod
    def tearDownClass(cls):
        if cls.m.meetings.current():
            cls.m.meetings.end()
        if cls._saved_cur is None:
            try:
                zconfig.CURRENT_PAPER.unlink()
            except OSError:
                pass
        else:
            zconfig.CURRENT_PAPER.write_bytes(cls._saved_cur)

    def setUp(self):
        m = self.m
        if m.meetings.current():
            m.meetings.end()
        m.board.reset()
        m.anchor.clear()
        m._p_wait, m._frag, m._prev_turn, m._turn, m._spec = [], None, None, None, None
        m._said, m._said_at, m._ended, m._mute = "", 0.0, 0.0, False
        m._last_reply = ""
        m.fast.history.clear()
        m.viewer = None
        m.pending_reports.clear()
        m.pending_answers.clear()
        import shutil
        shutil.rmtree(zconfig.STATE / "changes", ignore_errors=True)
        while not m.events.empty():
            m.events.get_nowait()
        m.meetings.start(session=f"s-1121b-{time.time()}", title="组会")
        self.calls, self.replies, self.deep = [], [], []
        m.fast.reply = self._fake
        m.fast.grounder = lambda *a, **k: None
        m.fast.warm = lambda *a, **k: True
        m._check_missed = lambda *a, **k: None
        m._deep = lambda *a, **k: self.deep.append((a, k))
        m._deep_start = lambda *a, **k: {"lock": mock.MagicMock(), "state": "running"}
        for k in ("amend", "stop_task"):
            m.hands.__dict__.pop(k, None)

    def _fake(self, said, light=False, remember=True):
        self.calls.append(said)
        tag, *sents = self.replies.pop(0) if self.replies else ("#chat", "嗯。")
        yield ("tag", tag)
        for s in sents:
            yield ("sentence", s)
        yield ("done", "".join(sents))

    def say(self, text: str):
        from zuhui.ears import Utterance
        m = self.m
        m._said, m._said_at = "", 0.0
        now = time.time()
        m.handle(Utterance(n=1, text=text, t="", shot=None, session="", heard=now, landed=now, sent=now, started=now))

    def ai_lines(self) -> list[dict]:
        return [l for l in self.m.meetings.current_lines() if l["who"] == "student"]

    def add(self, title: str, status: str = "queued", ago: float = 0.0, **fields) -> dict:
        m = self.m
        t = m.board.add(remark=f"（原话）{title}", title=title, paper=m._paper_now())
        if status in ("working", "done"):
            m.board.claim(t["id"])
        if status == "done":
            m.board.finish(t["id"], result=f"{title}，改好了。")
        if ago:
            when = (datetime.now() - timedelta(seconds=ago)).isoformat(timespec="seconds")
            m.board.annotate(t["id"], created=when, **({"finished": when} if status == "done" else {}))
        if fields:
            m.board.annotate(t["id"], **fields)
        return m.board.get(t["id"])

    def cut_turn(self, text: str):
        """上一句交给 AI 的话被这句打断、一个字没回（_turn_begin 的 pending）。"""
        m = self.m
        now = time.time()
        m.meetings.add_line("teacher", text)
        m.fast.remember("user", text)
        m._prev_turn = {"who": None, "t": now, "t0": now - 1, "text": text, "words": text, "acts": [],
                        "answered": False, "spoken": "", "cut": True, "addressed": True}


class DeepQuestionTest(FlowBase):
    """1：交给深想的是他前面问的那个问题，逐字稿上那一步也显示它。"""

    def test_think_deeper_takes_earlier_question(self):
        self.replies = [("#ask", "我倾向不全部讲。第二章那段已经把几类讲清了，")]
        self.say(ASK_TABLE)
        self.say(DEEPER)
        self.assertEqual(len(self.calls), 1)                                 # 深想不过快嘴
        (args, kw), = self.deep
        self.assertIn("要不要把这个表一里面的几类方法全部讲一遍", kw["topic"])
        self.assertIn(kw["topic"], args[1])                                 # 交过去的问题里写着它
        step = self.ai_lines()[-1]["steps"][0]
        self.assertEqual(step["k"], "深入思考")
        self.assertNotIn("想个答案", step["v"])
        self.assertIn("表一里面的几类方法", step["v"])

    def test_think_deeper_own_question(self):
        self.say("你仔细想一想，二点三第二段要不要删掉？")
        (args, kw), = self.deep
        self.assertEqual(kw["topic"], "二点三第二段要不要删掉")

    def test_fast_deep_with_question(self):
        from zuhui.fast import FastBrain
        self.assertEqual(FastBrain._parse_tag("#deep 要不要在第二章把表一的几类方法都介绍一遍？", False),
                         ("#deep 要不要在第二章把表一的几类方法都介绍一遍？", "#deep", ""))
        self.assertEqual(FastBrain._parse_tag("#deep 我去查一下。", False)[2], "我去查一下。\n")   # 整句话：照念
        self.replies = [("#deep 要不要在第二章把表一的几类方法都介绍一遍", "好，我交给后台看一下。")]
        self.say("那你让后台查查吧")
        (args, kw), = self.deep
        self.assertEqual(kw["topic"], "要不要在第二章把表一的几类方法都介绍一遍")

    def test_fast_deep_without_question_uses_context(self):
        self.replies = [("#ask", "我倾向不全部讲。"), ("#deep", "好，我交给后台看一下。")]
        self.say(ASK_TABLE)
        self.say("那你让后台查查吧")
        (args, kw), = self.deep
        self.assertIn("表一里面的几类方法", kw["topic"])

    def test_deep_start_names_from_question(self):
        # 状态栏的短名、"刚查过的"记的都是那个问题，不是原话
        from zuhui import app
        m = self.m
        prompts = []

        def fake_ask(prompt, **k):
            prompts.append(prompt)
            return "表一几类方法讲不讲"
        m.hands.ask_paper = mock.Mock(return_value=("不用全讲，第二章那段已经够了。", None))
        m.fast.deep_log = []
        with mock.patch("zuhui.claude_text.ask", fake_ask):
            job = app.Meeting._deep_start(m, "【他要你把刚才的问题想透】" + DEEPER, topic="第二章 要不要把表一的几类方法都讲一遍")
            job["ev"].wait(5)
            for th in threading.enumerate():
                if th.name == "deep-topic":
                    th.join(5)
        self.assertEqual(job["question"], "第二章 要不要把表一的几类方法都讲一遍")
        self.assertIn("要不要把表一的几类方法都讲一遍", prompts[0])
        self.assertNotIn("想个答案", prompts[0])
        self.assertEqual(m.fast.deep_log[-1]["q"], "第二章 要不要把表一的几类方法都讲一遍")
        ctx = m.fast._messages("下一句")[-1]["content"]
        self.assertIn("问的是：第二章 要不要把表一的几类方法都讲一遍", ctx)


class HoldOffTest(FlowBase):
    """2：嘴上"先不动"就不派；被停顿切开的两半并起来看。"""

    def test_cut_then_tail_hold_off_not_dispatched(self):
        m = self.m
        self.cut_turn(HALF1)
        self.replies = [("#chat", HOLD)]
        self.say(HALF2)
        self.assertEqual(m.board.tasks(), [])                                # 以前 _cover_pending 照样记了一条开改
        self.assertIn("连起来是", self.calls[-1])
        self.assertIn("人工打分法，会出现", self.calls[-1])

    def test_cut_then_tail_recorded_as_one(self):
        m = self.m
        self.cut_turn(HALF1)
        self.replies = [("#edit 2.3 第一段改成只有基于规则的方法，即阈值法、查表法和人工打分法，会出现",
                         "好，2.3 第一段那句改成只有基于规则的方法，即阈值法、查表法和人工打分法，会出现，我记下了，交给后台排队改。")]
        self.say(HALF2)
        tasks = m.board.tasks()
        self.assertEqual(len(tasks), 1)
        self.assertIn("人工打分法，会出现", tasks[0]["remark"])               # 工人看得到前半截

    def test_own_edit_with_hold_off_not_recorded(self):
        m = self.m
        self.replies = [("#edit 2.3 第一段补一句说明", HOLD)]
        self.say("你补一个说明吧，二点三第一段那里。")
        self.assertEqual(m.board.tasks(), [])
        self.assertIn("没记", m.fast.history[-1]["content"])

    def test_cover_respects_hold_off(self):
        m = self.m
        self.replies = [("#chat", "好，那这条你自己改，我这边不动。")]
        self.say("把二点三第一段的阈值法删掉。")
        self.assertEqual(m.board.tasks(), [])


class NamedCancelTest(FlowBase):
    """3：点名不要某条任务：排队的取消，正在改的停下并恢复原样，不记"撤销……"的新任务。"""

    def board(self, dun_status="working"):
        self.add("2.3 第一段补一句说明", status="done", ago=300)
        t = self.add(T_DUN, status=dun_status, ago=40)
        self.add("2.4 指标那句加个例子", ago=20)
        return t

    def test_working_stopped_and_restored(self):
        m = self.m
        t = self.board()
        m.hands.stop_task = mock.Mock(return_value=True)
        self.say(CANCEL)
        m.hands.stop_task.assert_called_once_with(t["id"])
        self.assertEqual(self.calls, [])                                     # 不过快嘴
        self.assertEqual(len(m.board.tasks()), 3)                            # 没有新记一条
        self.assertTrue(m.board.get(t["id"])["retracted"])
        self.assertEqual(self.ai_lines()[-1]["text"], f"好，「{T_DUN}」停了，稿子保持原样。")
        d = zconfig.STATE / "changes"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{t['id']}.json").write_text(json.dumps({"id": t["id"], "changed": True}), encoding="utf-8")
        m.board.finish(t["id"], error="已撤回")
        m.viewer = types.SimpleNamespace(reject_task=mock.Mock(return_value={"ok": True}))
        m._on_task_done(m.board.get(t["id"]))
        m.viewer.reject_task.assert_called_once_with(t["id"])
        got = m.board.get(t["id"])
        self.assertTrue(got["rejected"])
        self.assertFalse(got.get("note"))                                    # 不进修改清单
        n = len(self.calls)
        m._report(m.events.get_nowait()[1])
        self.assertEqual(len(self.calls), n)                                 # 汇报不念

    def test_queued_cancelled(self):
        m = self.m
        t = self.board(dun_status="queued")
        self.say(CANCEL)
        got = m.board.get(t["id"])
        self.assertTrue(got["rejected"] and got["retracted"])
        self.assertEqual(self.ai_lines()[-1]["text"], f"好，「{T_DUN}」先不改了。")
        self.assertFalse(m.board.get("T3").get("rejected"))                  # 别的不动

    def test_fast_cancel_title_is_undo(self):
        # 快嘴把它写成 "#edit 撤销……的任务"：不记新任务，按撤回办（正在改的停下）
        m = self.m
        t = self.board()
        m.hands.stop_task = mock.Mock(return_value=True)
        self.replies = [("#edit 撤销全文顿号按语气改的任务", "好，顿号那条我撤掉，不再改。")]
        self.say("顿号那个就这样吧，别弄了。")
        self.assertEqual(len(m.board.tasks()), 3)
        self.assertFalse(any(x["title"].startswith("撤销") for x in m.board.tasks()))
        m.hands.stop_task.assert_called_once_with(t["id"])
        self.assertTrue(m.board.get(t["id"])["retracted"])
        self.assertIn("停了", self.ai_lines()[-1]["text"])

    def test_act_never_adds_cancel_task(self):
        from zuhui.ears import Utterance
        m = self.m
        t = self.board(dun_status="queued")
        u = Utterance(n=1, text="那条不要了", t="", shot=None, session="")
        m.act("#edit 取消全文顿号按语气改的任务", u, None)
        self.assertEqual(len(m.board.tasks()), 3)
        self.assertTrue(m.board.get(t["id"])["rejected"])

    def test_status_question_untouched(self):
        m = self.m
        self.board()
        self.replies = [("#status", "顿号那条正在改。")]
        self.say("这个顿号任务改好了吗？")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(t.get("rejected") for t in m.board.tasks()))


class VagueAndOwnEditTest(FlowBase):
    """4：含糊的改法先问；他说自己已经改了、快嘴反问确认时，兜底不记；兜底的标题不以"的"开头。"""

    def test_vague_asks_first(self):
        m = self.m
        self.replies = [("#edit " + T_DUN, "顿号我记下了，交给后台排队改。")]
        self.say(VAGUE)
        self.assertEqual(m.board.tasks(), [])
        self.assertIn("没说改哪一处", self.calls[-1])
        self.assertEqual(self.ai_lines()[-1]["text"], "顿号符号改哪一处，改成什么样？")

    def test_vague_after_proposal_is_answer(self):
        # AI 刚提议过改哪一处：他说"把顿号符号改一下"是在同意，不拦
        m = self.m
        m._last_reply = "2.3 第一段那几个顿号读着断，改成逗号会顺一些，要不要改？"
        self.replies = [("#edit 2.3 第一段顿号改成逗号", "好，2.3 第一段的顿号改成逗号，我记下了，交给后台排队改。")]
        self.say(VAGUE)
        self.assertEqual(len(m.board.tasks()), 1)

    def test_own_edit_not_recorded(self):
        m = self.m
        self.add(T_DUN)
        self.replies = [("#ask", OWN_REPLY)]
        self.say(OWN)
        self.assertEqual(len(m.board.tasks()), 1)                             # 以前补记了一条"的后面那个顿号删掉了"


class AnswerTest(FlowBase):
    """5：AI 刚问过，他的短回答不是口头禅。"""

    def test_nod_not_skipped(self):
        m = self.m
        m._last_reply = NOD_Q
        self.replies = [("#silent",), ("#edit 正文其余单位写法统一", "好，其余几处单位写法也统一，我记下了，交给后台排队改。")]
        self.say("统一，统一。")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("你刚问了他", self.calls[0])
        self.assertIn("你刚才判了不接", self.calls[1])
        self.assertEqual([t["title"] for t in m.board.tasks()], ["正文其余单位写法统一"])
        line = [l for l in m.meetings.current_lines() if l["who"] == "teacher"][-1]
        self.assertIsNone(line.get("skip"))

    def test_ok_after_question_goes_to_model(self):
        m = self.m
        m._last_reply = "2.4 那句加好了。要不要也挪进表一，你定。"
        self.replies = [("#edit 2.4 那句挪进表一", "好，挪进表一，我记下了，交给后台排队改。")]
        self.say("好的。")
        self.assertEqual(len(self.calls), 1)                                 # 以前"好的"当收尾确认、不接
        self.assertEqual(len(m.board.tasks()), 1)

    def test_old_question_not_answer(self):
        m = self.m
        m._last_reply = NOD_Q
        m.__dict__["_last_reply_t"] = time.time() - 600
        self.replies = [("#silent",)]
        self.say("统一，统一。")
        self.assertEqual(len(self.calls), 1)


class TaskIdTest(FlowBase):
    """6：说出口的话里不报任务编号。"""

    def test_fast_reply_scrubs_ids(self):
        from zuhui.fast import FastBrain
        m = self.m
        self.add("2.3 第一段补一句说明", status="done")
        self.add(T_DUN)
        f = m.fast
        f._messages = lambda text, light=False: [{"role": "system", "content": ""}, {"role": "user", "content": text}]
        f._deltas = lambda messages: iter(["#status\n", "你是说那个顿号你自己改，对吧？", "T2 还在排队收尾，", "T1 已经改好了。"])
        got = [v for k, v in FastBrain.reply(f, "顿号那条呢") if k == "sentence"]
        said = "".join(got)
        self.assertNotRegex(said, r"T\d")
        self.assertIn("「全文顿号按语气改」那条还在排队收尾", said)
        self.assertIn("「2.3 第一段补一句说明」那条已经改好了", said)
        self.assertNotRegex(f.history[-1]["content"].split("\n", 1)[1], r"T\d")   # 历史里也不留

    def test_deep_answer_scrubs_ids(self):
        m = self.m
        self.add(T_DUN)
        self.assertEqual(m._no_ids("T1 还没开始。"), "「全文顿号按语气改」那条还没开始。")


class DupAnswerTest(FlowBase):
    """7：快嘴已经当面答过的，后台补查的答案不再念。"""

    def job(self, answer: str, **kw) -> dict:
        m = self.m
        sec0 = m.meetings.elapsed() or 0.0
        return dict({"q": "（这句话里刚才漏答的事：评测指标里的 ABC 是什么意思。只答这几件。）",
                     "question": "评测指标里的 ABC 是什么意思", "answer": answer, "sec0": sec0, "t0": time.time(),
                     "spoken": False, "lock": threading.Lock()}, **kw)

    def post(self, job):
        m = self.m
        job["line"] = m.meetings.add_line("student", job["answer"], tag="#deep-answer", extra={"unspoken": True})
        job["ready_at"] = time.time()
        m.pending_answers.append(job)

    ANSWER = "ABC 就是判错率，指方法判断的正负和参照不一致的单元占全部单元的比例，越小越好，2.4 节写的。"

    def test_reasked_and_answered_not_read(self):
        m = self.m
        job = self.job(self.ANSWER)
        m.meetings.add_line("teacher", "然后你这个ABC确是什么东西呢？不是不是雀，就是ABC是什么东西？")
        m.meetings.add_line("student", "ABC 是判错率，论文 2.4 节写的，是方法判断的正负和参照不一致的比例，越小越好。", tag="#ask")
        self.post(job)
        self.assertEqual(m._take_fresh_answers(), [])
        self.assertTrue(job["spoken"])
        self.assertTrue(job["line"].get("stale"))

    def test_not_answered_is_read(self):
        m = self.m
        job = self.job(self.ANSWER)
        m.meetings.add_line("teacher", "表一里面有几类方法？")
        m.meetings.add_line("student", "表一里列了四类：阈值法、查表法、人工打分法和随机法。", tag="#ask")
        self.post(job)
        self.assertEqual(m._take_fresh_answers(), [job])

    def test_explicit_deepen_still_read(self):
        m = self.m
        job = self.job(self.ANSWER, explicit=True)
        m.meetings.add_line("teacher", "就是ABC是什么东西？")
        m.meetings.add_line("student", "ABC 是判错率，论文 2.4 节写的，是方法判断的正负和参照不一致的比例，越小越好。", tag="#ask")
        self.post(job)
        self.assertEqual(m._take_fresh_answers(), [job])


if __name__ == "__main__":
    unittest.main()
