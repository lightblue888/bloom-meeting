# -*- coding: utf-8 -*-
"""模型来源（设置 → 通用 → 模型来源）：存、校验、起 claude 时带的环境变量和型号、界面上的名字、/api/provider。
不连任何服务商（不花钱）：ZUHUI_HOME 指到临时目录，HTTP 部分在子进程里起一个独立的对比页服务。

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

UNIT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from zuhui import provider, config
out = {}
out["default"] = [provider.active(), config.assistant_name(config.load()), provider.map_args(["--model", "claude-haiku-5-5"])]
provider.save(provider.clean({"kind": "deepseek", "key": "sk-test-0000-1234"}))
env = provider.apply_env({"ANTHROPIC_API_KEY": "old", "PATH": "x"})
out["env"] = {k: env.get(k) for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                                       "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "PATH")}
out["args"] = [provider.map_args(["-p", "--model", "claude-haiku-5-5"]), provider.map_args(["--model", "claude-opus-5-5", "--effort", "max"])]
out["names"] = [config.assistant_name(config.load()), config.model_label("dialog", "claude-haiku-5-5"), config.model_label("edit", "claude-opus-5-5"),
                config.model_label("effort", "max")]
pub = provider.public()
out["public"] = {k: pub[k] for k in ("kind", "key_set", "key_tail", "active", "label", "base_url")}
out["leak"] = "sk-test-0000-1234" in json.dumps(pub)
out["keep_key"] = provider.clean({"kind": "deepseek", "key": ""})["key"] == "sk-test-0000-1234"      # 不重填密钥 = 沿用
out["facts"] = provider.facts()[:40]
provider.save(provider.clean({"kind": "anthropic", "key": "sk-ant-test-5678"}))
env = provider.apply_env({})
out["anthropic"] = [env.get("ANTHROPIC_API_KEY"), env.get("ANTHROPIC_BASE_URL"), provider.map_args(["--model", "claude-opus-5-5"]),
                    config.assistant_name(config.load()), provider.facts()]
bad = []
for b in ({"kind": "custom", "base_url": "https://x.com/v1", "key": "k-12345678", "fast": "a", "main": "b"},
          {"kind": "custom", "base_url": "ftp://x.com", "key": "k-12345678", "fast": "a", "main": "b"},
          {"kind": "custom", "base_url": "https://x.com", "key": "k 1234", "fast": "a", "main": "b"},
          {"kind": "custom", "base_url": "https://x.com", "key": "k-12345678", "fast": "a;rm", "main": "b"},
          {"kind": "nope"}):
    try:
        provider.clean(b); bad.append("accepted")
    except ValueError as e:
        bad.append(str(e))
out["bad"] = bad
provider.save(provider.clean({"kind": "claude"}))
out["back"] = [provider.active(), provider.map_args(["--model", "claude-haiku-5-5"])]
print("RESULT " + json.dumps(out, ensure_ascii=False))
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
st, d = req("POST", "/api/provider", {"kind": "kimi", "key": "sk-kimi-test-9999"})
out["save"] = [st, d["kind"], d["key_tail"], d["active"], "sk-kimi-test-9999" in json.dumps(d), d["fast"]]
out["hits"] = len(hits)
out["bad"] = req("POST", "/api/provider", {"kind": "custom", "base_url": "https://x.com/v1", "key": "k-12345678", "fast": "a", "main": "b"})
out["evil"] = req("POST", "/api/provider", {"kind": "claude"}, origin="https://evil.example")[0]
out["still"] = req("GET", "/api/provider")[1]["kind"]
out["people"] = [p["name"] for p in v.participants(guests=False)]
out["models"] = (v.models_get() if callable(getattr(v, "models_get", None)) else None)
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
    def test_store_env_models(self):
        r = run(UNIT)
        self.assertEqual(r["default"], [None, "Claude", ["--model", "claude-haiku-5-5"]])     # 没设：一切照旧
        e = r["env"]
        self.assertEqual(e["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(e["ANTHROPIC_AUTH_TOKEN"], "sk-test-0000-1234")
        self.assertIsNone(e["ANTHROPIC_API_KEY"])                                              # 别家用 AUTH_TOKEN，旧的 API_KEY 去掉
        self.assertEqual((e["ANTHROPIC_DEFAULT_HAIKU_MODEL"], e["ANTHROPIC_DEFAULT_OPUS_MODEL"]), ("deepseek-flash", "deepseek-flash[1m]"))
        self.assertEqual(e["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertEqual(e["PATH"], "x")
        self.assertEqual(r["args"], [["-p", "--model", "deepseek-flash"], ["--model", "deepseek-flash[1m]", "--effort", "max"]])
        self.assertEqual(r["names"], ["DeepSeek", "deepseek-flash", "deepseek-flash[1m]", "Max"])  # AI 默认叫 DeepSeek
        self.assertEqual(r["public"], {"kind": "deepseek", "key_set": True, "key_tail": "1234", "active": True,
                                       "label": "DeepSeek", "base_url": "https://api.deepseek.com/anthropic"})
        self.assertFalse(r["leak"])                                                          # 密钥不回给界面
        self.assertTrue(r["keep_key"])
        self.assertTrue(r["facts"].strip().startswith("【模型来源】"))
        self.assertEqual(r["anthropic"], ["sk-ant-test-5678", None, ["--model", "claude-opus-5-5"], "Claude", ""])
        self.assertEqual(r["bad"], ["base_url", "base_url", "key", "model", "kind"])
        self.assertEqual(r["back"], [None, ["--model", "claude-haiku-5-5"]])

    def test_http(self):
        r = run(HTTP)
        self.assertEqual(r["get0"], "claude")
        self.assertEqual(r["save"], [200, "kimi", "9999", True, False, "kimi-k3"])
        self.assertEqual(r["hits"], 1)
        self.assertEqual(r["bad"], [400, {"error": "base_url"}])
        self.assertEqual(r["evil"], 403)                                                     # 别的网站改不了来源
        self.assertEqual(r["still"], "kimi")
        self.assertEqual(r["people"][1], "Kimi")                                              # 参会人里 AI 跟着叫 Kimi


if __name__ == "__main__":
    unittest.main(verbosity=2)
