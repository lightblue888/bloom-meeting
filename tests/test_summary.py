# -*- coding: utf-8 -*-
"""纪要（第 1 批）：纪要格式的设置（保存、限长）、新纪要字段的整理（每个人、时间对到逐字稿）、旧纪要照常显示、
每人发言时长、回顾里换格式重新生成（后台任务、失败不盖掉原来的纪要）、HTTP 接口。

全离线、不花钱：模型一律用假的 ask_json 顶替（unittest.mock）；HTTP 那组在子进程里起对比页服务，ZUHUI_HOME 指到临时目录，
没设模型来源，重新生成会直接失败（不会去调任何接口）。不碰真实的 ~/.zuhui、不碰正在跑的 Bloom。

    python -X utf8 -m unittest tests.test_summary -v
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
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-summary-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import meetings as mt  # noqa: E402
from zuhui import summary_prefs  # noqa: E402

CFG = {"meeting": {"me": "林晓"}, "assistant": {"name": "小B"}}

LINES = [
    {"t": "00:12", "sec": 12.0, "who": "teacher", "text": "我们开始吧，今天先听王一凡讲进展。"},
    {"t": "00:20", "sec": 20.0, "who": "student", "text": "好的。", "tag": "#chat"},
    {"t": "01:05", "sec": 65.0, "who": "guest", "name": "王一凡", "text": "我这周把基线都跑完了，TimeGAN 还差一组。"},
    {"t": "02:10", "sec": 130.0, "who": "teacher", "text": "TimeGAN 下周补上，表 2 也要更新。"},
    {"t": "02:30", "sec": 150.0, "who": "system", "text": "切到 论文.md"},
    {"t": "03:30", "sec": 210.0, "who": "guest", "name": "赵雨桐", "text": "我这边数据划分改成按时间切了。"},
]


def fake_reply():
    return {
        "title": "进展汇报",
        "speakers": ["林晓", "王一凡", {"name": "赵雨桐"}, "王一凡"],
        "abstract": "王一凡和赵雨桐汇报进展。",
        "conclusions": [{"text": "TimeGAN 下周补上", "t": "02:10"}, "表 2 要更新（2:10）", {"text": "瞎编的时间", "t": "59:00"}],
        "sections": [{"title": "数据", "cards": [{"title": "划分", "bullets": [{"text": "按时间切", "t": "03:31"}]}]}],
        "people": [
            {"name": "王一凡", "said": [{"text": "基线跑完了，TimeGAN 差一组", "t": "01:05"}],
             "feedback": [{"text": "下周补上", "by": "林晓", "t": "02:10"}], "asks": [{"text": "更新表 2", "by": "林晓", "t": "02:10"}]},
            {"name": "王一凡", "said": [{"text": "重复的一项"}]},
            {"name": "空的人", "said": [], "feedback": [], "asks": []},
            {"name": "赵雨桐", "said": ["数据划分改成按时间切"]},
        ],
        "todos": [{"text": "王一凡补 TimeGAN", "who": "王一凡", "t": "02:10"}],
    }


class PrefsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zuhui-summary-prefs-"))
        self.p = mock.patch.object(summary_prefs, "PREFS_FILE", self.tmp / "summary_prefs.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_and_save(self):
        self.assertEqual(summary_prefs.load(), {"template": "default", "custom": ""})
        self.assertEqual(summary_prefs.save({"template": "progress"})["template"], "progress")
        self.assertEqual(summary_prefs.load()["template"], "progress")
        r = summary_prefs.save({"custom": "  每人三句：进展、问题、下周。\r\n老师意见单列。\x07 "})
        self.assertEqual(r["custom"], "每人三句：进展、问题、下周。\n老师意见单列。")
        self.assertEqual(summary_prefs.load(), {"template": "progress", "custom": "每人三句：进展、问题、下周。\n老师意见单列。"})
        pub = summary_prefs.public()
        self.assertEqual(pub["templates"], ["default", "progress", "paper", "proposal", "custom"])
        self.assertEqual(pub["custom_max"], 2000)

    def test_limits_and_bad_values(self):
        summary_prefs.save({"custom": "字" * 2000})                      # 正好 2000：收
        self.assertEqual(len(summary_prefs.load()["custom"]), 2000)
        with self.assertRaises(ValueError) as e:
            summary_prefs.save({"custom": "字" * 2001})
        self.assertEqual(str(e.exception), "too_long")
        self.assertEqual(len(summary_prefs.load()["custom"]), 2000)      # 超长的没存进去
        with self.assertRaises(ValueError) as e:
            summary_prefs.save({"template": "weekly"})
        self.assertEqual(str(e.exception), "template")
        summary_prefs.PREFS_FILE.write_text("{坏的", encoding="utf-8")     # 文件坏了：回到默认
        self.assertEqual(summary_prefs.load(), {"template": "default", "custom": ""})
        summary_prefs.PREFS_FILE.write_text(json.dumps({"template": "nope", "custom": "x" * 3000}), encoding="utf-8")
        self.assertEqual(summary_prefs.load(), {"template": "default", "custom": "x" * 2000})


class NormalizeTest(unittest.TestCase):
    def test_parse_clock(self):
        self.assertEqual(mt.parse_clock("03:21"), 201)
        self.assertEqual(mt.parse_clock("75:12"), 4512)
        self.assertEqual(mt.parse_clock("1:02:03"), 3723)
        self.assertEqual(mt.parse_clock("[03:21]"), 201)
        self.assertEqual(mt.parse_clock(12.5), 12.5)
        for bad in ("3:61", "abc", "", None, True, -3):
            self.assertIsNone(mt.parse_clock(bad), bad)

    def test_new_fields(self):
        s = mt.normalize_summary(fake_reply(), LINES)
        self.assertEqual(s["speakers"], ["林晓", "王一凡", "赵雨桐"])
        c = s["conclusions"]
        self.assertEqual(c[0], {"text": "TimeGAN 下周补上", "t": "02:10", "sec": 130.0})
        self.assertEqual(c[1], {"text": "表 2 要更新", "t": "02:10", "sec": 130.0})      # 句末括号里的时间挪出来
        self.assertEqual(c[2], {"text": "瞎编的时间"})                                    # 离哪一行都太远：不写时间
        card = s["sections"][0]["cards"][0]
        self.assertEqual(card["bullets"][0], {"text": "按时间切", "t": "03:30", "sec": 210.0})  # 对到最近的那一行
        self.assertEqual((s["sections"][0]["t"], s["sections"][0]["sec"]), ("03:30", 210.0))  # 没写就取最早的一条
        self.assertEqual([p["name"] for p in s["people"]], ["王一凡", "赵雨桐"])          # 重名的、全空的不要
        w = s["people"][0]
        self.assertEqual(w["feedback"], [{"text": "下周补上", "t": "02:10", "sec": 130.0, "by": "林晓"}])
        self.assertEqual(w["asks"][0]["by"], "林晓")
        self.assertEqual(s["people"][1]["said"], [{"text": "数据划分改成按时间切"}])
        self.assertEqual(s["todos"], [{"text": "王一凡补 TimeGAN", "t": "02:10", "sec": 130.0, "who": "王一凡"}])

    def test_limits(self):
        s = mt.normalize_summary({"title": "题" * 100, "conclusions": ["长" * 1000] * 30, "people": "不是列表", "sections": [1, None]}, LINES)
        self.assertEqual(len(s["title"]), 60)
        self.assertEqual(len(s["conclusions"]), 12)
        self.assertEqual(len(s["conclusions"][0]["text"]), 400)
        self.assertEqual((s["people"], s["sections"], s["todos"]), ([], [], []))

    def test_old_summary_still_reads(self):
        old = {"title": "旧纪要", "speakers": ["林晓", "Claude"], "abstract": "摘要。", "conclusions": ["结论一", "结论二"],
               "sections": [{"title": "主题", "cards": [{"title": "卡片", "bullets": ["要点"]}]}], "todos": ["待办"]}
        md = mt.render_markdown(old)                                       # 旧纪要的 Markdown 和以前一字不差
        self.assertEqual(md, "# 旧纪要\n\n发言人：林晓、Claude\n\n主题摘要：摘要。\n\n## 核心结论\n\n- 结论一\n- 结论二\n\n"
                             "## 主题\n\n### 01 卡片\n\n- 要点\n\n## 后续工作安排\n\n- 待办\n")
        s = mt.normalize_summary(old, LINES)                               # 旧格式也能过：各条变成 {"text"}
        self.assertEqual(s["conclusions"], [{"text": "结论一"}, {"text": "结论二"}])
        self.assertEqual(s["people"], [])

    def test_markdown_new(self):
        s = mt.normalize_summary(fake_reply(), LINES)
        s["template"] = "progress"
        md = mt.render_markdown(s)
        self.assertIn("- TimeGAN 下周补上 `02:10`", md)
        self.assertIn("### 王一凡", md)
        self.assertIn("进展\n\n- 基线跑完了，TimeGAN 差一组 `01:05`", md)
        self.assertIn("- 林晓：下周补上 `02:10`", md)
        self.assertLess(md.index("## 每个人"), md.index("## 数据"))          # 进展汇报：每个人在前
        s["template"] = "default"
        md = mt.render_markdown(s)
        self.assertGreater(md.index("## 每个人"), md.index("## 数据"))
        self.assertIn("讲了什么", md)


class TalkTimeTest(unittest.TestCase):
    def test_estimate_and_measured(self):
        lines = [
            {"sec": 0, "who": "teacher", "text": "一二三四五六七八九"},                      # 9 字 = 2 秒（下一句 10 秒后）
            {"sec": 10, "who": "guest", "name": "王一凡", "text": "字" * 90},                # 估 20 秒，下一句 12 秒后：12 秒
            {"sec": 22, "who": "student", "text": "OK sure I will do it"},                  # 6 个词 / 2.6
            {"sec": 30, "who": "teacher", "text": "很长" * 100, "start": 100.0, "end": 106.5},  # 重转写过：6.5 秒
            {"sec": 31, "who": "system", "text": "切到 论文"},
            {"sec": 32, "who": "student", "text": "没念出来的答案" * 20, "unspoken": True},
            {"sec": 40, "who": "guest", "name": "赵雨桐", "text": "   "},
            {"sec": 50, "who": "guest", "name": "赵雨桐", "text": "好"},
        ]
        r = mt.talk_time(lines)
        by = {(x["who"], x["name"]): x for x in r}
        self.assertEqual(set(by), {("teacher", None), ("guest", "王一凡"), ("student", None), ("guest", "赵雨桐")})
        self.assertAlmostEqual(by[("teacher", None)]["sec"], 8.5, places=1)
        self.assertEqual(by[("teacher", None)]["n"], 2)
        self.assertAlmostEqual(by[("guest", "王一凡")]["sec"], 12.0, places=1)
        self.assertAlmostEqual(by[("student", None)]["sec"], round(6 / mt.WORDS_PER_S, 1), places=1)
        self.assertEqual(by[("guest", "赵雨桐")]["n"], 1)
        self.assertEqual([x["sec"] for x in r], sorted([x["sec"] for x in r], reverse=True))   # 从多到少
        self.assertAlmostEqual(sum(x["share"] for x in r), 1.0, places=2)

    def test_same_start_and_empty(self):
        self.assertEqual(mt.talk_time([]), [])
        r = mt.talk_time([{"sec": 5, "who": "teacher", "text": "好的好的"}, {"sec": 5, "who": "guest", "name": "甲", "text": "嗯嗯嗯嗯"},
                          {"sec": 6, "who": "teacher", "text": "再说一句话吧"}])
        by = {(x["who"], x["name"]): x["sec"] for x in r}
        self.assertAlmostEqual(by[("guest", "甲")], 0.9, places=1)          # 同一时刻开口的两句都只能说到下一句开口（1 秒）
        self.assertGreater(by[("teacher", None)], 1.0)


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zuhui-summary-"))
        self.p = mock.patch.object(summary_prefs, "PREFS_FILE", self.tmp / "summary_prefs.json")
        self.p.start()
        self.seen: list[dict] = []

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, reply=None, error: Exception | None = None):
        def ask_json(prompt, system="", **kw):
            self.seen.append({"prompt": prompt, "system": system})
            if error:
                raise error
            return reply if reply is not None else fake_reply()
        return mock.patch("zuhui.claude_text.ask_json", ask_json)

    def test_templates_and_prompt(self):
        with self.fake():
            s = mt.summarize(CFG, {"title": "组会", "duration_s": 600}, LINES, [], template="progress")
        self.assertEqual(s["template"], "progress")
        sys_ = self.seen[-1]["system"]
        self.assertIn("进展汇报", sys_)
        self.assertIn("小B", sys_)                                           # AI 叫什么照设置，不写死
        self.assertNotIn("Claude", sys_)
        self.assertIn("[01:05] 王一凡：", self.seen[-1]["prompt"])
        self.assertEqual(s["people"][0]["name"], "王一凡")
        self.assertIn("## 每个人", s["markdown"])
        summary_prefs.save({"template": "custom", "custom": "每人一段，最后列老师意见。"})
        with self.fake():
            s = mt.summarize(CFG, {"title": "组会"}, LINES, [])              # 不给 template：按设置
        self.assertEqual(s["template"], "custom")
        self.assertIn("每人一段，最后列老师意见。", self.seen[-1]["system"])
        summary_prefs.save({"custom": ""})
        with self.fake():
            s = mt.summarize(CFG, {"title": "组会"}, LINES, [])              # 自定义没写要求：按默认写
        self.assertEqual(s["template"], "default")
        self.assertNotIn("本组对纪要的要求", self.seen[-1]["system"])

    def test_failure(self):
        with self.fake(error=RuntimeError("还没设置模型来源")):
            s = mt.summarize(CFG, {"title": "组会"}, LINES, [{"id": "T1", "status": "done", "title": "改表 2"}])
        self.assertTrue(s["source"].startswith("fallback"))                  # 散会后的流水线：兜底纪要
        with self.fake(error=RuntimeError("还没设置模型来源")):
            with self.assertRaises(RuntimeError) as e:
                mt.summarize(CFG, {"title": "组会"}, LINES, [], fallback=False)
        self.assertIn("还没设置模型来源", str(e.exception))
        with self.fake(reply=["不是对象"]):
            with self.assertRaises(RuntimeError):
                mt.summarize(CFG, {"title": "组会"}, LINES, [], fallback=False)
        self.assertEqual(len(self.seen), 4)                                  # 不是 JSON 对象：再要一次精简版，还不行才放弃

    def test_meetings_get_and_regenerate(self):
        lib = mt.Meetings(root=self.tmp / "meetings")
        d = lib.root / "20261118-100000"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": d.name, "title": "组会", "started": "2026-11-18T10:00:00", "ended": "2026-11-18T10:30:00",
                                                 "state": "ended", "duration_s": 1800}, ensure_ascii=False), encoding="utf-8")
        (d / "transcript.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in LINES), encoding="utf-8")
        old = {"title": "旧纪要", "speakers": ["林晓"], "abstract": "摘要", "conclusions": ["结论"], "sections": [], "todos": ["待办"]}
        (d / "summary.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        g = lib.get(d.name)
        self.assertEqual(g["summary"], old)                                  # 旧纪要原样给前端
        self.assertEqual({(x["who"], x["name"]) for x in g["talk"]},
                         {("teacher", None), ("student", None), ("guest", "王一凡"), ("guest", "赵雨桐")})
        with self.fake(error=RuntimeError("接口 401")):
            with self.assertRaises(RuntimeError):
                mt.regenerate(lib, CFG, d.name, "paper")
        self.assertEqual(json.loads((d / "summary.json").read_text(encoding="utf-8")), old)   # 失败：原来的纪要不动
        with self.fake():
            mt.regenerate(lib, CFG, d.name, "paper")
        s = lib.get(d.name)["summary"]
        self.assertEqual(s["template"], "paper")
        self.assertEqual(s["conclusions"][0]["t"], "02:10")
        self.assertIn("论文讨论", self.seen[-1]["system"])
        self.assertTrue((d / "summary.md").read_text(encoding="utf-8").startswith("# 进展汇报"))
        with self.assertRaises(KeyError):
            mt.regenerate(lib, CFG, "nope", "default")


SERVER = r"""
import json, os, sys, socket, time, http.client
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from zuhui import relay_cfg, share
share.PUBLIC_RELAY = "https://127.0.0.1:1/"
relay_cfg.set_mode("off")
from zuhui.config import STATE
from zuhui.view import Viewer
d = STATE / "meetings" / "20261118-090000"
d.mkdir(parents=True)
(d / "meta.json").write_text(json.dumps({"id": d.name, "title": "组会", "started": "2026-11-18T09:00:00", "ended": "2026-11-18T09:20:00",
                                         "state": "ended", "duration_s": 1200}), encoding="utf-8")
(d / "transcript.jsonl").write_text(json.dumps({"t": "00:05", "sec": 5, "who": "teacher", "text": "开始吧"}, ensure_ascii=False) + "\n", encoding="utf-8")
(d / "summary.json").write_text(json.dumps({"title": "旧", "speakers": [], "abstract": "a", "conclusions": ["c"], "sections": [], "todos": []}), encoding="utf-8")
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = Viewer(port=port)
v.serve(open_browser=False)
def req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse(); data = r.read()
    return [r.status, json.loads(data.decode("utf-8"))]
out = {}
out["prefs0"] = req("GET", "/api/summary/prefs")
out["save"] = req("POST", "/api/summary/prefs", {"template": "proposal"})
out["bad"] = req("POST", "/api/summary/prefs", {"template": "x"})
out["long"] = req("POST", "/api/summary/prefs", {"custom": "a" * 2001})
out["max"] = req("POST", "/api/summary/prefs", {"custom": "a" * 2000})
req("POST", "/api/summary/prefs", {"custom": ""})
out["regen_bad"] = req("POST", "/api/meetings/" + d.name + "/summary", {"template": "weekly"})
out["regen_404"] = req("POST", "/api/meetings/20000101-000000/summary", {"template": "default"})
out["regen_custom_empty"] = req("POST", "/api/meetings/" + d.name + "/summary", {"template": "custom"})
out["regen"] = req("POST", "/api/meetings/" + d.name + "/summary", {"template": "paper"})
job = None
for _ in range(100):                     # 没设模型来源：后台马上失败（不调任何接口）
    det = req("GET", "/api/meetings/" + d.name)[1]
    job = det.get("summary_job")
    if job and job.get("state") != "running":
        break
    time.sleep(0.1)
out["job"] = job
out["detail_talk"] = det.get("talk")
out["summary_after"] = det.get("summary")
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class HttpTest(unittest.TestCase):
    def test_http(self):
        env = dict(os.environ, ZUHUI_HOME=tempfile.mkdtemp(prefix="zuhui_home_summary_"), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", SERVER, str(ROOT)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        self.assertEqual(r["prefs0"][1]["template"], "default")
        self.assertEqual((r["save"][0], r["save"][1]["template"]), (200, "proposal"))
        self.assertEqual(r["bad"], [400, {"error": "template"}])
        self.assertEqual(r["long"], [400, {"error": "too_long"}])
        self.assertEqual((r["max"][0], len(r["max"][1]["custom"])), (200, 2000))
        self.assertEqual(r["regen_bad"], [400, {"ok": False, "error": "template"}])
        self.assertEqual(r["regen_404"], [404, {"ok": False, "error": "not_found"}])
        self.assertEqual(r["regen_custom_empty"], [400, {"ok": False, "error": "custom_empty"}])
        self.assertEqual(r["regen"][0], 200)
        self.assertEqual((r["regen"][1]["job"]["state"], r["regen"][1]["job"]["template"]), ("running", "paper"))
        self.assertEqual(r["job"]["state"], "failed")
        self.assertIn("模型来源", r["job"]["error"])
        self.assertEqual(r["summary_after"]["title"], "旧")                     # 失败：原来的纪要还在
        self.assertEqual(r["detail_talk"][0]["who"], "teacher")


if __name__ == "__main__":
    unittest.main()
