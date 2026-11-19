# -*- coding: utf-8 -*-
"""公共中继：relay/token_server.py 的 BLOOM_PUBLIC=1 模式，和 Bloom 这边（share.py / relay_cfg.py）用通行码连它。

在临时目录里起一份中继（127.0.0.1 随便一个空端口、没有 LiveKit），不碰任何真的服务器，也不碰正在跑的 Bloom。

    python -m unittest tests.test_public_relay -v
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req(method, url, body=None, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class PublicRelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bloom_pub_relay_"))
        env_file = cls.tmp / "livekit.env"
        env_file.write_text("LIVEKIT_API_KEY=APItest123\nLIVEKIT_API_SECRET=" + "s" * 40 +
                            "\nLIVEKIT_URL=wss://relay.test\nBLOOM_PUBLIC=1\nBLOOM_MAX_LIVE=1\n", encoding="utf-8")
        cls.port = _free_port()
        env = dict(os.environ, BLOOM_ENV=str(env_file), BLOOM_PORT=str(cls.port), STATE_DIRECTORY=str(cls.tmp))
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "relay" / "token_server.py")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                if _req("GET", cls.base + "/healthz")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()

    def test_a_guest_acts(self):
        """访客在会议页上做的事（第 3 批）：标记谁都能；打字问 AI、先选中再说只有共同主持人（按主持人推来的参会人名单）；
        念出来这些不在白名单里，中继直接挡掉。名字带 a_：先跑，结束时把房间收掉（上限只许一场同时在开）。"""
        b = self.base
        c, d = _req("POST", b + "/dev/register", {})
        self.assertEqual(c, 200)
        host = {"Authorization": f"Device {d['device']}:{d['secret']}"}
        room = "roomGUEST01"
        state = {"live": True, "title": "t", "participants": [{"name": "小王", "rank": "cohost"}, {"name": "小李", "rank": "participant"}]}
        self.assertEqual(_req("POST", b + f"/push/{room}/state", state, host)[0], 200)
        c, tok = _req("GET", b + f"/token?room={room}&name=%E5%B0%8F%E7%8E%8B")
        self.assertEqual(c, 200)
        co = {"Authorization": "Bearer " + tok["token"]}
        c, tok2 = _req("GET", b + f"/token?room={room}&name=%E5%B0%8F%E6%9D%8E")
        self.assertEqual(c, 200)
        part = {"Authorization": "Bearer " + tok2["token"]}
        ok = {"/api/ask": {"text": "第三节怎么改"}, "/api/bookmark": {"note": "这里重要"},
              "/api/anchor": {"text": "选中的一段", "section": "3 实验"}}
        for path, body in ok.items():
            c, j = _req("POST", b + f"/view/{room}/act", {"path": path, "body": body}, co)
            self.assertEqual(c, 200, (path, j))
            self.assertTrue(j.get("id"))
        self.assertEqual(_req("POST", b + f"/view/{room}/act", {"path": "/api/bookmark", "body": {}}, part)[0], 200)   # 参会者也能标
        for path in ("/api/ask", "/api/anchor"):                                                                        # 参会者不能打字问、不能选中
            self.assertEqual(_req("POST", b + f"/view/{room}/act", {"path": path, "body": {"text": "x"}}, part), (403, {"error": "cohost_only"}), path)
        self.assertEqual(_req("POST", b + f"/view/{room}/act", {"path": "/api/meeting/end", "body": {}}, co)[0], 403)
        self.assertEqual(_req("POST", b + f"/view/{room}/act", {"path": "/api/ask", "body": {}})[0], 401)   # 没进会的人不行
        c, j = _req("GET", b + f"/push/{room}/inbox", headers=host)
        self.assertEqual(c, 200)
        self.assertEqual([(a["path"], a["by"]) for a in j["acts"]], [(p, "小王") for p in ok] + [("/api/bookmark", "小李")])
        self.assertEqual(j["acts"][0]["body"], {"text": "第三节怎么改"})
        self.assertEqual(_req("POST", b + f"/push/{room}/clear", {}, host)[0], 200)

    def test_devices_and_rooms(self):
        b = self.base
        self.assertEqual(_req("GET", b + "/healthz")[1], {"ok": True, "public": True, "live": 0})   # live：在开的会（自动更新看它）
        c1, d1 = _req("POST", b + "/dev/register", {})
        c2, d2 = _req("POST", b + "/dev/register", {})
        self.assertEqual((c1, c2), (200, 200))
        self.assertEqual(d1["ws"], "wss://relay.test")
        a1 = {"Authorization": f"Device {d1['device']}:{d1['secret']}"}
        a2 = {"Authorization": f"Device {d2['device']}:{d2['secret']}"}
        bad = {"Authorization": f"Device {d1['device']}:wrong"}
        self.assertEqual(_req("GET", b + "/push/roomAAAAAA1/have", headers=bad)[0], 401)
        self.assertEqual(_req("GET", b + "/push/roomAAAAAA1/have", headers=a1)[0], 200)        # 先用的归它
        self.assertEqual(_req("GET", b + "/push/roomAAAAAA1/have", headers=a2)[0], 401)        # 别的电脑碰不了
        st = {"live": True, "title": "t"}
        self.assertEqual(_req("POST", b + "/push/roomAAAAAA1/state", st, a1)[0], 200)
        self.assertEqual(_req("GET", b + "/push/roomBBBBBB2/have", headers=a2)[0], 200)
        self.assertEqual(_req("POST", b + "/push/roomBBBBBB2/state", st, a2), (429, {"error": "busy"}))   # 同时在开的会到上限
        c, j = _req("POST", b + "/push/roomAAAAAA1/join", {"identity": "bloom-host", "name": "主持人", "role": "teacher"}, a1)
        self.assertEqual(c, 200)
        self.assertEqual(j["url"], "wss://relay.test")
        self.assertEqual(_req("POST", b + "/push/roomAAAAAA1/join", {"identity": "someone", "role": "teacher"}, a1)[0], 400)
        import jwt
        claims = jwt.decode(j["token"], "s" * 40, algorithms=["HS256"])
        self.assertEqual((claims["sub"], claims["video"]["room"], claims["video"]["canSubscribe"]), ("bloom-host", "roomAAAAAA1", True))
        self.assertEqual(_req("GET", b + "/push/roomAAAAAA1/participants", headers=a1)[0], 502)   # 这里没有 LiveKit
        self.assertEqual(_req("GET", b + "/token?room=roomAAAAAA1&name=%E5%B0%8F%E7%8E%8B")[0], 200)              # 访客照常能进
        import http.client                                                                            # 太大：只看头就回 413，不收正文
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/push/roomAAAAAA1/paper")
        for k, v in {**a1, "Content-Type": "text/html", "Content-Length": str(13 * 1024 * 1024)}.items():
            c.putheader(k, v)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 413)
        c.close()
        saved = {}                                                                                    # 存盘：刚领走、还没推状态的房间也要存下归属
        for _ in range(40):
            try:
                saved = json.loads((self.tmp / "rooms.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            if "roomBBBBBB2" in saved:
                break
            time.sleep(0.25)
        self.assertEqual(saved.get("roomBBBBBB2", {}).get("owner"), d2["device"], saved.keys())
        self.assertIsNone(saved["roomBBBBBB2"]["state"])
        self.assertEqual(saved["roomAAAAAA1"]["owner"], d1["device"])
        for _ in range(9):
            _req("POST", b + "/dev/register", {})
        self.assertEqual(_req("POST", b + "/dev/register", {})[0], 429)                              # 一个 IP 一天最多 10 个

    def test_bloom_side(self):
        """Bloom 这边：公共中继下自动领通行码；没打开邀请时 armed 为假（什么都不发）；打开后用通行码推得上去。"""
        home = tempfile.mkdtemp(prefix="zuhui_home_")
        code = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from zuhui import relay_cfg, share
base = sys.argv[2] + "/"
share.PUBLIC_RELAY = base
relay_cfg.set_mode("public")
from zuhui.config import load
cfg = load()
s = share.Share(cfg)
out = {"base": s.base, "configured": s.configured, "public": s.public, "armed0": s.armed}
relay_cfg.set_invite(True)
out["armed1"] = s.armed
out["test"] = relay_cfg.test(s)
out["device_saved"] = bool(share.device(base, register=False))
out["pub"] = relay_cfg.public(cfg)
relay_cfg.set_mode("off")
cfg2 = load()
out["off_base"] = cfg2["share"]["base_url"]
print("RESULT " + json.dumps(out, ensure_ascii=False))
"""
        env = dict(os.environ, ZUHUI_HOME=home, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", code, str(ROOT), self.base], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        self.assertEqual(r["base"], self.base + "/")
        self.assertTrue(r["configured"])
        self.assertTrue(r["public"])
        self.assertFalse(r["armed0"])                        # 没打开邀请：不连中继
        self.assertTrue(r["armed1"])
        self.assertTrue(r["test"]["ok"], r["test"])          # 自动领了通行码，推得上去
        self.assertTrue(r["device_saved"])
        self.assertEqual((r["pub"]["mode"], r["pub"]["invite_on"]), ("public", True))
        self.assertEqual(r["off_base"], "")                  # 关闭：没有邀请链接


if __name__ == "__main__":
    unittest.main()
