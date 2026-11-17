# -*- coding: utf-8 -*-
"""耳朵的回声比对 / 静音 / 主持人在别的设备上 / 插话：合成信号的单元测试（不用真录音，不碰正在跑的 Bloom）。

    python -m unittest tests.test_echo_gate -v
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import time as _time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SR = 16000


def speechish(seed: int, seconds: float, amp: float = 0.12) -> np.ndarray:
    """像说话的合成声：一个个 120–260 毫秒的"音节"，每个音节自己的基频（带滑动）和三个共振峰，音节之间有短停顿。"""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(seconds * SR), np.float32)
    t = 0
    while t < len(out):
        n = int(rng.uniform(0.12, 0.26) * SR)
        gap = int(rng.uniform(0.03, 0.12) * SR)
        f0 = rng.uniform(100, 220)
        fs = rng.uniform(250, 3500, 3)
        k = np.arange(n)
        f = f0 * (1 + 0.15 * (k / n - 0.5))
        ph = 2 * np.pi * np.cumsum(f) / SR
        sig = np.zeros(n, np.float32)
        for h in range(1, 30):
            fh = f0 * h
            if fh > 7000:
                break
            g = sum(np.exp(-((fh - c) / 180.0) ** 2) for c in fs) + 0.02
            sig += (g * np.sin(h * ph)).astype(np.float32)
        env = np.sin(np.pi * k / n) ** 0.7
        seg = sig * env
        seg = seg / (np.abs(seg).max() + 1e-9) * amp
        e = min(len(out), t + n)
        out[t:e] = seg[:e - t]
        t += n + gap
    return out


def room(x: np.ndarray, gain: float = 0.08) -> np.ndarray:
    """扬声器 → 房间 → 麦克风（几条反射）→ 回声消除后剩下的一点。"""
    h = np.zeros(int(0.06 * SR), np.float32)
    for d, a in ((0, 1.0), (int(0.004 * SR), 0.5), (int(0.017 * SR), 0.3), (int(0.041 * SR), 0.15)):
        h[d] += a
    return (np.convolve(x, h)[:len(x)] * gain).astype(np.float32)


def i16(x):
    return np.clip(x * 32768, -32768, 32767).astype(np.int16)


class Clock:
    def __init__(self, t):
        self.now = t

    def time(self):
        return self.now

    def __getattr__(self, k):
        return getattr(_time, k)


class PlaybackRefTest(unittest.TestCase):
    def setUp(self):
        from zuhui import ears
        self.ears = ears
        self.pb = ears.PlaybackRef()

    def test_stream_put_level_read(self):
        x = i16(speechish(1, 1.0))
        t0 = 5000.0
        rng = np.random.default_rng(0)
        for i in range(100):                                      # 10 毫秒一块，到达时刻抖 ±20 毫秒
            self.pb.put("g", x[i * 160:(i + 1) * 160], t0 + (i + 1) * 0.01 + rng.uniform(-0.02, 0.02))
        self.assertGreater(self.pb.level(t0, t0 + 1.0), -40)
        self.assertEqual(self.pb.level(t0 + 1.3, t0 + 2.0), -120.0)
        y = self.pb.read(t0, t0 + 1.0)
        env = lambda s: np.sqrt((s[:len(s) // 160 * 160].reshape(-1, 160).astype(np.float64) ** 2).mean(1))   # noqa: E731
        ey, ex = env(y), env(x.astype(np.float32) / 32768)
        best = max(np.corrcoef(ey[5 + k:90 + k], ex[5:90])[0, 1] for k in range(-3, 4))
        self.assertGreater(best, 0.9)                             # 抖动被按样本数接住：10 毫秒的包络对得上（错位不超过 30 毫秒）
        self.assertEqual(len(y), 16000)

    def test_put_at_cut_and_kinds(self):
        x = i16(speechish(2, 2.0))
        self.pb.put_at("tts", x, 100.0)
        self.assertGreater(self.pb.level(101.0, 101.5, kinds=("tts",)), -40)
        self.assertEqual(self.pb.level(101.0, 101.5, kinds=("guest",)), -120.0)
        self.pb.cut("tts", 100.5)                                 # 被打断：后面那截作废
        self.assertEqual(self.pb.level(100.6, 102.0), -120.0)
        self.assertGreater(self.pb.level(100.0, 100.5), -40)

    def test_resample_and_rate(self):
        x32 = i16(np.repeat(speechish(3, 1.0), 2))                # 32 kHz
        self.pb.put_at("tts", x32, 10.0, sr=32000, rate=1.25)     # 快 25%：1 秒的声音 0.8 秒放完
        self.assertGreater(self.pb.level(10.6, 10.75), -45)
        self.assertEqual(self.pb.level(10.85, 11.2), -120.0)


class EchoMatchTest(unittest.TestCase):
    def setUp(self):
        from zuhui import ears
        self.ears = ears
        self.F = 512 / SR

    def _match(self, mic, ref, lag_s, voiced=None):
        e = self.ears
        n = len(mic) // 512
        offs = range(0, int(round((e.LAG_MAX - e.LAG_MIN) / self.F)) + 1)
        # ref 从 (mic 起点 − LAG_MAX) 开始读
        pad = int(e.LAG_MAX * SR)
        r = np.concatenate([np.zeros(pad, np.float32), ref])[:len(mic) + int((e.LAG_MAX - e.LAG_MIN) * SR)]
        return e.echo_match(np, mic[:n * 512], np.ones(n, bool) if voiced is None else voiced, r, offs)

    def test_echo_is_recognised(self):
        ref = speechish(10, 4.0, 0.25)
        lag = 0.4
        mic = np.concatenate([np.zeros(int(lag * SR), np.float32), room(ref)])[:len(ref)]
        mic += np.random.default_rng(1).normal(0, 0.0005, len(mic)).astype(np.float32)
        r = self._match(mic, ref, lag)
        self.assertGreaterEqual(r["sim"], self.ears.ECHO_SIM)
        self.assertGreaterEqual(r["cover"], self.ears.ECHO_COVER)
        self.assertAlmostEqual(self.ears.LAG_MAX - r["off"] * self.F, lag, delta=0.07)

    def test_host_over_playback_is_not_echo(self):
        ref = speechish(11, 4.0, 0.25)
        host = speechish(99, 4.0, 0.08)                           # 你说话的音量和残余回声差不多（最难的情况）
        lag = 0.4
        echo = np.concatenate([np.zeros(int(lag * SR), np.float32), room(ref)])[:len(ref)]
        r = self._match(echo + host, ref, lag)
        self.assertTrue(r["sim"] is None or r["sim"] < self.ears.ECHO_SIM, r)

    def test_nothing_playing(self):
        host = speechish(12, 2.0)
        r = self._match(host, np.zeros(len(host), np.float32), 0.4)
        self.assertEqual(r["cover"], 0.0)
        self.assertIsNone(r["sim"])


class EarsGateTest(unittest.TestCase):
    """模拟时钟跑真的 _AudioVAD + _Collector：放音、回声、你说话、静音、主持人在别的设备上、插话。"""
    REC0 = 2_000_000.0
    D = 0.3                      # 口喷鸡写文件比真实时间晚这么多
    L = 0.12                     # 放出去 → 录到

    def setUp(self):
        from zuhui import ears
        self.ears = importlib.reload(ears)
        self.clock = Clock(self.REC0)
        self.ears.time = self.clock
        self.ears.MIC = self.ears._MicState(Path(tempfile.mkdtemp()) / "mic_mute.json")
        self.ears.PLAYBACK = self.ears.PlaybackRef()
        self.tmp = Path(tempfile.mkdtemp(prefix="echo_gate_"))
        (self.tmp / "live").mkdir()
        self.fh = open(self.tmp / "live" / "audio.pcm", "wb")
        self.student = [False]
        self.vad = self.ears._AudioVAD(self.tmp, muted=lambda: self.student[0])
        self.vad._sess = None                                   # 合成声用能量判人声
        self.barges = []
        self.vad.on_barge = lambda why: self.barges.append((self.clock.now, why))
        self.logs, self.kept = [], []
        self.c = self.ears._Collector(self.tmp, 2.0, 2, False, emit=lambda u: self.kept.append(u.text), partial=None, drop=None)
        self.c.vad = self.vad
        self.c.voice = None                                     # 声纹另测；这里只看回声 / 静音
        self.c.on_log = lambda kind, **kw: self.logs.append(dict(kw, kind=kind))
        self.mic = np.zeros(0, np.float32)
        self.written = 0

    def tearDown(self):
        self.fh.close()
        self.ears.time = _time                                  # 别把模拟时钟留给后面的测试
        importlib.reload(self.ears)

    def play_and_record(self, ref: np.ndarray | None, mic_extra: np.ndarray | None, seconds: float, kind="guest"):
        """接下来 seconds 秒：扬声器放 ref（访客那一路，按 10 毫秒到达），麦克风录到它的回声 + mic_extra（你说的）。"""
        n = int(seconds * SR)
        t_audio0 = len(self.mic) / SR
        echo = np.zeros(n, np.float32)
        if ref is not None:
            echo = np.concatenate([np.zeros(int(self.L * SR), np.float32), room(ref[:n], 0.2)])[:n]
            echo = np.pad(echo, (0, n - len(echo)))
        extra = np.zeros(n, np.float32) if mic_extra is None else np.pad(mic_extra[:n], (0, max(0, n - len(mic_extra))))
        self.mic = np.concatenate([self.mic, echo + extra + np.random.default_rng(len(self.mic)).normal(0, 3e-4, n).astype(np.float32)])
        r16 = i16(ref[:n]) if ref is not None else None
        steps = int(seconds / 0.05)
        for k in range(steps):
            self.clock.now += 0.05
            ta = self.clock.now - self.REC0
            if r16 is not None:                                 # 到达时刻 = 录到时刻 − L
                for b in range(int((ta - t_audio0 - 0.05) / 0.01), int((ta - t_audio0) / 0.01)):
                    if 0 <= b and (b + 1) * 160 <= len(r16):
                        if kind == "guest":
                            self.ears.PLAYBACK.put("g", r16[b * 160:(b + 1) * 160], self.REC0 + t_audio0 + (b + 1) * 0.01 - self.L)
                        else:
                            self.ears.PLAYBACK.put_at("tts", r16[b * 160:(b + 1) * 160], self.REC0 + t_audio0 + b * 0.01 - self.L)
            upto = min(len(self.mic), int(max(0.0, ta - self.D) * SR))
            if upto > self.written:
                self.fh.write(i16(self.mic[self.written:upto]).tobytes())
                self.fh.flush()
                self.written = upto
            self.vad.pump()
        return t_audio0, t_audio0 + seconds

    def settle(self, seconds=1.0):
        return self.play_and_record(None, None, seconds)

    def seg(self, a, b, text):
        self.c.add({"start": a + 0.05, "end": b - 0.05, "text": text})

    def test_echo_dropped_host_kept(self):
        self.settle(0.5)
        # 先放一阵访客的声音（学到延迟）
        for i in range(4):
            a, b = self.play_and_record(speechish(20 + i, 3.0, 0.3), None, 3.0)
            self.settle(1.2)
            self.seg(a + self.L, b, f"回声第{i}段")
        dropped = [l for l in self.logs if l["kind"] == "echo"]
        self.assertGreaterEqual(len(dropped), 3, self.logs)
        self.assertGreaterEqual(len(self.vad.lags), 2)
        # 访客说着、你也在说（你的声音比回声大）：照收
        a, b = self.play_and_record(speechish(30, 3.0, 0.3), speechish(77, 3.0, 0.3), 3.0)
        self.settle(1.2)
        n0 = len(self.c.buf)
        self.seg(a, b, "你在放音时说的话")
        self.assertEqual(len(self.c.buf), n0 + 1, self.logs[-2:])
        # 什么都没放、你说话：照收
        a, b = self.play_and_record(None, speechish(78, 2.0, 0.15), 2.0)
        self.settle(1.2)
        self.seg(a, b, "安静时说的")
        self.assertIn("安静时说的", "".join(self.c.buf))

    def test_mute_drops_without_text_and_recovers(self):
        self.settle(0.5)
        self.ears.MIC.set(True)
        a, b = self.play_and_record(None, speechish(40, 2.0, 0.15), 2.0)
        self.assertFalse(any(v for _, v in list(self.vad.frames)[-40:]))   # 静音时不算你在说话
        self.settle(1.0)
        self.ears.MIC.set(False)
        self.settle(1.0)
        self.seg(a, b, "静音时说的秘密")                           # 静音解除后才落地，照样不收
        self.assertNotIn("秘密", "".join(self.c.buf))
        self.assertFalse(any("秘密" in str(l) for l in self.logs))  # 日志里也不记字
        a, b = self.play_and_record(None, speechish(41, 2.0, 0.15), 2.0)
        self.settle(1.2)
        self.seg(a, b, "打开以后说的")
        self.assertIn("打开以后说的", "".join(self.c.buf))

    def test_host_elsewhere(self):
        self.settle(0.5)
        self.ears.PLAYBACK.host_elsewhere, self.ears.PLAYBACK.host_elsewhere_sim = "x", 0.52
        a, b = self.play_and_record(speechish(50, 2.0, 0.3), None, 2.0)
        self.settle(1.2)
        self.seg(a, b, "家里录到的")
        self.assertEqual(self.c.buf, [])
        kinds = [l["kind"] for l in self.logs]
        self.assertIn("mic", kinds)
        self.assertIn("echo", kinds)

        class FakeVoice:
            ok = True

            def sim(self, x):
                return 0.62
        self.c.voice = FakeVoice()                              # 没在放音、声纹很像你：你回到电脑前了
        a, b = self.play_and_record(None, speechish(51, 3.0, 0.15), 3.0)
        self.settle(1.2)
        self.seg(a, b, "我回来了")
        self.assertIsNone(self.ears.PLAYBACK.host_elsewhere)
        self.assertIn("我回来了", "".join(self.c.buf))

    def test_barge_still_fires_and_guest_echo_does_not(self):
        self.settle(0.5)
        for i in range(3):                                      # 学延迟
            a, b = self.play_and_record(speechish(60 + i, 3.0, 0.3), None, 3.0)
            self.settle(1.2)
            self.seg(a + self.L, b, f"回声{i}")
        self.student[0] = True                                  # 学生在出声（扬声器放学生的声音）
        self.play_and_record(speechish(70, 2.0, 0.05), None, 2.0, kind="tts")     # 学生的回声很小：回声底
        # 访客的声音突然很响地从扬声器出来（不是你）：不该算你打断
        self.play_and_record(speechish(71, 2.0, 0.6), None, 2.0)
        self.assertEqual(self.barges, [], self.barges)
        # 你开口（比回声响得多）：照样打断
        t0 = self.clock.now
        self.play_and_record(speechish(72, 1.5, 0.05), speechish(79, 1.5, 0.3), 1.5, kind="tts")
        self.assertTrue(self.barges, "你开口了却没打断")
        self.assertLess(self.barges[0][0] - t0 - self.D, 0.9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
