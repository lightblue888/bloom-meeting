# -*- coding: utf-8 -*-
"""第 7 批：回顾里手改逐字稿（zuhui/transcript_edit.py）——改一句、整场替换（可加进热词）、访客改名、撤销；
原始版和干净版的文件不动；回顾（Meetings.get）、每人发言时长、参会人、导出、跨会议检索（past.py）都用改过的；
会还在开、会后整理没完不能改；HTTP 接口。

全离线、不花钱：ZUHUI_HOME 指到临时目录，不调模型，不碰真实的 ~/.zuhui、不碰正在跑的 Bloom。

    python -X utf8 -m unittest tests.test_b7_transcript_edit -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-b7-edit-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import meetings as mt  # noqa: E402
from zuhui import past  # noqa: E402
from zuhui import transcript_edit as te  # noqa: E402
from zuhui import voice_prefs  # noqa: E402

CFG = {"meeting": {"me": "林晓"}, "assistant": {"name": "小B"}}
LINES = [
    {"t": "00:01", "sec": 1.0, "who": "teacher", "text": "我们先看一下欧佩斯的结果"},
    {"t": "00:05", "sec": 5.0, "who": "guest", "name": "iPhone", "text": "嗯那个欧佩斯跑了三次"},
    {"t": "00:09", "sec": 9.0, "who": "student", "text": "三次的平均是 0.82"},
    {"t": "00:12", "sec": 12.0, "who": "system", "text": "切到 欧佩斯 附录"},
    {"t": "00:15", "sec": 15.0, "who": "guest", "name": "iPhone", "text": "第二张表是欧佩斯的"},
    {"t": "00:20", "sec": 20.0, "who": "guest", "name": "阿杰", "text": "我补充一句"},
]


def make(lib: mt.Meetings, mid: str = "20261110-100000", cleanup: list[dict] | None = None) -> Path:
    d = lib.root / mid
    d.mkdir(parents=True)
    started = "2026-11-10T10:00:00"
    (d / "meta.json").write_text(json.dumps({"id": mid, "title": "组会", "started": started, "ended": started,
                                             "state": "ended", "duration_s": 600,
                                             "papers": [{"id": "aaaa1111", "name": "IBP", "path": "C:/p/ibp.md"}]},
                                            ensure_ascii=False), encoding="utf-8")
    (d / "transcript.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in LINES), encoding="utf-8")
    (d / "participants.json").write_text(json.dumps([
        {"id": "me", "name": "林晓", "role": "teacher", "you": True},
        {"id": "claude", "name": "小B", "role": "student"},
        {"id": "iPhone", "name": "iPhone", "role": "guest"},
        {"id": "阿杰", "name": "阿杰", "role": "guest"}], ensure_ascii=False), encoding="utf-8")
    if cleanup is not None:
        (d / "cleanup.json").write_text(json.dumps({"version": 1, "items": cleanup}, ensure_ascii=False), encoding="utf-8")
        mt.write_clean(d, mt.apply_cleanup(mt.Meetings._load_lines(d), cleanup))
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zuhui-b7-edit-"))
        self.lib = mt.Meetings(root=self.tmp / "meetings")
        self.mid = "20261110-100000"
        self.d = make(self.lib, self.mid, cleanup=[{"id": "c1", "line": 1, "kind": "retract", "before": "嗯那个", "after": ""}])
        self.raw_before = (self.d / "transcript.jsonl").read_bytes()
        self.clean_before = (self.d / "transcript.clean.jsonl").read_bytes()
        past._INDEX.__init__()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def edit(self, **body):
        return te.edit(self.lib, self.mid, body, CFG)

    def texts(self):
        return [l["text"] for l in self.lib.get(self.mid)["transcript"]]


class EditTest(Base):
    def test_line_replace_speaker_undo(self):
        g = self.lib.get(self.mid)
        self.assertEqual(g["edits"], {"n": 0, "last": None})
        self.assertEqual(g["transcript"][1]["text"], "欧佩斯跑了三次")            # 整理删掉的口头禅
        # 改一句：改的是看到的那一版（干净版），按原始版的行号记
        r = self.edit(op="line", i=2, text="三次的平均是 0.83")
        self.assertEqual((r["n"], r["last"]["op"]), (1, "line"))
        self.assertEqual(self.texts()[2], "三次的平均是 0.83")
        self.assertTrue(self.lib.get(self.mid)["transcript"][2]["edited"])
        # 整场替换：只换人说的话（系统行不动），换了几处照实说
        r = self.edit(op="replace", find="欧佩斯", repl="Opus")
        self.assertEqual(r["count"], 3)
        t = self.texts()
        self.assertEqual(t[0], "我们先看一下Opus的结果")
        self.assertEqual(t[3], "切到 欧佩斯 附录")
        self.assertEqual(t[4], "第二张表是Opus的")
        # 访客改名：这个人所有的句子
        self.edit(**{"op": "speaker", "from": "iPhone", "to": "小周"})
        g = self.lib.get(self.mid)
        self.assertEqual([l.get("name") for l in g["transcript"] if l["who"] == "guest"], ["小周", "小周", "阿杰"])
        self.assertEqual({x["name"] for x in g["talk"] if x["who"] == "guest"}, {"小周", "阿杰"})
        self.assertEqual([p["name"] for p in g["participants"]], ["林晓", "小B", "小周", "阿杰"])
        # 原始版、干净版的文件一个字没动；原始版照旧给（转写训练、对录屏用）
        self.assertEqual((self.d / "transcript.jsonl").read_bytes(), self.raw_before)
        self.assertEqual((self.d / "transcript.clean.jsonl").read_bytes(), self.clean_before)
        self.assertEqual(g["transcript_raw"][4]["text"], "第二张表是欧佩斯的")
        # 撤销：一条一条往回
        self.assertEqual(self.edit(op="undo")["last"]["op"], "replace")
        self.assertEqual(self.lib.get(self.mid)["transcript"][1].get("name"), "iPhone")
        self.edit(op="undo")
        self.assertEqual(self.texts()[0], "我们先看一下欧佩斯的结果")
        r = self.edit(op="undo")
        self.assertEqual(r, {"ok": True, "n": 0, "last": None})
        self.assertEqual(self.texts()[2], "三次的平均是 0.82")
        with self.assertRaisesRegex(ValueError, "nothing"):
            self.edit(op="undo")

    def test_delete_line_and_merge_names(self):
        self.edit(op="line", i=5, text="  ")
        self.assertNotIn("我补充一句", self.texts())
        self.edit(**{"op": "speaker", "from": "iPhone", "to": "阿杰"})          # 同一个人换了个名字进会：并成一个
        g = self.lib.get(self.mid)
        self.assertEqual([p["name"] for p in g["participants"]], ["林晓", "小B", "阿杰"])
        self.assertEqual([x["name"] for x in g["talk"] if x["who"] == "guest"], ["阿杰"])

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, "no_match"):
            self.edit(op="replace", find="没有这个词", repl="x")
        with self.assertRaisesRegex(ValueError, "too_long"):
            self.edit(op="replace", find="欧" * 41, repl="x")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.edit(op="replace", find="  ", repl="x")
        with self.assertRaisesRegex(ValueError, "same"):
            self.edit(op="replace", find="欧佩斯", repl="欧佩斯")
        with self.assertRaisesRegex(ValueError, "too_long"):
            self.edit(op="line", i=0, text="长" * (te.LINE_MAX + 1))
        with self.assertRaisesRegex(ValueError, "not_found"):
            self.edit(op="line", i=99, text="x")
        with self.assertRaisesRegex(ValueError, "not_found"):
            self.edit(op="line", i=3, text="系统行不能改")
        with self.assertRaisesRegex(ValueError, "same"):
            self.edit(op="line", i=0, text="我们先看一下欧佩斯的结果")
        with self.assertRaisesRegex(ValueError, "taken"):
            self.edit(**{"op": "speaker", "from": "iPhone", "to": "林晓"})       # 和主持人重名
        with self.assertRaisesRegex(ValueError, "taken"):
            self.edit(**{"op": "speaker", "from": "iPhone", "to": "小B"})        # 和 AI 重名
        with self.assertRaisesRegex(ValueError, "taken"):                        # 设置里改过名：这场会当时的名字也算
            te.edit(self.lib, self.mid, {"op": "speaker", "from": "iPhone", "to": "林晓"}, {"meeting": {"me": "新名字"}})
        with self.assertRaisesRegex(ValueError, "too_long"):
            self.edit(**{"op": "speaker", "from": "iPhone", "to": "名" * 33})
        with self.assertRaisesRegex(ValueError, "not_found"):
            self.edit(**{"op": "speaker", "from": "没来的人", "to": "x"})
        with self.assertRaisesRegex(ValueError, "bad"):
            self.edit(op="nope")
        with self.assertRaisesRegex(ValueError, "not_found"):
            te.edit(self.lib, "20990101-000000", {"op": "undo"}, CFG)
        self.assertEqual(te.load(self.d), [])

    def test_export_and_past_use_edited(self):
        self.edit(op="replace", find="欧佩斯", repl="Opus")
        self.edit(**{"op": "speaker", "from": "iPhone", "to": "小周"})
        md = self.lib.export(self.mid, "md")["text"]
        self.assertIn("第二张表是Opus的", md)
        self.assertIn("**小周**", md)
        self.assertNotIn("iPhone", md)
        raw = self.lib.export(self.mid, "txt", "raw")["text"]
        self.assertIn("嗯那个欧佩斯跑了三次", raw)                                   # 原始版照原样
        lines = past._lines_of(self.d)
        self.assertIn("第二张表是Opus的", [l["text"] for l in lines])
        chunks = past._chunks(self.d, {"id": self.mid})
        self.assertTrue(any("Opus" in c["text"] and "小周" in c["text"] for c in chunks if c["kind"] == "line"))
        sig = past._sig(self.d)
        self.edit(op="undo")
        self.assertNotEqual(past._sig(self.d), sig)                                   # 撤销了：检索的缓存跟着重建

    def test_no_cleanup_yet(self):
        mid2 = "20261111-100000"
        d2 = make(self.lib, mid2)
        r = te.edit(self.lib, mid2, {"op": "line", "i": 1, "text": "欧佩斯跑了三次"}, CFG)
        self.assertEqual(r["n"], 1)
        self.assertEqual(self.lib.get(mid2)["transcript"][1]["text"], "欧佩斯跑了三次")
        self.assertEqual(past._lines_of(d2)[1]["text"], "欧佩斯跑了三次")

    def test_hotword(self):
        voice_prefs.save({"hotwords": ["张老师"]})
        r = self.edit(op="replace", find="欧佩斯", repl="Opus", hot=True)
        self.assertEqual(r["hot"], "added")
        self.assertEqual(voice_prefs.hotwords(), ["张老师", "Opus"])
        self.edit(op="undo")
        r = self.edit(op="replace", find="欧佩斯", repl="Opus", hot=True)
        self.assertEqual(r["hot"], "exists")
        voice_prefs.save({"hotwords": [f"w{k}" for k in range(voice_prefs.HOT_MAX)]})
        self.edit(op="undo")
        self.assertEqual(self.edit(op="replace", find="欧佩斯", repl="Opus", hot=True)["hot"], "full")
        voice_prefs.save({"hotwords": []})

    def test_post_guards(self):
        v = SimpleNamespace(meetings=self.lib, cfg=CFG, processing=lambda mid: mid == self.mid)
        self.assertEqual(te.post(v, self.mid, {"op": "undo"}), ({"ok": False, "error": "busy"}, 409))
        v.processing = lambda mid: False
        out, code = te.post(v, self.mid, {"op": "replace", "find": "欧佩斯", "repl": "Opus"})
        self.assertEqual((code, out["count"]), (200, 3))
        self.assertEqual(te.post(v, self.mid, {"op": "replace", "find": "无", "repl": "x"}), ({"ok": False, "error": "no_match"}, 400))
        live = self.lib.start(session="s", title="开着的会")
        self.assertEqual(te.post(v, live["id"], {"op": "undo"}), ({"ok": False, "error": "live"}, 409))
        self.lib.end()


SERVER = r"""
import json, os, sys, socket, http.client
sys.path.insert(0, sys.argv[1])
from zuhui import relay_cfg, share
share.PUBLIC_RELAY = "https://127.0.0.1:1/"
relay_cfg.set_mode("off")
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
from zuhui.view import Viewer
v = Viewer(port=port)
v.serve(open_browser=False)
def req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse(); data = r.read()
    return [r.status, json.loads(data.decode("utf-8"))]
m = v.meetings.start(session="s1", title="组会")
v.meetings.add_line("teacher", "我们先看欧佩斯")
v.meetings.add_line("guest", "欧佩斯跑了三次", name="iPhone")
out = {"live": req("POST", "/api/meetings/" + m["id"] + "/transcript", {"op": "undo"})}
v.meetings.end()
mid = m["id"]
out["replace"] = req("POST", "/api/meetings/" + mid + "/transcript", {"op": "replace", "find": "欧佩斯", "repl": "Opus"})
out["rename"] = req("POST", "/api/meetings/" + mid + "/transcript", {"op": "speaker", "from": "iPhone", "to": "小周"})
out["bad"] = req("POST", "/api/meetings/" + mid + "/transcript", {"op": "line", "i": 0, "text": "x" * 3000})
out["missing"] = req("POST", "/api/meetings/20990101-000000/transcript", {"op": "undo"})
out["detail"] = req("GET", "/api/meetings/" + mid)[1]
out["export"] = req("GET", "/api/meetings/" + mid + "/export?fmt=txt")[1]
out["pron"] = req("POST", "/api/voice", {"pron": [{"w": "LaTeX", "r": "雷泰克"}]})
out["pron_bad"] = req("POST", "/api/voice", {"pron": [{"w": "x" * 41, "r": "y"}]})
out["voice"] = req("GET", "/api/voice")[1]
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class HttpTest(unittest.TestCase):
    def test_http(self):
        env = dict(os.environ, ZUHUI_HOME=tempfile.mkdtemp(prefix="zuhui_home_b7_edit_"), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", SERVER, str(ROOT)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        self.assertEqual(r["live"], [409, {"ok": False, "error": "live"}])
        self.assertEqual((r["replace"][0], r["replace"][1]["count"]), (200, 2))
        self.assertEqual(r["rename"][1]["last"], {"op": "speaker", "from": "iPhone", "to": "小周"})
        self.assertEqual(r["bad"], [400, {"ok": False, "error": "too_long"}])
        self.assertEqual(r["missing"], [404, {"ok": False, "error": "not_found"}])
        d = r["detail"]
        self.assertEqual([l["text"] for l in d["transcript"]], ["我们先看Opus", "Opus跑了三次"])
        self.assertEqual(d["transcript"][1]["name"], "小周")
        self.assertEqual(d["edits"]["n"], 2)
        self.assertEqual([l["text"] for l in d["transcript_raw"]], ["我们先看欧佩斯", "欧佩斯跑了三次"])
        self.assertIn("小周：Opus跑了三次", r["export"]["text"])
        self.assertEqual(r["pron"][1]["pron"], [{"w": "LaTeX", "r": "雷泰克"}])
        self.assertEqual(r["pron_bad"], [400, {"error": "pron_too_long"}])
        self.assertEqual((r["voice"]["pron_max"], r["voice"]["pron_len"]), (200, 40))


if __name__ == "__main__":
    unittest.main()
