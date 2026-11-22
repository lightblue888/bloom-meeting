# -*- coding: utf-8 -*-
"""会议软件（腾讯会议等）放出来的声音接进语音桥：当成一位访客（名字 = 软件名）直接转写，不用开邀请、不用中继。
录声音本身（WASAPI 按程序录）在 test_app_audio.py；这里用假的 AppListener，只测接线：

  · 挂上会议软件 → 访客转写起来、joined；有访客声音（hears_guests / direct_ok）；
  · 送进来的声音 → 判谁在说（guest_over 认出是它）、回声参考、送去转写；
  · 断开 / 散会 → left、停掉；config 里 app_audio.enabled = false 就不录。

    python -X utf8 -m unittest tests.test_app_audio_bridge -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-appaudio-")
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("不在真实状态目录里跑")

import numpy as np  # noqa: E402

import zuhui  # noqa: E402
from zuhui import bridge as zb  # noqa: E402


class FakeEars:
    def __init__(self):
        self.calls, self.alive = [], False
        self._thread = None
        self.healthy = True

    def start(self):
        self.calls.append(("start",))
        self.alive = True
        self._thread = types.SimpleNamespace(is_alive=lambda: self.alive)

    def stop(self):
        self.calls.append(("stop",))
        self.alive = False

    def joined(self, ident, name):
        self.calls.append(("joined", ident, name))

    def left(self, ident):
        self.calls.append(("left", ident))

    def feed(self, ident, name, pcm, t):
        self.calls.append(("feed", ident, name, len(pcm)))

    def talking(self, within=0.5):
        return ["腾讯会议"]


class FakeListener:
    last = None

    def __init__(self, on_pcm, on_state=None, **kw):
        self.on_pcm, self.on_state, self.stopped = on_pcm, on_state, False
        FakeListener.last = self

    def start(self):
        self.on_state("腾讯会议")

    def stop(self):
        self.stopped = True


def fake_module(ok=True):
    return types.SimpleNamespace(supported=lambda: ok, AppListener=FakeListener)


class AppAudioBridge(unittest.TestCase):
    def make(self, cfg=None):
        br = zb.AudioBridge(cfg or {}, None, me="测试", devices=False)
        br.guest_ears = FakeEars()
        return br

    def on(self, br, ok=True):
        mod = fake_module(ok)
        with mock.patch.dict(sys.modules, {"zuhui.app_audio": mod}), mock.patch.object(zuhui, "app_audio", mod, create=True):
            br.app_on()

    def test_attach_feed_and_detach(self):
        br = self.make()
        self.assertFalse(br.hears_guests)
        self.on(br)
        ge = br.guest_ears
        self.assertEqual(br.app_label, "腾讯会议")
        self.assertIn(("start",), ge.calls)
        self.assertIn(("joined", "app-audio", "腾讯会议"), ge.calls)
        self.assertTrue(br.hears_guests and br.direct_ok)
        self.assertEqual(br.guest_talking(), ["腾讯会议"])

        # 一秒静音（底噪）+ 两秒的"说话"（大声）+ 半秒静音：判谁在说认得出、送去转写、记进回声参考
        t0 = time.time() - 3.0
        loud = (np.sin(np.arange(16000) * 2 * np.pi * 300 / 16000) * 8000).astype(np.int16)
        FakeListener.last.on_pcm("腾讯会议", (np.random.default_rng(1).normal(0, 30, 16000)).astype(np.int16).tobytes(), t0 - 1)
        for k in range(2):
            FakeListener.last.on_pcm("腾讯会议", loud.tobytes(), t0 + k)
        FakeListener.last.on_pcm("腾讯会议", np.zeros(8000, np.int16).tobytes(), t0 + 2)
        feeds = [c for c in ge.calls if c[0] == "feed"]
        self.assertEqual(len(feeds), 4)
        self.assertTrue(all(c[1] == "app-audio" and c[2] == "腾讯会议" for c in feeds))
        self.assertEqual(br.guest_over(t0 + 0.2, t0 + 1.8), "腾讯会议")
        self.assertIsNone(br.guest_over(t0 + 2.6, t0 + 2.9))

        br.stop()                                         # 散会：停录、left、语音房间没开 → 访客转写也停
        self.assertTrue(FakeListener.last.stopped)
        self.assertIsNone(br.app_label)
        self.assertIn(("left", "app-audio"), ge.calls)
        self.assertEqual(ge.calls[-1], ("stop",))
        self.assertFalse(br.hears_guests)

    def test_restarts_ears_if_stopped(self):
        br = self.make()
        self.on(br)
        br.guest_ears.alive = False                       # 语音房间断开时把访客转写一起停了
        FakeListener.last.on_pcm("腾讯会议", np.zeros(320, np.int16).tobytes(), time.time())
        self.assertTrue(br.guest_ears.alive)
        br.stop()

    def test_off_in_config_or_unsupported(self):
        br = self.make({"app_audio": {"enabled": False}})
        self.on(br)
        self.assertIsNone(br.app_audio)
        br = self.make()
        self.on(br, ok=False)
        self.assertIsNone(br.app_audio)
        self.assertFalse(br.hears_guests)


if __name__ == "__main__":
    unittest.main()
