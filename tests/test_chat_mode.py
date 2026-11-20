# -*- coding: utf-8 -*-
"""没有论文的会（mode=chat）：没选论文就开会，这场只记逐字稿，AI 不接话。

覆盖：没选论文的判断（移除过、新装机器）；/api/paper/clear 之后 /api/current 的 paper / papers / ready.paper / mode；
开会后 meta.mode=="chat"、papers==[]、会名用默认的；handle 一句话快嘴不被调、逐字稿里有这句、没有 AI 的话；
打字问、叫 AI 名字也不回；访客状态 paper false、mode chat、不推论文页；占位页不带本机路径；参会人名单里没有 AI；
纪要提示词不提改论文；有论文时一切照旧（mode=="paper"，AI 照常回）。

全离线、不花钱：模型一律用假的；会议对象用哑嘴（mute=True），不起耳朵和工人；HTTP 那组在子进程里起对比页服务。
ZUHUI_HOME 指到临时目录，不碰真实的 ~/.zuhui、不碰正在跑的 Bloom；中继指到 https://127.0.0.1:1/。

    python -X utf8 -m unittest tests.test_chat_mode -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-chat-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import meetings as mt  # noqa: E402
from zuhui import share  # noqa: E402

share.PUBLIC_RELAY = "https://127.0.0.1:1/"

_KEEP = ("current_paper.json", "open_papers.json")


class _StateGuard(unittest.TestCase):
    """同一个进程里别的测试也用这个沙盒：跑完把"当前论文""摊开的单子"恢复原样。"""

    def setUp(self):
        self._saved = {}
        for n in _KEEP:
            p = zconfig.STATE / n
            self._saved[n] = p.read_bytes() if p.exists() else None

    def tearDown(self):
        for n, data in self._saved.items():
            p = zconfig.STATE / n
            if data is None:
                try:
                    p.unlink()
                except OSError:
                    pass
            else:
                p.write_bytes(data)

    def paper_file(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="chat-paper-"))
        f = d / "测试论文.md"
        f.write_text("# 1 引言\n\n我们研究组会助手。\n\n# 2 方法\n\n先听再改。\n", encoding="utf-8")
        return f

    def use_paper(self, f: Path) -> None:
        zconfig.CURRENT_PAPER.write_text(json.dumps({"path": str(f), "name": "测试论文"}, ensure_ascii=False), encoding="utf-8")


class NoPaperTest(_StateGuard):
    def test_cleared_and_fresh_install(self):
        f = self.paper_file()
        self.use_paper(f)
        cfg = {"paper": {"file": str(f.parent / "不存在.md"), "title": "默认那篇"}}
        self.assertFalse(zconfig.no_paper(cfg))
        self.assertEqual(zconfig.paper_path(cfg), f)
        zconfig.clear_paper()                                   # 大厅里点了「移除」
        self.assertTrue(zconfig.no_paper(cfg))
        self.assertEqual(zconfig.paper_path(cfg), zconfig.NO_PAPER)
        self.assertTrue(zconfig.NO_PAPER.parent.is_dir())       # 后台进程的工作目录要在
        self.assertFalse(zconfig.NO_PAPER.exists())
        self.assertEqual(zconfig.paper_title(cfg), "")
        cfg_ok = {"paper": {"file": str(f), "title": "默认那篇"}}
        self.assertTrue(zconfig.no_paper(cfg_ok))               # 移除过：不退回 config 里那篇
        zconfig.CURRENT_PAPER.unlink()                          # 新装的机器：没有 current_paper.json
        self.assertTrue(zconfig.no_paper(cfg))                  # config 里那篇也不在
        self.assertFalse(zconfig.no_paper(cfg_ok))              # config 里那篇在：照旧用它
        self.assertEqual(zconfig.paper_title(cfg_ok), "默认那篇")
        self.use_paper(f)                                       # 重新选一篇：恢复成有论文
        self.assertFalse(zconfig.no_paper(cfg))


class MetaModeTest(unittest.TestCase):
    def test_meta_list_get(self):
        lib = mt.Meetings(Path(tempfile.mkdtemp(prefix="chat-lib-")))
        m = lib.start(session="s-chat", mode="chat")
        self.assertEqual((m["mode"], m["papers"], m["active_paper"]), ("chat", [], None))
        self.assertTrue(m["title"].startswith("组会 · "))
        lib.end()
        p = lib.start(session="s-paper", title="某篇")
        self.assertEqual(p["mode"], "paper")
        lib.end()
        old = lib.root / "20200101-000000"                     # 旧会议：meta 里没有 mode
        old.mkdir()
        (old / "meta.json").write_text(json.dumps({"id": old.name, "title": "旧会", "state": "ended"}), encoding="utf-8")
        modes = {x["id"]: x["mode"] for x in lib.list()}
        self.assertEqual(modes[m["id"]], "chat")
        self.assertEqual(modes[p["id"]], "paper")
        self.assertEqual(modes[old.name], "paper")
        self.assertEqual(lib.get(m["id"])["meeting"]["mode"], "chat")
        self.assertEqual(lib.get(old.name)["meeting"]["mode"], "paper")

    def test_summary_prompt(self):
        seen = {}

        def fake(user, system=None, **kw):
            seen["user"], seen["system"] = user, system
            return {"title": "交易计划", "speakers": ["林晓"], "abstract": "a", "conclusions": [], "sections": [], "todos": []}
        lines = [{"who": "teacher", "text": "明天先看开盘量能", "t": "00:05", "sec": 5}]
        with mock.patch("zuhui.claude_text.ask_json", fake):
            s = mt.summarize({"meeting": {"me": "林晓"}}, {"title": "组会", "mode": "chat"}, lines, [], template="paper")
        self.assertNotIn("改论文", seen["system"])
        self.assertNotIn("任务板", seen["system"])
        self.assertNotIn("任务板", seen["user"])
        self.assertNotIn("论文讨论", seen["system"])           # 选了"论文讨论"格式：没有论文的会按默认格式写
        self.assertEqual(s["template"], "default")
        with mock.patch("zuhui.claude_text.ask_json", fake):
            mt.summarize({"meeting": {"me": "林晓"}}, {"title": "组会"}, lines, [], template="default")
        self.assertIn("改论文", seen["system"])                 # 改论文的会照旧
        self.assertIn("任务板", seen["user"])
        with mock.patch("zuhui.claude_text.ask_json", side_effect=RuntimeError("离线")):
            s = mt.summarize({"meeting": {"me": "林晓"}}, {"title": "组会", "mode": "chat"}, lines, [], template="default")
        self.assertNotIn("改好", s["abstract"])                 # 兜底纪要也不提改稿


def _meeting():
    from zuhui import app
    from zuhui.config import load
    return app.Meeting(load(), dry=True, mute=True)


class ChatMeetingTest(_StateGuard):
    """会议对象（哑嘴、不起耳朵和工人）。快嘴换成假的，记下有没有被调。"""

    def setUp(self):
        super().setUp()
        self.m = _meeting()
        if self.m.meetings.current():
            self.m.meetings.end()
        self.calls = []

        def fake(said, light=False, remember=True):
            self.calls.append(said)
            yield ("tag", "#chat")
            yield ("sentence", "好的。")
            yield ("done", "好的。")
        self.m.fast.reply = fake

    def tearDown(self):
        if self.m.meetings.current():
            self.m.meetings.end()
        super().tearDown()

    def utter(self, text: str):
        from zuhui.ears import Utterance
        now = time.time()
        return Utterance(n=1, text=text, t="", shot=None, session="", heard=now, landed=now, sent=now, started=now)

    def test_clear_then_chat_meeting(self):
        from zuhui.config import assistant_name
        m = self.m
        m.clear_paper()
        self.assertTrue(json.loads(zconfig.CURRENT_PAPER.read_text(encoding="utf-8")).get("none"))
        self.assertEqual(mt.load_open_papers()["papers"], [])
        self.assertEqual(Path(m.paper.path), zconfig.NO_PAPER)
        self.assertEqual(Path(m.hands.folder), zconfig.NO_PAPER.parent.resolve())   # 后台的工作目录是个真实存在的空目录
        self.assertTrue(m._chat_now())
        m.mouth.ensure_server = mock.Mock(return_value=True)
        m.on_meeting_started(f"s-chat-{time.time()}")
        m.mouth.ensure_server.assert_not_called()                # 不拉起配音服务
        cur = m.meetings.current()
        self.assertEqual((cur["mode"], cur["papers"], cur["active_paper"]), ("chat", [], None))
        self.assertTrue(cur["title"].startswith("组会 · "))
        self.assertFalse((m.meetings.root / cur["id"] / "carry_in.json").exists())   # 不带上次遗留
        with self.assertRaises(RuntimeError):
            m.set_paper(str(self.paper_file()))                  # 开着没有论文的会：不能加论文
        with self.assertRaises(RuntimeError):
            m.clear_paper()                                      # 开着会不能移除
        m.handle(self.utter("帮我记一下明天的交易计划，先看开盘量能"))
        ai = assistant_name(m.cfg)
        m.handle(self.utter(f"{ai}，你觉得怎么样？"))             # 叫了 AI 的名字也不回
        m.ask_typed("你在吗")                                     # 打字发的也不回
        kind, u = m.events.get_nowait()
        self.assertEqual(kind, "utterance")
        m.handle(u)
        m._speculate("你好你好")                                  # 不抢跑
        self.assertIsNone(m._spec)
        self.assertEqual(self.calls, [])                          # 快嘴一次都没被调
        lines = m.meetings.current_lines()
        self.assertEqual([l["who"] for l in lines], ["teacher", "teacher", "teacher"])
        self.assertIn("交易计划", lines[0]["text"])
        self.assertTrue(lines[2].get("typed"))
        self.assertFalse(any(l["who"] == "student" for l in lines))
        self.assertEqual(m.board.tasks(), [])
        self.assertFalse(getattr(m, "_mute", False))
        self.assertEqual(mt.load_open_papers()["papers"], [])

    def test_paper_meeting_unchanged(self):
        from zuhui.paper import Paper
        m = self.m
        f = self.paper_file()
        self.use_paper(f)
        m.paper = Paper(f, "测试论文")
        m.fast.paper = m.paper
        self.assertFalse(m._chat_now())
        with mock.patch.object(type(m), "_gpu_up", lambda self: None):
            m.on_meeting_started(f"s-paper-{time.time()}")
        cur = m.meetings.current()
        self.assertEqual(cur["mode"], "paper")
        self.assertEqual([p["name"] for p in cur["papers"]], ["测试论文"])
        self.assertEqual(cur["title"], "测试论文")
        m.handle(self.utter("第一节讲的是什么？"))
        self.assertEqual(len(self.calls), 1)                      # 有论文：AI 照常回
        self.assertTrue(any(l["who"] == "student" for l in m.meetings.current_lines()))


class FastWarmTest(unittest.TestCase):
    def test_skip_warm(self):
        from zuhui.board import Board
        from zuhui.fast import FastBrain
        from zuhui.paper import Paper
        fb = FastBrain({"fast": {}}, Paper(zconfig.NO_PAPER, ""), Board())
        fb.skip_warm = lambda: True
        fb._cli = mock.Mock(side_effect=AssertionError("没有论文的会不该起对话模型"))
        with mock.patch("zuhui.provider.mode", return_value="claude"):
            self.assertTrue(fb.warm())
        fb._cli.assert_not_called()

    def test_app_facts(self):
        from zuhui import app_facts
        self.assertIn("没选论文就开的会：只记逐字稿", app_facts.render("小B"))


SERVER = r"""
import json, os, sys, socket, time, http.client
sys.path.insert(0, sys.argv[1])
from zuhui import relay_cfg, share
share.PUBLIC_RELAY = "https://127.0.0.1:1/"
relay_cfg.set_mode("off")
from zuhui import config as zc
from zuhui.view import Viewer, PLACEHOLDER
d = sys.argv[2]
f = os.path.join(d, "论文.md")
open(f, "w", encoding="utf-8").write("# 1 引言\n\n正文。\n")
zc.CURRENT_PAPER.write_text(json.dumps({"path": f, "name": "论文"}, ensure_ascii=False), encoding="utf-8")
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = Viewer(port=port)
v.serve(open_browser=False)
def req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse(); data = r.read()
    return [r.status, json.loads(data.decode("utf-8"))]
def cur_view():
    c = req("GET", "/api/current")[1]
    return {"mode": c.get("mode"), "exists": c["paper"].get("exists"), "papers": c.get("papers"),
            "ready_paper": c["ready"].get("paper"), "ids": [p.get("id") for p in c.get("participants") or []],
            "meeting_mode": (c.get("meeting") or {}).get("mode"), "paper_path": c["paper"].get("path")}
out = {"state": str(zc.STATE)}
out["before"] = cur_view()
out["clear"] = req("POST", "/api/paper/clear", {})
out["after"] = cur_view()
v.rebuild()
out["placeholder"] = v.html
m = v.meetings.start(session="s1", mode="chat")
v.meetings.add_line("teacher", "先记一下")
out["live"] = cur_view()
out["clear_live"] = req("POST", "/api/paper/clear", {})
gs = v.guest_state()
out["guest"] = {"paper": gs.get("paper"), "mode": gs.get("mode"), "people": [p["name"] for p in gs.get("people") or []],
                "pids": [p.get("id") for p in gs.get("participants") or []], "meeting_mode": (gs.get("meeting") or {}).get("mode")}
out["ai"] = v.participants(guests=False)[1]["name"]
out["paper_key"] = v.guest_paper_key()
v._raw = PLACEHOLDER.format(msg="出错了<pre>" + os.path.join(d, "secret-path.md") + "</pre>")
out["guest_page"] = v.guest_paper()[0]
v.meetings.end()
out["list"] = [x.get("mode") for x in req("GET", "/api/meetings")[1]]
det = req("GET", "/api/meetings/" + m["id"])[1]
out["detail"] = det["meeting"].get("mode")
out["detail_people"] = [p.get("id") for p in det.get("participants") or []]
zc.CURRENT_PAPER.write_text(json.dumps({"path": f, "name": "论文"}, ensure_ascii=False), encoding="utf-8")
v.set_mirror(zc.paper_path(v.cfg))
out["back"] = cur_view()
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class HttpTest(unittest.TestCase):
    def test_http(self):
        home = tempfile.mkdtemp(prefix="zuhui_home_chat_")
        env = dict(os.environ, ZUHUI_HOME=home, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", SERVER, str(ROOT), tempfile.mkdtemp(prefix="chat-http-")],
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        b = r["before"]
        self.assertEqual((b["mode"], b["exists"], b["ready_paper"]), ("paper", True, True))
        self.assertIn("claude", b["ids"])
        self.assertEqual(r["clear"], [200, {"ok": True}])
        a = r["after"]
        self.assertEqual((a["mode"], a["exists"], a["papers"], a["ready_paper"]), ("chat", False, [], False))
        self.assertEqual(a["paper_path"], "")                       # 没选论文：不给路径
        self.assertNotIn("claude", a["ids"])
        self.assertIn("暂无文档", r["placeholder"])
        self.assertNotIn(r["state"], r["placeholder"])              # 占位页不带本机路径
        self.assertNotIn("no-paper", r["placeholder"])
        lv = r["live"]
        self.assertEqual((lv["mode"], lv["meeting_mode"]), ("chat", "chat"))
        self.assertNotIn("claude", lv["ids"])
        self.assertEqual(r["clear_live"][0], 409)                   # 开着会不能移除
        g = r["guest"]
        self.assertEqual((g["paper"], g["mode"], g["meeting_mode"]), (False, "chat", "chat"))
        self.assertNotIn(r["ai"], g["people"])
        self.assertNotIn("claude", g["pids"])
        self.assertIsNone(r["paper_key"])                           # 不推论文页
        self.assertNotIn("secret-path", r["guest_page"])            # 给访客的占位页不带出错细节
        self.assertIn("chat", r["list"])
        self.assertEqual(r["detail"], "chat")
        self.assertNotIn("claude", r["detail_people"])
        bk = r["back"]
        self.assertEqual((bk["mode"], bk["exists"], bk["ready_paper"]), ("paper", True, True))   # 重新选一篇：恢复
        self.assertIn("claude", bk["ids"])


if __name__ == "__main__":
    unittest.main()
