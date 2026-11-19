# -*- coding: utf-8 -*-
"""本机后端（127.0.0.1:47330）的安全检查，都在沙盒里（ZUHUI_HOME 指到临时目录、随便一个空端口），不碰正在跑的 Bloom：

- DNS 重绑定：Host 不是 127.0.0.1 / localhost / [::1] 的一律 403（别的网站把自己的域名解析到 127.0.0.1 就能读会议）。
- 别的网站用 <img>、链接发的 GET（不带 Origin，浏览器标 Sec-Fetch-Site: cross-site）不能叫 Bloom 开始录屏、重设原文。
- 论文页、批注卡片里的转义连引号一起转（访客写的批注引文、名字拼在 title="…" 里）。
- 公式不出 \\href / \\style 这些（访客传的论文里 \\href{javascript:…} 点一下就在本机后端的页面里跑脚本）。
- 两个 TTS 服务不认浏览器发来的请求（POST /say 能往任意路径写 wav）。

    python -m unittest tests.test_security_local -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import http.client
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SERVER = r"""
import json, os, sys, socket, http.client
sys.path.insert(0, sys.argv[1])
import zuhui.view as V
calls = []
V._deep_link = lambda url: (calls.append(url), True)[1]        # 不真的去叫 Bloom 录屏
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = V.Viewer(port=port)
v.serve(open_browser=False)
def req(method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse(); r.read()
    return r.status
out = {}
out["host_ok"] = req("GET", "/api/current", {"Host": "127.0.0.1:%d" % port})
out["host_localhost"] = req("GET", "/api/current", {"Host": "localhost:%d" % port})
out["host_v6"] = req("GET", "/version", {"Host": "[::1]:%d" % port})
out["rebind_get"] = req("GET", "/api/current", {"Host": "evil.example:%d" % port})
out["rebind_head"] = req("HEAD", "/api/meetings/x/video", {"Host": "evil.example:%d" % port})
out["rebind_post"] = req("POST", "/api/relay", {"Host": "evil.example:%d" % port, "Content-Type": "application/json"}, b'{"invite_on": true}')
img = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}
out["img_record"] = req("GET", "/record", img)
out["img_rebase"] = req("GET", "/rebase", img)
out["samesite_record"] = req("GET", "/record", {"Sec-Fetch-Site": "same-site"})
out["calls_after_evil"] = list(calls)
out["page_record"] = req("GET", "/record", {"Sec-Fetch-Site": "same-origin"})    # 论文页自己的"开始录屏"
out["app_rebuild"] = req("GET", "/rebuild", {"Origin": "http://tauri.localhost", "Sec-Fetch-Site": "cross-site"})
out["script_rebuild"] = req("GET", "/rebuild")                                     # 本机脚本：不带这些头
out["evil_post_noorigin"] = req("POST", "/api/relay", {"Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"}, b'{"invite_on": true}')
out["calls"] = list(calls)
print("RESULT " + json.dumps(out), flush=True)
os._exit(0)
"""


class LocalHttpGuardTest(unittest.TestCase):
    def test_host_and_cross_site_get(self):
        home = tempfile.mkdtemp(prefix="zuhui_sec_home_")
        env = dict(os.environ, ZUHUI_HOME=home, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", SERVER, str(ROOT)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-2000:] + p.stderr[-2000:])
        r = json.loads(line[7:])
        self.assertEqual((r["host_ok"], r["host_localhost"], r["host_v6"]), (200, 200, 200))
        self.assertEqual((r["rebind_get"], r["rebind_head"], r["rebind_post"]), (403, 403, 403))
        self.assertEqual((r["img_record"], r["img_rebase"], r["samesite_record"]), (403, 403, 403))
        self.assertEqual(r["calls_after_evil"], [])                    # 别的网站叫不动录屏
        self.assertEqual((r["page_record"], r["app_rebuild"], r["script_rebuild"]), (200, 200, 200))
        self.assertEqual(r["evil_post_noorigin"], 403)
        self.assertEqual(r["calls"], ["blurt://meeting"])


def _node():
    return shutil.which("node")


class PageEscapeTest(unittest.TestCase):
    """论文页注入的脚本：esc() 连引号一起转；公式配置去掉 \\href 这些。"""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="zuhui_sec_home_")
        os.environ.setdefault("ZUHUI_HOME", cls.home)
        sys.path.insert(0, str(ROOT))

    def _scripts(self):
        from zuhui import comments, export
        from zuhui.view import _inject
        return {"view": _inject(1, [], "me"), "comments": comments.inject("zh", False), "export": export._FIG_JS}

    def test_esc_escapes_quotes(self):
        node = _node()
        if not node:
            self.skipTest("no node")
        payload = '" onmouseover="alert(1)" x=\'<b>&'
        for name, js in self._scripts().items():
            if name == "export":
                continue
            m = re.search(r"function esc\(s\)\{.*?\}\s*\n", js)
            self.assertIsNotNone(m, name)
            prog = m.group(0) + "process.stdout.write(esc(" + json.dumps(payload) + "));"
            out = subprocess.run([node, "-e", prog], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
            self.assertNotIn('"', out, name)
            self.assertNotIn("'", out, name)
            self.assertNotIn("<", out, name)
            self.assertIn("&quot;", out, name)

    def test_mathjax_has_no_href_or_style(self):
        for name, js in self._scripts().items():
            if "window.MathJax" not in js.replace(" ", ""):
                continue
            flat = js.replace(" ", "")
            self.assertIn("packages:{'[-]':['html','require']}", flat, name)
            for mac in ("href", "class", "style", "cssId", "data"):
                self.assertIn(f"{mac}:['{{#2}}',2]", flat, (name, mac))


class GuestUploadBacklogTest(unittest.TestCase):
    """访客连着传论文：排着导入的到上限就不再去中继取（每份最多 50 MB，堆在内存和磁盘上）。"""

    def test_pending_cap(self):
        sys.path.insert(0, str(ROOT))
        os.environ.setdefault("ZUHUI_HOME", tempfile.mkdtemp(prefix="zuhui_sec_home_"))
        import types
        import zuhui.view as V
        fetched, posted = [], []
        fake = types.SimpleNamespace(meetings=types.SimpleNamespace(current=lambda: {"id": "sec-test-mid"}),
                                     GUEST_UPLOADS_PENDING=2, on_log=None,
                                     _local_post=lambda path, body, timeout=60: posted.append(body) or {"ok": True})
        act = {"id": "a1", "by": "访客", "path": "upload", "body": {"upload": "u1"}}
        V._GUEST_UP[0] = 2
        try:
            r = V.Viewer.guest_act(fake, act, lambda uid: fetched.append(uid) or ("p.md", b"# x\n"))
        finally:
            V._GUEST_UP[0] = 0
        self.assertFalse(r.get("ok"))
        self.assertEqual(fetched, [])
        r = V.Viewer.guest_act(fake, act, lambda uid: fetched.append(uid) or ("p.md", b"# x\n"))
        self.assertTrue(r.get("ok"))
        self.assertEqual(fetched, ["u1"])
        self.assertEqual(V._GUEST_UP[0], 0)


class TtsServerGuardTest(unittest.TestCase):
    """TTS 服务（IndexTTS2 / GPT-SoVITS 适配）：浏览器发来的（带 Origin、Sec-Fetch-*，或 Host 不是本机）一律 403。"""

    def _serve(self, mod):
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        mod.State.server = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _req(self, port, method, path, headers=None, body=b"{}"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=body if method == "POST" else None, headers=headers or {})
        r = c.getresponse()
        r.read()
        return r.status

    def test_browser_requests_refused(self):
        sys.path.insert(0, str(ROOT))
        os.environ.setdefault("ZUHUI_HOME", tempfile.mkdtemp(prefix="zuhui_sec_home_"))
        from zuhui import tts_gsv, tts_server
        for mod in (tts_gsv, tts_server):
            port = self._serve(mod)
            evil = Path(tempfile.mkdtemp(prefix="zuhui_sec_tts_")) / "x" / "evil.wav"
            body = json.dumps({"text": "hi", "wav": str(evil), "play": False}).encode()
            self.assertEqual(self._req(port, "POST", "/say", {"Origin": "https://evil.example", "Content-Type": "text/plain"}, body), 403, mod.__name__)
            self.assertEqual(self._req(port, "POST", "/say", {"Sec-Fetch-Site": "cross-site"}, body), 403, mod.__name__)
            self.assertEqual(self._req(port, "POST", "/quit", {"Host": f"evil.example:{port}"}), 403, mod.__name__)
            self.assertEqual(self._req(port, "GET", "/health", {"Host": f"evil.example:{port}"}), 403, mod.__name__)
            self.assertFalse(evil.parent.exists(), mod.__name__)                      # 没去建目录、写文件
            self.assertNotEqual(self._req(port, "GET", "/health"), 403, mod.__name__)  # 本机程序照常


if __name__ == "__main__":
    unittest.main(verbosity=2)
