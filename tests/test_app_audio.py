# -*- coding: utf-8 -*-
"""按程序录声音（zuhui/app_audio.py）：真机测试，要 Windows 10.0.20348 以上，别的系统跳过。

1. find_app() 找得到腾讯会议的根进程，别的 wemeetapp.exe 都在它下面（没开腾讯会议就跳过）；
2. 子进程放 2 秒 1 kHz 测试音，只录这个子进程：音量明显大于静音、主频约 1 kHz、有声的长度约 2 秒、
   帧流连续（总时长 ≈ 墙上时间）、子进程退出后 alive 变假且没有报错。
   同时另开一路强制走 48 kHz 双声道（自己下混、降到 16 kHz）的退路，结果也要对；
3. 录一个不出声的子进程：全是静音、帧流照样连续、没有报错；进程不存在时 start() 抛 AppAudioError；
4. AppListener：把 python.exe 复制成一个独特名字当"会议软件"，它再开一个同名子进程；
   find_app 取根进程，监听器挂上、on_state 回调、on_pcm 带显示名，根进程退出后断开，stop() 不再多回调。

会往扬声器放 2 秒中等音量（-20 dBFS）的 1 kHz 测试音。不碰真实的 ~/.zuhui、音频设置和正在跑的 Bloom。

    python -X utf8 -m unittest tests.test_app_audio -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ZUHUI_HOME", tempfile.mkdtemp(prefix="zuhui-test-appaudio-"))     # 必须在 import zuhui 之前
from zuhui import app_audio  # noqa: E402


def _build() -> int:
    try:
        return sys.getwindowsversion().build if sys.platform == "win32" else 0
    except Exception:
        return 0


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PLAYER = "import sys, winsound; sys.stdin.readline(); winsound.PlaySound(sys.argv[1], winsound.SND_FILENAME)"
SLEEPER = "import time; time.sleep(float(__import__('sys').argv[1]))"
# "会议软件"：开一个同名子进程（睡 1 秒），打印它的 pid，自己再睡 argv[1] 秒
FAKE_APP = (
    "import subprocess, sys, time\n"
    "kid = subprocess.Popen([sys.executable, '-S', '-c', 'import time; time.sleep(1.0)'])\n"
    "print(kid.pid, flush=True)\n"
    "time.sleep(float(sys.argv[1]))\n"
    "kid.wait()\n"
)


def _tone_wav(path: Path, secs: float = 2.0, rate: int = 48000, freq: float = 1000.0, amp: float = 0.1) -> None:
    n = int(secs * rate)
    x = amp * np.sin(2 * np.pi * freq * np.arange(n) / rate)
    fade = int(0.02 * rate)
    x[:fade] *= np.linspace(0, 1, fade)
    x[-fade:] *= np.linspace(1, 0, fade)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def _kill(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.kill()
        try:
            p.wait(5)
        except Exception:
            pass


class _Sink:
    """收 on_pcm(pcm, t)。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.chunks: list[tuple[float, bytes]] = []

    def __call__(self, pcm: bytes, t: float) -> None:
        with self.lock:
            self.chunks.append((t, pcm))

    def check(self, tc: unittest.TestCase) -> None:
        """每块是 10 ms 的整数倍，一块接一块没有缝。"""
        with self.lock:
            chunks = list(self.chunks)
        tc.assertTrue(chunks, "一块都没收到")
        for t, pcm in chunks:
            tc.assertGreater(len(pcm), 0)
            tc.assertEqual(len(pcm) % app_audio.FRAME_BYTES, 0, "不是 10 ms 的整数倍")
            tc.assertLessEqual(len(pcm), app_audio.FRAME_BYTES * app_audio.MAX_FRAMES_PER_CALL)
        for (t1, p1), (t2, _) in zip(chunks, chunks[1:]):
            tc.assertAlmostEqual(t2, t1 + len(p1) / (2 * app_audio.RATE), delta=1e-6, msg="帧流断了")

    def audio(self) -> np.ndarray:
        with self.lock:
            return np.frombuffer(b"".join(p for _, p in self.chunks), dtype="<i2")

    def span(self) -> tuple[float, float]:
        with self.lock:
            (t0, _), (t1, p1) = self.chunks[0], self.chunks[-1]
        return t0, t1 + len(p1) / (2 * app_audio.RATE)


class StreamLogicTest(unittest.TestCase):
    """补静音、接包、降采样的逻辑（不碰 WASAPI，假时钟）。真机上会议软件不出声时也一直送包，这条路平时走不到。"""

    def test_fill_gap_and_late_burst(self):
        sink = _Sink()
        cap = app_audio.AppCapture(0, sink)
        cap._t0 = 1000.0
        clock = mock.MagicMock()
        with mock.patch.object(app_audio, "time", clock):
            clock.time.return_value = 1000.5             # 半秒没包：补到 1000.4
            cap._fill(1000.5)
            cap._emit()
            self.assertAlmostEqual(cap._total, 0.4 * app_audio.RATE, delta=1)
            clock.time.return_value = 1000.52            # 来一个 10 ms 的包，起点 1000.51：中间补 0.11 秒静音
            cap._feed(np.full(160, 1000, "<i2").tobytes(), 160, 0, 0)
            cap._emit()
            clock.time.return_value = 1000.53            # 晚到的一大包（起点早于流末尾）：直接接上，不丢
            cap._feed(np.full(4800, 2000, "<i2").tobytes(), 4800, 0, 0)
            cap._fill(1000.6)                            # 流已经超前，不再补
            cap._emit()
        sink.check(self)
        a = sink.audio()
        t0, _ = sink.span()
        self.assertEqual(t0, 1000.0)
        i = int(np.argmax(a == 1000))
        self.assertEqual(i, int(round(0.51 * app_audio.RATE)))
        self.assertTrue((a[:i] == 0).all())
        self.assertTrue((a[i:i + 160] == 1000).all())
        self.assertEqual(int((a == 2000).sum()) + len(cap._buf) // 2, 4800)

    def test_down3_chunked(self):
        rng = np.random.default_rng(0)
        x = (3000 * np.sin(2 * np.pi * 1000 * np.arange(48000) / 48000)).astype(np.float32)
        whole = app_audio._Down3()(x)
        d, parts, i = app_audio._Down3(), [], 0
        while i < len(x):
            n = int(rng.integers(1, 700))
            parts.append(d(x[i:i + n]))
            i += n
        chunked = np.concatenate(parts)
        self.assertEqual(len(chunked), 16000)
        self.assertTrue(np.allclose(whole, chunked, atol=1e-2))
        rms = float(np.sqrt((chunked[1000:] ** 2).mean()))
        self.assertAlmostEqual(rms, 3000 / np.sqrt(2), delta=60)


@unittest.skipUnless(sys.platform == "win32" and _build() >= 20348, "要 Windows 10.0.20348 以上")
class AppAudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bloom-appaudio-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # 1 ---------------------------------------------------------------
    def test_find_wemeet_root(self):
        procs = app_audio._snapshot()
        mine = [p for p, _, n in procs if n.lower() == "wemeetapp.exe"]
        if not mine:
            self.skipTest("没开腾讯会议")
        found = app_audio.find_app()
        self.assertIsNotNone(found)
        pid, exe, label = found
        self.assertEqual((exe, label), ("wemeetapp.exe", "腾讯会议"))
        by_pid = {p: (pp, n.lower()) for p, pp, n in procs}
        parent = by_pid.get(by_pid[pid][0])
        self.assertFalse(parent is not None and parent[1] == "wemeetapp.exe", "取到的不是根进程")
        # 别的 wemeetapp.exe 往上找都能找到这个根
        for p in mine:
            seen = set()
            while p != pid and p in by_pid and p not in seen and by_pid[p][1] == "wemeetapp.exe":
                seen.add(p)
                p = by_pid[p][0]
            self.assertEqual(p, pid, "有 wemeetapp.exe 不在这棵树下")

    # 2 ---------------------------------------------------------------
    def test_tone_child(self):
        wav = self.tmp / "tone_1k.wav"
        _tone_wav(wav)
        child = subprocess.Popen([sys.executable, "-S", "-c", PLAYER, str(wav)], stdin=subprocess.PIPE,
                                 creationflags=NO_WINDOW)
        self.addCleanup(_kill, child)
        s16, s48 = _Sink(), _Sink()
        cap16 = app_audio.AppCapture(child.pid, s16)
        cap48 = app_audio.AppCapture(child.pid, s48)
        cap48._formats = ((48000, 2),)
        t_start = time.time()
        cap16.start()
        self.addCleanup(cap16.stop)
        cap48.start()
        self.addCleanup(cap48.stop)
        print(f"\n  直接初始化成 {cap16.fmt}，退路 {cap48.fmt}", flush=True)
        self.assertEqual(cap48.fmt, (48000, 2))
        self.assertTrue(cap16.alive and cap48.alive)
        time.sleep(0.4)
        child.stdin.write(b"go\n")
        child.stdin.close()
        peak = {16: 0.0, 48: 0.0}
        deadline = time.time() + 10
        while child.poll() is None and time.time() < deadline:
            peak[16] = max(peak[16], cap16.level)
            peak[48] = max(peak[48], cap48.level)
            time.sleep(0.05)
        self.assertIsNotNone(child.poll(), "播放子进程 10 秒内没结束")
        t_exit = time.time()
        dead: dict[int, float] = {}
        deadline = time.time() + 3
        while len(dead) < 2 and time.time() < deadline:
            for key, cap in ((16, cap16), (48, cap48)):
                if key not in dead and not cap.alive:
                    dead[key] = time.time()
            time.sleep(0.01)
        self.assertFalse(cap16.alive, "进程退出后 alive 还是真")
        self.assertFalse(cap48.alive, "进程退出后 alive 还是真")
        self.assertIsNone(cap16.error)
        self.assertIsNone(cap48.error)
        cap16.stop()
        cap48.stop()
        for key, cap, sink in ((16, cap16, s16), (48, cap48, s48)):
            with self.subTest(fmt=cap.fmt):
                sink.check(self)
                a = sink.audio().astype(np.float64)
                win = app_audio.RATE // 10
                rms = np.sqrt((a[: len(a) // win * win].reshape(-1, win) ** 2).mean(axis=1))
                loud_s = float((rms > 500).sum()) * 0.1
                i = int(np.argmax(rms))
                seg = a[max(0, i - 2) * win:(i + 3) * win]
                spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
                f_peak = float(np.fft.rfftfreq(len(seg), 1 / app_audio.RATE)[int(np.argmax(spec))])
                t0, t1 = sink.span()
                print(f"  {cap.fmt}: level 峰值 {peak[key]:.0f}，100 ms RMS 峰值 {rms.max():.0f}，"
                      f"有声 {loud_s:.1f} 秒，主频 {f_peak:.0f} Hz，流起点 {t0 - t_start:+.3f} 秒，"
                      f"流末尾 {t1 - dead[key]:+.3f} 秒（相对抓取结束），进程退出后 {dead[key] - t_exit:.2f} 秒结束，"
                      f"丢包 {cap.glitches}", flush=True)
                self.assertGreater(peak[key], 500, "level 没比静音大")
                self.assertGreater(rms.max(), 500)
                self.assertAlmostEqual(f_peak, 1000.0, delta=40)
                self.assertTrue(1.5 <= loud_s <= 2.6, f"有声 {loud_s} 秒")
                self.assertLess(abs(t0 - t_start), 0.3, "流的起点不对")
                self.assertLess(abs(t1 - dead[key]), app_audio.LAG_S + 0.06, "总时长和墙上时间对不上")
                self.assertLess(dead[key] - t_exit, app_audio.CHECK_S + 0.3, "进程退出后没及时结束")

    # 3 ---------------------------------------------------------------
    def test_silent_child(self):
        child = subprocess.Popen([sys.executable, "-S", "-c", SLEEPER, "30"], creationflags=NO_WINDOW)
        self.addCleanup(_kill, child)
        sink = _Sink()
        cap = app_audio.AppCapture(child.pid, sink)
        t0 = time.time()
        cap.start()
        self.addCleanup(cap.stop)
        time.sleep(1.5)
        self.assertTrue(cap.alive)
        t1 = time.time()
        cap.stop()
        self.assertIsNone(cap.error)
        self.assertFalse(cap.alive)
        sink.check(self)
        a = sink.audio()
        dur = len(a) / app_audio.RATE
        first, _ = sink.span()
        rms = float(np.sqrt((a.astype(np.float64) ** 2).mean()))
        print(f"\n  静音：{dur:.3f} 秒（墙上 {t1 - t0:.3f} 秒），最大值 {int(np.abs(a).max())}，RMS {rms:.2f}",
              flush=True)
        # Windows 转格式时会加 ±1 的抖动，不是真声音
        self.assertLessEqual(int(np.abs(a).max()), 8, "不出声的进程录到了声音")
        self.assertLess(rms, 5)
        self.assertLess(abs(first - t0), 0.3)
        # 有包时流末尾约等于"现在"，没包时补到"现在 - LAG_S"
        self.assertGreater(dur, t1 - t0 - app_audio.LAG_S - 0.08, "帧流比墙上时间短")
        self.assertLess(dur, t1 - t0 + 0.03, "帧流比墙上时间长")

    def test_missing_pid(self):
        p = subprocess.Popen([sys.executable, "-S", "-c", "pass"], creationflags=NO_WINDOW)
        p.wait(10)
        with self.assertRaises(app_audio.AppAudioError):
            app_audio.AppCapture(p.pid, lambda pcm, t: None).start()

    # 4 ---------------------------------------------------------------
    def test_listener(self):
        name = f"bloomtst{os.getpid()}.exe"
        exe = self.tmp / name
        shutil.copy2(Path(sys.base_prefix) / "python.exe", exe)
        env = dict(os.environ)
        env["PYTHONHOME"] = sys.base_prefix
        env["PATH"] = sys.base_prefix + os.pathsep + env.get("PATH", "")
        env.pop("PYTHONPATH", None)
        root = subprocess.Popen([str(exe), "-S", "-c", FAKE_APP, "3"], stdout=subprocess.PIPE, env=env,
                                cwd=sys.base_prefix, creationflags=NO_WINDOW)
        self.addCleanup(root.stdout.close)
        self.addCleanup(_kill, root)
        line = root.stdout.readline().strip()
        if not line.isdigit():
            self.skipTest(f"复制出来的 python 跑不起来：{line!r}")
        kid = int(line)
        apps = {name: "测试"}
        same = [(p, pp) for p, pp, n in app_audio._snapshot() if n.lower() == name]
        self.assertIn((kid, root.pid), same, "同名子进程没起来")
        self.assertEqual(app_audio.find_app(apps), (root.pid, name, "测试"))

        states: list = []
        labels: list = []
        on, off = threading.Event(), threading.Event()

        def on_state(lbl):
            states.append((lbl, len(labels)))
            (on if lbl else off).set()

        self.assertTrue({root.pid, kid} <= app_audio.tree_pids(root.pid), "子进程不在进程树里")
        # 假会议软件不出声：当它麦克风一直开着（在开会）。在不在开会怎么判见 ListenerLogic
        lis = app_audio.AppListener(lambda lbl, pcm, t: labels.append(lbl), on_state, poll_s=0.3, apps=apps,
                                    activity_fn=lambda pids: (True, False))
        lis.start()
        self.addCleanup(lis.stop)
        self.assertTrue(on.wait(5), "没挂上")
        self.assertEqual(lis.label, "测试")
        self.assertEqual(states[0], ("测试", 0), "on_state 要先于第一块 on_pcm")
        root.wait(10)
        self.assertTrue(off.wait(5), "进程退出后没断开")
        self.assertIsNone(lis.label)
        n_pcm = len(labels)
        lis.stop()
        print(f"\n  监听：{[s for s, _ in states]}，收到 {n_pcm} 块", flush=True)
        self.assertEqual([s for s, _ in states], ["测试", None])
        self.assertGreater(n_pcm, 10)
        self.assertEqual(set(labels), {"测试"})

    def test_activity_real_session(self):
        """真的音频会话：子进程放 4 秒全零的 wav（听不见），放的时候它的扬声器会话是活动的、麦克风没有；
        不出声的进程两样都没有。"""
        wav = self.tmp / "silent.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(bytes(2 * 48000 * 4))
        quiet = subprocess.Popen([sys.executable, "-S", "-c", SLEEPER, "6"], creationflags=NO_WINDOW)
        self.addCleanup(_kill, quiet)
        p = subprocess.Popen([sys.executable, "-S", "-c", PLAYER, str(wav)], stdin=subprocess.PIPE, creationflags=NO_WINDOW)
        self.addCleanup(_kill, p)
        self.addCleanup(p.stdin.close)
        p.stdin.write(b"go\n")
        p.stdin.flush()
        co = app_audio._coinit()
        try:
            seen = None
            end = time.time() + 3.5
            while time.time() < end:
                seen = app_audio.activity({p.pid})
                if seen and seen[1]:
                    break
                time.sleep(0.1)
            self.assertEqual(seen, (False, True), "放声音的进程扬声器会话应是活动的")
            self.assertEqual(app_audio.activity({quiet.pid}), (False, False))
        finally:
            if co:
                app_audio._couninit()


class _FakeCap:
    """假的 AppCapture：不真录，记下开了几个、停了几个。"""
    made: list = []

    def __init__(self, pid, on_pcm):
        self.pid, self.on_pcm, self.stopped = pid, on_pcm, False
        _FakeCap.made.append(self)

    alive = property(lambda self: not self.stopped)
    error = None

    def start(self):
        pass

    def stop(self):
        self.stopped = True


class ListenerLogic(unittest.TestCase):
    """AppListener 判在不在开会：不起线程，直接按假时钟调 _poll。

    托盘里挂着（会话都不活动）不算；提示音（扬声器响一两秒）不算；扬声器连着响够 SPK_HOLD_S 算；麦克风一开就算；
    开着会的麦克风、扬声器都停够 IDLE_S 算开完；软件关了算开完；查不了会话照老办法（进程在就算）；
    enabled=False 只报在不在开会、不录，set_enabled 切换。"""

    def setUp(self):
        _FakeCap.made = []
        self.act = (False, False)
        self.found = (100, "x.exe", "测试")
        self.states = []
        self.patches = [mock.patch.object(app_audio, "AppCapture", _FakeCap),
                        mock.patch.object(app_audio, "find_app", lambda apps=None: self.found),
                        mock.patch.object(app_audio, "tree_pids", lambda pid: {pid})]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def make(self, enabled=True):
        return app_audio.AppListener(lambda *a: None, self.states.append, enabled=enabled, activity_fn=lambda pids: self.act)

    def run_until(self, lis, t0, t1, step=2.0):
        t = t0
        while t <= t1 + 1e-9:
            lis._poll(t)
            t += step
        return t

    def test_tray_idle_and_notification(self):
        lis = self.make()
        self.run_until(lis, 0, 20)
        self.act = (False, True)                     # 提示音：扬声器响了两轮
        self.run_until(lis, 22, 24)
        self.act = (False, False)
        self.run_until(lis, 26, 40)
        self.assertEqual(self.states, [])
        self.assertEqual(_FakeCap.made, [])
        self.assertIsNone(lis.label)

    def test_speaker_held_counts(self):
        lis = self.make()
        self.act = (False, True)
        lis._poll(0)
        lis._poll(4)
        self.assertEqual(self.states, [])
        lis._poll(6)
        self.assertEqual(self.states, ["测试"])
        self.assertTrue(lis.capturing)

    def test_mic_starts_and_idle_ends(self):
        lis = self.make()
        self.act = (True, True)
        lis._poll(0)
        self.assertEqual(self.states, ["测试"])
        self.assertEqual(len(_FakeCap.made), 1)
        self.act = (False, False)                    # 会开完了：麦克风、扬声器都停了
        lis._poll(2)
        lis._poll(8)
        self.assertEqual(self.states, ["测试"], "停了不到 IDLE_S 不算开完")
        self.act = (True, False)                     # 中间又开了麦克风：重新计时
        lis._poll(10)
        self.act = (False, False)
        lis._poll(12)
        lis._poll(20)
        self.assertEqual(self.states, ["测试"])
        lis._poll(22)
        self.assertEqual(self.states, ["测试", None])
        self.assertTrue(_FakeCap.made[0].stopped)
        self.assertFalse(lis.capturing)

    def test_app_closed_and_reopened(self):
        lis = self.make()
        self.act = (True, False)
        lis._poll(0)
        self.found = None
        lis._poll(2)
        self.assertEqual(self.states, ["测试", None])
        self.found = (200, "x.exe", "测试")         # 重开了：新进程
        lis._poll(4)
        self.assertEqual(self.states, ["测试", None, "测试"])
        self.assertEqual([c.pid for c in _FakeCap.made], [100, 200])

    def test_no_session_info_falls_back(self):
        lis = self.make()
        self.act = None
        lis._poll(0)
        self.assertEqual(self.states, ["测试"])
        self.assertTrue(lis.capturing)

    def test_disabled_reports_but_does_not_record(self):
        lis = self.make(enabled=False)
        self.act = (True, True)
        lis._poll(0)
        self.assertEqual(self.states, ["测试"])
        self.assertEqual(lis.label, "测试")
        self.assertFalse(lis.capturing)
        lis.set_enabled(True)
        lis._poll(1)
        self.assertTrue(lis.capturing)
        lis.set_enabled(False)
        lis._poll(2)
        self.assertFalse(lis.capturing)
        self.assertTrue(_FakeCap.made[0].stopped)
        self.assertEqual(self.states, ["测试"], "开关只管录不录，不算开完会")


if __name__ == "__main__":
    unittest.main()
