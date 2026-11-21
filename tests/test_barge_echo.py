# -*- coding: utf-8 -*-
"""AI 说话时被莫名其妙打断：按模拟的时间线和识别结果重放几种情况（句子都是编的，不碰真实录音、论文和正在跑的 Bloom）。

1. 回声被识别成近音的别字（"四点三第二段讲的是"→"试点散第二单讲得"）：字面两字一组对不上，按顺序、按读音对得上，丢掉，不打断；
   声纹不像你的同样丢掉；
2. 转写晚到：你的话在 AI 开口前就说完了，识别慢、字在 AI 开口后才到——不截断 AI，压着，它说完再并成一句处理；
3. 你真插话：音量上听出你开口、叫停、声纹像你——照常马上打断；
4. 拿不准的（放音时录到、音量上没听出你、声纹和字都说不准）：不打断，压到 AI 说完；中途你真插话就并在一起；
5. 下一轮回话还在合成、一个字没出声时被撤掉：上一轮早念完的那行不标"被打断"；
6. 只有音量、没有文字：连续响的帧不够 ears.barge.frames 不打断，配置改得到；打断后几秒没收到字，日志记一笔；
7. 判停听出你插话时，"好的，我明白了"这种和 AI 常用接话重字的话不再当回声丢掉。

    python -X utf8 -m unittest tests.test_barge_echo -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-barge-")     # 必须在 import zuhui 之前
    os.environ.setdefault("BLURT_HOME", tempfile.mkdtemp(prefix="blurt-test-barge-"))
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import share  # noqa: E402

share.PUBLIC_RELAY = "https://127.0.0.1:1/"

from zuhui import app as zapp  # noqa: E402
from zuhui import ears as zears  # noqa: E402
from zuhui.ears import Utterance  # noqa: E402
from zuhui.mouth import Mouth  # noqa: E402

# 编的句子：AI 正在念的、回声识别成的别字、你在 AI 开口前说完的、你插话说的
AI_1 = "四点三第二段讲的是，"
AI_2 = "在九个样本、五百个窗口里，两百个在基准回放下偏低，旧方法却把其中三成算成偏高。"
ECHO_GARBLED = "试点散第二单讲得。"          # 两字一组只对上"第二"，按顺序对上"点""第""二""讲"
LATE_1 = "就好比你这是先总后分的那个那个结构，嗯，为什么我读着还是别扭呢？"
LATE_2 = "说不上来为什么。"
UNSURE = "这个地方还得再斟酌一下措辞。"
REAL = "这个数和表三对不上吧，你再看看。"
STOP = "不对不对，你先停一下。"


class FakeMouth:
    """只记"哪几段什么时候在放"：speaking = 还有句子没放完；played 和真嘴一样是 [开始, 结束或 None]。"""
    playing_overlap = Mouth.playing_overlap

    def __init__(self):
        self.played: list = []
        self.pending = 0
        self.last_ack_text, self.last_ack_at = "", 0.0
        self.stops = 0

    @property
    def speaking(self):
        return self.pending > 0

    @property
    def audible(self):
        return bool(self.played) and (self.played[-1][1] is None or time.time() - self.played[-1][1] < 0.4)

    def stop(self):
        self.stops += 1
        self.pending = 0
        if self.played and self.played[-1][1] is None:
            self.played[-1][1] = time.time()

    def finish(self):
        """这一轮念完了。"""
        self.pending = 0
        if self.played and self.played[-1][1] is None:
            self.played[-1][1] = time.time() - 1.0


class FakeMeetings:
    def __init__(self):
        self.partial, self.cuts = None, []

    def set_partial(self, p):
        self.partial = p

    def annotate(self, line, **kw):
        self.cuts.append((line, kw))

    def current(self):
        return {"id": "test"}


class FakeFast:
    def __init__(self):
        self.mem = []

    def remember(self, role, text):
        self.mem.append((role, text))


def make_meeting(cfg_barge: dict | None = None):
    """只装这几个判断用到的东西的会议对象（不起嘴、耳朵、工人、模型）。"""
    m = zapp.Meeting.__new__(zapp.Meeting)
    m.cfg = {"meeting": {"lang": "zh"}, "ears": {"barge": dict(zconfig.DEFAULTS["ears"]["barge"], **(cfg_barge or {}))}}
    m.mouth = FakeMouth()
    m.meetings = FakeMeetings()
    m.fast = FakeFast()
    m.bridge = None
    m.ears = None
    m._talk = None
    m._cancel = threading.Event()
    m._said, m._said_at, m._said_fixed = "", 0.0, ""
    m._barge_t = 0.0
    m.last_speech = 0.0
    m.logs = []
    m.log = lambda kind, **kw: m.logs.append(dict(kw, kind=kind))
    m.started = []
    m._start_talk = lambda fn, *args: m.started.append(args[0] if args else None)
    return m


def air(m, seconds_ago: float, *sentences: str):
    """AI 从 seconds_ago 秒前开始念这几句（第一句开头算），现在还在念。"""
    now = time.time()
    m.mouth.pending = 1
    m.mouth.played.append([now - seconds_ago, None])
    for i, s in enumerate(sentences):
        m._note_aired(s)
        m._aired_txt[-1] = (now - seconds_ago + 2.0 * i, m._aired_txt[-1][1])   # 第 i 句在开口 2i 秒后开始念
        m._said = (m._said + zapp._bare(s))[-400:]


def utt(text, ws, we, n=1):
    return Utterance(n=n, text=text, t="00:00", shot=None, session="", heard=we, started=ws, segs=[(ws, we)])


class EchoTest(unittest.TestCase):
    def test_garbled_echo_dropped_by_order_and_sound(self):
        m = make_meeting()
        air(m, 2.0, AI_1, AI_2)
        now = time.time()
        ws, we = now - 1.6, now - 0.6                     # AI 开口 0.4 秒后录到、一秒长
        # 两字一组的老比对认不出来（只对上"第二"）
        self.assertFalse(m._echo_segment(ws, we, ECHO_GARBLED))
        self.assertTrue(m._drop_segment(ws, we, ECHO_GARBLED))
        self.assertEqual(m.mouth.stops, 0)
        self.assertTrue(any(l["kind"] == "echo" and "对上" in l["why"] for l in m.logs), m.logs)

    def test_homophone_echo_dropped_by_pinyin(self):
        m = make_meeting()
        air(m, 2.0, AI_1, AI_2)
        sound = {"四": "si", "似": "si", "点": "dian", "典": "dian", "三": "san", "第": "di", "地": "di", "二": "er",
                 "段": "duan", "讲": "jiang", "奖": "jiang", "的": "de", "得": "de", "是": "shi"}
        m._pinyin = lambda: (lambda text: [sound.get(c, c) for c in text])   # 假的读音表（快识别进程不在）
        now = time.time()
        garbled = "似典三地二段奖得。"                     # 字面按顺序只对上"三""二""段"，读音全对上
        self.assertLess(m._lcs(zapp._bare(garbled), zapp._bare(AI_1)) / 8, 0.5)
        self.assertTrue(m._drop_segment(now - 1.6, now - 0.6, garbled))
        m2 = make_meeting()                              # 没有读音：判不了，压着不打断
        air(m2, 2.0, AI_1, AI_2)
        m2._pinyin = lambda: None
        self.assertFalse(m2._drop_segment(now - 1.6, now - 0.6, garbled))
        self.assertEqual(m2._seg_lookup(now - 1.6, now - 0.6), "unsure")

    def test_echo_dropped_by_voiceprint(self):
        m = make_meeting()
        air(m, 2.0, AI_1, AI_2)
        m._seg_voice = lambda ws, we: (0.08, 1.0)        # 声纹一点不像你
        now = time.time()
        self.assertTrue(m._drop_segment(now - 1.6, now - 0.6, "随便什么别的字也行。"))
        self.assertTrue(any("声纹" in l.get("why", "") for l in m.logs), m.logs)

    def test_voice_like_you_still_interrupts(self):
        m = make_meeting()
        air(m, 3.0, AI_1, AI_2)
        m._seg_voice = lambda ws, we: (0.52, 1.3)        # 声纹像你：照常打断
        now = time.time()
        ws, we = now - 1.5, now - 0.3
        self.assertFalse(m._drop_segment(ws, we, REAL))
        m.on_partial({"text": REAL, "t": "00:01", "n": 1, "ws": ws, "we": we})
        self.assertEqual(m.mouth.stops, 1)


class LateTest(unittest.TestCase):
    def test_late_transcript_held_then_merged(self):
        m = make_meeting()
        now = time.time()
        # 你 11 秒前到 2 秒前在说；AI 1 秒前才开口（回的是你前面那半句）；这两段字现在才到
        air(m, 1.0, "你说的是第二节那句，")
        s1, e1, s2, e2 = now - 11.0, now - 2.6, now - 2.6, now - 2.0
        self.assertFalse(m._drop_segment(s1, e1, LATE_1))
        self.assertEqual(m._seg_lookup(s1, e1), "stale")
        m.on_partial({"text": LATE_1, "t": "00:01", "n": 1, "ws": s1, "we": e1})
        self.assertEqual(m.mouth.stops, 0, "没人在说话，AI 被晚到的字打断了")
        self.assertEqual(m.meetings.partial["text"], LATE_1)          # 逐字稿照样先显示
        self.assertNotIn("ws", m.meetings.partial)
        self.assertTrue(m._hold_late(utt(LATE_1, s1, e1, 1)))
        m._drop_segment(s2, e2, LATE_2)
        self.assertTrue(m._hold_late(utt(LATE_2, s2, e2, 2)))
        m._release_held()
        self.assertEqual(m.started, [], "AI 还没说完就接着处理了")
        m.mouth.finish()
        m._release_held()
        self.assertEqual(len(m.started), 1, "两句晚到的要并成一句交给 AI（分开交第二句会把第一句的回话打断）")
        u = m.started[0]
        self.assertIn("别扭", u.text)
        self.assertIn("说不上来", u.text)
        self.assertAlmostEqual(u.started, s1)
        self.assertEqual(m.mouth.stops, 0)

    def test_stop_word_said_before_ai_started_still_stops(self):
        m = make_meeting()
        now = time.time()
        air(m, 1.0, "你说的是第二节那句，")
        self.assertFalse(m._hold_late(utt(STOP, now - 4.0, now - 2.5)))

    def test_pending_reply_not_aired_is_cut_as_before(self):
        m = make_meeting()
        now = time.time()
        m.mouth.played.append([now - 30.0, now - 25.0])  # 上一轮早念完了
        m.mouth.pending = 1                              # 这一轮还在合成，没出声
        self.assertIsNone(m._air_start())
        self.assertFalse(m._drop_segment(now - 2.0, now - 1.0, LATE_2))
        self.assertEqual(m._seg_lookup(now - 2.0, now - 1.0), "ok")
        self.assertFalse(m._hold_late(utt(LATE_2, now - 2.0, now - 1.0)))


class RealBargeTest(unittest.TestCase):
    def test_loudness_barge_text_interrupts(self):
        m = make_meeting()
        air(m, 5.0, AI_1, AI_2)
        now = time.time()
        ws, we = now - 1.8, now - 0.4
        m._barge_t = now - 1.5                           # 判停从音量上听出你插话了
        self.assertFalse(m._drop_segment(ws, we, REAL))
        self.assertEqual(m._seg_lookup(ws, we), "ok")
        m.on_partial({"text": REAL, "t": "00:01", "n": 1, "ws": ws, "we": we})
        self.assertEqual(m.mouth.stops, 1)
        self.assertFalse(m._hold_late(utt(REAL, ws, we)))

    def test_stop_words_interrupt_without_loudness(self):
        m = make_meeting()
        air(m, 5.0, AI_1, AI_2)
        now = time.time()
        ws, we = now - 1.5, now - 0.4
        self.assertFalse(m._drop_segment(ws, we, STOP))
        m.on_partial({"text": STOP, "t": "00:01", "n": 1, "ws": ws, "we": we})
        self.assertEqual(m.mouth.stops, 1)

    def test_long_speech_counts_as_you(self):
        m = make_meeting()
        air(m, 9.0, AI_1, AI_2)
        m._seg_voice = lambda ws, we: (None, 4.5)        # 没登记声纹，但录到 4.5 秒人声
        now = time.time()
        self.assertFalse(m._drop_segment(now - 6.0, now - 0.5, UNSURE * 3))
        self.assertEqual(m._seg_lookup(now - 6.0, now - 0.5), "ok")

    def test_barged_reply_words_not_dropped_as_echo(self):
        m = make_meeting()
        m._said_fixed = zapp._bare("恩，好的。好的好的。恩恩，收到。行，明白了。")   # AI 常用的接话
        air(m, 6.0, AI_1, AI_2)
        now = time.time()
        m._barge_t = now - 2.0
        text = "好的，我明白了，我明白了。"
        self.assertFalse(m._echo_segment(now - 1.8, now - 0.3, text))
        m._barge_t = 0.0                                 # 没听出你插话：照旧按回声丢
        self.assertTrue(m._echo_segment(now - 1.8, now - 0.3, text))


class UnsureTest(unittest.TestCase):
    def test_unsure_held_and_merged_with_real_barge(self):
        m = make_meeting()
        air(m, 4.0, AI_1, AI_2)
        now = time.time()
        ws, we = now - 1.6, now - 0.5
        self.assertFalse(m._drop_segment(ws, we, UNSURE))
        self.assertEqual(m._seg_lookup(ws, we), "unsure")
        m.on_partial({"text": UNSURE, "t": "00:01", "n": 1, "ws": ws, "we": we})
        self.assertEqual(m.mouth.stops, 0)
        self.assertTrue(m._hold_late(utt(UNSURE, ws, we, 1)))
        # 你接着真插话了：这句照常交出去，压着的并在前面
        merged = m._with_held(utt(REAL, now - 0.4, now, 2))
        self.assertTrue(merged.text.startswith("这个地方"), merged.text)
        self.assertIn("表三", merged.text)
        self.assertEqual(m._held, [])

    def test_hold_has_a_ceiling(self):
        m = make_meeting({"hold_max_s": 0.0})
        air(m, 4.0, AI_1, AI_2)
        now = time.time()
        m._drop_segment(now - 1.6, now - 0.5, UNSURE)
        self.assertTrue(m._hold_late(utt(UNSURE, now - 1.6, now - 0.5)))
        m._release_held()                                # AI 还在说，但压够了：照样交
        self.assertEqual(len(m.started), 1)


class CutLabelTest(unittest.TestCase):
    def test_cancelled_pending_reply_does_not_mark_old_line(self):
        m = make_meeting()
        old_talk, new_talk = threading.Thread(target=lambda: None), threading.Thread(target=lambda: None)
        now = time.time()
        m.mouth.played.append([now - 90.0, now - 85.0])  # 上一轮那行 85 秒前就念完了
        m._speaking_line, m._speaking_talk = 7, old_talk
        m._talk = new_talk                               # 这一轮的回话还在合成
        m.mouth.pending = 1
        m._interrupt()
        self.assertEqual(m.meetings.cuts, [])

    def test_airing_line_is_marked(self):
        m = make_meeting()
        talk = threading.Thread(target=lambda: None)
        m.mouth.played.append([time.time() - 2.0, None])
        m.mouth.pending = 1
        m._speaking_line, m._speaking_talk, m._talk = 8, talk, talk
        m._interrupt()
        self.assertEqual(m.meetings.cuts, [(8, {"cut": True})])

    def test_between_sentences_of_same_turn_is_marked(self):
        m = make_meeting()
        talk = threading.Thread(target=lambda: None)
        m.mouth.played.append([time.time() - 5.0, time.time() - 2.0])   # 第一句念完，第二句还在合成
        m.mouth.pending = 1
        m._speaking_line, m._speaking_talk, m._talk = 9, talk, talk
        m._interrupt()
        self.assertEqual(m.meetings.cuts, [(9, {"cut": True})])

    def test_barge_without_words_is_logged(self):
        m = make_meeting()
        m._barge_check = time.time() - 7.0
        m._check_barge_heard()
        self.assertTrue(any(l["kind"] == "barge" and l.get("heard") is False for l in m.logs))
        m.logs.clear()
        m._barge_check = time.time() - 7.0
        m.on_partial({"text": REAL, "t": "00:01", "n": 1})   # 收到字了
        m._check_barge_heard()
        self.assertFalse(any(l.get("heard") is False for l in m.logs))


class VadBargeTest(unittest.TestCase):
    """只有音量（Silero 换成能量门限）：放音时一阵回声 7 帧响，不到 8 帧不打断；你开口 12 帧响，打断；帧数能配。"""

    def _vad(self, cfg=None):
        d = Path(tempfile.mkdtemp(prefix="barge_vad_"))
        (d / "live").mkdir()
        v = zears._AudioVAD(d, muted=lambda: True)
        v._sess = None
        v.echo_gate = False
        v.set_barge(cfg)
        fired = []
        v.on_barge = lambda why: fired.append(why)
        return v, d, fired

    @staticmethod
    def _frames(*runs):
        out = []
        rng = np.random.default_rng(3)
        for n, amp in runs:
            for _ in range(n):
                out.append((rng.normal(0, amp, 512)).astype(np.float32))
        return (np.clip(np.concatenate(out) * 32768, -32768, 32767).astype(np.int16)).tobytes()

    def test_short_echo_burst_does_not_barge_but_voice_does(self):
        v, d, fired = self._vad()
        (d / "live" / "audio.pcm").write_bytes(self._frames((40, 0.001), (7, 0.03), (20, 0.001)))
        v.pump()
        self.assertEqual(fired, [])
        with open(d / "live" / "audio.pcm", "ab") as f:
            f.write(self._frames((12, 0.06), (10, 0.001)))
        v.pump()
        self.assertEqual(len(fired), 1)

    def test_frames_configurable(self):
        v, d, fired = self._vad({"frames": 14, "floor": 0.02, "ratio": 2.5})
        self.assertEqual(v.BARGE_FRAMES, 14)
        (d / "live" / "audio.pcm").write_bytes(self._frames((40, 0.001), (12, 0.06), (20, 0.001)))
        v.pump()
        self.assertEqual(fired, [], "配成 14 帧，12 帧响不该打断")
        v2, _, _ = self._vad({"frames": "x"})          # 配错了：照旧默认
        self.assertEqual(v2.BARGE_FRAMES, 8)

    def test_wall_and_audio_time_round_trip(self):
        v, _, _ = self._vad()
        v._offs.extend([100.25, 100.4, 100.3])
        self.assertAlmostEqual(v.audio_t(v.wall(12.5)), 12.5)


class CollectorTest(unittest.TestCase):
    """耳朵把每段的墙上起止带给 app：半句（partial）带最新一段的，整句（Utterance.segs）带每一段的。"""

    def test_partial_and_utterance_carry_wall_times(self):
        class V:
            knows, audio_now, pump_wall = True, 20.0, 1020.0

            @staticmethod
            def wall(t):
                return t + 1000.0

        parts, outs = [], []
        c = zears._Collector(Path(tempfile.mkdtemp()), 2.0, 2, False, emit=outs.append, partial=parts.append,
                             drop=lambda ws, we, t: False)
        c.vad, c.voice, c.echo_gate = V(), None, False
        c.add({"start": 10.0, "end": 12.0, "text": "第一段说的话"})
        c.add({"start": 12.5, "end": 14.0, "text": "第二段接着说"})
        self.assertEqual((parts[-1]["ws"], parts[-1]["we"]), (1012.5, 1014.0))
        c.flush()
        self.assertEqual(outs[0].segs, [(1010.0, 1012.0), (1012.5, 1014.0)])
        c.add({"start": 15.0, "end": 16.0, "text": "下一句话"})
        c.flush()
        self.assertEqual(outs[1].segs, [(1015.0, 1016.0)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
