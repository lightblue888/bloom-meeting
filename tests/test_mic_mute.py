# -*- coding: utf-8 -*-
"""主持人麦克风静音：POST /api/mic/mute、/api/mic/stream 推的格式、Ears.set_muted 的状态（存盘、散会清掉）。

HTTP 那部分在子进程里起一个独立的对比页服务（ZUHUI_HOME 指到临时目录、随便一个空端口），不碰正在跑的 Bloom（47330）。

    python -m unittest tests.test_mic_mute -v
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class MicStateTest(unittest.TestCase):
    def setUp(self):
        from zuhui import ears
        self.ears = ears
        self.path = Path(tempfile.mkdtemp()) / "mic_mute.json"
        self.old = ears.MIC
        ears.MIC = ears._MicState(self.path)

    def tearDown(self):
        self.ears.MIC = self.old

    def _ears(self):
        e = object.__new__(self.ears.Ears)     # 不起线程、不起快识别，只要静音这几个方法
        e._q, e.en, e.vad = queue.Queue(), False, None
        return e

    def test_set_query_persist(self):
        e = self._ears()
        self.assertFalse(e.set_muted(None))
        self.assertTrue(e.set_muted(True))
        self.assertTrue(e.set_muted(None))
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["muted"])
        self.assertTrue(self.ears._MicState(self.path).muted)          # 后端重启：还是静音
        kind, payload = e._q.get_nowait()
        self.assertEqual((kind, payload["kind"]), ("log", "mic"))
        self.assertEqual(e.mic_state(), {"level": None, "muted": True})
        self.assertFalse(e.set_muted(False))
        self.assertFalse(self.path.exists())
        self.assertFalse(self.ears._MicState(self.path).muted)

    def test_overlap_spans(self):
        m = self.ears.MIC
        t0 = time.time()
        m.set(True)
        time.sleep(0.3)
        m.set(False)
        t1 = time.time()
        self.assertGreater(m.overlap(t0, t1), 0.8)                       # 静音那一段里说的
        self.assertEqual(m.overlap(t1 + 1, t1 + 2), 0.0)                  # 打开以后说的
        self.assertEqual(m.overlap(t0 - 5, t0 - 1), 0.0)                  # 静音之前说的（晚到的转写）照收

    def test_stale_session_reset(self):
        m = self.ears.MIC
        m.session = "C:/old/session"
        m.set(True)
        m2 = self.ears._MicState(self.path)
        self.assertTrue(m2.muted)
        self.assertEqual(m2.session, "C:/old/session")                   # Ears._run 开新一场时据此清掉


SERVER = r"""
import json, os, queue, sys, threading, time, socket, http.client
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from zuhui import ears
ears.MIC = ears._MicState(Path(os.environ["ZUHUI_HOME"]) / "mic_mute.json")
from zuhui.view import Viewer
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = Viewer(port=port)
v.serve(open_browser=False)
out = {}
def post(body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/mic/mute", body=json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse(); return r.status, json.loads(r.read().decode("utf-8"))
out["unwired"] = post({"muted": True})
e = object.__new__(ears.Ears); e._q, e.en, e.vad = queue.Queue(), False, None
v.mic_mute = lambda m=None: e.set_muted(m)
level = {"v": 0.37}
v.mic_level = lambda: level["v"]
sc = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
sc.request("GET", "/api/mic/stream"); sr = sc.getresponse()
def event():
    while True:
        line = sr.fp.readline().decode("utf-8").strip()
        if line.startswith("data: "):
            return line[6:]
out["ctype"] = sr.getheader("Content-Type")
out["ev0"] = event()
out["mute_on"] = post({"muted": True})
out["ev1"] = event()
out["bad"] = post({"muted": "maybe"})
out["str_false"] = post({"muted": "false"})
out["ev2"] = event()
level["v"] = None
out["ev3"] = event()
out["mute_on2"] = post({"muted": True})
out["persist"] = json.loads((Path(os.environ["ZUHUI_HOME"]) / "mic_mute.json").read_text(encoding="utf-8"))
out["mute_off"] = post({"muted": False})
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class MuteHttpTest(unittest.TestCase):
    def test_endpoint_and_stream(self):
        home = tempfile.mkdtemp(prefix="zuhui_home_")
        env = dict(os.environ, ZUHUI_HOME=home, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", SERVER, str(ROOT)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-2000:] + p.stderr[-2000:])
        r = json.loads(line[7:])
        self.assertEqual(r["unwired"][0], 503)
        self.assertTrue(r["ctype"].startswith("text/event-stream"))
        self.assertEqual(json.loads(r["ev0"]), {"level": 0.35, "muted": False})   # 取到 0.05
        self.assertEqual(r["mute_on"], [200, {"muted": True}])
        self.assertEqual(json.loads(r["ev1"]), {"level": 0.35, "muted": True})
        self.assertEqual(r["bad"][0], 400)
        self.assertEqual(r["str_false"], [200, {"muted": False}])
        self.assertEqual(json.loads(r["ev2"]), {"level": 0.35, "muted": False})
        self.assertEqual(json.loads(r["ev3"]), {"level": None, "muted": False})   # 没在录音：level 是 null
        self.assertEqual(r["mute_on2"], [200, {"muted": True}])
        self.assertTrue(r["persist"]["muted"])
        self.assertEqual(r["mute_off"], [200, {"muted": False}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
