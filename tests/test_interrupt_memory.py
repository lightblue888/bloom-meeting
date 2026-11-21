# -*- coding: utf-8 -*-
"""AI 被打断后以为自己说完了（2026-11-21 晚上那场暴露的，句子都是编的，句式、长短和当时一样）：
1. 回话早生成完了、念到第一句就被打断：快嘴历史里还是整段，被叫"你继续说"时回"刚才那句我说完了"；
   现在嘴记着每句放了没有、放到几成（mouth.aired()），被打断时历史改成只含念出口的，后面写明没说出口的；
2. 回话念完"明白了，"就被停了：后面那句问话他没听到，他的"没有啊"不是在回答它；
3. 改稿汇报刚开头就被打断：他说"删吧"时快嘴两次 #silent，接着"你去删啊"又派了重复任务；
4. "继续，继续。""你继续说，我没有打断你。"：两分钟内、之后没再出声，直接从被打断那句念完，不过模型；
5. 逐字稿只在真开始念过的那一行标 cut；回话还在生成时被打断，只记念出口的。

全离线、不花钱：嘴是不起线程、不出声的真 Mouth（只用它的记账，"放"由测试一句一句推），快嘴换成假的生成器。
ZUHUI_HOME / BLURT_HOME 指到临时目录，论文是临时目录里的假论文；中继指到 https://127.0.0.1:1/。

    python -X utf8 -m unittest tests.test_interrupt_memory -v
"""
from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-cutmem-")     # 必须在 import zuhui 之前
    os.environ.setdefault("BLURT_HOME", tempfile.mkdtemp(prefix="blurt-test-cutmem-"))
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import dialog_rules as R  # noqa: E402
from zuhui import share  # noqa: E402

share.PUBLIC_RELAY = "https://127.0.0.1:1/"

from zuhui.fast import CUT_MARK, UNSAID_HEAD, FastBrain  # noqa: E402
from zuhui.mouth import Mouth, speakable  # noqa: E402

PAPER = """# 测试稿

## 3 方法

### 3.2 指标

甲指标衡量路径像不像真的，乙指标衡量回测结果准不准。

## 4 实验

### 4.1 结果

图 2 把两者放在一起，数值见表 4。两者不能互相替代。
"""

# 编的回话
LONG_1 = "你问的是 4.1 第一条那句。"
LONG_2 = "这里的差距指的是生成路径回测出的结果和参照回放差多少。"
LONG_3 = "要不要在第一次出现时补一句定义，你定。"
ASK_1 = "明白了，"
ASK_2 = "你是想知道论文凭什么从降了两成推到消不掉。"
ASK_3 = "要不要我这样记下？"
REP_1 = "4.1 开头那句图 2 把两者放在一起，"
REP_2 = "数值见表 4 我删掉了，现在只剩两个问句。"
REP_3 = "图 2 和表 4 后面还有引用，不影响。"


def bare_mouth() -> Mouth:
    """真 Mouth 的记账部分：不起合成、播放线程，不找 ffplay。测试用 play() 一句一句"放"。"""
    m = Mouth.__new__(Mouth)
    m._lock = threading.Lock()
    m._gen, m._pending, m._proc, m._stream_conn = 0, 0, None, None
    m._synth_q, m._play_q = queue.Queue(), queue.Queue()
    m._paused_at, m._paused_s, m._suspended, m._held_ref, m._rec = None, 0.0, None, None, None
    m._unpaused = threading.Event()
    m._unpaused.set()
    m.rate, m.played, m.backchannels = 1.0, [], []
    m.last_ack_text, m.last_ack_at = "", 0.0
    m._air_init()
    return m


def play(m: Mouth, n: int = 1, frac: float | None = None, dur: float = 2.0) -> None:
    """按顺序放排着的 n 句：on_start → 开始出声 → 放完。frac：最后一句只放到这几成（不放完，等着被打断）。"""
    m = getattr(m, "mouth", m)                              # 会议对象也行：放它的嘴
    for i in range(n):
        gen, spk, cb = m._synth_q.get_nowait()
        if cb is not None:
            cb(spk)
        r = m._air_begin(gen, spk, dur=dur)
        if frac is not None and i == n - 1:
            r["t0"] -= dur * frac + r["lead"]
            return
        m._air_end(r)
        with m._lock:
            m._pending -= 1


def drain(m: Mouth) -> list[str]:
    """排着的几句都放完，返回它们的读法（念"继续"、兜底那句时排上的就在这里）。"""
    m = getattr(m, "mouth", m)
    out = []
    while not m._synth_q.empty():
        out.append(m._synth_q.queue[0][1])
        play(m, 1)
    return out


class RulesTest(unittest.TestCase):
    def test_wants_resume(self):
        for yes in ("继续，继续。", "你你你你你继续说，我没有打断你。", "接着说吧", "你说完", "继续吧", "然后呢？",
                    "我没打断你。", "好的，继续。", "go on", "Continue, please."):
            self.assertTrue(R.wants_resume(yes), yes)
        for no in ("记下来，继续改。我没有打断你。", "继续看下一节", "我说完了。", "继续往下", "你说", "嗯。",
                   "我没有打断你，你去删啊。", "那你继续把三点二改完"):
            self.assertFalse(R.wants_resume(no), no)

    def test_bare_go_ahead(self):
        for yes in ("删了吧，删了吧。", "删吧，删吧。", "我没有打断你，你去删啊。", "那你改吧", "加上吧", "可以，改吧"):
            self.assertTrue(R.bare_go_ahead(yes), yes)
        for no in ("删掉第二句吧", "别删", "不删了", "继续改", "好的", "删的时候注意一下表四"):
            self.assertFalse(R.bare_go_ahead(no), no)


class MouthAirTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(Mouth, "_pref", staticmethod(lambda: "auto"))
        p.start()
        self.addCleanup(p.stop)

    def test_cut_mid_sentence(self):
        m = bare_mouth()
        key = lambda s: None  # noqa: E731
        m.say(LONG_1 + LONG_2 + LONG_3, on_start=key)
        self.assertTrue(m.speaking)
        play(m, 1)
        play(m, 1, frac=0.5)
        info = m.stop()
        self.assertIs(info["key"], key)
        self.assertEqual(info["said"], [LONG_1])
        self.assertEqual(info["part"], LONG_2)
        self.assertAlmostEqual(info["frac"], 0.5, delta=0.05)
        self.assertEqual(info["unsaid"], [LONG_2, LONG_3])
        self.assertTrue(info["heard"].startswith(LONG_1 + LONG_2[:8]), info["heard"])
        self.assertNotIn(LONG_3, info["heard"])
        self.assertTrue(info["aired"])
        self.assertFalse(m.speaking)
        self.assertFalse(m.aired(key)["live"])                 # 被打断以后不会再放

    def test_barely_started_sentence_not_heard(self):
        m = bare_mouth()
        key = lambda s: None  # noqa: E731
        for s in (ASK_1, ASK_2, ASK_3):                         # 快嘴一句一句交给嘴（"明白了，"单独一段）
            m.say(s, on_start=key)
        play(m, 1)
        play(m, 1, frac=0.05)
        info = m.stop()
        self.assertEqual(info["heard"], ASK_1)                 # 第二句才出了个声：不算听到
        self.assertEqual(info["unsaid"], [ASK_2, ASK_3])

    def test_nearly_done_counts_as_said(self):
        m = bare_mouth()
        key = lambda s: None  # noqa: E731
        m.say(ASK_1, on_start=key)
        m.say(ASK_2, on_start=key)
        play(m, 1)
        play(m, 1, frac=0.95)
        info = m.stop()
        self.assertEqual(info["unsaid"], [])                   # 最后一句放到九成五：都听到了

    def test_paused_time_not_counted(self):
        m = bare_mouth()
        key = lambda s: None  # noqa: E731
        m.say(LONG_1 + LONG_2, on_start=key)
        play(m, 1)
        play(m, 1, frac=0.5)                                    # 墙上过了一秒……
        m._paused_at = time.time() - 0.8                        # ……其中 0.8 秒是先停一下（pause）停着的
        info = m.stop()
        self.assertEqual(info["heard"], LONG_1)                # 真放出来的只有 0.2 秒：这句不算听到
        self.assertEqual(info["unsaid"], [LONG_2])

    def test_stop_reports_the_queued_reply_not_the_finished_one(self):
        m = bare_mouth()
        a, b = (lambda s: None), (lambda s: None)
        m.say(LONG_1, on_start=a)
        play(m, 1)                                              # 上一段念完了
        m.say(ASK_2, on_start=b)                                # 这一段还在合成，一个字没出声
        info = m.stop()
        self.assertIs(info["key"], b)
        self.assertFalse(info["aired"])
        self.assertEqual(info["heard"], "")
        self.assertEqual(info["unsaid"], [ASK_2])
        self.assertIsNone(m.stop())                             # 没在念：没有被打断的

    def test_aired_progress_and_skipped(self):
        m = bare_mouth()
        key = lambda s: None  # noqa: E731
        m.say(LONG_1 + LONG_2 + LONG_3, on_start=key)
        gen, spk, cb = m._synth_q.get_nowait()                  # 第一句合成失败，没放
        play(m, 1)                                              # 第二句照样放了
        info = m.aired()
        self.assertIs(info["key"], key)
        self.assertEqual(info["said"], [LONG_2])                # 没放的那句不算没说出口（后面的都念了）
        self.assertEqual(info["unsaid"], [LONG_3])
        self.assertTrue(info["live"])

    def test_speakable_is_matched(self):
        m = bare_mouth()
        key = lambda s: None  # noqa: E731
        m.say("第2段改好了。表 3 也改了。", on_start=key)
        gen, spk, cb = m._synth_q.get_nowait()
        self.assertEqual(spk, speakable("第2段改好了。"))       # 排队的是读法，记账认得回原文
        r = m._air_begin(gen, spk, dur=1.0)
        self.assertEqual(r["text"], "第2段改好了。")


class _FakeProc:
    """假 ffplay：放 secs 秒自己结束；被 kill 就马上结束。"""
    secs = 3.0

    def __init__(self, *a, **k):
        self._ev = threading.Event()

    def poll(self):
        return 0 if self._ev.is_set() else None

    def kill(self):
        self._ev.set()

    def wait(self, timeout=None):
        self._ev.wait(self.secs)
        self._ev.set()
        return 0


class MouthPlayTest(unittest.TestCase):
    """真的 _play 走一遍（ffplay 换成假进程）：开始放、被 stop 杀掉、自己放完，都记对。"""

    def setUp(self):
        p = mock.patch.object(Mouth, "_pref", staticmethod(lambda: "auto"))
        p.start()
        self.addCleanup(p.stop)
        import wave
        self.wav = Path(tempfile.mkdtemp(prefix="cutmem-wav-")) / "s.wav"
        with wave.open(str(self.wav), "wb") as w:              # 两秒静音
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\0\0" * 32000)

    def mouth(self) -> Mouth:
        m = bare_mouth()
        m.ffplay, m.ffmpeg = "ffplay", None
        return m

    def test_killed_mid_sentence(self):
        m = self.mouth()
        key = lambda s: None  # noqa: E731
        m.say(LONG_2 + LONG_3, on_start=key)
        gen, spk, cb = m._synth_q.get_nowait()
        with mock.patch("zuhui.mouth.subprocess.Popen", _FakeProc):
            th = threading.Thread(target=m._play, args=(self.wav, gen, cb, spk))
            th.start()
            time.sleep(1.2)                                     # 两秒的句子放了一秒（减去出声前那 0.2 秒）
            info = m.stop()
            th.join(3)
        self.assertFalse(th.is_alive())
        self.assertEqual(info["part"], LONG_2)
        self.assertAlmostEqual(info["frac"], 0.5, delta=0.12)
        self.assertEqual(info["unsaid"], [LONG_2, LONG_3])
        self.assertEqual(m.aired(key)["part"], LONG_2)         # 放音线程收尾后还是记成被打断那一刻

    def test_stream_killed_mid_sentence(self):
        from zuhui.mouth import _Stream
        m = self.mouth()
        key = lambda s: None  # noqa: E731
        m.say(LONG_2, on_start=key)
        gen, spk, cb = m._synth_q.get_nowait()
        st = _Stream(spk)
        st.sr = 16000
        st.put(b"\0\0" * 32000)                                 # 收完了：两秒
        st.finish()
        m._stream_sr = 16000

        class Pipe(_FakeProc):
            def __init__(self, *a, **k):
                super().__init__()
                self.stdin = mock.MagicMock()
        m._pipe_player = lambda sr: Pipe()
        th = threading.Thread(target=m._play_stream, args=(st, gen, cb, spk))
        th.start()
        time.sleep(1.0)
        info = m.stop()
        th.join(3)
        self.assertEqual(info["part"], LONG_2)
        self.assertAlmostEqual(info["frac"], 0.5, delta=0.12)  # 流式的按收到的秒数算

    def test_played_to_the_end(self):
        m = self.mouth()
        key = lambda s: None  # noqa: E731
        m.say(LONG_2, on_start=key)
        gen, spk, cb = m._synth_q.get_nowait()
        with mock.patch("zuhui.mouth.subprocess.Popen", type("P", (_FakeProc,), {"secs": 0.2})):
            m._play(self.wav, gen, cb, spk)
        info = m.aired(key)
        self.assertEqual(info["said"], [LONG_2])
        self.assertEqual(info["unsaid"], [])
        self.assertIsNone(m.stop())                             # 早放完了：没有被打断的


class MarkCutTest(unittest.TestCase):
    def brain(self) -> FastBrain:
        b = FastBrain.__new__(FastBrain)
        from collections import deque
        b.history = deque(maxlen=40)
        b.cfg = {}
        return b

    def test_rewrite_found(self):
        b = self.brain()
        b.remember("user", "回测偏差是什么意思")
        b.remember("assistant", "#ask\n" + LONG_1 + LONG_2 + LONG_3)
        with mock.patch.object(FastBrain, "is_cli", new_callable=mock.PropertyMock, return_value=False):
            ok = b.mark_cut(LONG_1, LONG_2 + LONG_3, full=LONG_1 + LONG_2 + LONG_3)
        self.assertTrue(ok)
        self.assertEqual(len(b.history), 2)
        c = b.history[-1]["content"]
        self.assertEqual(c, f"#ask\n{LONG_1}{CUT_MARK}\n{UNSAID_HEAD}{LONG_2}{LONG_3}）")
        self.assertEqual(b.recent_replies(1), [LONG_1])         # "最近说过的"只算念出口的

    def test_not_found_add_or_not(self):
        b = self.brain()
        b.remember("assistant", "#chat\n别的事。")
        with mock.patch.object(FastBrain, "is_cli", new_callable=mock.PropertyMock, return_value=False):
            self.assertFalse(b.mark_cut("", ASK_2, add=False))
            self.assertEqual(len(b.history), 1)
            self.assertFalse(b.mark_cut("", ASK_2, tag="#ask"))
        self.assertIn("一个字都没念出来", b.history[-1]["content"])
        self.assertEqual(b.recent_replies(3), ["别的事。"])

    def test_cli_correction_once(self):
        b = self.brain()
        b.remember("assistant", "#ask\n" + ASK_1 + ASK_2 + ASK_3)
        with mock.patch.object(FastBrain, "is_cli", new_callable=mock.PropertyMock, return_value=True):
            b.mark_cut(ASK_1, ASK_2 + ASK_3)
            b.mark_cut(ASK_1, ASK_2 + ASK_3)                    # 回话线程又记一遍：同一条，不再补更正
        self.assertEqual(len(b.history), 2)
        self.assertTrue(b.history[-1]["content"].startswith("（更正："))
        self.assertIn(ASK_2[:6], b.history[-1]["content"])

    def test_keeps_trailing_note(self):
        b = self.brain()
        b.remember("assistant", "#ask\n" + LONG_1 + LONG_2 + "\n（查到的完整结果在逐字稿上，对方要细说时照着说：……）")
        with mock.patch.object(FastBrain, "is_cli", new_callable=mock.PropertyMock, return_value=False):
            b.mark_cut(LONG_1, LONG_2)
        self.assertTrue(b.history[-1]["content"].endswith("（查到的完整结果在逐字稿上，对方要细说时照着说：……）"))


class FlowBase(unittest.TestCase):
    """会议对象（dry、不起耳朵和工人），嘴换成 bare_mouth()，快嘴换成假的：每一轮按 self.replies 依次回，
    像真的一样生成完才把这一轮记进历史。"""

    @classmethod
    def setUpClass(cls):
        d = Path(tempfile.mkdtemp(prefix="paper-cutmem-"))
        cls.paper_file = d / "测试稿.md"
        cls.paper_file.write_text(PAPER, encoding="utf-8")
        cls._saved_cur = zconfig.CURRENT_PAPER.read_bytes() if zconfig.CURRENT_PAPER.exists() else None
        zconfig.CURRENT_PAPER.write_text(json.dumps({"path": str(cls.paper_file), "name": "测试稿"}, ensure_ascii=False),
                                         encoding="utf-8")
        from zuhui import app
        cls.m = app.Meeting(zconfig.load(), dry=True, mute=True)

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
        p = mock.patch.object(Mouth, "_pref", staticmethod(lambda: "auto"))
        p.start()
        self.addCleanup(p.stop)
        m = self.m
        if m.meetings.current():
            m.meetings.end()
        m.board.reset()
        m.anchor.clear()
        m._p_wait, m._frag, m._prev_turn, m._turn, m._spec = [], None, None, None, None
        m._said, m._said_at, m._ended, m._mute = "", 0.0, 0.0, False
        m._cut = m._cut_report = m._last_cut = None
        m._aired_at, m._talk = 0.0, None
        m._last_reply = ""
        m.fast.history.clear()
        m.viewer = None
        m.pending_reports.clear()
        m.pending_answers.clear()
        m.mouth = bare_mouth()
        while not m.events.empty():
            m.events.get_nowait()
        m.meetings.start(session=f"s-cutmem-{time.time()}", title="组会")
        self.calls, self.replies, self.hooks = [], [], {}
        m.fast.reply = self._fake
        m.fast.grounder = lambda *a, **k: None
        m.fast.warm = lambda *a, **k: True
        m._check_missed = lambda *a, **k: None
        m._deep = lambda *a, **k: None
        m._deep_start = lambda *a, **k: {"lock": mock.MagicMock(), "state": "running"}
        cli = mock.patch.object(FastBrain, "is_cli", new_callable=mock.PropertyMock, return_value=False)
        cli.start()
        self.addCleanup(cli.stop)

    def _fake(self, said, light=False, remember=True):
        self.calls.append(said)
        n = len(self.calls)
        tag, *sents = self.replies.pop(0) if self.replies else ("#chat", "嗯。")
        yield ("tag", tag)
        if tag == "#silent":
            self.m.fast.history.append({"role": "user", "content": said})
            self.m.fast.history.append({"role": "assistant", "content": "#silent"})
            yield ("done", "")
            return
        for i, s in enumerate(sents):
            yield ("sentence", s)
            hook = self.hooks.get((n, i))
            if hook:
                hook()                                          # 念到这句之后：对方插话了
        body = "".join(sents)
        self.m.fast.history.append({"role": "user", "content": said})
        self.m.fast.history.append({"role": "assistant", "content": f"{tag}\n{body}"})
        yield ("done", body)

    def say(self, text: str, cancel: threading.Event | None = None):
        from zuhui.ears import Utterance
        m = self.m
        now = time.time()
        m.handle(Utterance(n=1, text=text, t="", shot=None, session="", heard=now, landed=now, sent=now, started=now),
                 cancel)

    def ai_lines(self) -> list[dict]:
        return [l for l in self.m.meetings.current_lines() if l["who"] == "student"]

    def last_ai(self) -> str:
        return next(h["content"] for h in reversed(self.m.fast.history) if h["role"] == "assistant")

    def add_done(self, title: str) -> dict:
        m = self.m
        t = m.board.add(remark=f"（原话）{title}", title=title, paper=m._paper_now())
        m.board.claim(t["id"])
        m.board.finish(t["id"], result=f"{title}，改好了。")
        return m.board.get(t["id"])


class CutHistoryTest(FlowBase):
    def test_finished_reply_cut_then_resume(self):
        """[18:09:58]：整段生成完了，念到第二句就被打断；"继续，继续。"从第二句开头念完，不过模型。"""
        m = self.m
        self.replies = [("#ask", LONG_1, LONG_2, LONG_3), ("#silent",)]
        self.say("4.1 第一条那句的差距到底是什么意思？")
        self.assertIn(LONG_3, self.last_ai())                   # 生成完：历史里先是整段
        play(m, 1)
        play(m, 1, frac=0.4)
        m._barge("测试：放音时你开口了")
        c = self.last_ai()
        self.assertTrue(c.startswith("#ask\n" + LONG_1), c)
        self.assertIn(CUT_MARK, c)
        self.assertIn(UNSAID_HEAD + LONG_2 + LONG_3, c)        # 没说出口的写明了，对方没听到
        line = self.ai_lines()[-1]
        self.assertTrue(line.get("cut"))
        self.assertEqual(m._last_reply, m._last_cut["heard"])  # 后面的问话没念出来：不算问过他
        self.say("别忘了看一下表四。")                             # 中间一句没接的话：不算接着说了别的
        n = len(self.calls)
        self.say("继续，继续。")
        self.assertEqual(len(self.calls), n)                    # 不过模型
        self.assertEqual(drain(m), [speakable(LONG_2), speakable(LONG_3)])
        self.assertTrue(m.fast.history[-2]["content"].endswith("继续，继续。"))
        self.assertEqual(m.fast.history[-1]["content"], "#ask\n" + LONG_2 + LONG_3)
        self.assertIsNone(m._cut)                               # 用掉了
        self.assertEqual(self.ai_lines()[-1]["text"], LONG_2 + LONG_3)

    def test_resume_variant_and_expiry(self):
        m = self.m
        self.replies = [("#ask", LONG_1, LONG_2), ("#chat", "好的。")]
        self.say("差距是什么意思？")
        play(m, 1, frac=0.5)
        m._barge("测试")
        m._cut["t"] -= 200                                      # 过了两分钟多
        self.say("你你你你你继续说，我没有打断你。")
        self.assertEqual(len(self.calls), 2)                    # 过期了：交给模型
        self.assertEqual(drain(m), [speakable("好的。")])

    def test_resume_not_after_other_reply(self):
        m = self.m
        self.replies = [("#ask", LONG_1, LONG_2), ("#chat", "表四在第四章。"), ("#chat", "嗯，你说。")]
        self.say("差距是什么意思？")
        play(m, 1, frac=0.5)
        m._barge("测试")
        self.say("表四在哪？")
        play(m, 1)                                              # 之后它又说了别的
        self.say("你接着说。")
        self.assertNotIn(speakable(LONG_2), drain(m))           # 不再把刚才没念完的那句翻出来

    def test_ask_cut_then_short_answer(self):
        """[17:44:45]：念完"明白了，"就被停了；后面那句问话他没听到，"没有啊"不是在回答它。"""
        m = self.m
        self.replies = [("#ask", ASK_1, ASK_2, ASK_3), ("#chat", "好，你说。")]
        self.say("我还是不知道为什么消不掉，你只是说了一个现象。")
        play(m, 1)
        play(m, 1, frac=0.05)
        m._barge("测试")
        self.assertEqual(m._last_reply, ASK_1)
        self.say("没有啊。")
        self.assertEqual(len(self.calls), 2)
        said = self.calls[-1]
        self.assertNotIn("你刚问了他", said)                    # 没念出来的问话：不当他在回答
        hist = [h["content"] for h in m.fast.history]
        self.assertTrue(any(h.startswith("#ask\n" + ASK_1 + CUT_MARK) and ASK_3 in h for h in hist), hist)

    def test_only_the_airing_line_is_marked(self):
        m = self.m
        self.replies = [("#chat", LONG_1, LONG_2), ("#ask", ASK_2)]
        self.say("第一句话。")
        play(m, 2)                                              # 上一轮念完了
        self.say("那下一个问题呢？")                               # 这一轮还在合成，一个字没出声
        m._barge("测试")
        lines = self.ai_lines()
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0].get("cut"))                   # 上一轮那行不标被打断
        self.assertIn(NONE_HEARD_SNIP, self.last_ai())

    def test_cut_while_generating(self):
        """回话还在生成时被打断：只记念出口的那截；"继续"交给模型接着说（它的话还没说完）。"""
        m = self.m
        cancel = threading.Event()
        m._cancel = cancel
        m._talk = threading.current_thread()                    # 回话线程就是这一轮

        def barge():
            play(m, 1, frac=0.5)
            m._barge("测试")
        self.hooks[(1, 0)] = barge
        self.replies = [("#ask", LONG_2, LONG_3), ("#chat", "接着说，后面是这样。")]
        self.say("差距是什么意思？", cancel)
        self.assertNotIn(LONG_3, self.ai_lines()[-1]["text"])
        c = self.last_ai()
        self.assertIn(CUT_MARK, c)
        self.assertIn(UNSAID_HEAD + LONG_2, c)
        self.assertNotIn(LONG_3, c)                             # 还没生成到、没送去念的不在里面
        self.assertEqual(sum(1 for h in m.fast.history if h["role"] == "user"), 1)
        m._talk = None
        m._cancel = threading.Event()
        self.say("继续。")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("接着往下说", self.calls[-1])


NONE_HEARD_SNIP = "一个字都没念出来"


class ReportCutTest(FlowBase):
    def report(self, title: str) -> dict:
        m = self.m
        t = self.add_done(title)
        self.replies.insert(0, ("#status", REP_1, REP_2, REP_3))
        m._report(t)
        return t

    def test_report_cut_then_go_ahead(self):
        """[18:04:46]：汇报刚开头就被打断；"删了吧"两次 #silent → 现在说已经删了，不再派任务。"""
        m = self.m
        t = self.report("4.1 开头删掉图 2 那句")
        play(m, 1, frac=0.6)
        m._barge("测试：听到你说话")
        c = self.last_ai()
        self.assertTrue(c.startswith("#status\n4.1 开头那句"), c)
        self.assertIn(UNSAID_HEAD, c)
        self.assertIn("我删掉了", c.split(CUT_MARK)[1])          # 删掉了这件事他没听到
        self.replies = [("#silent",), ("#silent",)]
        self.say("删了吧，删了吧。")
        self.assertEqual(len(m.board.tasks()), 1)
        self.assertEqual(m.board.get(t["id"])["status"], "done")
        self.assertIn("已经改好了", self.calls[-2])              # 快嘴知道那条做完了
        self.assertIn("别标 #silent", self.calls[-2])
        self.assertEqual(drain(m), [speakable("这条已经删了。")])   # 两次都不接：软件照实说一句
        self.assertEqual(self.last_ai(), "#chat\n这条已经删了。")
        # 又说"你去删啊"，快嘴打了 #edit：是同一条，不另派
        self.replies = [("#edit 删掉总起句里提图的半句话", "好，我把那半句删掉，排队去改。")]   # 标题和那条不像
        self.say("我没有打断你，你去删啊。")
        self.assertEqual(len(m.board.tasks()), 1)
        said = drain(m)
        self.assertTrue(any("已经改好了" in s for s in said), said)

    def test_report_waits_after_barge(self):
        """[18:04:45]：你一开口它停下，改稿汇报马上趁嘴空了开口。刚打断的几秒内不插播。"""
        m = self.m
        t = self.add_done("4.1 开头删掉图 2 那句")
        m.pending_reports[:] = [t]
        m.last_speech = 0.0
        started = []
        with mock.patch.object(m, "_start_talk", lambda fn, *a: started.append(fn)), \
                mock.patch.object(m, "_guests_talking", lambda *a: False):
            m._barge_t = time.time()
            m._flush_reports()
            self.assertEqual(started, [])
            m._barge_t = time.time() - 10
            m._flush_reports()
            self.assertEqual(len(started), 1)

    def test_report_not_aired_requeued(self):
        m = self.m
        t = self.report("4.1 开头删掉图 2 那句")
        m._barge("测试")                                        # 第一句还在门口等着，一个字没出声
        self.assertEqual([x["id"] for x in m.pending_reports], [t["id"]])
        self.assertIsNone(m._cut)
        self.assertFalse(any(l.get("cut") for l in self.ai_lines()))

    def test_report_resume(self):
        m = self.m
        self.report("4.1 开头删掉图 2 那句")
        play(m, 1, frac=0.6)
        m._barge("测试")
        n = len(self.calls)
        self.say("你接着说吧，我没打断你。")
        self.assertEqual(len(self.calls), n)
        # 从被打断的那一句开头念起（第一句只放了六成）
        self.assertEqual(drain(m), [speakable(REP_1 + REP_2), speakable(REP_3)])
        self.assertEqual(self.ai_lines()[-1]["tag"], "#report")


if __name__ == "__main__":
    unittest.main()
