# -*- coding: utf-8 -*-
"""会议中继的连接码：拆开、存好（livekit.env + relay.json）、叠到 share.base_url、移除；
GET/POST /api/relay、POST /api/relay/test。子进程里起独立的对比页服务（ZUHUI_HOME 指到临时目录），不碰正在跑的 Bloom。

    python -m unittest tests.test_relay_cfg -v
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
sys.path.insert(0, sys.argv[1])
from zuhui import relay_cfg, share
share.PUBLIC_RELAY = "https://127.0.0.1:1/"        # 测试里不碰真的公共中继
relay_cfg.set_mode("off")                          # 从"关闭"开始：不看开发机 config.json 里写的中继
from zuhui.view import Viewer
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = Viewer(port=port)
v.serve(open_browser=False)
def req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse(); data = r.read()
    return r.status, json.loads(data.decode("utf-8"))
out = {}
from zuhui.config import load
fb = str((load().get("share") or {}).get("base_url") or "")
out["fallback"] = fb + ("/" if fb and not fb.endswith("/") else "")      # config.json 里手动配的（开发机上有）
out["start"] = req("GET", "/api/relay")
out["test_unset"] = req("POST", "/api/relay/test", {})
out["bad"] = req("POST", "/api/relay", {"code": "hello"})
code = relay_cfg.make("https://127.0.0.1:1/", "APIabcdef123456", "s" * 40)
out["save"] = req("POST", "/api/relay", {"code": "  " + code + "\n"})
out["share_base"] = v.share.base
out["invite"] = v.share.invite()
out["env"] = (relay_cfg.ENV_FILE.read_text(encoding="utf-8"))
out["test_down"] = req("POST", "/api/relay/test", {})
out["clear"] = req("POST", "/api/relay", {"clear": True})
out["after"] = [relay_cfg.ENV_FILE.exists(), relay_cfg.RELAY_FILE.exists(), v.share.base]
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class RelayCodeTest(unittest.TestCase):
    def test_parse(self):
        env = dict(os.environ, ZUHUI_HOME=tempfile.mkdtemp(prefix="zuhui_home_"))
        code = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
from zuhui import relay_cfg
ok = relay_cfg.parse(relay_cfg.make("https://meet.example.com", "APIx1y2z3", "a" * 32))
bad = []
for c in ["", "bloom1.", "bloom1.@@@", relay_cfg.make("http://meet.example.com/", "APIx1y2z3", "a" * 32),
          relay_cfg.make("https://meet.example.com/", "k", "a" * 32), relay_cfg.make("https://x.com/", "APIx1y2z3", "short")]:
    try:
        relay_cfg.parse(c); bad.append("accepted")
    except ValueError as e:
        bad.append(str(e))
relay_cfg.save(relay_cfg.make("https://1-2-3-4.sslip.io/", "APIabcdef123456", "b" * 40))
from zuhui.config import load
print("RESULT " + json.dumps({"ok": ok, "bad": bad, "base": load()["share"]["base_url"], "pub": relay_cfg.public(load())}))
"""
        p = subprocess.run([sys.executable, "-c", code, str(ROOT)], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        self.assertEqual(r["ok"], {"url": "https://meet.example.com/", "ws": "wss://meet.example.com", "key": "APIx1y2z3", "secret": "a" * 32})
        self.assertEqual(r["bad"], ["code"] * 6)                      # 空的、截断的、乱码、http、key 太短、secret 太短
        self.assertEqual(r["base"], "https://1-2-3-4.sslip.io/")      # config.load() 叠上去
        self.assertEqual(r["pub"], {"mode": "own", "configured": True, "host": "1-2-3-4.sslip.io", "key_tail": "3456", "managed": True,
                                    "invite_on": True})

    def test_http(self):
        env = dict(os.environ, ZUHUI_HOME=tempfile.mkdtemp(prefix="zuhui_home_"), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", SERVER, str(ROOT)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        self.assertEqual((r["start"][1]["mode"], r["start"][1]["configured"]), ("off", False))
        self.assertEqual(r["test_unset"][1], {"ok": False, "error": "unset"})
        self.assertEqual(r["bad"], [400, {"error": "code"}])
        self.assertEqual(r["save"][0], 200)
        self.assertTrue(r["save"][1]["configured"])
        self.assertEqual(r["share_base"], "https://127.0.0.1:1/")      # 不用重启：邀请马上换地址
        self.assertTrue(r["invite"]["ok"])
        self.assertTrue(r["invite"]["relay"])
        self.assertIn("LIVEKIT_URL=wss://127.0.0.1:1", r["env"])
        self.assertEqual(r["test_down"][1], {"ok": False, "error": "unreachable"})
        self.assertEqual(r["after"], [False, True, "https://127.0.0.1:1/"])   # 设置里粘的密钥删掉，回到公共中继
        self.assertEqual((r["clear"][1]["mode"], r["clear"][1]["invite_on"]), ("public", False))


if __name__ == "__main__":
    unittest.main()
