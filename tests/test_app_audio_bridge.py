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

    def __init__(self, on_pcm, on_state=None, enabled=True, **kw):
        self.on_pcm, self.on_state, self.stopped, self.enabled = on_pcm, on_state, False, enabled
        FakeListener.last = self

    def start(self):
        self.on_state("腾讯会议")

    def set_enabled(self, on):
        self.enabled = on

    def stop(self):
        self.stopped = True
        self.on_state(None)


def fake_module(ok=True):
    return types.SimpleNamespace(supported=lambda: ok, AppListener=FakeListener)


class AppAudioBridge(unittest.TestCase):
    def setUp(self):
        self.saved = Path(zconfig.STATE) / zb.AudioBridge.APP_LISTEN_FILE
        self.saved.unlink(missing_ok=True)
        self.addCleanup(lambda: self.saved.unlink(missing_ok=True))

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

    def test_unsupported(self):
        br = self.make()
        self.on(br, ok=False)
        self.assertIsNone(br.app_audio)
        self.assertFalse(br.hears_guests)

    def test_off_still_shown_and_toggle(self):
        """config 里关了（或者会议页上点了「停止收听」）：照样知道腾讯会议在开会（会议页上显示它），但不录、不转写；
        点「开始收听」马上 joined、开始录，点「停止收听」马上 left、停录；开关记下来，下一场会照这个。"""
        br = self.make({"app_audio": {"enabled": False}})
        self.on(br)
        lis, ge = FakeListener.last, br.guest_ears
        self.assertFalse(lis.enabled)
        self.assertEqual(br.app_status(), {"name": "腾讯会议", "on": False})
        self.assertNotIn(("joined", "app-audio", "腾讯会议"), ge.calls)
        self.assertFalse(br.hears_guests)
        lis.on_pcm("腾讯会议", np.zeros(320, np.int16).tobytes(), time.time())
        self.assertFalse([c for c in ge.calls if c[0] == "feed"], "关着时不送去转写")

        self.assertTrue(br.set_app_listen(True))
        self.assertTrue(lis.enabled)
        self.assertIn(("joined", "app-audio", "腾讯会议"), ge.calls)
        self.assertTrue(br.hears_guests)
        lis.on_pcm("腾讯会议", np.zeros(320, np.int16).tobytes(), time.time())
        self.assertEqual(len([c for c in ge.calls if c[0] == "feed"]), 1)
        self.assertEqual(self.make().app_listen, True)

        self.assertFalse(br.set_app_listen(False))
        self.assertFalse(lis.enabled)
        self.assertEqual(ge.calls[-1], ("left", "app-audio"))
        self.assertFalse(br.hears_guests)
        self.assertEqual(br.app_status(), {"name": "腾讯会议", "on": False})
        self.assertFalse(self.make().app_listen, "点过「停止收听」：下一场会也不录")
        br.stop()
        self.assertIsNone(br.app_status()["name"])
        self.assertEqual([c for c in ge.calls if c[0] == "left"], [("left", "app-audio")], "没在录时散会不再 left")

    def test_app_participant(self):
        """会议页参会人里的会议软件：在开会才有，带 listening；颜色避开在场访客。"""
        from zuhui.view import Viewer
        stub = types.SimpleNamespace(app_audio_state=lambda: {"name": "腾讯会议", "on": False},
                                     guest_colors=lambda names: {n: f"#00000{i}" for i, n in enumerate(names)})
        people = [{"name": "我", "rank": "host"}, {"name": "小王", "rank": "participant"}]
        p = Viewer.app_participant(stub, people)
        self.assertEqual((p["id"], p["name"], p["rank"], p["listening"], p["color"]), ("app-audio", "腾讯会议", "app", False, "#000001"))
        stub.app_audio_state = lambda: {"name": None, "on": True}
        self.assertIsNone(Viewer.app_participant(stub, people))
        stub.app_audio_state = lambda: 1 / 0
        self.assertIsNone(Viewer.app_participant(stub, people))


if __name__ == "__main__":
    unittest.main()
