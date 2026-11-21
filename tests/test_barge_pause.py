# -*- coding: utf-8 -*-
"""AI 说话时听到人声：先停一下（放音进程挂起，不杀），等这段转写回来再定。全离线：句子都是编的，
放音用假播放器（一个按实时走的小进程），不碰真实录音、论文、配置和正在跑的 Bloom。

1. 嘴：pause() 把正在放的进程挂起、resume() 从停的地方接着放；停着时 speaking 照旧、audible 变假；
   回声参考的时间轴扣掉停的那几秒（整句文件、边合成边播两种）；停着时 stop() 照样杀得掉；句与句之间停下，下一句等恢复；
2. 会议：附和接着说、这句压到 AI 说完再当回答交出去；回声接着说；没听到字接着说；
   正事、叫停、点名、一直说个不停才停；按文字打断那条路也一样；嘴停不住、回话还没出声、配置关了时照旧马上停；
3. 耳朵：停一下以后接着放，还能再听出你打断；AI 静了一阵刚开口、头几个字的回声漏进来，按起声对齐认出来，不算你打断；
4. 语音桥：推给访客的那一路跟着停，访客那边排着没放的先清掉，恢复后补推。

    python -X utf8 -m unittest tests.test_barge_pause -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from collections import deque
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-pause-")     # 必须在 import zuhui 之前
    os.environ.setdefault("BLURT_HOME", tempfile.mkdtemp(prefix="blurt-test-pause-"))
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import share  # noqa: E402

share.PUBLIC_RELAY = "https://127.0.0.1:1/"

from zuhui import app as zapp  # noqa: E402
from zuhui import bridge as zbridge  # noqa: E402
from zuhui import ears as zears  # noqa: E402
from zuhui import mouth as zmouth  # noqa: E402
from zuhui.ears import PLAYBACK, REF_ON_DB, Utterance  # noqa: E402

# 编的句子
AI_LINE = "你的意思是，第三节后面这几段只是展开说明，对吧？"
AI_ECHO = "你的意思是吧？"                       # 回声被识别成的（AI 自己那句的开头）
ACK = "删了吧，删了吧。"
POINT = "不是不是，这一段的结论下得太早了。"
REAL_POPEN = subprocess.Popen

# 假播放器：每 tick 秒走一步，把走到第几步写进文件；挂起时不走，恢复后接着走完
PLAYER = ("import sys,time\nn=int(sys.argv[2])\nfor i in range(n):\n"
          "    open(sys.argv[1],'w').write(str(i+1))\n    time.sleep(float(sys.argv[3]))\n")
# 假管道播放器：按 16 kHz 单声道的实时速度从标准输入读；像 ffplay 一样一口先读一截（0.25 秒）再放，写的那边会领先一点
PIPE_PLAYER = ("import sys,time\nwhile True:\n    b=sys.stdin.buffer.read(8000)\n    if not b:\n        break\n"
               "    time.sleep(len(b)/32000)\n")


def _wait(cond, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def _noise(secs, amp=0.1, seed=0, sr=16000):
    rng = np.random.default_rng(seed)
    return np.clip(rng.normal(0, amp, int(secs * sr)) * 32768, -32768, 32767).astype(np.int16)


def _wav(path: Path, secs=1.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(_noise(secs).tobytes())
    return path


def make_mouth():
    """真的嘴（不起配音服务、不找 ffplay）：放音走 _play / _play_stream，进程换成假播放器。"""
    with mock.patch("zuhui.components.which", return_value=None), mock.patch("zuhui.components.edge_tts", return_value=None):
        m = zmouth.Mouth({"mouth": {"rate": 1.0, "stream": False}})
    m.ffplay = "ffplay"                  # 只为让 _play 拼 ffplay 那条命令；真起的是假播放器
    return m


class _Players:
    """顶替 subprocess.Popen：不管命令是什么，起一个假播放器。"""

    def __init__(self, ticks=25, tick=0.04):
        self.procs, self.ticks, self.tick = [], ticks, tick
        self.prog = Path(tempfile.mkdtemp(prefix="pause_prog_"))

    def __call__(self, cmd, *a, **kw):
        f = self.prog / f"p{len(self.procs)}.txt"
        p = REAL_POPEN([sys.executable, "-c", PLAYER, str(f), str(self.ticks), str(self.tick)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.prog = f
        self.procs.append(p)
        return p

    @staticmethod
    def step(p) -> int:
        try:
            return int(p.prog.read_text() or 0)
        except (OSError, ValueError):
            return 0


def _enqueue(m, item, said=None):
    with m._lock:
        gen = m._gen
        m._pending += 1
    m._play_q.put((gen, item, False, None, said))


class MouthPauseTest(unittest.TestCase):
    def setUp(self):
        PLAYBACK.clear()
        self.dir = Path(tempfile.mkdtemp(prefix="pause_mouth_"))

    def tearDown(self):
        PLAYBACK.clear()

    def test_pause_freezes_player_and_resume_continues(self):
        m, players = make_mouth(), _Players(ticks=25, tick=0.04)         # 假播放器放 1 秒
        wav = _wav(self.dir / "a.wav", 1.0)
        with mock.patch.object(zmouth.subprocess, "Popen", players):
            _enqueue(m, wav)
            self.assertTrue(_wait(lambda: players.procs and players.step(players.procs[0]) >= 3))
            p = players.procs[0]
            t_start = m.played[-1][0]
            self.assertTrue(m.audible)
            t_p = time.time()
            self.assertTrue(m.pause())
            self.assertTrue(m.paused)
            self.assertTrue(m.speaking, "停一下不是说完了：这一轮还在")
            n0 = players.step(p)
            time.sleep(0.7)
            self.assertLessEqual(players.step(p) - n0, 1, "挂起了，假播放器不该再往前走")
            self.assertFalse(m.audible, "停下 0.4 秒以后不算在出声")
            self.assertAlmostEqual(m.played[-1][1], t_p, delta=0.05)
            self.assertLessEqual(PLAYBACK.level(t_p + 0.05, time.time(), kinds=("tts",)), REF_ON_DB,
                                 "停着的这段在回声参考里不算在放")
            dt = m.resume()
            t_r = time.time()
            self.assertGreaterEqual(dt, 0.65)
            self.assertFalse(m.paused)
            self.assertTrue(m.audible)
            self.assertGreater(PLAYBACK.level(t_r, t_r + 0.3, kinds=("tts",)), REF_ON_DB, "拿下来的那截挪到恢复以后")
            self.assertTrue(_wait(lambda: not m.speaking, 5.0))
            t_end = time.time()
        self.assertGreaterEqual(t_end - t_start, 1.0 + 0.65 - 0.1, "停着的时间不算放音：总时长 = 1 秒 + 停的")
        self.assertEqual(p.wait(2), 0, "放完自己退出，不是被杀")
        self.assertAlmostEqual(m.played[-1][0], t_r, delta=0.05)      # 接着放的那截另起一行
        self.assertIsNotNone(m.played[-1][1])
        self.assertAlmostEqual(m.paused_seconds, dt, delta=0.01)

    def test_stop_while_paused_kills_player(self):
        m, players = make_mouth(), _Players(ticks=50, tick=0.04)
        wav = _wav(self.dir / "b.wav", 2.0)
        with mock.patch.object(zmouth.subprocess, "Popen", players):
            _enqueue(m, wav)
            self.assertTrue(_wait(lambda: players.procs and players.step(players.procs[0]) >= 2))
            self.assertTrue(m.pause())
            t_p = time.time()
            time.sleep(0.2)
            m.stop()
            self.assertFalse(m.paused)
            self.assertFalse(m.speaking)
            p = players.procs[0]
            self.assertNotEqual(p.wait(3), 0, "挂起的播放器照样杀掉")
            self.assertTrue(_wait(lambda: m._proc is None and m._rec is None, 3.0))
        self.assertAlmostEqual(m.played[-1][1], t_p, delta=0.05, msg="played 记到停下那一刻，不是杀掉那一刻")

    def test_pause_between_sentences_holds_next(self):
        m, players = make_mouth(), _Players(ticks=5, tick=0.04)
        wav = _wav(self.dir / "c.wav", 0.2)
        with mock.patch.object(zmouth.subprocess, "Popen", players):
            with m._lock:
                m._pending += 1                      # 这一轮前一句还在合成：没在出声
            self.assertTrue(m.pause())
            self.assertFalse(m.audible)
            _enqueue(m, wav)                         # 下一句排上了
            time.sleep(0.5)
            self.assertEqual(players.procs, [], "停着：下一句先不放")
            m.resume()
            self.assertTrue(_wait(lambda: len(players.procs) == 1, 2.0), "恢复后接着放")
            self.assertTrue(_wait(lambda: m._pending == 1, 3.0))
            with m._lock:
                m._pending -= 1

    def test_stream_reference_timeline_skips_pause(self):
        m = make_mouth()
        m._stream_sr = 16000
        procs = []

        def pipe_player(sr):
            p = REAL_POPEN([sys.executable, "-c", PIPE_PLAYER], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, bufsize=0)
            procs.append(p)
            return p
        m._pipe_player = pipe_player
        st = zmouth._Stream("编的一句话，边合成边播。")
        st.sr = 16000
        pcm = _noise(1.2, seed=5).tobytes()
        for i in range(0, len(pcm), 3200):
            st.put(pcm[i:i + 3200])
        st.finish()
        _enqueue(m, st)
        self.assertTrue(_wait(lambda: m._proc is not None and m.played))
        t_first = m.played[-1][0]
        time.sleep(0.4)
        t_p = time.time()
        self.assertTrue(m.pause())
        time.sleep(0.8)
        self.assertLessEqual(PLAYBACK.level(t_p + 0.05, time.time() - 0.05, kinds=("tts",)), REF_ON_DB,
                             "停着时没有新的一块记成在放")
        dt = m.resume()
        t_r = time.time()
        time.sleep(0.35)                                 # 写的那边接着写几块
        self.assertGreater(PLAYBACK.level(t_r + 0.02, t_r + 0.25, kinds=("tts",)), REF_ON_DB, "恢复以后接着记成在放")
        self.assertTrue(_wait(lambda: not m.speaking, 5.0))
        tr = PLAYBACK._tracks["tts"]
        ref_end = tr["t0"] + tr["end"] / 16000.0
        # 1.2 秒的声音 + 停的那一下：回声参考一直排到这时候（没扣掉停的话，后半截会早 dt 秒、被 cut 掉一截）
        self.assertGreaterEqual(ref_end - t_first, 1.2 + dt - 0.3)
        self.assertEqual(procs[0].wait(2), 0)


# ---------------------------------------------------------------- 会议：停一下以后怎么定

class FakeMouth:
    """只记"哪几段什么时候在放"、停了几次。can_pause=False：停不住（老的嘴、没有播放进程）。"""
    playing_overlap = zmouth.Mouth.playing_overlap

    def __init__(self, can_pause=True):
        self.played: list = []
        self.pending = 0
        self.last_ack_text, self.last_ack_at = "", 0.0
        self.stops = self.pauses = self.resumes = 0
        self.paused_at = None
        self.can_pause = can_pause

    @property
    def speaking(self):
        return self.pending > 0

    @property
    def paused(self):
        return self.paused_at is not None

    @property
    def audible(self):
        if not self.played:
            return False
        e = self.played[-1][1]
        return (e is None and self.paused_at is None) or (e is not None and time.time() - e < 0.4)

    def pause(self):
        if not self.can_pause or not self.speaking:
            return False
        self.pauses += 1
        self.paused_at = time.time()
        if self.played and self.played[-1][1] is None:
            self.played[-1][1] = self.paused_at
        return True

    def resume(self):
        if self.paused_at is None:
            return 0.0
        dt = time.time() - self.paused_at
        self.paused_at = None
        self.resumes += 1
        self.played.append([time.time(), None])
        return dt

    def stop(self):
        self.stops += 1
        self.pending = 0
        self.paused_at = None
        if self.played and self.played[-1][1] is None:
            self.played[-1][1] = time.time()

    def finish(self):
        self.pending = 0
        if self.played and self.played[-1][1] is None:
            self.played[-1][1] = time.time() - 1.0


class FakeVad:
    knows = True

    def __init__(self):
        self.last_voice, self.audio_now, self.pump_wall = -1.0, 0.0, time.time()

    def talk(self, ago=0.0):
        """你 ago 秒前最后出声。"""
        self.pump_wall, self.audio_now, self.last_voice = time.time(), 100.0, 100.0 - ago

    def mic_off(self):
        return False

    def audio_t(self, w):
        return w

    def raw_voiced(self, *a, **kw):
        return None


class FakeEars:
    def __init__(self):
        self.vad = FakeVad()

    def user_talking(self, window=0.5):
        v = self.vad
        return v.last_voice >= 0 and (v.audio_now - v.last_voice) + (time.time() - v.pump_wall) < window


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


def make_meeting(cfg_barge: dict | None = None, can_pause=True):
    m = zapp.Meeting.__new__(zapp.Meeting)
    m.cfg = {"meeting": {"lang": "zh"}, "ears": {"barge": dict(zconfig.DEFAULTS["ears"]["barge"], **(cfg_barge or {}))}}
    m.mouth = FakeMouth(can_pause)
    m.meetings = FakeMeetings()
    m.fast = FakeFast()
    m.bridge = None
    m.ears = FakeEars()
    m._talk = None
    m._cancel = threading.Event()
    m._said, m._said_at, m._said_fixed = "", 0.0, ""
    m._barge_t = 0.0
    m._listen = None
    m._held = []
    m.last_speech = 0.0
    m.logs = []
    m.log = lambda kind, **kw: m.logs.append(dict(kw, kind=kind))
    m.started = []
    m._start_talk = lambda fn, *args: m.started.append(args[0] if args else None)
    return m


def air(m, seconds_ago: float, sentence: str = AI_LINE):
    """AI 从 seconds_ago 秒前开始念这句，现在还在念。"""
    m.mouth.pending = 1
    m.mouth.played.append([time.time() - seconds_ago, None])
    m._note_aired(sentence)
    m._said = (m._said + zapp._bare(sentence))[-400:]


def utt(text, ws, we, n=1):
    return Utterance(n=n, text=text, t="00:00", shot=None, session="", heard=we, started=ws, segs=[(ws, we)])


def barges(m):
    return [l["text"] for l in m.logs if l["kind"] == "barge"]


class ListenTest(unittest.TestCase):
    def test_ack_carries_on_and_is_answered_after(self):
        m = make_meeting()
        air(m, 3.0)
        m.ears.vad.talk(0.0)
        m._barge("放音时听到人声（音量 0.050）")
        self.assertEqual((m.mouth.pauses, m.mouth.stops), (1, 0))
        self.assertIsNotNone(m._listen)
        self.assertTrue(any(t.startswith("停一下听：放音时听到人声") for t in barges(m)), m.logs)
        m._barge("放音时听到人声（音量 0.060）")                      # 停着时又响了一下：不重复停
        self.assertEqual(m.mouth.pauses, 1)
        # 你说完了，0.8 秒后这段转写回来
        m.ears.vad.talk(0.8)
        now = time.time()
        ws, we = now - 1.3, now - 0.8
        self.assertIsNot(m._drop_segment(ws, we, ACK), True)
        self.assertEqual((m.mouth.resumes, m.mouth.stops), (1, 0))
        self.assertIsNone(m._listen)
        self.assertTrue(any(t.startswith("是附和（删了吧") and t.endswith("接着说") for t in barges(m)), m.logs)
        m.on_partial({"text": ACK, "t": "00:01", "n": 1, "ws": ws, "we": we})
        self.assertEqual(m.mouth.stops, 0, "附和那段按字也不能把它截断")
        self.assertEqual(m.meetings.partial["text"], ACK)            # 逐字稿照样先显示
        u = utt(ACK, ws, we)
        m._listen_tick(u)
        self.assertTrue(m._hold_late(u))
        self.assertEqual(m._held[-1][1], "ack")
        m._release_held()
        self.assertEqual(m.started, [], "AI 还没说完：先压着")
        m.mouth.finish()
        m.ears.vad.talk(5.0)
        m._release_held()
        self.assertEqual(len(m.started), 1, "AI 说完了，把这句当回答交出去")
        self.assertIn("删了吧", m.started[0].text)
        self.assertEqual(m.mouth.stops, 0)

    def test_ack_is_held_past_hold_ceiling(self):
        m = make_meeting({"hold_max_s": 0.0})
        air(m, 3.0)
        now = time.time()
        u = utt("好好好。", now - 1.0, now - 0.6)
        self.assertTrue(m._hold_late(u))
        m._release_held()
        self.assertEqual(m.started, [], "只压着附和：压多久都不为它截断 AI")

    def test_echo_carries_on(self):
        m = make_meeting()
        air(m, 0.6)
        m._barge("放音时听到人声（音量 0.040）")
        m.ears.vad.talk(0.6)                                          # 停下以后就没声了
        now = time.time()
        self.assertIs(m._drop_segment(now - 0.9, now - 0.3, AI_ECHO), True)
        self.assertEqual((m.mouth.resumes, m.mouth.stops), (1, 0))
        self.assertIn("是回声，接着说", barges(m))

    def test_echo_seen_by_ears_carries_on(self):
        m = make_meeting()
        air(m, 0.6)
        m._barge("放音时听到人声（音量 0.040）")
        m.ears.vad.talk(0.6)
        m._listen["echo"] = True                                      # 耳朵按花纹认出是回声（主循环收到那行 echo 日志）
        m._listen_tick()
        self.assertEqual((m.mouth.resumes, m.mouth.stops), (1, 0))

    def test_no_words_carries_on_after_quiet(self):
        m = make_meeting({"listen_quiet_s": 0.3})
        air(m, 2.0)
        m._barge("放音时听到人声（音量 0.030）")
        m.ears.vad.talk(0.5)
        m._check_barge_heard()
        self.assertEqual(m.mouth.resumes, 0, "刚停下：还在等字")
        time.sleep(0.35)
        m._check_barge_heard()
        self.assertEqual((m.mouth.resumes, m.mouth.stops), (1, 0))
        self.assertIn("没听到话，接着说", barges(m))
        self.assertFalse(any(l.get("heard") is False for l in m.logs), "停一下的不再记'6 秒没收到你的话'")

    def test_point_stops(self):
        m = make_meeting()
        air(m, 3.0)
        m._barge("放音时听到人声（音量 0.080）")
        m.ears.vad.talk(0.8)
        now = time.time()
        ws, we = now - 2.0, now - 0.8
        self.assertIsNot(m._drop_segment(ws, we, POINT), True)
        self.assertEqual((m.mouth.stops, m.mouth.resumes), (1, 0))
        self.assertTrue(any(t.startswith("在说正事（不是不是") and t.endswith("停下") for t in barges(m)), m.logs)
        self.assertFalse(m._hold_late(utt(POINT, ws, we)), "停下了：这句照常交给 AI")

    def test_stop_words_and_name_stop(self):
        for text in ("等一下。", "Claude，你先看第二段。", "停。"):
            m = make_meeting()
            air(m, 3.0)
            m._barge("放音时听到人声（音量 0.050）")
            m.ears.vad.talk(0.8)
            now = time.time()
            m._drop_segment(now - 1.2, now - 0.8, text)
            self.assertEqual(m.mouth.stops, 1, text)

    def test_keeps_talking_counts_as_point(self):
        m = make_meeting({"listen_max_s": 0.3})
        air(m, 3.0)
        m._barge("放音时听到人声（音量 0.070）")
        time.sleep(0.35)
        m.ears.vad.talk(0.0)                                          # 一直在说，字还没回来
        m._check_barge_heard()
        self.assertEqual(m.mouth.stops, 1)
        self.assertIn("说了好一会儿还没说完，停下", barges(m))

    def test_ack_then_more_words_stops(self):
        m = make_meeting()
        air(m, 4.0)
        m._barge("放音时听到人声（音量 0.050）")
        m.ears.vad.talk(0.0)                                          # "好好"之后还在说
        now = time.time()
        m._drop_segment(now - 1.5, now - 1.0, "好好。")
        self.assertEqual((m.mouth.resumes, m.mouth.stops), (0, 0), "附和完还在说：等后面那段")
        m.ears.vad.talk(0.7)
        m._drop_segment(now - 0.9, now - 0.1, "那第三节的表也得重画。")
        self.assertEqual(m.mouth.stops, 1)

    def test_utterance_decides_when_segments_missing(self):
        m = make_meeting()
        air(m, 3.0)
        m._barge("放音时听到人声（音量 0.050）")
        m.ears.vad.talk(1.0)
        now = time.time()
        m._listen_tick(utt("明白了。", now - 1.5, now - 1.0))
        self.assertEqual((m.mouth.resumes, m.mouth.stops), (1, 0))

    def test_stopped_elsewhere_clears_listen(self):
        m = make_meeting()
        air(m, 3.0)
        m._barge("放音时听到人声（音量 0.050）")
        m.mouth.stop()                                                # 访客插话、散会……别处叫停了
        m._listen_tick()
        self.assertIsNone(m._listen)
        self.assertEqual(m.mouth.resumes, 0)

    def test_text_path_ack_does_not_stop(self):
        m = make_meeting()
        air(m, 6.0)
        m._seg_voice = lambda ws, we: (0.6, 1.2)                     # 声纹像你：这段算你的话
        now = time.time()
        ws, we = now - 1.5, now - 0.4
        self.assertIsNot(m._drop_segment(ws, we, ACK), True)
        m.on_partial({"text": ACK, "t": "00:01", "n": 1, "ws": ws, "we": we})
        self.assertEqual(m.mouth.stops, 0, "按字判也一样：附和不停")
        self.assertTrue(any(l["kind"] == "barge" and l.get("by") == "text" and l.get("verdict") == "ack" for l in m.logs))
        self.assertTrue(m._hold_late(utt(ACK, ws, we)))
        # 同样的路，说的是正事：照常停
        m2 = make_meeting()
        air(m2, 6.0)
        m2._seg_voice = lambda ws, we: (0.6, 1.2)
        m2._drop_segment(ws, we, POINT)
        m2.on_partial({"text": POINT, "t": "00:01", "n": 1, "ws": ws, "we": we})
        self.assertEqual(m2.mouth.stops, 1)
        self.assertTrue(any(l.get("by") == "text" and l["text"].startswith("在说正事") for l in m2.logs))

    def test_cannot_pause_stops_at_once(self):
        m = make_meeting(can_pause=False)
        air(m, 3.0)
        m._barge("放音时听到人声（音量 0.050）")
        self.assertEqual(m.mouth.stops, 1)
        self.assertIsNone(m._listen)

    def test_pause_off_in_config(self):
        m = make_meeting({"pause": False})
        air(m, 3.0)
        m._barge("放音时听到人声（音量 0.050）")
        self.assertEqual((m.mouth.pauses, m.mouth.stops), (0, 1))

    def test_reply_not_aired_stops_at_once(self):
        m = make_meeting()
        m.mouth.played.append([time.time() - 30.0, time.time() - 25.0])
        m.mouth.pending = 1                                           # 回话还在合成，没出声
        m._barge("你接着在说，学生那句还没出声就撤了")
        self.assertEqual((m.mouth.pauses, m.mouth.stops), (0, 1))

    def test_long_pause_still_counts_as_airing(self):
        m = make_meeting()
        air(m, 5.0)
        m._barge("放音时听到人声（音量 0.050）")
        m.mouth.played[-1][1] = time.time() - 3.0                     # 已经停了 3 秒
        self.assertIsNotNone(m._air_start(), "停着不算这一轮说完了：晚到的附和照样压着")


class BargeKindTest(unittest.TestCase):
    def test_kinds(self):
        ack = ["没有啊。", "删了吧，删了吧。", "明白了。", "好好，我知道了。", "好好好。", "啊，就等你。", "嗯。", "对对对。",
               "可以可以。", "行。", "继续。", "你说。", "接着说吧。", "记下来吧。", "那你改吧。", "好的，你继续。",
               "你继续说，我没有打断你。", "OK。", "Yeah, go on.", "不用了。", "好，我等你说完。", "没关系，你先说。"]
        stop = ["不是不是。", "等一下。", "停。", "打住吧。", "好好好，打住吧。", "感觉有点问题。", "什么叫按顺序挑方法？",
                "说实话，我没有看太懂这个。", "Claude，你先看第二段。", "别删。", "这个数和表三对不上吧，你再看看。",
                "对吧？", "Wait, that's wrong."]
        for t in ack:
            self.assertEqual(zapp.barge_kind(t), "ack", t)
        for t in stop:
            self.assertEqual(zapp.barge_kind(t), "stop", t)
        self.assertEqual(zapp.barge_kind("。"), "none")


# ---------------------------------------------------------------- 耳朵

def _speechish(seed, secs, amp=0.15):
    """像说话的声音：每 64 毫秒换一个频谱形状的噪声（花纹比对认得出来、两段之间不相关）。"""
    rng = np.random.default_rng(seed)
    n, blk = int(secs * 16000), 1024
    out = np.zeros(n, np.float32)
    for i in range(0, n, blk):
        X = np.fft.rfft(rng.normal(0, 1, blk))
        env = np.repeat(rng.uniform(0.02, 1.0, 16), len(X) // 16 + 1)[:len(X)]
        y = np.fft.irfft(X * env, blk)
        out[i:i + blk] = y[:min(blk, n - i)]
    return out * (amp / (np.sqrt((out ** 2).mean()) + 1e-9))


def _pcm(x):
    return np.clip(np.asarray(x) * 32768, -32768, 32767).astype(np.int16).tobytes()


class EarsTest(unittest.TestCase):
    def setUp(self):
        PLAYBACK.clear()

    def tearDown(self):
        PLAYBACK.clear()

    def _vad(self, muted):
        d = Path(tempfile.mkdtemp(prefix="pause_vad_"))
        (d / "live").mkdir()
        v = zears._AudioVAD(d, muted=muted)
        v._sess = None                       # 不用 Silero：能量门限
        fired = []
        v.on_barge = lambda why: fired.append(why)
        return v, d / "live" / "audio.pcm", fired

    def test_rearmed_after_resume(self):
        state = {"muted": True}
        v, pcm, fired = self._vad(lambda: state["muted"])
        v.echo_gate = False
        v.pending = lambda: True             # 停着时：这一轮还没说完、又没在出声
        quiet, loud = _noise(40 * 0.032, 0.001, 1), _noise(12 * 0.032, 0.06, 2)
        pcm.write_bytes(quiet.tobytes() + loud.tobytes())
        v.pump()
        self.assertEqual(len(fired), 1)
        state["muted"] = False               # 停一下：不出声了
        with open(pcm, "ab") as f:
            f.write(_noise(20 * 0.032, 0.001, 3).tobytes())
        v.pump()
        state["muted"] = True                # 接着放
        with open(pcm, "ab") as f:
            f.write(_noise(20 * 0.032, 0.001, 4).tobytes() + _noise(12 * 0.032, 0.06, 5).tobytes())
        v.pump()
        self.assertEqual(len(fired), 2, "接着放以后你再开口，照样听得出来")

    def _onset_case(self, mic_seed):
        """AI 静了好一阵后开口（参考从这时开始）；麦克风里 0.288 秒后出现 mic_seed 那段声音。
        学到的延迟是错的（1.3 秒）：老的回声比对只在那附近找，认不出来。→ 判没判你打断"""
        v, pcm, fired = self._vad(lambda: True)
        off = time.time() - 50.0
        v._offs.append(off)                  # 录音里 t 秒 = 墙上 off + t
        v.lags.extend([1.3, 1.3, 1.3])
        t_m = 64 * 512 / 16000.0             # 麦克风里那段声音的起点（对齐到帧）
        ref = _speechish(1, 1.2)
        PLAYBACK.put_at("tts", _pcm(ref), off + t_m - 0.288)
        mic = _speechish(mic_seed, 1.2) * (0.5 if mic_seed == 1 else 1.0)
        pcm.write_bytes(_noise(t_m, 0.001, 9).tobytes() + _pcm(mic) + _noise(0.5, 0.001, 8).tobytes())
        v.pump()
        return fired

    def test_onset_echo_is_not_barge(self):
        self.assertEqual(self._onset_case(1), [], "AI 自己头几个字的回声：不算你打断")

    def test_voice_at_ai_onset_still_barges(self):
        self.assertEqual(len(self._onset_case(2)), 1, "AI 刚开口时你真说话了：照样算")


# ---------------------------------------------------------------- 语音桥

class _Src:
    def __init__(self, queued):
        self.queued_duration, self.cleared = queued, 0

    def clear_queue(self):
        self.cleared += 1


class _Loop:
    @staticmethod
    def call_soon_threadsafe(fn, *a):
        fn(*a)


class _Paused:
    paused = True


class BridgeTest(unittest.TestCase):
    def _bridge(self):
        br = zbridge.AudioBridge.__new__(zbridge.AudioBridge)
        br.mouth, br.connected = _Paused(), True
        return br

    def test_hold_clears_queue_and_repushes(self):
        br = self._bridge()
        src = _Src(0.05)                                   # 访客那边还排着 50 毫秒没放
        recent = deque([bytes([i]) * (zbridge.FRAME * 2) for i in range(20)], maxlen=150)
        threading.Timer(0.3, lambda: setattr(br.mouth, "paused", False)).start()
        t0 = time.time()
        again = br._student_hold(src, _Loop(), recent, None)
        self.assertGreaterEqual(time.time() - t0, 0.28, "嘴停着：这一路也停着")
        self.assertEqual(src.cleared, 1)
        self.assertEqual([b[0] for b in again], [15, 16, 17, 18, 19], "排着没放的那几帧恢复后补推")
        self.assertEqual(len(recent), 0)

    def test_hold_gives_up_when_cut(self):
        br = self._bridge()

        class Dead:
            returncode = 1

            @staticmethod
            def poll():
                return 1
        self.assertIsNone(br._student_hold(_Src(0.0), _Loop(), deque(), Dead()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
