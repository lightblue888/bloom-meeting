# -*- coding: utf-8 -*-
"""会中交互（第 3 批）：打字问 AI（限长、只有主持人和共同主持人、和说话走一样的流程、逐字稿标出打字的）、
书签（存取、备注限长、只能改自己的；纪要优先写标过的地方）、长时间没人说话、先选中再说（谁选的谁用、过期、用一次就清）、回顾修改清单的勾选和大厅遗留同一份（carry.json）、接口。

全离线、不花钱：模型一律用假的顶替；会议对象用哑嘴（mute=True）、不起耳朵和工人；HTTP 那组在子进程里起对比页服务。
ZUHUI_HOME 指到临时目录，不碰真实的 ~/.zuhui、不碰正在跑的 Bloom。

    python -X utf8 -m unittest tests.test_inmeeting -v
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
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-inmeeting-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import inmeeting as im  # noqa: E402
from zuhui import meetings as mt  # noqa: E402


class AskTextTest(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(im.clean_question("  第三节\x07怎么改？ \n\n\n 说一下 "), "第三节怎么改？\n说一下")
        for bad in ("", "   ", None, "\n\n"):
            with self.assertRaises(ValueError) as e:
                im.clean_question(bad)
            self.assertEqual(str(e.exception), "empty")
        self.assertEqual(len(im.clean_question("问" * im.ASK_MAX)), 500)
        with self.assertRaises(ValueError) as e:
            im.clean_question("问" * (im.ASK_MAX + 1))
        self.assertEqual(str(e.exception), "too_long")


class BookmarkTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bm-"))

    def test_add_update(self):
        b1 = im.add_bookmark(self.d, 75.4, " 这里  很重要 ", by="林晓", host=True, now=1000)
        self.assertEqual((b1["id"], b1["t"], b1["note"], b1["by"], b1["host"]), ("b1", "01:15", "这里 很重要", "林晓", True))
        b2 = im.add_bookmark(self.d, 30, "", by="小周", host=False)
        self.assertEqual([b["id"] for b in im.load_bookmarks(self.d)], ["b2", "b1"])   # 按时间排
        self.assertEqual(len(im.add_bookmark(self.d, 1, "注" * im.NOTE_MAX)["note"]), 100)
        with self.assertRaises(ValueError) as e:
            im.add_bookmark(self.d, 1, "注" * (im.NOTE_MAX + 1))
        self.assertEqual(str(e.exception), "too_long")
        self.assertEqual(im.update_bookmark(self.d, b2["id"], "我标的", by="小周", host=False)["note"], "我标的")
        with self.assertRaises(ValueError) as e:                  # 访客改不了别人的
            im.update_bookmark(self.d, b1["id"], "改别人的", by="小周", host=False)
        self.assertEqual(str(e.exception), "not_yours")
        self.assertEqual(im.update_bookmark(self.d, b2["id"], "主持人改", host=True)["note"], "主持人改")
        with self.assertRaises(ValueError) as e:
            im.update_bookmark(self.d, "b99", "x")
        self.assertEqual(str(e.exception), "not_found")
        self.assertIn("[00:30] 小周：主持人改", im.bookmarks_block(im.load_bookmarks(self.d)))

    def test_cap(self):
        with mock.patch.object(im, "BOOKMARK_CAP", 2):
            im.add_bookmark(self.d, 1)
            im.add_bookmark(self.d, 2)
            with self.assertRaises(ValueError) as e:
                im.add_bookmark(self.d, 3)
            self.assertEqual(str(e.exception), "too_many")


class QuietAnchorTest(unittest.TestCase):
    def test_quiet(self):
        lines = [{"sec": 10, "text": "你好"}]
        self.assertAlmostEqual(im.quiet_seconds(1000 + 700, 1000, lines), 700 - 10 - mt.speech_seconds("你好"), places=3)
        self.assertEqual(im.quiet_seconds(5000, 1000, lines, busy=True), 0.0)
        self.assertEqual(im.quiet_seconds(1100, 1000, [], last_speech=1090), 10)
        self.assertEqual(im.quiet_seconds(1000 + im.QUIET_S, 1000, []), im.QUIET_S)

    def test_anchor(self):
        a = im.Anchor(ttl=60)
        with self.assertRaises(ValueError):
            a.set("  ")
        with self.assertRaises(ValueError) as e:
            a.set("字" * (im.ANCHOR_MAX + 1))
        self.assertEqual(str(e.exception), "too_long")
        a.set("这一段\n要改", section="3 实验", by=None, now=100)
        self.assertEqual(a.public(now=130)["left_s"], 30)
        self.assertIsNone(a.take(by="小周", now=110))                       # 不是他选的：不动
        self.assertEqual(a.take(by=None, now=110)["text"], "这一段 要改")
        self.assertIsNone(a.get(now=111))                                   # 用一次就清
        a.set("共同主持人选的", by="小周", now=200)
        self.assertIsNone(a.get(now=261))                                   # 60 秒没用就过期
        a.set("共同主持人选的", by="小周", now=300)
        self.assertFalse(a.clear(by="阿杰", host=False))                    # 别人清不了
        self.assertTrue(a.clear(by="小周", host=False))
        a.set("主持人选的", now=time.time())
        self.assertTrue(a.clear(host=True))
        self.assertIn("选中了这一段（在 3 实验）", im.anchor_note({"text": "x", "section": "3 实验"}))


class SummaryTest(unittest.TestCase):
    LINES = [{"t": "00:05", "sec": 5, "who": "teacher", "text": "先听小周汇报"},
             {"t": "03:00", "sec": 180, "who": "guest", "name": "小周", "text": "我的实验做完了"}]

    def test_bookmarks_in_prompt(self):
        marks = [{"id": "b1", "sec": 180, "t": "03:00", "note": "实验结论", "by": "林晓"}]
        seen = {}

        def fake(prompt, system="", **kw):
            seen["prompt"], seen["system"] = prompt, system
            return {"title": "组会", "speakers": ["林晓", "小周"], "abstract": "a", "conclusions": [], "sections": [], "todos": []}
        with mock.patch("zuhui.claude_text.ask_json", fake):
            mt.summarize({"meeting": {"me": "林晓"}}, {"title": "组会", "duration_s": 600}, self.LINES, [],
                         template="default", bookmarks=marks)
        self.assertIn("[03:00] 林晓：实验结论", seen["prompt"])
        self.assertIn(im.BOOKMARK_RULE, seen["system"])
        with mock.patch("zuhui.claude_text.ask_json", fake):
            mt.summarize({}, {"title": "组会"}, self.LINES, [], template="default")
        self.assertNotIn(im.BOOKMARK_RULE, seen["system"])                 # 没标过：不加这条

    def test_old_summary_with_agenda_still_renders(self):
        old = {"title": "x", "speakers": [], "abstract": "a", "conclusions": [], "sections": [], "todos": [],
               "agenda": [{"id": "a1", "title": "汇报", "covered": True}]}
        self.assertIn("# x", mt.render_markdown(old))                       # 以前存下的纪要带着议程字段：照常出，不报错

def _meeting():
    from zuhui import app
    from zuhui.config import load
    m = app.Meeting(load(), dry=True, mute=True)
    return m


class MeetingFlowTest(unittest.TestCase):
    """会议对象（哑嘴、不起耳朵和工人）：打字问的走和说话一样的流程。模型换成假的。"""

    @classmethod
    def setUpClass(cls):
        cls.m = _meeting()

    def setUp(self):
        m = self.m
        if m.meetings.current():
            m.meetings.end()
        m.board.reset()
        m.anchor.clear()
        m._p_wait = []
        while not m.events.empty():
            m.events.get_nowait()
        self.mid = m.meetings.start(session=f"s-{time.time()}", title="组会")["id"]

    def reply(self, tag: str, text: str):
        def fake(said, light=False):
            self.said = said
            yield ("tag", tag)
            yield ("sentence", text)
            yield ("done", text)
        self.m.fast.reply = fake

    def test_limits_and_not_live(self):
        m = self.m
        for bad, code in (("", "empty"), ("问" * 501, "too_long")):
            with self.assertRaises(ValueError) as e:
                m.ask_typed(bad)
            self.assertEqual(str(e.exception), code)
        with self.assertRaises(ValueError) as e:                           # 参会者不能打字问
            m.ask_typed("问", by="小周")
        self.assertEqual(str(e.exception), "cohost_only")
        self.assertEqual(m.meetings.current_lines(), [])
        self.assertTrue(m.events.empty())
        m.meetings.end()
        with self.assertRaises(ValueError) as e:
            m.ask_typed("问")
        self.assertEqual(str(e.exception), "not_live")

    def test_host_typed_marks_transcript(self):
        m = self.m
        self.reply("#chat", "第三节讲的是实验设置。")
        m.ask_typed("第三节讲的是什么？")
        kind, u = m.events.get_nowait()
        self.assertEqual((kind, u.typed, getattr(u, "guest", None)), ("utterance", True, None))
        m.handle(u)
        lines = m.meetings.current_lines()
        self.assertEqual((lines[0]["who"], lines[0]["text"], lines[0].get("typed")), ("teacher", "第三节讲的是什么？", True))
        self.assertEqual((lines[1]["who"], lines[1]["text"]), ("student", "第三节讲的是实验设置。"))
        self.assertIn("打字发给你的", self.said)

    def test_cohost_and_participant(self):
        from zuhui import roles
        m = self.m
        roles.set_role("阿杰", "cohost")
        self.reply("#edit 改标题", "好，标题改短。")
        m.ask_typed("标题改短一点", by="阿杰")
        _, u = m.events.get_nowait()
        self.assertEqual(u.guest, "阿杰")
        m.handle(u)
        self.assertEqual([t["title"] for t in m.board.tasks()], ["改标题"])   # 共同主持人：能让 AI 改论文
        line = next(l for l in m.meetings.current_lines() if l["who"] == "guest")
        self.assertEqual((line["name"], line.get("typed")), ("阿杰", True))

    def test_anchor_used_once(self):
        m = self.m
        m.anchor.set("我们提出了一种全新的方法", section="1 引言")
        self.reply("#edit 改引言一句", "好，这句改得谦虚一点。")
        m.ask_typed("这句别说全新")
        _, u = m.events.get_nowait()
        m.handle(u)
        t = m.board.tasks()[0]
        self.assertIn("「我们提出了一种全新的方法」", t["remark"])
        self.assertEqual(t["focus"]["text"], "我们提出了一种全新的方法")
        self.assertEqual(t["focus"]["section"], "1 引言")
        self.assertIsNone(m.anchor.get())                                  # 用了就清
        self.assertIn("选中了这一段", self.said)
        m.anchor.set("别人选的", by="阿杰")                                   # 共同主持人选的：主持人这句不用它
        self.reply("#chat", "嗯。")
        m.ask_typed("好的继续")
        _, u = m.events.get_nowait()
        m.handle(u)
        self.assertNotIn("选中了这一段", self.said)
        self.assertIsNotNone(m.anchor.get())

    def test_extras_host_and_guest(self):
        from zuhui import live_api
        from zuhui.view import Viewer
        m = self.m
        v = object.__new__(Viewer)
        v.meetings, v.cfg, v.anchor = m.meetings, m.cfg, m.anchor
        self.assertEqual(sorted(live_api.extras(v, host=True)), ["anchor", "bookmarks", "quiet_s"])
        self.assertEqual(sorted(live_api.extras(v, host=False)), ["anchor", "bookmarks"])    # 多久没人说话只给主持人


SERVER = r"""
import json, os, sys, socket, time, http.client
sys.path.insert(0, sys.argv[1])
from unittest import mock
from zuhui import relay_cfg, share
share.PUBLIC_RELAY = "https://127.0.0.1:1/"
relay_cfg.set_mode("off")
from zuhui import roles, past
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
from zuhui.view import Viewer
v = Viewer(port=port)
v.serve(open_browser=False)
def req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse(); data = r.read()
    return [r.status, json.loads(data.decode("utf-8"))]
out = {}
out["ask_idle"] = req("POST", "/api/ask", {"text": "你好"})
m = v.meetings.start(session="s1", title="组会")
from zuhui import inmeeting
v.meetings.add_line("teacher", "先听小周讲实验")
out["bm"] = req("POST", "/api/bookmark", {"note": "这里重要"})
out["bm_long"] = req("POST", "/api/bookmark", {"note": "长" * 101})
out["bm_edit"] = req("POST", "/api/bookmark", {"id": out["bm"][1]["bookmark"]["id"], "note": "改了备注"})
out["ask_noapp"] = req("POST", "/api/ask", {"text": "你好"})
out["anchor"] = req("POST", "/api/anchor", {"text": "选中的这一段", "section": "3 实验"})
cur = req("GET", "/api/current")[1]
out["cur"] = {k: cur.get(k) for k in ("bookmarks", "anchor", "quiet_s")}
out["cur_private"] = "private" in cur
gs = v.guest_state()
out["guest_keys"] = sorted(k for k in ("bookmarks", "anchor", "private", "quiet_s") if k in gs)
out["g_ask"] = v.guest_act({"id": "x8", "by": "小周", "path": "/api/ask", "body": {"text": "问"}}, None)
out["g_anchor"] = v.guest_act({"id": "x3", "by": "小周", "path": "/api/anchor", "body": {"text": "我也选一段"}}, None)
roles.set_role("阿杰", "cohost")
out["g_anchor_co"] = v.guest_act({"id": "x4", "by": "阿杰", "path": "/api/anchor", "body": {"text": "共同主持人选的"}}, None)
out["g_ask_co"] = v.guest_act({"id": "x9", "by": "阿杰", "path": "/api/ask", "body": {"text": "问"}}, None)
out["g_bm"] = v.guest_act({"id": "x6", "by": "小周", "path": "/api/bookmark", "body": {"note": "访客标的"}}, None)
out["g_bm_other"] = v.guest_act({"id": "x7", "by": "小周", "path": "/api/bookmark", "body": {"id": out["bm"][1]["bookmark"]["id"], "note": "改主持人的"}}, None)
v.meetings.end()
past.tick(v.meetings, m["id"] + "/note:T1", True)
out["detail"] = req("GET", "/api/meetings/" + m["id"])[1]
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class HttpTest(unittest.TestCase):
    def test_http(self):
        env = dict(os.environ, ZUHUI_HOME=tempfile.mkdtemp(prefix="zuhui_home_inmeeting_"), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", SERVER, str(ROOT)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        self.assertEqual(r["ask_idle"], [409, {"ok": False, "error": "not_live"}])
        self.assertEqual(r["bm"][0], 200)
        self.assertEqual((r["bm"][1]["bookmark"]["note"], r["bm"][1]["bookmark"]["host"]), ("这里重要", True))
        self.assertEqual(r["bm_long"], [400, {"ok": False, "error": "too_long"}])
        self.assertEqual(r["bm_edit"][1]["bookmark"]["note"], "改了备注")
        self.assertEqual(r["ask_noapp"], [503, {"ok": False, "error": "not_ready"}])
        self.assertEqual(r["cur"]["anchor"]["text"], "选中的这一段")
        self.assertEqual([b["note"] for b in r["cur"]["bookmarks"]], ["改了备注"])
        self.assertFalse(r["cur_private"])
        self.assertIsInstance(r["cur"]["quiet_s"], int)
        self.assertEqual(r["guest_keys"], ["anchor", "bookmarks"])   # 多久没人说话不推给访客
        self.assertEqual(r["g_ask"], {"ok": False, "error": "cohost_only"})       # 参会者不能打字问
        self.assertEqual(r["g_ask_co"], {"ok": False, "error": "not_ready"})      # 共同主持人放行（这里没起会议对象）
        self.assertEqual(r["g_anchor"], {"ok": False, "error": "cohost_only"})
        self.assertEqual(r["g_anchor_co"]["anchor"]["by"], "阿杰")
        self.assertEqual((r["g_bm"]["bookmark"]["by"], r["g_bm"]["bookmark"]["host"]), ("小周", False))
        self.assertEqual(r["g_bm_other"], {"ok": False, "error": "not_yours"})
        d = r["detail"]
        self.assertEqual([b["note"] for b in d["bookmarks"]], ["改了备注", "访客标的"])
        self.assertEqual(d["carry_done"], ["note:T1"])                       # 修改清单的勾选和大厅遗留同一份


if __name__ == "__main__":
    unittest.main()
