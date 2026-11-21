# -*- coding: utf-8 -*-
"""2026-11-21 傍晚那场组会暴露的改稿、快嘴毛病（句子、论文都是编的，句式、识别错字和当时一样）：
1. 快嘴嘴上说"那条还在改，我去改它的写法"，兜底却按原话另记了一条；补记的任务标题照抄口语
   （"不用写答案，不用写答案""你你你这个总起句为什么要扯到表一删掉啊""第一段 我说了要扩写的"）；
2. 对方只说"你再想想"，交给深想的问题成了半截话（"这个是不是写的太，就是你后面"）；
3. 对方在文档里手改了一段，快嘴只拿到片段，说"我这边看不到你改成什么样，你念一下"。

全离线、不花钱：快嘴换成假的生成器，补漏检查、深想都换掉；会议对象用哑嘴（mute=True）、dry=True 不起工人。
ZUHUI_HOME / BLURT_HOME 指到临时目录，论文是临时目录里的假论文；中继指到 https://127.0.0.1:1/。

    python -X utf8 -m unittest tests.test_dialog_1121c -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-1121c-")     # 必须在 import zuhui 之前
    os.environ.setdefault("BLURT_HOME", tempfile.mkdtemp(prefix="blurt-test-1121c-"))
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import dialog_rules as R  # noqa: E402
from zuhui import doc_edits, share  # noqa: E402

share.PUBLIC_RELAY = "https://127.0.0.1:1/"

PAPER = """# 测试稿

## 2 方法

### 2.3 基线

只有基于规则的方法，即阈值法、查表法和人工打分法会出现。表 1 列了几类方法。

（1）规则法的命中率都很高。三种规则的命中率在九成以上。

### 2.4 指标

ABC 指标是判错的比例，越小越好。

## 3 结论

本文提出了一个基准。
"""

T_RUN = "3.2 第二段改成先问后答"
SUPP = "不用写答案，不用写答案。"
SUPP_REPLY = f"明白，不写答案，只留问题。「{T_RUN}」那条还在改，我去改它的写法。"
STUTTER = "表一把几类方法放在一起是干什么？你你你这个总起句为什么要扯到表一删掉啊？"
STUTTER_REPLY = "你是问 2.3 末尾那句“表 1 列了几类方法”是什么用意。这句放在这里确实有点突兀。"
SHORT_Q = "你这个2.3的第一段，呃，这个是不是写的太，就是你后面。后面是不是写的太少了？是不是应该稍微展开一点呢？"
SHORT_REPLY = "你是说 2.3 第一段太短，想展开一点。我可以补一句：三种规则的命中率都在九成以上，但判错的比例差很多。你要我这样展开吗？"


class RulesTest(unittest.TestCase):
    def test_refers_back_and_names_task(self):
        self.assertTrue(R.refers_back(SUPP_REPLY))
        self.assertTrue(R.names_task(SUPP_REPLY, T_RUN))
        self.assertTrue(R.names_task(f"好，{T_RUN}那条我一起改。", T_RUN))
        self.assertFalse(R.names_task("好，3.2 第三段我记下了。", T_RUN))
        self.assertFalse(R.names_task(SUPP_REPLY, "改"))                    # 太短的标题不算

    def test_oral_free(self):
        self.assertEqual(R.oral_free("不用写答案，不用写答案"), "不用写答案")
        self.assertEqual(R.oral_free("你你你这个总起句为什么要扯到表一删掉啊"), "总起句把表一删掉")
        self.assertEqual(R.oral_free("我说了要扩写的"), "扩写")
        self.assertEqual(R.oral_free("呃呃呃"), "")

    def test_where_of(self):
        self.assertEqual(R.where_of("那么二点三的第一段应该怎么改呢？"), "2.3 第一段")
        self.assertEqual(R.where_of("2.3 第（1）条太短"), "2.3 第（1）条")
        self.assertEqual(R.where_of("3.3 节补一句"), "3.3")
        self.assertEqual(R.where_of("摘要那句删掉"), "摘要")
        self.assertEqual(R.where_of("随便说说"), "")

    def test_restated_edit(self):
        self.assertEqual(R.restated_edit("好，我把 2.3 末尾那句“表 1 列了几类方法”删掉。我这就排队去改。"),
                         "2.3 末尾那句“表 1 列了几类方法”删掉")
        self.assertEqual(R.restated_edit("好，2.4 里的判错比例统一改成判错率，我记下了，排队去改。"),
                         "2.4 里的判错比例统一改成判错率")
        self.assertEqual(R.restated_edit("好，我在 2.3 讲规则法的地方补一句：规则法不需要训练数据，命中率都很高。我记下了，排队去改。"),
                         "在 2.3 讲规则法的地方补一句")
        self.assertEqual(R.restated_edit(SHORT_REPLY), "2.3 第一段太短，想展开一点")      # "你是说……"也是复述
        for no in (SUPP_REPLY, STUTTER_REPLY, "你是说 2.3 第一段要展开，对吧？",
                   "我倾向在 2.3 后面补一句说明，要不要我这样记下？", "2.3 那句前面已经删了，不用再改。"):
            self.assertEqual(R.restated_edit(no), "", no)

    def test_edit_title(self):
        self.assertEqual(R.edit_title(SUPP, SUPP_REPLY), "不用写答案")
        self.assertEqual(R.edit_title(STUTTER, STUTTER_REPLY), "2.3 总起句把表一删掉")
        t = R.edit_title("那么二点三的第一段应该怎么改呢？二点三的第一段，我说了要扩写的。",
                         "2.3 第一段现在只有一句。我前面建议补两点：一是命中率，二是判错率。你要我把这两点补进去吗？")
        self.assertEqual(t, "2.3 第一段 扩写")
        self.assertEqual(R.edit_title("你去删啊。", "好，我把 2.3 末尾那句“表 1 列了几类方法”删掉。我这就排队去改。"),
                         "2.3 末尾那句“表 1 列了几类方法”删掉")

    def test_question_of_no_fragment(self):
        q = R.question_of(SHORT_Q)
        self.assertEqual(q, "2.3 第一段 后面是不是写的太少了，是不是应该稍微展开一点")
        self.assertNotIn("就是你后面", q)
        self.assertEqual(R.question_of("呃，这个是不是写的太，就是你后面。"), "")

    def test_think_question(self):
        q = R.think_question(R.question_of(SHORT_Q), SHORT_REPLY)
        self.assertTrue(q.startswith("2.3 第一段 后面是不是写的太少了，是不是应该稍微展开一点（我前面提议补一句"), q)
        q2 = R.think_question("", SHORT_REPLY)                              # 他前面没问：用 AI 的问话
        self.assertTrue(q2.startswith("2.3 第一段 要不要这样展开（我前面提议补一句"), q2)
        self.assertEqual(R.think_question("2.4 的 ABC 指标是什么意思", ""), "2.4 的 ABC 指标是什么意思")
        self.assertEqual(R.proposal_of("我倾向不全部讲。"), "倾向不全部讲")
        self.assertEqual(R.proposal_of("论文里写的是九成。"), "")

    def test_denies_hand_edit(self):
        for yes in ("我这边看不到你改成什么样，你念一下改后的句子。", "论文全文里是你改之前的版本。",
                    "要核对的话你念一下改后的句子。", "I can't see your edits yet."):
            self.assertTrue(R.denies_hand_edit(yes), yes)
        for no in ("我看不到屏幕。", "你改的那段读着顺了。", "要不要我念一下原文？"):
            self.assertFalse(R.denies_hand_edit(no), no)


class HandEditNote(unittest.TestCase):
    LINE = {"t": "02:10", "sec": 130.0, "who": "system", "text": "林晓 改了 2.3 第（1）段", "tag": doc_edits.TAG,
            "by": "林晓", "where": "2.3 第（1）段", "section": "2.3 基线",
            "before": "…规则法的命中率都很高。三种…", "after": "…规则法的命中率都很高，但判错率差很多。三种…"}

    def test_full_paragraph_attached(self):
        text = PAPER.replace("规则法的命中率都很高。", "规则法的命中率都很高，但判错率差很多。")
        n = doc_edits.note([self.LINE], text=text)
        self.assertIn("02:10 林晓 改了 2.3 第（1）段：改前「…规则法的命中率都很高。三种…」", n)
        self.assertIn("最近改的段落现在整段是", n)
        self.assertIn("〔2.3 第（1）段〕（1）规则法的命中率都很高，但判错率差很多。三种规则的命中率在九成以上。", n)
        self.assertNotIn("最近改的段落", doc_edits.note([self.LINE]))           # 不给全文（改稿工人那边）：照旧只有片段
        self.assertNotIn("最近改的段落", doc_edits.note([self.LINE], text=PAPER))  # 全文里找不到改后的那截：不附

    def test_caps(self):
        long = "（1）规则法的命中率都很高，但判错率差很多。三种规则的命中率在九成以上。" + "另有补充说明。" * 300
        text = PAPER.replace("（1）规则法的命中率都很高。三种规则的命中率在九成以上。", long)
        n = doc_edits.note([self.LINE], text=text)
        block = n.split("现在整段是（论文全文里也是这样）：\n", 1)[1]
        self.assertLessEqual(len(block), doc_edits.FULL_MAX + 20)
        many = [dict(self.LINE, where=f"2.3 第（{i}）段") for i in range(5)]
        self.assertEqual(doc_edits.note(many, text=PAPER.replace("规则法的命中率都很高。", "规则法的命中率都很高，但判错率差很多。")
                                        ).count("〔"), 1)                    # 同一段只附一次，最多 FULL_N 段

    def test_prompt_rule(self):
        s = (ROOT / "prompts" / "fast_system.md").read_text(encoding="utf-8")
        self.assertIn("不许说“看不到你改的”", s)
        self.assertIn("一律写一句完整的问题", s)


# ---------------------------------------------------------------- 会议对象上跑
def _meeting():
    from zuhui import app
    from zuhui.config import load
    return app.Meeting(load(), dry=True, mute=True)


class FlowBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = Path(tempfile.mkdtemp(prefix="paper-1121c-"))
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
            zconfig.CURRENT_PAPER.unlink(missing_ok=True)
        else:
            zconfig.CURRENT_PAPER.write_bytes(cls._saved_cur)

    def setUp(self):
        m = self.m
        if m.meetings.current():
            m.meetings.end()
        self.paper_file.write_text(PAPER, encoding="utf-8")
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
        m.meetings.start(session=f"s-1121c-{time.time()}", title="组会")
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

    def add(self, title: str, status: str = "queued", ago: float = 0.0) -> dict:
        m = self.m
        t = m.board.add(remark=f"（原话）{title}", title=title, paper=m._paper_now())
        if status in ("working", "done"):
            m.board.claim(t["id"])
        if status == "done":
            m.board.finish(t["id"], result=f"{title}，改好了。")
        if ago:
            m.board.annotate(t["id"], created=(datetime.now() - timedelta(seconds=ago)).isoformat(timespec="seconds"))
        return m.board.get(t["id"])


class FoldTest(FlowBase):
    """1：快嘴嘴上说在改那条、没打标签：并进正在改 / 排队的那条，不另记。"""

    def test_promise_folds_into_running(self):
        m = self.m
        t = self.add(T_RUN, status="working", ago=12)
        m.hands.amend = mock.Mock(return_value=True)
        self.replies = [("#chat", SUPP_REPLY)]
        self.say(SUPP)
        m.hands.amend.assert_called_once()
        self.assertEqual(m.hands.amend.call_args[0][0], t["id"])
        self.assertIn("不用写答案", m.hands.amend.call_args[0][1])
        self.assertEqual(len(m.board.tasks()), 1)                            # 以前另记了一条"不用写答案，不用写答案"
        self.assertTrue(any("并进了" in h["content"] for h in m.fast.history))

    def test_promise_folds_into_queued_keeps_title(self):
        m = self.m
        t = self.add(T_RUN, ago=12)
        self.replies = [("#chat", SUPP_REPLY.replace("还在改", "还在排队"))]
        self.say(SUPP)
        tasks = m.board.tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], T_RUN)                           # 兜底拼的标题不顶掉原来的
        self.assertIn("又补充了：不用写答案", tasks[0]["remark"])
        self.assertNotIn("原来记的标题", tasks[0]["remark"])
        self.assertEqual(tasks[0]["id"], t["id"])

    def test_promise_without_running_task_gets_clean_title(self):
        m = self.m
        self.add(T_RUN, status="done", ago=60)
        self.replies = [("#chat", "明白，不写答案，只留问题，我去改。")]
        self.say(SUPP)
        titles = [x["title"] for x in m.board.tasks()]
        self.assertEqual(titles, [T_RUN, "不用写答案"])

    def test_cover_title_not_oral(self):
        m = self.m
        self.replies = [("#ask", STUTTER_REPLY)]
        self.say(STUTTER)
        titles = [x["title"] for x in m.board.tasks()]
        self.assertEqual(titles, ["2.3 总起句把表一删掉"])


class DeepTest(FlowBase):
    """2：只说"你再想想"：交过去的是一句完整的问题，带上 AI 上一句的提议，不是半截话。"""

    def test_think_again(self):
        self.replies = [("#ask", SHORT_REPLY)]
        self.say(SHORT_Q)
        self.say("你再想想。")
        (args, kw), = self.deep
        topic = kw["topic"]
        self.assertTrue(topic.startswith("2.3 第一段 后面是不是写的太少了，是不是应该稍微展开一点"), topic)
        self.assertIn("我前面提议补一句", topic)
        self.assertNotIn("就是你后面", topic)
        self.assertIn(topic, args[1])
        step = self.ai_lines()[-1]["steps"][0]
        self.assertTrue(step["v"].startswith("2.3 第一段"), step)

    def test_fast_deep_line_names_question(self):
        # 快嘴打 #deep 的那一行：逐字稿上补一步"深入思考 <问题>"（以前只有"对照"，界面上只剩"交给后台"）
        self.replies = [("#deep 2.3 第一段要不要展开、怎么展开", "好，我去看一下原文，半分钟左右。")]
        self.say("那你让后台查查吧")
        steps = self.ai_lines()[-1]["steps"]
        self.assertEqual([s["v"] for s in steps if s["k"] == "深入思考"], ["2.3 第一段要不要展开、怎么展开"])

    def test_deep_step_before_airing(self):
        from zuhui.ears import Utterance
        m = self.m
        line = m._speech_line("#deep", extra={"steps": [{"k": "对照", "v": "文档全文"}]})
        u = Utterance(n=1, text="二点三第一段要不要展开？", t="", shot=None, session="")
        m._deep_step(line, u)
        self.assertEqual(line.box["extra"]["steps"][-1]["k"], "深入思考")
        self.assertIn("要不要展开", line.box["extra"]["steps"][-1]["v"])
        line("好，我去看一下。")                                             # 开行时带上这一步
        self.assertEqual(self.ai_lines()[-1]["steps"][-1]["k"], "深入思考")


class HandEditTest(FlowBase):
    """3：手改过一段，快嘴说看不到：这句不念，转给后台去读改后的那段；每轮的上下文里附上改后整段。"""

    def hand_edit(self):
        m = self.m
        new = PAPER.replace("规则法的命中率都很高。", "规则法的命中率都很高，但判错率差很多。")
        self.paper_file.write_text(new, encoding="utf-8")
        m.meetings.add_line("system", "我 改了 2.3 第（1）段", tag=doc_edits.TAG,
                            extra={"by": "我", "where": "2.3 第（1）段", "section": "2.3 基线",
                                   "before": "…规则法的命中率都很高。三种…", "after": "…规则法的命中率都很高，但判错率差很多。三种…"})

    def test_context_has_full_paragraph(self):
        self.hand_edit()
        note = self.m._hand_edits_note()
        self.assertIn("〔2.3 第（1）段〕（1）规则法的命中率都很高，但判错率差很多。", note)

    def test_cant_see_goes_to_deep(self):
        self.hand_edit()
        self.replies = [("#chat", "好，你改了 2.3 第一段。", "我这边看不到你改成什么样，你念一下改后的句子。")]
        self.say("我改了一下这个2.3的第一段。")
        self.assertNotIn("看不到", self.ai_lines()[-1]["text"])
        self.assertEqual(len(self.deep), 1)
        self.assertIn("手改", self.deep[0][1].get("why") or "")

    def test_no_hand_edit_left_alone(self):
        self.replies = [("#chat", "好。", "我这边看不到你改成什么样。")]
        self.say("我改了一下这个2.3的第一段。")
        self.assertEqual(self.deep, [])                                     # 没有手改记录：不拦


if __name__ == "__main__":
    unittest.main()
