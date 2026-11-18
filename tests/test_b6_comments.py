# -*- coding: utf-8 -*-
"""第 6 批：论文上打字批注（zuhui/comments.py、paper_api、view.guest_act、relay/token_server.py 的 ACT_PATHS）。
沙盒 ZUHUI_HOME + 临时会议库：主持人、共同主持人、访客都能写、回复、标为已解决；只有主持人和共同主持人能删；
限 500 字（501 字报 too_long，前端 maxlength 也是 500）；存进这场会的 comments.json，散会后还在（回顾里能看）；
没开会时写不了；访客经中继：/api/comment 在白名单里（另起一份临时中继，不碰真的服务器）。
页尾注入的脚本、样式在、文案跟着界面语言、PPT 讲稿把"编辑"去掉。
跑：python -X utf8 -m unittest tests.test_b6_comments -v"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ZUHUI_HOME"] = os.environ.get("ZUHUI_HOME") or tempfile.mkdtemp(prefix="zuhui-test-home-")

from zuhui import comments, paper_api, roles  # noqa: E402
from zuhui.meetings import Meetings  # noqa: E402

ANCHOR = {"key": "组会上老师的意见常常口头给出", "quote": "组会上老师的意见常常口头给出，学生会后凭记忆修改论文。",
          "section": "1 引言", "ri": 3}


class CommentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cm-case-"))
        self.lib = Meetings(root=self.tmp / "meetings")
        self.viewer = SimpleNamespace(meetings=self.lib, cfg={"meeting": {"me": "王老师"}}, on_log=lambda *a, **k: None)

    def tearDown(self):
        try:
            self.lib.end()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, body, by=None):
        return paper_api.post(self.viewer, "/api/comment", body, by=by, guest=by is not None)

    def test_flow(self):
        self.assertEqual(self.post({"op": "add", "text": "x", "anchor": ANCHOR})[1], 409)      # 没开会
        m = self.lib.start()
        roles.begin(m["id"])
        roles.set_role("小王", "cohost")
        out, code = self.post({"op": "add", "paper": "abcd1234", "anchor": ANCHOR, "text": "这句太口语了"})
        self.assertEqual(code, 200, out)
        c = out["comments"][0]
        self.assertEqual((c["by"], c["paper"], c["section"], c["ri"]), ("王老师", "abcd1234", "1 引言", 3))
        self.assertFalse(c["resolved"])
        self.assertRegex(c["t"], r"^\d\d:\d\d$")
        out, code = self.post({"op": "reply", "id": c["id"], "text": "我改成书面语"}, by="小李")         # 访客能回复
        self.assertEqual(code, 200)
        self.assertEqual(out["comments"][0]["replies"][0]["by"], "小李")
        out, code = self.post({"op": "add", "paper": "abcd1234", "anchor": ANCHOR, "text": "第二条"}, by="小李")
        self.assertEqual(code, 200)
        self.assertEqual(len(out["comments"]), 2)
        out, code = self.post({"op": "resolve", "id": c["id"], "resolved": True}, by="小李")            # 访客能标已解决
        self.assertTrue(out["comments"][0]["resolved"])
        self.assertEqual(out["comments"][0]["resolved_by"], "小李")
        out, code = self.post({"op": "delete", "id": c["id"]}, by="小李")                             # 参会者不能删
        self.assertEqual((code, out["error"]), (403, "cohost_only"))
        rid = self.post({"op": "reply", "id": c["id"], "text": "再说一句"}, by="小王")[0]["comments"][0]["replies"][-1]["id"]
        out, code = self.post({"op": "delete", "id": c["id"], "reply": rid}, by="小王")              # 共同主持人能删回复
        self.assertEqual(code, 200)
        self.assertEqual(len(out["comments"][0]["replies"]), 1)
        out, code = self.post({"op": "delete", "id": out["comments"][1]["id"]})                       # 主持人能删
        self.assertEqual(code, 200)
        self.assertEqual(len(out["comments"]), 1)
        # 存在这场会的目录里，散会后还在
        d = self.lib.live_dir()
        self.assertTrue((d / comments.FILE).is_file())
        self.lib.end()
        self.assertEqual(len(comments.load(d)), 1)
        self.assertEqual(comments.current(self.viewer)[0]["text"], "这句太口语了")             # 散会后：上一场的
        self.assertEqual(self.post({"op": "reply", "id": c["id"], "text": "散会后"})[1], 409)

    def test_limits(self):
        self.lib.start()
        out, code = self.post({"op": "add", "anchor": ANCHOR, "text": "字" * 501})
        self.assertEqual((code, out["error"]), (400, "too_long"))
        out, code = self.post({"op": "add", "anchor": ANCHOR, "text": "字" * 500})
        self.assertEqual(code, 200)
        self.assertEqual(self.post({"op": "add", "anchor": ANCHOR, "text": "  \n "})[0]["error"], "empty")
        self.assertEqual(self.post({"op": "reply", "id": "c99", "text": "x"})[1], 404)
        self.assertEqual(self.post({"op": "zap"})[1], 400)
        big = dict(ANCHOR, quote="长" * 999, key="k" * 999, section="节" * 999)
        c = self.post({"op": "add", "anchor": big, "text": "ok"})[0]["comments"][-1]
        self.assertLessEqual(len(c["quote"]), comments.QUOTE_MAX)
        self.assertLessEqual(len(c["key"]), comments.KEY_MAX)
        self.assertLessEqual(len(c["section"]), 120)
        self.assertEqual(self.post({"op": "add", "anchor": ANCHOR, "text": "x"}, by="  ")[1], 403)    # 没名字的访客

    def test_inject(self):
        zh, en = comments.inject("zh"), comments.inject("en", readonly=True)
        self.assertIn("zh-cm-card", zh)
        self.assertIn("写批注", zh)
        self.assertIn("Add a comment", en)
        self.assertIn("var READONLY = true", en)
        self.assertIn("var READONLY = false", zh)
        self.assertIn('maxlength="500"', zh)
        from zuhui import view
        page = view._inject(None, [], "我", {}, "zh")
        self.assertIn("zh-cm-css", page)

    def test_guest_act_routes_comment(self):
        from zuhui import view
        self.lib.start()
        v = view.Viewer.__new__(view.Viewer)
        v.meetings, v.cfg, v.on_log = self.lib, {"meeting": {"me": "王老师"}}, None
        r = v.guest_act({"id": "a1", "by": "小李", "path": "/api/comment",
                         "body": {"op": "add", "anchor": ANCHOR, "text": "访客的批注"}}, lambda _x: ("", b""))
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["comments"][0]["by"], "小李")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RelayWhitelistTest(unittest.TestCase):
    """中继：访客发 /api/comment（谁都行，删由主持人那边查），排进队给主持人取；不在白名单的照样挡。"""

    def test_comment_path_allowed(self):
        import urllib.error
        import urllib.request

        def req(method, url, body=None, headers=None):
            data = None if body is None else json.dumps(body).encode()
            rq = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(rq, timeout=10) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read() or b"{}")
                except ValueError:
                    return e.code, {}
        tmp = Path(tempfile.mkdtemp(prefix="bloom_cm_relay_"))
        env_file = tmp / "livekit.env"
        env_file.write_text("LIVEKIT_API_KEY=APItest123\nLIVEKIT_API_SECRET=" + "s" * 40 +
                            "\nLIVEKIT_URL=wss://relay.test\nBLOOM_PUBLIC=1\nBLOOM_MAX_LIVE=2\n", encoding="utf-8")
        port = _free_port()
        env = dict(os.environ, BLOOM_ENV=str(env_file), BLOOM_PORT=str(port), STATE_DIRECTORY=str(tmp))
        proc = subprocess.Popen([sys.executable, str(ROOT / "relay" / "token_server.py")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        b = f"http://127.0.0.1:{port}"
        try:
            for _ in range(80):
                try:
                    if req("GET", b + "/healthz")[0] == 200:
                        break
                except OSError:
                    time.sleep(0.1)
            c, d = req("POST", b + "/dev/register", {})
            self.assertEqual(c, 200)
            host = {"Authorization": f"Device {d['device']}:{d['secret']}"}
            room = "roomCOMMENT1"
            state = {"live": True, "title": "t", "participants": [{"name": "小李", "rank": "participant"}]}
            self.assertEqual(req("POST", b + f"/push/{room}/state", state, host)[0], 200)
            c, tok = req("GET", b + f"/token?room={room}&name=%E5%B0%8F%E6%9D%8E")
            self.assertEqual(c, 200)
            part = {"Authorization": "Bearer " + tok["token"]}
            body = {"op": "add", "anchor": ANCHOR, "text": "访客的批注"}
            c, j = req("POST", b + f"/view/{room}/act", {"path": "/api/comment", "body": body}, part)
            self.assertEqual(c, 200, j)
            self.assertEqual(req("POST", b + f"/view/{room}/act", {"path": "/api/paper/versions/restore", "body": {}}, part)[0], 403)
            c, j = req("GET", b + f"/push/{room}/inbox", headers=host)
            self.assertEqual([(a["path"], a["by"], a["body"]) for a in j["acts"]], [("/api/comment", "小李", body)])
            req("POST", b + f"/push/{room}/clear", {}, host)
        finally:
            proc.kill()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
