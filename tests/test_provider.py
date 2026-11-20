# -*- coding: utf-8 -*-
"""模型来源（设置 → 通用 → 模型来源）：三种状态、存和校验、直接调 API（会上对话流式、纪要一次性）、/api/provider。
不连任何服务商（不花钱）：ZUHUI_HOME 指到临时目录，接口是测试里起的一个假的 Anthropic 兼容服务，HTTP 部分在子进程里起对比页服务。

    python -m unittest tests.test_provider -v
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

FAKE = r"""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
SEEN = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        SEEN.append({"path": self.path, "model": body.get("model"), "stream": bool(body.get("stream")),
                     "auth": self.headers.get("Authorization") or "", "xkey": self.headers.get("x-api-key") or "",
                     "roles": [m["role"] for m in body.get("messages", [])], "system": body.get("system", "")})
        if body.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for piece in ("你", "好"):
                d = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}}
                self.wfile.write(("event: content_block_delta\ndata: " + json.dumps(d) + "\n\n").encode())
            self.wfile.write(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
            return
        b = json.dumps({"type": "message", "content": [{"type": "text", "text": "好"}], "stop_reason": "end_turn"}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
"""

UNIT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
""" + FAKE + r"""
from zuhui import provider, config, llm_api, claude_text
out = {}
out["fresh"] = [provider.load()["kind"], provider.mode(), config.assistant_name(config.load())]
try:
    claude_text.ask("hi"); out["fresh_ask"] = "answered"
except RuntimeError as e:
    out["fresh_ask"] = str(e)
provider.save(provider.clean({"kind": "claude"}))
out["claude"] = [provider.mode(), config.assistant_name(config.load()), provider.facts()]
provider.save(provider.clean({"kind": "deepseek", "key": "sk-test-0000-1234"}))
out["deepseek"] = [provider.mode(), config.assistant_name(config.load()), llm_api.model("fast"), llm_api.model("main"),
                   config.model_label("dialog", "claude-haiku-5-5"), config.model_label("effort", "max")]
pub = provider.public()
out["public"] = {k: pub[k] for k in ("kind", "key_set", "key_tail", "active", "label", "base_url")}
out["leak"] = "sk-test-0000-1234" in json.dumps(pub)
out["keep_key"] = provider.clean({"kind": "deepseek", "key": ""})["key"] == "sk-test-0000-1234"
out["facts"] = provider.facts().strip()[:6]
provider.save(provider.clean({"kind": "custom", "base_url": "http://127.0.0.1:%d" % PORT, "key": "sk-local-5678",
                              "fast": "f-model", "main": "m-model[1m]"}))
out["complete"] = llm_api.complete("sys", [{"role": "assistant", "content": "开场白"}, {"role": "user", "content": "hi"}], tier="main")
out["stream"] = "".join(llm_api.stream("sys", [{"role": "user", "content": "hi"}], tier="fast"))
out["ask_fast"] = claude_text.ask("hi", model=claude_text.HAIKU)
out["ask_main"] = claude_text.ask("hi", model=claude_text.SONNET)
from zuhui.fast import FastBrain
fb = object.__new__(FastBrain); fb.last_error = None
out["dialog"] = [fb.is_cli, "".join(fb._deltas([{"role": "system", "content": "你是助手"}, {"role": "user", "content": "你好"}]))]
out["seen"] = SEEN
provider.save(provider.clean({"kind": "anthropic", "key": "sk-ant-test-9999"}))
out["anthropic"] = [provider.mode(), llm_api.model("fast"), llm_api.model("main"), config.assistant_name(config.load())]
bad = []
for b in ({"kind": "custom", "base_url": "https://x.com/v1", "key": "k-12345678", "fast": "a", "main": "b"},
          {"kind": "custom", "base_url": "ftp://x.com", "key": "k-12345678", "fast": "a", "main": "b"},
          {"kind": "custom", "base_url": "https://x.com", "key": "k 1234", "fast": "a", "main": "b"},
          {"kind": "custom", "base_url": "https://x.com", "key": "k-12345678", "fast": "a;rm", "main": "b"},
          {"kind": "unset"}, {"kind": "nope"}):
    try:
        provider.clean(b); bad.append("accepted")
    except ValueError as e:
        bad.append(str(e))
out["bad"] = bad
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
"""

HTTP = r"""
import json, os, sys, socket, http.client
sys.path.insert(0, sys.argv[1])
from zuhui.view import Viewer
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = Viewer(port=port)
hits = []
v.on_provider = lambda: hits.append(1)
v.serve(open_browser=False)
def req(method, path, body=None, origin=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json"}
    if origin:
        h["Origin"] = origin
    c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
    r = c.getresponse(); d = r.read()
    try:
        return r.status, json.loads(d.decode("utf-8"))
    except ValueError:
        return r.status, None
out = {}
out["get0"] = req("GET", "/api/provider")[1]["kind"]
out["people0"] = [p["name"] for p in v.participants(guests=False)]
st, d = req("POST", "/api/provider", {"kind": "kimi", "key": "sk-kimi-test-9999"})
out["save"] = [st, d["kind"], d["key_tail"], d["active"], "sk-kimi-test-9999" in json.dumps(d), d["fast"]]
out["hits"] = len(hits)
out["bad"] = req("POST", "/api/provider", {"kind": "custom", "base_url": "https://x.com/v1", "key": "k-12345678", "fast": "a", "main": "b"})
out["evil"] = req("POST", "/api/provider", {"kind": "claude"}, origin="https://evil.example")[0]
out["still"] = req("GET", "/api/provider")[1]["kind"]
out["people"] = [p["name"] for p in v.participants(guests=False)]
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


def run(script):
    home = tempfile.mkdtemp(prefix="zuhui_home_")
    env = dict(os.environ, ZUHUI_HOME=home, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", script, str(ROOT)], env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
    if line is None:
        raise AssertionError(p.stdout[-2000:] + p.stderr[-2000:])
    return json.loads(line[7:])


class ProviderTest(unittest.TestCase):
    def test_modes_and_api(self):
        r = run(UNIT)
        self.assertEqual(r["fresh"], ["unset", "none", "AI"])                       # 新装：没设，AI 不默认叫 Claude
        self.assertIn("未设置模型来源", r["fresh_ask"])
        self.assertEqual(r["claude"], ["claude", "Claude", ""])                      # Claude Code 账号：一切照旧
        self.assertEqual(r["deepseek"], ["api", "DeepSeek", "deepseek-flash", "deepseek-flash", "DeepSeek Flash", "Max"])
        self.assertEqual(r["public"], {"kind": "deepseek", "key_set": True, "key_tail": "1234", "active": True,
                                       "label": "DeepSeek", "base_url": "https://api.deepseek.com/anthropic"})
        self.assertFalse(r["leak"])                                                  # 密钥不回给界面
        self.assertTrue(r["keep_key"])                                               # 不重填密钥 = 沿用
        self.assertEqual(r["facts"], "【模型来源】")
        self.assertEqual((r["complete"], r["stream"], r["ask_fast"], r["ask_main"]), ("好", "你好", "好", "好"))
        self.assertEqual(r["dialog"], [False, "你好"])                               # 会上对话：直接流式调接口
        seen = r["seen"]
        self.assertEqual([x["model"] for x in seen], ["m-model", "f-model", "f-model", "m-model", "f-model"])   # 两档型号；[1m] 去掉
        self.assertEqual([x["stream"] for x in seen], [False, True, False, False, True])
        self.assertTrue(all(x["auth"] == "Bearer sk-local-5678" and not x["xkey"] for x in seen))
        self.assertEqual(seen[0]["roles"], ["user", "assistant", "user"])            # 开头补一句 user，交替
        self.assertEqual(seen[0]["system"], "sys")
        self.assertEqual(r["anthropic"], ["api", "claude-haiku-5-5", "claude-opus-5-5", "Claude"])
        self.assertEqual(r["bad"], ["base_url", "base_url", "key", "model", "kind", "kind"])

    def test_http(self):
        r = run(HTTP)
        self.assertEqual(r["get0"], "unset")
        self.assertEqual(r["people0"][1], "AI")
        self.assertEqual(r["save"], [200, "kimi", "9999", True, False, "kimi-k3"])
        self.assertEqual(r["hits"], 1)
        self.assertEqual(r["bad"], [400, {"error": "base_url"}])
        self.assertEqual(r["evil"], 403)                                             # 别的网站改不了来源
        self.assertEqual(r["still"], "kimi")
        self.assertEqual(r["people"][1], "Kimi")                                      # 参会人里 AI 跟着叫 Kimi


if __name__ == "__main__":
    unittest.main(verbosity=2)
