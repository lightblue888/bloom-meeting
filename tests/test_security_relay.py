# -*- coding: utf-8 -*-
"""会议中继（relay/token_server.py）的安全检查：在临时目录起一份公共模式的中继（127.0.0.1 空端口、没有 LiveKit），
不碰任何真的服务器。

- 名字认领：一场会里一个名字归第一个用它的浏览器（拿到的钥匙），别人用同一个名字（含大小写、空白不同的写法）领不到 token，
  bloom-host / bloom-student、主持人和 AI 的名字访客都不能用（共同主持人是按名字定的）。
- 房间数满了不丢正在开的会；挤出内存的房间还归原来那台电脑，别的电脑领不走。
- 公共中继一个房间在内存里有上限；结果一份最多 1 MB；SVG / 网页图关进沙盒。

    python -m unittest tests.test_security_relay -v
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
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req(method, url, body=None, headers=None, raw=False):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            b = r.read()
            return r.status, (b if raw else json.loads(b or b"{}")), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            b = e.read()
            return e.code, (b if raw else json.loads(b or b"{}")), dict(e.headers)
        except (ValueError, OSError):
            return e.code, {}, dict(e.headers)
    except OSError:                       # 中继没收请求体就回了错、关了连接
        return 0, {}, {}


class _Relay:
    def __init__(self, extra: str = "", tmp: Path | None = None):
        self.tmp = tmp or Path(tempfile.mkdtemp(prefix="bloom_sec_relay_"))
        (self.tmp / "livekit.env").write_text("LIVEKIT_API_KEY=APItest123\nLIVEKIT_API_SECRET=" + "s" * 40 +
                                              "\nLIVEKIT_URL=wss://relay.test\nBLOOM_PUBLIC=1\n" + extra, encoding="utf-8")
        self.start()

    def start(self):
        port = _free_port()
        env = dict(os.environ, BLOOM_ENV=str(self.tmp / "livekit.env"), BLOOM_PORT=str(port), STATE_DIRECTORY=str(self.tmp))
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "relay" / "token_server.py")], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            if _req("GET", self.base + "/healthz")[0] == 200:
                return
            time.sleep(0.1)
        raise RuntimeError("relay did not start")

    def stop(self):
        self.proc.kill()
        self.proc.wait(timeout=10)

    def device(self, ip: str) -> dict:
        c, d, _ = _req("POST", self.base + "/dev/register", {}, {"X-Forwarded-For": ip})
        assert c == 200, (c, d)
        return {"Authorization": f"Device {d['device']}:{d['secret']}"}

    def token(self, room: str, name: str, claim: str | None = None):
        q = {"room": room, "name": name}
        if claim:
            q["claim"] = claim
        c, j, _ = _req("GET", self.base + "/token?" + urllib.parse.urlencode(q))
        return c, j


LIVE = {"live": True, "title": "t", "participants": [
    {"name": "主持人甲", "rank": "host"}, {"name": "Bloom AI", "rank": "assistant"}, {"name": "Alice", "rank": "cohost"}]}


class NameClaimTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _Relay("BLOOM_CLAIMS_PER_IP=5\n")             # 默认 20；调小了少发几次请求

    @classmethod
    def tearDownClass(cls):
        cls.r.stop()

    def test_name_cannot_be_taken_over(self):
        r = self.r
        host = r.device("10.0.0.1")
        room = "roomCLAIM01"
        self.assertEqual(_req("POST", r.base + f"/push/{room}/state", LIVE, host)[0], 200)
        c, first = r.token(room, "Alice")
        self.assertEqual(c, 200)
        self.assertTrue(first.get("claim"))
        for other in ("Alice", "alice", " ALICE ", "Alice　"):           # 别人、换个写法：领不到
            self.assertEqual(r.token(room, other)[0], 409, other)
            self.assertEqual(r.token(room, other, "wrong-key")[0], 409, other)
        c, again = r.token(room, "Alice", first["claim"])                     # 同一个浏览器刷新：照样能进
        self.assertEqual(c, 200)
        self.assertEqual(again["claim"], first["claim"])
        tok = again["token"]
        c, j, _ = _req("POST", r.base + f"/view/{room}/act", {"path": "/api/ask", "body": {"text": "x"}}, {"Authorization": "Bearer " + tok})
        self.assertEqual(c, 200, j)
        for reserved in ("bloom-host", "bloom-student", "Bloom-Host", "主持人甲", "bloom  ai"):   # 主持人那边的身份和名字
            self.assertEqual(r.token(room, reserved)[0], 409, reserved)
        c, j = r.token(room, "Bob")                                            # 新名字照常
        self.assertEqual(c, 200)
        _req("POST", r.base + f"/push/{room}/clear", {}, host)

    def test_name_flood_limited_per_ip(self):
        """拿一堆名字刷：一个 IP 在一个房间里新认领的名字有上限（这里调成 5），刷满了也不让出别人认领过的名字。"""
        r = self.r
        host = r.device("10.0.0.3")
        room = "roomCLAIM03"
        self.assertEqual(_req("POST", r.base + f"/push/{room}/state", LIVE, host)[0], 200)
        c, alice = r.token(room, "Alice")
        self.assertEqual(c, 200)

        def tok_from(ip, name):
            q = urllib.parse.urlencode({"room": room, "name": name})
            return _req("GET", r.base + "/token?" + q, headers={"X-Forwarded-For": ip})[0]
        codes = [tok_from("10.9.0.1", f"spam{i}") for i in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(set(codes[5:]), {429})
        self.assertEqual(tok_from("10.9.0.2", "alice"), 409)                  # 认领过的照样不让
        self.assertEqual(tok_from("10.9.0.2", "Dave"), 200)                   # 别的 IP 来的新人照常
        self.assertEqual(r.token(room, "Alice", alice["claim"])[0], 200)
        _req("POST", r.base + f"/push/{room}/clear", {}, host)

    def test_claim_survives_restart(self):
        r = _Relay()
        try:
            host = r.device("10.0.0.2")
            room = "roomCLAIM02"
            self.assertEqual(_req("POST", r.base + f"/push/{room}/state", LIVE, host)[0], 200)
            c, first = r.token(room, "Carol")
            self.assertEqual(c, 200)
            time.sleep(3)                                                      # 存盘线程 2 秒内写一次
            r.stop()
            r.start()
            self.assertEqual(_req("POST", r.base + f"/push/{room}/state", LIVE, host)[0], 200)
            self.assertEqual(r.token(room, "carol")[0], 409)
            self.assertEqual(r.token(room, "Carol", first["claim"])[0], 200)
        finally:
            r.stop()


class RoomsAndMemoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _Relay("BLOOM_MAX_ROOMS=6\nBLOOM_ROOM_BYTES=3000000\nBLOOM_MAX_LIVE=10\n")

    @classmethod
    def tearDownClass(cls):
        cls.r.stop()

    def test_live_room_survives_flood_and_stays_owned(self):
        r = self.r
        victim = r.device("10.1.0.1")
        live_room, idle_room = "victimLIVE1", "victimIDLE1"
        self.assertEqual(_req("POST", r.base + f"/push/{idle_room}/state", {"live": False, "upcoming": True, "title": "下一场"}, victim)[0], 200)
        self.assertEqual(_req("POST", r.base + f"/push/{live_room}/state", {"live": True, "title": "victim"}, victim)[0], 200)
        n = 0
        for ip in range(3):                                                   # 别人领一堆房间把房间数挤满
            for k in range(3):
                atk = r.device(f"10.2.{ip}.{k}")
                for j in range(4):
                    _req("GET", r.base + f"/push/flood{ip}{k}{j}x/have", headers=atk)
                    n += 1
        self.assertGreater(n, 6)
        st = _req("GET", r.base + f"/view/{live_room}/state")[1].get("state") or {}
        self.assertEqual(st.get("title"), "victim")                           # 正在开的会没被挤掉
        thief = r.device("10.3.0.1")
        for room in (live_room, idle_room):                                   # 挤出内存的房间也领不走
            self.assertIn(_req("GET", r.base + f"/push/{room}/have", headers=thief)[0], (401, 503), room)
        self.assertEqual(_req("GET", r.base + f"/push/{idle_room}/have", headers=victim)[0], 200)   # 原来那台电脑照样能用
        _req("POST", r.base + f"/push/{live_room}/clear", {}, victim)

    def test_room_byte_budget_and_results(self):
        r = self.r
        dev = r.device("10.4.0.1")
        room = "budgetROOM1"
        self.assertEqual(_req("POST", r.base + f"/push/{room}/state", {"live": False, "title": "b"}, dev)[0], 200)
        png = b"\x89PNG" + b"y" * 900_000
        codes = [_req("POST", r.base + f"/push/{room}/asset/a{i}.png", png, dict(dev, **{"Content-Type": "image/png"}))[0] for i in range(5)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertIn(413, codes[3:])                                         # 一个房间 3 MB：第四张放不下
        c, j, _ = _req("POST", r.base + f"/push/{room}/asset/a0.png", png, dict(dev, **{"Content-Type": "image/png"}))
        self.assertEqual(c, 200)                                              # 换掉同名的那张不算多占
        big = b'"' + b"x" * (1024 * 1024 + 10) + b'"'
        self.assertNotEqual(_req("POST", r.base + f"/push/{room}/result/r1", big, dev)[0], 200)   # 一份结果最多 1 MB
        self.assertEqual(_req("POST", r.base + f"/push/{room}/result/r2", {"ok": True}, dev)[0], 200)

    def test_svg_and_html_assets_are_sandboxed(self):
        r = self.r
        dev = r.device("10.5.0.1")
        room = "svgROOM001"
        _req("POST", r.base + f"/push/{room}/state", {"live": False, "title": "s"}, dev)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        self.assertEqual(_req("POST", r.base + f"/push/{room}/asset/x.svg", svg, dict(dev, **{"Content-Type": "image/svg+xml"}))[0], 200)
        c, _b, h = _req("GET", r.base + f"/view/{room}/fig/x.svg", raw=True)
        self.assertEqual(c, 200)
        self.assertEqual(h.get("Content-Security-Policy"), "sandbox")
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")


if __name__ == "__main__":
    unittest.main()
