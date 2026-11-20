# -*- coding: utf-8 -*-
"""设置 → 语音 → 麦克风（zuhui/mic_prefs.py）：record.mic 的读写只动这一个键、选"系统默认"删掉它、开着会 409、
大厅的麦克风那一行（Viewer.devices）跟着选的走，GET / POST /api/mic/device 走得通。

ZUHUI_HOME、BLURT_HOME 都指到临时目录，不碰真实的 ~/.zuhui、~/.blurt；系统里有哪些麦克风用假的（不起 PortAudio）。

    python -X utf8 -m unittest tests.test_mic_prefs -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-mic-")
from zuhui import config as zconfig  # noqa: E402

STATE = Path(zconfig.STATE)
if os.path.normcase(str(STATE.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import mic_prefs  # noqa: E402

REALTEK = "麦克风阵列 (Realtek(R) Audio)"
CABLE = "CABLE Output (VB-Audio Virtual Cable)"
INPUTS = [CABLE, "麦克风 (ToDesk Virtual Audio)", REALTEK, "扬声器 (Realtek(R) Audio) [Loopback]"]


class FakeViewer:
    """mic_prefs 要的那几样：问系统（_probe_devices → _dev）、开没开会（meetings.current）。"""

    def __init__(self, live=False, inputs=INPUTS):
        self._dev = (0.0, None)
        self._dev_busy = False
        self._ready_cache = (time.time(), {"x": 1})
        self.probes = 0
        self.inputs = inputs
        cur = {"id": "m1", "state": "live"} if live else None
        self.meetings = type("M", (), {"current": staticmethod(lambda: cur)})()

    def _probe_devices(self):
        self.probes += 1
        self._dev = (time.time(), {"mic": REALTEK, "speaker": "扬声器 (Realtek(R) Audio)", "inputs": list(self.inputs)})


class MicPrefsTest(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("BLURT_HOME")
        self.home = Path(tempfile.mkdtemp(prefix="blurt-test-mic-"))
        os.environ["BLURT_HOME"] = str(self.home)
        self.cfg = self.home / "config.json"

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BLURT_HOME", None)
        else:
            os.environ["BLURT_HOME"] = self.old

    def write(self, text: str):
        self.cfg.write_text(text, encoding="utf-8")

    def read(self) -> dict:
        return json.loads(self.cfg.read_text(encoding="utf-8"))

    def test_default_when_missing(self):
        self.assertEqual(mic_prefs.chosen(), "")
        self.assertEqual(mic_prefs.save(""), "")
        self.assertFalse(self.cfg.exists())                                  # 本来就是系统默认：不写文件

    def test_save_keeps_other_keys(self):
        # 手改过的：带 BOM、尾逗号；别的键、record 里别的键都要原样留着
        self.write('﻿{"lang": "zh", "record": {"system_audio": true, "dictation": {"asr_model": "fun-asr-nano-onnx"},}, "x": [1, 2,],}')
        self.assertEqual(mic_prefs.save(f"  {REALTEK} "), REALTEK)
        d = self.read()
        self.assertEqual(d["lang"], "zh")
        self.assertEqual(d["x"], [1, 2])
        self.assertEqual(d["record"], {"system_audio": True, "dictation": {"asr_model": "fun-asr-nano-onnx"}, "mic": REALTEK})
        self.assertEqual(mic_prefs.chosen(), REALTEK)
        self.assertFalse(list(self.home.glob("config.json.*.tmp")))          # 临时文件换上去了
        # 系统默认：删掉 mic 这个键（record.py 不认 "default"），别的照旧
        self.assertEqual(mic_prefs.save("default"), "")
        self.assertEqual(self.read()["record"], {"system_audio": True, "dictation": {"asr_model": "fun-asr-nano-onnx"}})
        self.assertEqual(mic_prefs.chosen(), "")
        mic_prefs.save(CABLE)
        mic_prefs.save("")
        self.assertNotIn("mic", self.read()["record"])

    def test_bad_values(self):
        self.write('{"record": {"mic": "USB"}}')
        for v, code in (("x" * (mic_prefs.MAX + 1), "too_long"), ("off", "bad"), (123, "bad"), ("a\nb", "bad")):
            with self.assertRaises(ValueError) as cm:
                mic_prefs.save(v)
            self.assertEqual(str(cm.exception), code)
        self.assertEqual(self.read(), {"record": {"mic": "USB"}})

    def test_unreadable_left_alone(self):
        self.write('{"record": {"mic": "USB"')                               # 手改坏了
        with self.assertRaises(OSError):
            mic_prefs.save(REALTEK)
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), '{"record": {"mic": "USB"')

    def test_chosen_reads_like_capture_rs(self):
        for raw, want in (('{"record": {"mic": "default"}}', ""), ('{"record": {"mic": ""}}', ""), ('{"record": {"mic": null}}', ""),
                          ('{"record": {"mic": false}}', "off"), ('{"record": {"mic": "off"}}', "off"),
                          ('{"record": {"mic": "none"}}', "off"), ('{"record": {"mic": "USB Mic"}}', "USB Mic"), ('{"record": 3}', "")):
            self.write(raw)
            self.assertEqual(mic_prefs.chosen(), want, raw)

    def test_http(self):
        v = FakeViewer()
        out, code = mic_prefs.http_get(v)
        self.assertEqual(code, 200)
        self.assertEqual(out["mic"], "")
        self.assertEqual(out["default"], REALTEK)
        self.assertEqual(out["devices"], INPUTS[:3])                         # [Loopback] 不算麦克风
        self.assertFalse(out["missing"])
        self.assertFalse(out["live"])
        # 上一场录音记下的设备：换了麦克风就不作数了
        (STATE / "devices.json").write_text(json.dumps({"mic": "旧的", "speaker": "旧的"}), encoding="utf-8")
        out, code = mic_prefs.http_post(v, {"mic": CABLE})
        self.assertEqual(code, 200)
        self.assertEqual(out["mic"], CABLE)
        self.assertFalse(out["missing"])
        self.assertFalse((STATE / "devices.json").exists())
        self.assertEqual(v._ready_cache[0], 0.0)
        self.assertEqual(self.read(), {"record": {"mic": CABLE}})
        out, code = mic_prefs.http_post(v, {"mic": "x" * 300})
        self.assertEqual((code, out["error"]), (400, "too_long"))
        # 拔掉了：还是它，missing
        v.inputs = [REALTEK]
        v._dev = (0.0, None)
        out, _ = mic_prefs.http_get(v)
        self.assertEqual((out["mic"], out["missing"]), (CABLE, True))
        # 开着会：不让改，文件不动
        live = FakeViewer(live=True)
        out, code = mic_prefs.http_post(live, {"mic": REALTEK})
        self.assertEqual((code, out["error"]), (409, "live"))
        self.assertEqual(self.read(), {"record": {"mic": CABLE}})
        self.assertTrue(mic_prefs.http_get(live)[0]["live"])

    def test_lobby_row_follows_choice(self):
        from zuhui.view import Viewer
        v = object.__new__(Viewer)                                           # 不起服务，只要 devices()
        v._dev = (time.time(), {"mic": REALTEK, "speaker": "扬声器", "inputs": INPUTS})
        v._dev_busy = False
        dj = STATE / "devices.json"
        dj.write_text(json.dumps({"mic": "上一场录的", "speaker": "耳机"}), encoding="utf-8")
        try:
            d = v.devices()                                                   # 系统默认：照旧用录音那边记下的
            self.assertEqual((d["mic"], d["source"]), ("上一场录的", "recorder"))
            mic_prefs.save(CABLE)
            d = v.devices()
            self.assertEqual((d["mic"], d["source"], d["missing"], d["speaker"]), (CABLE, "setting", False, "耳机"))
            self.assertNotIn("inputs", d)
            v._dev = (time.time(), {"mic": REALTEK, "speaker": "扬声器", "inputs": [REALTEK]})
            self.assertTrue(v.devices()["missing"])
            self.write('{"record": {"mic": false}}')
            d = v.devices()
            self.assertEqual((d["mic"], d.get("off")), (None, True))
            mic_prefs.save("")
            dj.unlink()
            d = v.devices()
            self.assertEqual((d["mic"], d["source"]), (REALTEK, "system"))
        finally:
            dj.unlink(missing_ok=True)


SERVER = r"""
import json, os, sys, time, socket, http.client
sys.path.insert(0, sys.argv[1])
from zuhui import share
share.PUBLIC_RELAY = "https://127.0.0.1:1/"
from zuhui.view import Viewer
def fake(self):
    self._dev = (time.time(), {"mic": "Mic A", "speaker": "Spk", "inputs": ["Mic A", "USB Mic B"]})
    self._dev_busy = False
Viewer._probe_devices = fake
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = Viewer(port=port)
v.serve(open_browser=False)
def call(method, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, "/api/mic/device", body=None if body is None else json.dumps(body),
              headers={"Content-Type": "application/json"})
    r = c.getresponse(); return [r.status, json.loads(r.read().decode("utf-8"))]
out = {"get": call("GET"), "set": call("POST", {"mic": "USB Mic B"}), "devices": v.devices()}
v.meetings.current = lambda: {"id": "m", "state": "live"}
out["live"] = call("POST", {"mic": ""})
out["cfg"] = json.loads((__import__("pathlib").Path(os.environ["BLURT_HOME"]) / "config.json").read_text(encoding="utf-8"))
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class MicDeviceHttpTest(unittest.TestCase):
    def test_endpoint(self):
        env = dict(os.environ, ZUHUI_HOME=tempfile.mkdtemp(prefix="zuhui_home_"), BLURT_HOME=tempfile.mkdtemp(prefix="blurt_home_"),
                   PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", SERVER, str(ROOT)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-2000:] + p.stderr[-2000:])
        r = json.loads(line[7:])
        self.assertEqual(r["get"][0], 200)
        self.assertEqual((r["get"][1]["mic"], r["get"][1]["devices"], r["get"][1]["default"]), ("", ["Mic A", "USB Mic B"], "Mic A"))
        self.assertEqual((r["set"][0], r["set"][1]["mic"]), (200, "USB Mic B"))
        self.assertEqual((r["devices"]["mic"], r["devices"]["source"]), ("USB Mic B", "setting"))
        self.assertEqual((r["live"][0], r["live"][1]["error"]), (409, "live"))
        self.assertEqual(r["cfg"], {"record": {"mic": "USB Mic B"}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
