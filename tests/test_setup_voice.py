# -*- coding: utf-8 -*-
"""首次引导和声音：GET/POST /api/setup、GET/POST /api/voice、POST /api/voice/test，论文的字数和目录，
mouth.py 按选的声音来（不出声只记字、只用 Windows 自带时不碰在线语音）。

子进程里起独立的对比页服务（ZUHUI_HOME 指到临时目录、随便一个空端口），不碰正在跑的 Bloom（47330），也不真的出声。

    python -m unittest tests.test_setup_voice -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SERVER = r"""
import json, os, sys, socket, http.client
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from zuhui import voice_prefs, onboard
voice_prefs._SAPI.update(at=9e12, list=[{"name": "Microsoft Huihui Desktop", "lang": "zh-CN", "gender": "female"}], busy=False)
from zuhui.view import Viewer
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
paper = Path(os.environ["ZUHUI_HOME"]) / "paper.md"
paper.write_text("# 引言\n\n这是一篇测试论文 with some English words.\n\n## 方法 **一**\n\n内容。\n\n### 三级不算\n", encoding="utf-8")
v = Viewer(port=port, mirror=paper) if "mirror" in Viewer.__init__.__code__.co_varnames else Viewer(port=port)
v.mirror = paper
said = []
v.voice_test = lambda text: (said.append(text) or "sapi")
v.serve(open_browser=False)
def req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse(); data = r.read()
    try:
        return r.status, json.loads(data.decode("utf-8"))
    except ValueError:
        return r.status, None
out = {}
out["setup0"] = req("GET", "/api/setup")
out["voice0"] = req("GET", "/api/voice")
out["bad_voice"] = req("POST", "/api/voice", {"voice": "robot"})
out["bad_edge"] = req("POST", "/api/voice", {"edge_voice": "xx-YY-Nobody"})
out["bad_sapi"] = req("POST", "/api/voice", {"sapi_voice": "Nobody"})
out["edge"] = req("POST", "/api/voice", {"voice": "edge", "edge_voice": "zh-CN-XiaoxiaoNeural"})
out["cfg_edge"] = v.cfg["mouth"].get("edge_voice")
out["sapi"] = req("POST", "/api/voice", {"voice": "sapi", "sapi_voice": "Microsoft Huihui Desktop"})
out["hot_long"] = req("POST", "/api/voice", {"hotwords": ["x" * 21]})
out["hot_many"] = req("POST", "/api/voice", {"hotwords": [f"词{i}" for i in range(13)]})
out["hot"] = req("POST", "/api/voice", {"hotwords": ["  张老师 ", "", "Transformer", "张老师"]})
from zuhui.config import load
out["hot_cfg"] = (load().get("meeting") or {}).get("hotwords")
out["test"] = req("POST", "/api/voice/test", {"text": "试一下"})
out["said"] = list(said)
out["off"] = req("POST", "/api/voice", {"voice": "off"})
out["test_off"] = req("POST", "/api/voice/test", {})
out["skip"] = req("POST", "/api/setup", {"skipped": True})
out["setup1"] = req("GET", "/api/setup")
out["done"] = req("POST", "/api/setup", {"done": True})
out["reset"] = req("POST", "/api/setup", {"reset": True})
out["paper"] = req("GET", "/api/paper")[1]
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""

MOUTH = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from zuhui import voice_prefs
from zuhui.mouth import Mouth
m = Mouth({"mouth": {}})
m.ffplay = None
calls = []
m._synth_edge = lambda text, out: calls.append(("edge", text)) or None
m._synth_sapi = lambda text, out: calls.append(("sapi", text)) or None
out = {}
voice_prefs.save({"voice": "off"})
got = []
m.say("第一句。第二句。", on_start=got.append)
out["off_said"] = got
out["off_speaking"] = m.speaking
out["off_test"] = m.test("你好")
voice_prefs.save({"voice": "sapi"})
m.edge_tts = "edge-tts.exe"
m._synthesize("一句")
out["sapi_calls"] = list(calls)
calls.clear()
voice_prefs.save({"voice": "edge", "edge_voice": "en-US-GuyNeural"})
m._synthesize("一句")
out["edge_calls"] = list(calls)
out["edge_voice"] = m.edge_voice
out["voice_none"] = m._voice()
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


def _run(code: str, home: str) -> dict:
    env = dict(os.environ, ZUHUI_HOME=home, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", code, str(ROOT)], env=env, capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
    if line is None:
        raise AssertionError(p.stdout[-2000:] + p.stderr[-3000:])
    return json.loads(line[7:])


class SetupVoiceHttpTest(unittest.TestCase):
    def test_endpoints(self):
        home = tempfile.mkdtemp(prefix="zuhui_home_")
        r = _run(SERVER, home)
        self.assertEqual(r["setup0"][0], 200)
        self.assertFalse(r["setup0"][1]["done"])
        self.assertFalse(r["setup0"][1]["skipped"])
        self.assertIn("voice_engine", r["setup0"][1])
        v0 = r["voice0"][1]
        self.assertEqual(v0["voice"], "auto")
        self.assertEqual(v0["hot_max"], 12)
        self.assertTrue(any(e["id"] == "zh-CN-YunxiNeural" for e in v0["edge_voices"]))
        self.assertEqual(r["bad_voice"], [400, {"error": "voice"}])
        self.assertEqual(r["bad_edge"], [400, {"error": "edge_voice"}])
        self.assertEqual(r["bad_sapi"], [400, {"error": "sapi_voice"}])
        self.assertEqual((r["edge"][1]["voice"], r["edge"][1]["edge_voice"]), ("edge", "zh-CN-XiaoxiaoNeural"))
        self.assertEqual(r["cfg_edge"], "zh-CN-XiaoxiaoNeural")              # 后端共用的配置马上换上
        self.assertEqual(r["sapi"][1]["sapi_voice"], "Microsoft Huihui Desktop")
        self.assertEqual(r["hot_long"], [400, {"error": "hot_too_long"}])
        self.assertEqual(r["hot_many"], [400, {"error": "hot_too_many"}])
        self.assertEqual(r["hot"][1]["hotwords"], ["张老师", "Transformer"])   # 去空白、去空行、去重
        self.assertEqual(r["hot_cfg"], ["张老师", "Transformer"])              # config.load() 叠上去（lexicon 读它）
        self.assertEqual(r["test"], [200, {"ok": True, "engine": "sapi"}])
        self.assertEqual(r["said"], ["试一下"])
        self.assertEqual(r["test_off"], [200, {"ok": False, "error": "off"}])
        self.assertTrue(r["skip"][1]["skipped"])
        self.assertTrue(r["setup1"][1]["skipped"])
        self.assertTrue(r["done"][1]["done"])
        self.assertEqual((r["reset"][1]["done"], r["reset"][1]["skipped"]), (False, False))
        self.assertEqual(r["paper"]["outline"], ["引言", "方法 一"])           # 一二级标题，去掉粗体记号
        self.assertGreater(r["paper"]["words"], 10)


class MouthPrefTest(unittest.TestCase):
    def test_follows_choice(self):
        home = tempfile.mkdtemp(prefix="zuhui_home_")
        r = _run(MOUTH, home)
        self.assertEqual(r["off_said"], ["第一句。", "第二句。"])     # 不出声：照样进逐字稿
        self.assertFalse(r["off_speaking"])
        self.assertIsNone(r["off_test"])
        self.assertEqual([c[0] for c in r["sapi_calls"]], ["sapi"])   # 只用 Windows 自带：不碰在线语音
        self.assertEqual([c[0] for c in r["edge_calls"]], ["edge", "sapi"])   # 在线没念成，退回 Windows 自带
        self.assertEqual(r["edge_voice"], "en-US-GuyNeural")
        self.assertEqual(r["voice_none"], {})


if __name__ == "__main__":
    unittest.main()
