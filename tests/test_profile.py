# -*- coding: utf-8 -*-
"""设置里改昵称、头像、AI 的名字：GET/POST /api/profile、POST /api/profile/avatar、侧栏的 /avatars/me。

在子进程里起一个独立的对比页服务（ZUHUI_HOME 指到临时目录、随便一个空端口），不碰正在跑的 Bloom（47330）。

    python -m unittest tests.test_profile -v
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
import base64, io, json, os, sys, socket, http.client
sys.path.insert(0, sys.argv[1])
from PIL import Image
from zuhui.view import Viewer
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = Viewer(port=port)
named = []
v.on_profile = lambda me, ai: named.append([me, ai])
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
out["start"] = req("GET", "/api/profile")
out["rail0"] = req("GET", "/avatars/me")[0]
out["empty"] = req("POST", "/api/profile", {"name": "  "})
out["taken"] = req("POST", "/api/profile", {"name": "AI"})              # 和 AI 重名（新装没设模型来源时 AI 就叫 AI）
out["long"] = req("POST", "/api/profile", {"name": "x" * 25})
out["rename"] = req("POST", "/api/profile", {"name": " 小王\n同学 "})
out["named"] = list(named)
out["ai"] = req("POST", "/api/profile", {"assistant": " DeepSeek "})
out["ai_taken"] = req("POST", "/api/profile", {"assistant": "小王 同学"})
out["people"] = [p["name"] for p in v.participants(guests=False)]
out["ai_back"] = req("POST", "/api/profile", {"assistant": ""})
out["named2"] = list(named)
out["me_name"] = v.me_name()
buf = io.BytesIO(); Image.new("RGB", (900, 600), (10, 120, 200)).save(buf, "JPEG")
out["bad_img"] = req("POST", "/api/profile/avatar", {"data": "data:image/png;base64,aGVsbG8="})
out["pic"] = req("POST", "/api/profile/avatar", {"data": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()})
out["rail1"] = req("GET", "/avatars/me")[0]
out["size"] = Image.open(Path(os.environ["ZUHUI_HOME"]) / "avatars" / "me.png").size
out["remove"] = req("POST", "/api/profile/avatar", {"remove": True})
out["rail2"] = req("GET", "/avatars/me")[0]
out["saved"] = json.loads((Path(os.environ["ZUHUI_HOME"]) / "profile.json").read_text(encoding="utf-8"))
out["color"] = req("POST", "/api/profile", {"color": "#4ADE80"})
out["ai_color"] = req("POST", "/api/profile", {"ai_color": "#f472b6"})
out["bad_color"] = req("POST", "/api/profile", {"color": "red"})
out["colors_people"] = [p["color"] for p in v.participants(guests=False)]
out["guest_pal"] = v.guest_colors(["访客一", "访客二"])
def from_site(origin, method="POST", path="/api/profile", body=None, ctype="text/plain"):
    # 别的网站发来的：text/plain 的 POST 浏览器不预检，后端自己得拦
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={"Origin": origin, "Content-Type": ctype})
    r = c.getresponse(); r.read()
    return r.status, r.getheader("Access-Control-Allow-Origin")
out["evil_post"] = from_site("https://evil.example", body={"name": "被改了"})
out["evil_rebase"] = from_site("https://evil.example", "GET", "/rebase")
out["after_evil"] = req("GET", "/api/profile")[1]["name"]
out["app_get"] = from_site("http://tauri.localhost", "GET", "/api/profile")
out["evil_get"] = from_site("https://evil.example", "GET", "/api/profile")
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class ProfileHttpTest(unittest.TestCase):
    def test_name_and_avatar(self):
        home = tempfile.mkdtemp(prefix="zuhui_home_")
        env = dict(os.environ, ZUHUI_HOME=home, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", "from pathlib import Path\n" + SERVER, str(ROOT)], env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-2000:] + p.stderr[-2000:])
        r = json.loads(line[7:])
        self.assertEqual(r["start"][0], 200)
        self.assertIsNone(r["start"][1]["avatar"])                    # 新装的：没有自己的头像（界面上画花）
        self.assertFalse(r["start"][1]["custom"])
        self.assertEqual(r["rail0"], 404)
        self.assertEqual(r["empty"], [400, {"error": "empty"}])
        self.assertEqual(r["taken"], [400, {"error": "taken"}])
        self.assertEqual(r["long"], [400, {"error": "too_long"}])
        self.assertEqual(r["rename"][0], 200)
        self.assertEqual(r["rename"][1]["name"], "小王 同学")            # 换行、首尾空白收掉
        self.assertEqual(r["named"], [["小王 同学", "AI"]])        # 语音桥那边也换名字
        self.assertEqual((r["ai"][1]["assistant"], r["ai"][1]["assistant_custom"]), ("DeepSeek", True))
        self.assertEqual(r["ai_taken"], [400, {"error": "taken"}])
        self.assertEqual(r["people"], ["小王 同学", "DeepSeek"])        # 会议页上 AI 马上换名字
        self.assertEqual((r["ai_back"][1]["assistant"], r["ai_back"][1]["assistant_custom"]), ("AI", False))   # 空的 = 按模型来源叫（没设时叫 AI）
        self.assertEqual(r["named2"][-1], ["小王 同学", "AI"])
        self.assertEqual(r["me_name"], "小王 同学")                    # 会议页上马上是新名字
        self.assertEqual(r["bad_img"], [400, {"error": "bad_image"}])
        self.assertEqual(r["pic"][0], 200)
        self.assertTrue(r["pic"][1]["custom"])
        self.assertTrue(r["pic"][1]["avatar"].startswith("/avatars/me.png?v="))
        self.assertEqual(r["rail1"], 200)
        self.assertEqual(r["size"], [256, 256])                       # 居中裁成正方形，缩到 256
        self.assertEqual({k: r["remove"][1][k] for k in ("name", "avatar", "custom")}, {"name": "小王 同学", "avatar": None, "custom": False})
        self.assertEqual(r["rail2"], 404)
        self.assertEqual(r["saved"], {"name": "小王 同学"})
        self.assertEqual((r["color"][0], r["color"][1]["color"]), (200, "#4ade80"))   # 名字颜色：存成小写
        self.assertEqual(r["ai_color"][1]["ai_color"], "#f472b6")
        self.assertEqual(r["bad_color"], [400, {"error": "color"}])
        self.assertEqual(r["colors_people"], ["#4ade80", "#f472b6"])                 # 参会人跟着换色
        self.assertFalse(set(r["guest_pal"].values()) & {"#4ade80", "#f472b6"})       # 访客配色避开这两色
        # 跨源：只有 Bloom 自己的窗口和本机页面能调；别的网站改不了东西、也读不到
        self.assertEqual(r["evil_post"][0], 403)
        self.assertEqual(r["evil_rebase"][0], 403)
        self.assertEqual(r["after_evil"], "小王 同学")
        self.assertEqual(r["app_get"], [200, "http://tauri.localhost"])
        self.assertEqual(r["evil_get"], [200, None])


if __name__ == "__main__":
    unittest.main(verbosity=2)
