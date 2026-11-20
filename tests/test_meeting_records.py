# -*- coding: utf-8 -*-
"""会后记录和后台子进程（2026-11-19 端到端测试里找到的几处）：

- 散会后 participants.json 里每个人是这场会里最后的身份：中途设成共同主持人的还是共同主持人（以前存的是进会那一刻的身份，
  再加上 meetings.end() 里 roles.end() 先清了名单，共同主持人全成了参会者），改回参会者的是参会者；中途走了的人也在；
  开着会后端重启、接着开同一场，重启前来过的人也还在。
- 没有论文的会导出（逐字稿 md、纪要 docx/pdf）的"参会人"不写 AI；会后改了昵称，导出的还是当时的名字。
- 接话分类器的子进程起不来 / 开着会退出：过几秒再拉起，最多再试两次，日志里有退出码；重试用完了下一场开会再试；
  日志写在 ZUHUI_HOME 里，不写真实的 ~/.zuhui。
- 邀请链接"未开始"那一页：选了 / 移除了论文马上换（以前要等换房间号）。

全离线：会议对象用哑嘴，假的 viewer；分类器用临时写的假子进程；中继的推送换成记下来。ZUHUI_HOME 指到临时目录。

    python -X utf8 -m unittest tests.test_meeting_records -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-records-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import share  # noqa: E402

share.PUBLIC_RELAY = "https://127.0.0.1:1/"
_KEEP = ("current_paper.json", "open_papers.json", "roles.json")


class _Guard(unittest.TestCase):
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


class _Viewer:
    """只给参会人：你、AI，加上现在在场的访客（guests 里的）。"""

    def __init__(self):
        self.guests = []

    def participants(self, guests: bool = True):
        from zuhui import roles
        out = [{"id": "me", "name": "主持人甲", "role": "teacher", "rank": "host", "you": True},
               {"id": "claude", "name": "AI", "role": "student", "rank": "assistant"}]
        for g in (self.guests if guests else []):
            out.append({"id": "guest-" + g, "name": g, "role": "guest", "rank": "cohost" if roles.is_cohost(g) else "participant"})
        return out


class ParticipantsAtEndTest(_Guard):
    def setUp(self):
        super().setUp()
        from zuhui import app
        from zuhui.config import load
        self.m = app.Meeting(load(), dry=True, mute=True)
        if self.m.meetings.current():
            self.m.meetings.end()
        self.v = self.m.viewer = _Viewer()
        self.m._watch_post = lambda *a, **k: None          # 不跑散会收尾（重转写、纪要）

    def tearDown(self):
        if self.m.meetings.current():
            self.m.meetings.end()
        super().tearDown()

    def _start(self):
        m = self.m.meetings.start(session=f"s-rec-{time.time()}", mode="chat")
        self.m._seen_guests = {}
        return m

    def _people_file(self, mid):
        return {p["name"]: p.get("rank") for p in json.loads((self.m.meetings.root / mid / "participants.json").read_text(encoding="utf-8"))}

    def test_last_role_kept(self):
        from zuhui import roles
        mt = self._start()
        self.v.guests = ["王老师", "小李", "赵同学"]
        self.m._note_guests()                              # 进会时都是参会者
        roles.set_role("王老师", "cohost")                 # 进来一会儿以后才设成共同主持人
        roles.set_role("赵同学", "cohost")
        roles.set_role("赵同学", "participant")            # 又改回参会者
        self.m._note_guests()
        self.v.guests = ["王老师", "赵同学"]               # 小李中途走了
        self.m._note_guests()
        self.m._end_meeting(None, why="测试", by_button=True)
        got = self._people_file(mt["id"])
        self.assertEqual(got, {"主持人甲": "host", "王老师": "cohost", "小李": "participant", "赵同学": "participant"})
        self.assertNotIn("AI", got)                        # 没有论文的会：AI 没参加
        self.assertEqual(roles.get("王老师"), "participant")  # 散会照旧清掉这场的共同主持人（下一场提示恢复）
        self.assertIn("王老师", [x["name"] for x in roles.suggestions()])

    def test_resume_keeps_earlier_guests(self):
        from zuhui import roles
        mt = self._start()
        self.v.guests = ["王老师", "小李"]
        self.m._note_guests()
        roles.set_role("王老师", "cohost")
        self.m._note_guests()
        self.assertEqual(self._people_file(mt["id"])["王老师"], "cohost")   # 开着会就存了一份（设了身份也跟着存）
        self.m._seen_guests = {}                           # 后端重启：内存里记的人没了
        self.v.guests = ["王老师"]                         # 小李在重启前走了
        self.m._seen_guests = self.m._guests_saved(mt["id"])
        self.m._end_meeting(None, why="测试", by_button=True)
        self.assertEqual(self._people_file(mt["id"]), {"主持人甲": "host", "王老师": "cohost", "小李": "participant"})


class ExportPeopleTest(unittest.TestCase):
    def _g(self, mode, lines, people=None):
        return {"meeting": {"id": "x", "title": "组会", "started": "2026-11-19T10:00:00", "duration_s": 60, "mode": mode},
                "participants": people, "transcript": lines, "transcript_raw": lines}

    def test_chat_meeting_has_no_ai(self):
        from zuhui import export
        cfg = {"meeting": {"me": "主持人甲"}, "assistant": {"name": "小B"}}
        lines = [{"who": "teacher", "text": "先记一下", "t": "00:01", "sec": 1}]
        me_only = [{"id": "me", "name": "主持人甲", "role": "teacher", "rank": "host", "you": True}]
        self.assertEqual(export.info(self._g("chat", lines, me_only), cfg, "zh")["people"], ["主持人甲"])
        self.assertEqual(export.info(self._g("chat", lines, None), cfg, "zh")["people"], ["主持人甲"])
        self.assertEqual(export.info(self._g("paper", lines, None), cfg, "zh")["people"], ["主持人甲", "小B"])

    def test_md_export(self):
        from zuhui import meetings as mt
        lib = mt.Meetings(Path(tempfile.mkdtemp(prefix="rec-lib-")))
        m = lib.start(session="s-x", mode="chat")
        lib.add_line("teacher", "先记一下")
        lib.end()
        lib.save_participants(m["id"], [{"id": "me", "name": "当时的名字", "role": "teacher", "rank": "host", "you": True}])
        with mock.patch("zuhui.config.load", return_value={"meeting": {"me": "改过的名字"}, "assistant": {"name": "小B"}}):
            text = lib.export(m["id"], "md")["text"]
        self.assertIn("参会者：当时的名字\n", text)
        self.assertNotIn("小B", text)
        self.assertNotIn("改过的名字", text)
        self.assertIn("**当时的名字**", text)


STUB = r'''
import json, os, sys, threading
path = sys.argv[1]
nf = os.path.join(path, "n")
n = int(open(nf).read() or 0) if os.path.exists(nf) else 0
open(nf, "w").write(str(n + 1))
plan = [l.strip() for l in open(os.path.join(path, "plan"), encoding="utf-8") if l.strip()]
kind, *args = plan[min(n, len(plan) - 1)].split(":")
if kind == "die":
    os._exit(int(args[0]))
sys.stdout.write(json.dumps({"ready": True, "dev": "stub"}) + "\n"); sys.stdout.flush()
if kind == "ready_die":
    threading.Timer(float(args[0]), lambda: os._exit(int(args[1]))).start()
for line in sys.stdin:
    q = json.loads(line)
    sys.stdout.write(json.dumps({"id": q.get("id"), "p": 0.5}) + "\n"); sys.stdout.flush()
'''


class AddrRetryTest(unittest.TestCase):
    def setUp(self):
        from zuhui import addr
        self.addr = addr
        d = Path(tempfile.mkdtemp(prefix="addr-stub-"))
        (d / "addr_worker_stub.py").write_text(STUB, encoding="utf-8")
        self.model = d / "model"
        self.model.mkdir()
        self.p = [mock.patch.object(addr, "WORKER", d / "addr_worker_stub.py"),
                  mock.patch.object(addr.AddrModel, "RETRY_S", 0.2)]
        for x in self.p:
            x.start()
        self.made = []

    def tearDown(self):
        for m in self.made:
            m.close()
        for x in self.p:
            x.stop()

    def plan(self, *steps):
        (self.model / "plan").write_text("\n".join(steps) + "\n", encoding="utf-8")

    def new(self):
        m = self.addr.AddrModel(self.model, "cpu")
        self.made.append(m)
        return m

    def wait(self, fn, secs=20):
        t0 = time.time()
        while time.time() - t0 < secs:
            if fn():
                return True
            time.sleep(0.05)
        return False

    def launches(self):
        return int((self.model / "n").read_text())

    def test_retry_after_failed_start(self):
        self.plan("die:5", "ready")
        m = self.new()
        self.assertTrue(self.wait(lambda: m.ready))
        self.assertEqual((m.tries, self.launches()), (1, 2))
        self.assertEqual(m.prob([], "主持人", "主持人", "你好", None), 0.5)
        log = self.addr.WORKER_LOG.read_text(encoding="utf-8")
        self.assertIn("退出码 5", log)
        self.assertIn("第 1 次重试", log)
        self.assertTrue(str(self.addr.WORKER_LOG).startswith(str(zconfig.STATE)))   # 写在 ZUHUI_HOME 里

    def test_retry_after_exit_mid_meeting(self):
        self.plan("ready_die:0.5:7", "ready")
        m = self.new()
        self.assertTrue(self.wait(lambda: m.ready))
        self.assertTrue(self.wait(lambda: not m.ready))   # 开着会退出了
        self.assertTrue(self.wait(lambda: m.ready))       # 过一会儿自己又起来了
        self.assertIn("子进程退出了，退出码 7", self.addr.WORKER_LOG.read_text(encoding="utf-8"))
        self.assertEqual((m.tries, self.launches()), (1, 2))

    def test_give_up_then_fresh_next_meeting(self):
        self.plan("die:9")
        cfg = {"addr": {"enabled": True, "path": str(self.model)}}
        self.addr._insts.pop(str(self.model), None)
        m = self.addr.get(cfg)
        self.made.append(m)
        self.assertTrue(self.wait(lambda: m.gave_up))
        self.assertEqual((m.tries, self.launches()), (m.RETRIES, m.RETRIES + 1))
        self.assertIs(self.addr.get(cfg), m)              # 判每句时不换（不会一句拉一次）
        self.plan("ready")
        m2 = self.addr.get(cfg, fresh=True)               # 下一场开会：换一个新的再试
        self.made.append(m2)
        self.assertIsNot(m2, m)
        self.assertTrue(self.wait(lambda: m2.ready))
        self.addr._insts.pop(str(self.model), None)

    def test_missing_model_no_retry(self):
        m = self.addr.AddrModel(self.model / "不存在", "cpu")
        self.made.append(m)
        self.assertTrue(self.wait(lambda: m.gave_up))
        self.assertEqual(m.tries, 0)
        self.assertIn("FileNotFoundError", m.error or "")

    def test_release_while_loading(self):
        self.plan("die:3")
        m = self.new()
        m.close()                                         # 散会释放：不再重试
        time.sleep(0.6)
        self.assertFalse(m.gave_up)
        self.assertLessEqual(self.launches() if (self.model / "n").exists() else 0, 1)


class ResumeTailTest(unittest.TestCase):
    """后端重启后耳朵从末尾接着听：从报"开会"之前量好的位置读起，报完到真开始读之间说的话不丢。"""

    def test_start_pos(self):
        from zuhui import ears
        sess = Path(tempfile.mkdtemp(prefix="rec-sess-"))
        (sess / "live").mkdir()
        tp = sess / "live" / "transcript.jsonl"
        tp.write_text(json.dumps({"start": 1, "end": 2, "text": "重启前说的"}, ensure_ascii=False) + "\n", encoding="utf-8")
        pos = ears._transcript_end(sess)
        with open(tp, "a", encoding="utf-8") as f:     # 报了"开会"、还没开始读的这一会儿说的
            f.write(json.dumps({"start": 3, "end": 4, "text": "接上时说的"}, ensure_ascii=False) + "\n")
        got = []

        class C:
            buf, n, vad = [], 0, None

            def add(self, seg):
                got.append(seg["text"])

            def idle(self):
                return False

            def flush(self):
                pass

            def draft_tick(self):
                pass

            def preview(self, p):
                pass
        with mock.patch.object(ears.time, "sleep", lambda s: None):
            ears._tail_session(sess, False, lambda: False, C(), start_pos=pos)
        self.assertEqual(got, ["接上时说的"])
        got.clear()
        with mock.patch.object(ears.time, "sleep", lambda s: None):
            ears._tail_session(sess, False, lambda: False, C())          # 没给位置：照旧从现在的末尾
        self.assertEqual(got, [])


class UpcomingRefreshTest(_Guard):
    def test_paper_change_pushes_new_upcoming(self):
        pushed = []
        s = share.Share({"share": {"base_url": "https://127.0.0.1:1/"}, "meeting": {"me": "主持人甲"}})
        s._feed = (lambda: None, lambda: None, lambda: ("", {}))
        s._push = lambda what, body, ctype: pushed.append((what, json.loads(body) if body else None)) or {}
        s._have = lambda: {"sv": 1}
        s._with_avatars = lambda st, sent: st
        zconfig.clear_paper()
        with mock.patch.object(share.Share, "armed", new_callable=mock.PropertyMock, return_value=True), \
                mock.patch.object(share, "room_used", return_value=False):
            t = threading.Thread(target=s._push_loop, daemon=True)
            t.start()
            try:
                t0 = time.time()
                while time.time() - t0 < 10 and not pushed:
                    time.sleep(0.1)
                self.assertEqual(pushed[-1][1]["mode"], "chat")
                d = Path(tempfile.mkdtemp(prefix="rec-paper-"))
                f = d / "选好的论文.md"
                f.write_text("# 1 引言\n\n正文。\n", encoding="utf-8")
                zconfig.CURRENT_PAPER.write_text(json.dumps({"path": str(f), "name": "选好的论文"}, ensure_ascii=False), encoding="utf-8")
                t0 = time.time()
                while time.time() - t0 < 10 and pushed[-1][1].get("mode") != "paper":
                    time.sleep(0.1)
            finally:
                s.stop()
                t.join(5)
        self.assertEqual((pushed[-1][1]["mode"], pushed[-1][1]["title"]), ("paper", "选好的论文"))
        self.assertLess(len(pushed), 6)                    # 没变就不重推


if __name__ == "__main__":
    unittest.main()
