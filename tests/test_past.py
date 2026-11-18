# -*- coding: utf-8 -*-
"""跨会议（第 2 批）：往期要点怎么挑（同一篇论文、几场、多长）、遗留项（挑哪些、勾掉存在哪、纪要说落实了自动勾）、
开会时带进来的遗留和散会纪要里的"上次遗留"、检索往期纪要和逐字稿、会上 AI 的前缀和这一轮上下文、查论文带上往期、
回顾里提问（限长、出处、没检索到不调模型）、阶段汇总（选几场、存下来、Markdown）、HTTP 接口。

全离线、不花钱：模型一律用假的 ask_json / hands_api._ask 顶替；HTTP 那组在子进程里起对比页服务，ZUHUI_HOME 指到临时目录，
没设模型来源（提问走到模型那一步会直接失败，不会去调任何接口）。不碰真实的 ~/.zuhui、不碰正在跑的 Bloom。

    python -X utf8 -m unittest tests.test_past -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-past-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import meetings as mt  # noqa: E402
from zuhui import past  # noqa: E402

CFG = {"meeting": {"me": "林晓"}, "assistant": {"name": "小B"}}
PA = {"id": "aaaa1111", "name": "FooBench 论文", "path": "C:/papers/ibp/ibp.md"}
PB = {"id": "bbbb2222", "name": "大创申报书", "path": "C:/papers/dc/dc.md"}


def make(lib: mt.Meetings, mid: str, papers: list[dict], summary: dict | None = None, tasks: list[dict] | None = None,
         lines: list[dict] | None = None, title: str = "组会", state: str = "ended") -> Path:
    d = lib.root / mid
    d.mkdir(parents=True)
    started = f"{mid[:4]}-{mid[4:6]}-{mid[6:8]}T{mid[9:11]}:{mid[11:13]}:{mid[13:15]}"
    (d / "meta.json").write_text(json.dumps({"id": mid, "title": title, "started": started, "ended": started, "state": state,
                                             "duration_s": 1800, "papers": papers, "active_paper": papers[0]["id"] if papers else None},
                                            ensure_ascii=False), encoding="utf-8")
    (d / "transcript.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in (lines or [])), encoding="utf-8")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    if tasks is not None:
        (d / "board.json").write_text(json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")
    return d


def summ(tag: str, todos=None, people=None, conclusions=None) -> dict:
    return {"title": f"纪要{tag}", "speakers": ["林晓"], "abstract": f"摘要{tag}",
            "conclusions": conclusions if conclusions is not None else [{"text": f"结论{tag}", "t": "01:00", "sec": 60}],
            "sections": [], "people": people or [], "todos": todos if todos is not None else []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zuhui-past-"))
        self.lib = mt.Meetings(root=self.tmp / "meetings")
        self.p = mock.patch.object(past, "DIGESTS", self.tmp / "digests")
        self.p.start()
        past._INDEX.__init__()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class SamePaperTest(Base):
    def test_keys_and_related(self):
        self.assertIn("id:aaaa1111", past.paper_keys(PA))
        self.assertNotIn("name:main", past.paper_keys({"id": "x", "name": "main"}))       # 太常见的名字不算
        make(self.lib, "20261101-100000", [PA])
        make(self.lib, "20261102-100000", [PB])
        make(self.lib, "20261103-100000", [dict(PA, id="cccc3333", path="D:/copy/ibp.md")])   # 副本：编号变了，名字对得上
        make(self.lib, "20261104-100000", [PB, PA])                                         # 两篇都摊开过
        make(self.lib, "20261105-100000", [PA], state="live")                               # 正在开的不算
        ids = [m["id"] for m in past.related(self.lib, PA)]
        self.assertEqual(ids, ["20261104-100000", "20261103-100000", "20261101-100000"])  # 新的在前
        self.assertEqual([m["id"] for m in past.related(self.lib, PA, exclude="20261104-100000", limit=1)], ["20261103-100000"])
        self.assertEqual(past.related(self.lib, None), [])
        rows = self.lib.list()
        self.assertEqual(next(r for r in rows if r["id"] == "20261104-100000")["papers"],
                         [{"id": "bbbb2222", "name": "大创申报书"}, {"id": "aaaa1111", "name": "FooBench 论文"}])


class BriefTest(Base):
    def test_picks_same_paper_and_limits(self):
        for i in range(1, 6):
            make(self.lib, f"2026110{i}-100000", [PA], summ(str(i), todos=[{"text": f"待办{i}", "who": "王一凡"}],
                 people=[{"name": "王一凡", "said": [], "feedback": [{"text": f"意见{i}", "by": "林晓"}], "asks": []}]),
                 title=f"第{i}次")
        make(self.lib, "20261106-100000", [PB], summ("B", todos=["别的论文的待办"]), title="大创")
        b = past.brief(self.lib, PA, carried=[])
        self.assertTrue(b.startswith(past.BRIEF_HEAD))
        self.assertIn("11月5日《第5次》", b)
        self.assertIn("11月3日《第3次》", b)
        self.assertNotIn("第2次", b)                                    # 只看最近三场
        self.assertNotIn("别的论文", b)                                 # 别的论文的会不进来
        self.assertIn("王一凡：收到的意见 林晓：意见5", b)
        self.assertIn("待办：待办5（王一凡）", b)
        self.assertLessEqual(len(b), past.BRIEF_CHARS)
        b2 = past.brief(self.lib, PA, exclude="20261105-100000", carried=[])
        self.assertNotIn("第5次", b2)
        self.assertIn("第2次", b2)
        self.assertEqual(past.brief(self.lib, {"id": "nope", "name": "没开过会的论文"}), "")

    def test_budget(self):
        long = [{"text": "很长的结论" * 60}] * 12
        for i in range(1, 4):
            make(self.lib, f"2026110{i}-100000", [PA], summ(str(i), conclusions=long, todos=[{"text": "待办" * 80}] * 10))
        b = past.brief(self.lib, PA)
        self.assertLessEqual(len(b), past.BRIEF_CHARS)
        self.assertEqual(b.count("◆"), 3)                               # 三场都有，各占一份
        rows = past.snapshot_rows(past.carry(self.lib, PA))
        self.assertLessEqual(len(past.carry_block(rows, budget=300)), 360)


class CarryTest(Base):
    def setUp(self):
        super().setUp()
        tasks = [
            {"id": "T1", "title": "3.2 改清楚", "status": "done", "paper": "aaaa1111"},                       # 改了没收下
            {"id": "T2", "title": "摘要删一句", "status": "done", "resolved": True, "accepted": True},         # 收下了
            {"id": "T3", "title": "表 2 加一列", "status": "done", "note": True, "section": "4 实验"},          # 标注
            {"id": "T4", "title": "撤回的", "status": "done", "rejected": True},
            {"id": "T5", "title": "按钮太小", "status": "done", "note": True, "app": True},                    # 软件意见
            {"id": "T6", "title": "别的论文上的", "status": "done", "paper": "bbbb2222"},
            {"id": "T7", "title": "改失败了", "status": "failed"},
        ]
        make(self.lib, "20261101-100000", [PA], summ("1", todos=[{"text": "补消融实验", "who": "王一凡", "t": "02:10", "sec": 130},
                                                                  "图 3 坐标轴统一"]), tasks=tasks)
        make(self.lib, "20261108-100000", [PA], summ("2", todos=["图 3 坐标轴统一", "重跑表 4"]))

    def test_items_and_dedupe(self):
        items = past.carry(self.lib, PA)
        texts = [x["text"] for x in items]
        self.assertEqual(texts, ["图 3 坐标轴统一", "重跑表 4", "补消融实验", "3.2 改清楚", "表 2 加一列"])
        first = items[0]
        self.assertEqual(first["mid"], "20261108-100000")                # 两场都有的：只留最近那场的
        kinds = {x["text"]: x["kind"] for x in items}
        self.assertEqual((kinds["3.2 改清楚"], kinds["表 2 加一列"], kinds["补消融实验"]), ("edit", "note", "todo"))
        todo = next(x for x in items if x["text"] == "补消融实验")
        self.assertEqual((todo["who"], todo["t"], todo["sec"]), ("王一凡", "02:10", 130.0))
        self.assertEqual(next(x for x in items if x["kind"] == "note")["section"], "4 实验")
        self.assertEqual(past.carry(self.lib, PB), [])

    def test_tick_saved_and_validated(self):
        items = past.carry(self.lib, PA)
        g0 = past.generation()
        ref = next(x["id"] for x in items if x["text"] == "补消融实验")
        past.tick(self.lib, ref, True)
        self.assertGreater(past.generation(), g0)                        # 会上 AI 的往期要点下次重算
        saved = json.loads((self.lib.root / "20261101-100000" / past.CARRY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["done"][ref.split("/", 1)[1]]["by"], "host")
        self.assertNotIn("补消融实验", [x["text"] for x in past.carry(self.lib, PA)])
        past.tick(self.lib, ref, False)
        self.assertIn("补消融实验", [x["text"] for x in past.carry(self.lib, PA)])
        for bad in ("", "nope", "20261101-100000/zzz:1", "../x/todo:abc", "20261101-100000/todo:a b"):
            with self.assertRaises(ValueError):
                past.tick(self.lib, bad, True)
        with self.assertRaises(ValueError) as e:
            past.tick(self.lib, "20991231-000000/todo:abc", True)
        self.assertEqual(str(e.exception), "not_found")

    def test_snapshot_summary_and_settle(self):
        live = make(self.lib, "20261115-100000", [PA], state="live")
        rows = past.snapshot_in(self.lib, live.name, past.carry(self.lib, PA, exclude=live.name))
        self.assertEqual([r["id"] for r in rows], ["c1", "c2", "c3", "c4", "c5"])
        self.assertEqual(past.carry_in(self.lib, live.name), rows)
        b = past.brief(self.lib, PA, exclude=live.name, carried=rows)
        self.assertIn("[c3] 待办：补消融实验（王一凡，11月1日那场）", b)
        lines = [{"t": "00:10", "sec": 10, "who": "teacher", "text": "上次说的消融实验做完了吧"},
                 {"t": "00:20", "sec": 20, "who": "guest", "name": "王一凡", "text": "做完了，在表 5"}]
        seen = []

        def ask_json(prompt, system="", **kw):
            seen.append((prompt, system))
            return {"title": "组会", "conclusions": [], "todos": [],
                    "carried": [{"id": "c3", "done": True, "text": "王一凡做完了，结果在表 5", "t": "00:20"},
                                {"id": "c9", "done": True, "text": "编号对不上的"},
                                {"id": "c1", "done": "false", "text": "会上没提到"}]}
        with mock.patch("zuhui.claude_text.ask_json", ask_json):
            s = mt.summarize(CFG, {"title": "组会"}, lines, [], carried=rows)
        self.assertIn("上次遗留：\n[c1] 待办：图 3 坐标轴统一", seen[0][0])
        self.assertIn('"carried"', seen[0][1])
        self.assertEqual([c["id"] for c in s["carried"]], ["c1", "c2", "c3", "c4", "c5"])   # 漏掉的也列上，编号对不上的不要
        c3 = next(c for c in s["carried"] if c["id"] == "c3")
        self.assertEqual((c3["done"], c3["item"], c3["t"], c3["sec"]), (True, "补消融实验", "00:20", 20.0))
        self.assertFalse(next(c for c in s["carried"] if c["id"] == "c1")["done"])
        self.assertIn("## 上次遗留", s["markdown"])
        self.assertIn("- [x] 补消融实验：王一凡做完了，结果在表 5 `00:20`", s["markdown"])
        self.assertEqual(past.settle(self.lib, live.name, s), 1)
        left = [x["text"] for x in past.carry(self.lib, PA, exclude=live.name)]
        self.assertNotIn("补消融实验", left)                             # 落实了：原来那场里勾掉
        done = json.loads((self.lib.root / "20261101-100000" / past.CARRY_FILE).read_text(encoding="utf-8"))["done"]
        self.assertEqual(next(iter(done.values()))["by"], live.name)
        s2 = dict(s, carried=[dict(c, done=False) for c in s["carried"]])
        past.settle(self.lib, live.name, s2)                             # 重新生成后说没落实：这场会勾的取消
        self.assertIn("补消融实验", [x["text"] for x in past.carry(self.lib, PA, exclude=live.name)])
        ref = next(x["id"] for x in past.carry(self.lib, PA, exclude=live.name) if x["text"] == "重跑表 4")
        past.tick(self.lib, ref, True)                                   # 大厅里手勾的：重新生成不动它
        past.settle(self.lib, live.name, dict(s, carried=[{"ref": ref, "done": False}]))
        self.assertNotIn("重跑表 4", [x["text"] for x in past.carry(self.lib, PA, exclude=live.name)])
        with mock.patch("zuhui.claude_text.ask_json", lambda *a, **k: {"title": "x", "carried": [{"id": "c1", "done": True}]}):
            s3 = mt.summarize(CFG, {"title": "组会"}, lines, [])         # 没带遗留：模型多写的 carried 不要
        self.assertNotIn("carried", s3)

    def test_regenerate_uses_carry_in(self):
        live = make(self.lib, "20261115-100000", [PA], lines=[{"t": "00:10", "sec": 10, "who": "teacher", "text": "开始"}])
        past.snapshot_in(self.lib, live.name, past.carry(self.lib, PA, exclude=live.name))
        with mock.patch("zuhui.claude_text.ask_json", lambda *a, **k: {"title": "x", "carried": [{"id": "c2", "done": True}]}):
            s = mt.regenerate(self.lib, CFG, live.name, "default")
        self.assertTrue(next(c for c in s["carried"] if c["id"] == "c2")["done"])
        self.assertNotIn("重跑表 4", [x["text"] for x in past.carry(self.lib, PA, exclude=live.name)])


LINES_A = [
    {"t": "00:05", "sec": 5, "who": "teacher", "text": "今天看第三章的实验设置"},
    {"t": "02:10", "sec": 130, "who": "guest", "name": "王老师", "text": "第三章的消融实验不够，要补一组去掉注意力模块的对照"},
    {"t": "02:30", "sec": 150, "who": "student", "text": "记下了，第三章补消融"},
    {"t": "05:00", "sec": 300, "who": "system", "text": "切到 别的.md"},
]
LINES_B = [{"t": "01:00", "sec": 60, "who": "teacher", "text": "大创申报书的预算表要改"}]


class SearchTest(Base):
    def setUp(self):
        super().setUp()
        make(self.lib, "20261101-100000", [PA], summ("1", todos=[{"text": "补第三章消融实验", "who": "王一凡", "t": "02:10", "sec": 130}]),
             lines=LINES_A, title="第三章讨论")
        make(self.lib, "20261102-100000", [PB], summ("2"), lines=LINES_B, title="大创")

    def test_search_and_filter(self):
        hits = past.search(self.lib, "王老师对第三章消融实验怎么说的")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["mid"], "20261101-100000")
        self.assertTrue(any(h["kind"] == "line" and h.get("t") == "00:05" for h in hits) or
                        any(h.get("t") == "02:10" for h in hits))
        line = next(h for h in hits if h["kind"] == "line")
        said = past.speaker(line["text"], {"teacher": "林晓", "student": "小B"})
        self.assertIn("王老师：第三章的消融实验不够", said)
        self.assertIn("林晓：今天看第三章", said)
        self.assertNotIn("切到", said)                                   # 系统行不进检索
        self.assertEqual(past.search(self.lib, "第三章消融", mids=["20261102-100000"]), [])
        self.assertEqual(past.search(self.lib, "预算表")[0]["mid"], "20261102-100000")
        make(self.lib, "20261103-100000", [PB], summ("3", todos=["预算表再压一压"]), title="大创二")   # 新开的会：索引跟着变
        self.assertIn("20261103-100000", {h["mid"] for h in past.search(self.lib, "预算表")})

    def test_recall_block_only_when_asked(self):
        names = {"teacher": "林晓", "student": "小B"}
        self.assertEqual(past.recall_block(self.lib, "第三章消融实验怎么补", PA, names=names), "")    # 没问以前：不附
        r = past.recall_block(self.lib, "上次王老师对第三章怎么说的", PA, names=names)
        self.assertTrue(r.startswith("往期会议里和这句话最相关的几段"))
        self.assertIn("11月1日《第三章讨论》", r)
        self.assertNotIn("预算表", r)                                    # 只在同一篇论文的会里找
        self.assertLessEqual(len(r), past.RECALL_CHARS + 300)
        self.assertEqual(past.recall_block(self.lib, "上次说的", PA, exclude="20261101-100000"), "")
        r2 = past.recall_block(self.lib, "上周预算表怎么说的", {"id": "zzz", "name": "没开过会的论文"}, names=names)
        self.assertIn("预算表", r2)                                      # 这篇没开过会：搜全部


class AskTest(Base):
    def setUp(self):
        super().setUp()
        make(self.lib, "20261101-100000", [PA], summ("1"), lines=LINES_A, title="第三章讨论")

    def test_question_limits(self):
        self.assertEqual(past.clean_question("  上次\n说了 什么 "), "上次 说了 什么")
        self.assertEqual(len(past.clean_question("问" * past.ASK_MAX)), 300)
        for bad, code in (("", "empty"), ("  \n ", "empty"), ("问" * (past.ASK_MAX + 1), "too_long")):
            with self.assertRaises(ValueError) as e:
                past.ask(self.lib, CFG, bad)
            self.assertEqual(str(e.exception), code)

    def test_answer_with_sources(self):
        seen = []

        def ask_json(prompt, system="", **kw):
            seen.append(prompt)
            return {"answer": "11月1日那次王老师说第三章消融实验不够，要补一组对照 [1]。", "cites": [1, 99, "x"]}
        with mock.patch("zuhui.claude_text.ask_json", ask_json):
            r = past.ask(self.lib, CFG, "王老师对第三章消融实验怎么说的")
        self.assertTrue(r["found"])
        self.assertIn("[1]", r["answer"])
        self.assertEqual(len(r["sources"]), 1)
        s0 = r["sources"][0]
        self.assertEqual(s0["mid"], "20261101-100000")
        self.assertIn("t", s0)
        self.assertIsInstance(s0["sec"], float)
        self.assertIn("王老师：第三章", seen[0])
        self.assertIn("11月1日《第三章讨论》", seen[0])
        with mock.patch("zuhui.claude_text.ask_json", ask_json):
            r = past.ask(self.lib, CFG, "王老师对第三章消融实验怎么说的", mids=["20991231-000000"])
        self.assertEqual((r["found"], r["sources"], len(seen)), (False, [], 1))   # 一段都没检索到：不调模型
        with mock.patch("zuhui.claude_text.ask_json", side_effect=RuntimeError("还没设置模型来源")):
            with self.assertRaises(RuntimeError):
                past.ask(self.lib, CFG, "第三章消融")


class DigestTest(Base):
    def setUp(self):
        super().setUp()
        make(self.lib, "20261101-100000", [PA], summ("1", todos=["补消融实验"]), title="第一次")
        make(self.lib, "20261108-100000", [PA], summ("2", todos=["重跑表 4"]), title="第二次")
        make(self.lib, "20261115-100000", [PA], summ("3"), title="第三次")
        past.tick(self.lib, "20261101-100000/" + past.items_of(self.lib, {"id": "20261101-100000"})[0]["key"], True)

    def test_pick_validation(self):
        for mids, code in ((["20261101-100000"], "too_few"), (["x", "x"], "too_few"),
                           ([f"2026110{i}-100000" for i in range(10)] + [f"2026111{i}-100000" for i in range(5)], "too_many"),
                           (["20261101-100000", "20991231-000000"], "not_found")):
            with self.assertRaises(ValueError) as e:
                past.start_digest(self.lib, mids)
            self.assertEqual(str(e.exception), code)

    def test_run_and_save(self):
        seen = []

        def ask_json(prompt, system="", **kw):
            seen.append(prompt)
            return {"title": "十一月阶段汇总", "overview": "三次会都在改第三章。",
                    "issues": [{"text": "消融实验不够", "meetings": [1, 2, 7]}],
                    "feedback": [{"text": "数据划分写清楚", "by": "王老师", "meetings": [2, 3]}],
                    "open": [{"text": "重跑表 4", "who": "王一凡", "meeting": 2}]}
        d = past.start_digest(self.lib, ["20261115-100000", "20261101-100000", "20261108-100000"])
        self.assertEqual(d["state"], "running")
        self.assertEqual(d["mids"], ["20261101-100000", "20261108-100000", "20261115-100000"])   # 按时间排
        with self.assertRaises(RuntimeError):
            past.start_digest(self.lib, ["20261101-100000", "20261108-100000"])              # 一次只写一份
        with mock.patch("zuhui.claude_text.ask_json", ask_json):
            d = past.run_digest(self.lib, CFG, d["id"])
        self.assertEqual(d["state"], "done")
        self.assertIn("[1] 11月1日《第一次》", seen[0])
        self.assertIn("补消融实验（已经勾掉）", seen[0])
        self.assertIn("重跑表 4（还没做完）", seen[0])
        self.assertEqual(d["result"]["issues"][0]["meetings"], [1, 2])                         # 编号对不上的不要
        md = d["markdown"]
        for part in ("# 十一月阶段汇总", "## 高频问题", "- 消融实验不够（11月1日、11月8日）", "## 反复出现的意见",
                     "- 王老师：数据划分写清楚", "## 还没做完的待办", "- [ ] 重跑表 4（王一凡）（11月8日）"):
            self.assertIn(part, md)
        lst = past.list_digests()
        self.assertEqual((lst[0]["id"], lst[0]["state"], lst[0]["title"]), (d["id"], "done", "十一月阶段汇总"))
        self.assertEqual(past.get_digest(d["id"])["markdown"], md)
        self.assertIsNone(past.get_digest("20991231-000000"))
        with self.assertRaises(ValueError):
            past.get_digest("../etc")
        d2 = past.start_digest(self.lib, ["20261101-100000", "20261108-100000"])
        with mock.patch("zuhui.claude_text.ask_json", side_effect=RuntimeError("接口 401")):
            d2 = past.run_digest(self.lib, CFG, d2["id"])
        self.assertEqual((d2["state"], d2["error"]), ("failed", "接口 401"))


class FastBrainTest(Base):
    def test_prefix_and_turn_context(self):
        from zuhui import app_facts
        from zuhui.fast import FastBrain
        paper = self.tmp / "ibp.md"
        paper.write_text("# 1 引言\n\n这是论文。\n", encoding="utf-8")
        fb = object.__new__(FastBrain)
        fb.cfg = {"meeting": {"lang": "zh"}, "hands": {}}
        fb.fcfg = {}
        fb.system_prompt = "SYS"
        fb._facts_sig = app_facts.signature()
        fb.history = deque()
        fb.paper = SimpleNamespace(path=str(paper), title="IBP", outline=lambda: "1 引言", find=lambda t: None, current=None)
        fb.board = SimpleNamespace(summary=lambda: "（空）", tasks=lambda: [])
        self.assertNotIn("往期组会", fb._prefix(False))                   # 没接上：和以前一样
        fb.past_fn = lambda: "【往期组会】11月1日那次说要补消融"
        fb.past_recall = lambda t: "往期会议里和这句话最相关的几段：- X" if "上次" in t else ""
        self.assertIn("【往期组会】11月1日那次说要补消融", fb._prefix(False))
        self.assertIn("【往期组会】11月1日那次说要补消融", fb._prefix(True))
        turn = fb._messages("上次王老师怎么说的")[-1]["content"]
        self.assertIn("往期会议里和这句话最相关的几段", turn)
        self.assertNotIn("往期会议里和这句话最相关的几段", fb._messages("第三章怎么改")[-1]["content"])
        fb.past_fn = lambda: 1 / 0                                       # 出错：不放，不能让整轮回话崩掉
        self.assertNotIn("往期组会", fb._prefix(False))

    def test_hands_api_ask_paper_gets_past(self):
        from zuhui import hands_api
        from zuhui.hands import Hands
        paper = self.tmp / "ibp.md"
        paper.write_text("# 1 引言\n\n这是论文。\n", encoding="utf-8")
        h = object.__new__(Hands)
        h.cfg = dict(CFG, deep={})
        h._paper_file = paper
        seen = []

        def fake_ask(system, messages, **kw):
            seen.append(system)
            return "11月1日那次说要补消融。", None
        with mock.patch("zuhui.provider.mode", return_value="api"), mock.patch.object(hands_api, "_ask", fake_ask):
            ans, err = hands_api.ask_paper(h, "上次说的消融做了没", None, "", past="【往期】11月1日：补消融")
            self.assertEqual((ans, err), ("11月1日那次说要补消融。", None))
            self.assertIn("【往期】11月1日：补消融", seen[0])
            hands_api.ask_paper(h, "第三章怎么改", None, "")
            self.assertNotIn("往期组会（", seen[1])


    def test_cli_ask_paper_gets_past(self):
        """Claude Code 账号那一路（claude -p）：查论文的提示词里同样带上往期（不真起 claude，_ask 换成假的）。"""
        from zuhui.hands import Hands
        paper = self.tmp / "ibp.md"
        paper.write_text("# 1 引言\n\n这是论文。\n", encoding="utf-8")
        h = object.__new__(Hands)
        h.cfg = dict(CFG, deep={})
        h.hcfg = {}
        h._paper_file = paper
        seen = []
        with mock.patch("zuhui.provider.mode", return_value="claude"), \
                mock.patch.object(Hands, "_ask", lambda self, prompt, *a, **k: (seen.append(prompt), ("好", None))[1]):
            self.assertEqual(h.ask_paper("上次说的消融做了没", None, "", past="【往期】11月1日：补消融"), ("好", None))
        self.assertIn("【往期】11月1日：补消融", seen[0])
        self.assertIn("说清是哪一场", seen[0])


# API 那一路整条走一遍：模型来源设成一个本机假的 Anthropic 兼容接口，提问、阶段汇总、会上对话的固定前缀、查论文都真的发请求过去
API = r"""
import json, sys, threading
sys.path.insert(0, sys.argv[1])
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
SEEN = []
def reply(system):
    if "你在帮研究生回顾往期组会" in system:
        return json.dumps({"answer": "11月1日那次王老师说消融实验不够 [1]。", "cites": [1]}, ensure_ascii=False)
    if "你在帮研究生写一份组会的阶段汇总" in system:
        return json.dumps({"title": "阶段汇总", "overview": "都在改第三章。", "issues": [{"text": "消融实验", "meetings": [1, 2]}],
                           "feedback": [], "open": [{"text": "重跑表 4", "who": "王一凡", "meeting": 2}]}, ensure_ascii=False)
    return "#ask\n11月1日那次说要补消融。"
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        system = body.get("system", "")
        if isinstance(system, list):
            system = "".join(x.get("text", "") for x in system if isinstance(x, dict))
        SEEN.append({"model": body.get("model"), "stream": bool(body.get("stream")), "system": system,
                     "user": json.dumps(body.get("messages", []), ensure_ascii=False)})
        text = reply(system)
        if body.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            d = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
            self.wfile.write(("event: content_block_delta\ndata: " + json.dumps(d, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.write(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
            return
        b = json.dumps({"type": "message", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}, ensure_ascii=False).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

from pathlib import Path
from zuhui import provider, past
from zuhui.config import STATE
from zuhui.meetings import Meetings
provider.save(provider.clean({"kind": "custom", "base_url": f"http://127.0.0.1:{PORT}", "key": "k-test-12345678",
                              "fast": "fake-fast", "main": "fake-main"}))
lib = Meetings(root=STATE / "meetings")
pe = {"id": "aaaa1111", "name": "FooBench 论文", "path": str(STATE / "ibp.md")}
(STATE / "ibp.md").write_text("# 1 引言\n\n这是论文。\n", encoding="utf-8")
for mid, todo, line in (("20261101-100000", "补消融实验", "第三章的消融实验不够，要补一组"), ("20261108-100000", "重跑表 4", "表 4 要重跑")):
    d = lib.root / mid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": mid, "title": "组会", "started": "2026-11-" + mid[6:8] + "T10:00:00", "state": "ended",
                                             "duration_s": 600, "papers": [pe]}, ensure_ascii=False), encoding="utf-8")
    (d / "transcript.jsonl").write_text(json.dumps({"t": "02:10", "sec": 130, "who": "guest", "name": "王老师", "text": line}, ensure_ascii=False) + "\n", encoding="utf-8")
    (d / "summary.json").write_text(json.dumps({"title": "x", "abstract": "a", "conclusions": ["要补消融"], "sections": [], "todos": [todo]}, ensure_ascii=False), encoding="utf-8")
out = {}
out["ask"] = past.ask(lib, {"meeting": {"me": "林晓"}}, "王老师对第三章消融实验怎么说的")
d = past.start_digest(lib, ["20261101-100000", "20261108-100000"])
out["digest"] = past.run_digest(lib, {}, d["id"])
from zuhui.fast import FastBrain
from zuhui import app_facts
from types import SimpleNamespace
from collections import deque
fb = object.__new__(FastBrain)
fb.cfg = {"meeting": {"lang": "zh"}, "hands": {}}; fb.fcfg = {}; fb.system_prompt = "SYS"; fb._facts_sig = app_facts.signature()
fb.history = deque(); fb.last_error = None
fb.paper = SimpleNamespace(path=str(STATE / "ibp.md"), title="IBP", outline=lambda: "1 引言", find=lambda t: None, current=None)
fb.board = SimpleNamespace(summary=lambda: "（空）", tasks=lambda: [])
fb.past_fn = lambda: past.brief(lib, pe)
fb.past_recall = lambda t: past.recall_block(lib, t, pe, names={"teacher": "林晓", "student": "小B"})
past.warm(lib)
out["reply"] = [x for x in fb.reply("上次王老师对第三章怎么说的")]
from zuhui.hands import Hands
from zuhui import hands_api
h = object.__new__(Hands); h.cfg = {"meeting": {"me": "林晓"}, "deep": {}}; h._paper_file = Path(STATE / "ibp.md")
out["deep"] = hands_api.ask_paper(h, "上次说的消融做了没", None, "", past=past.brief(lib, pe))
out["seen"] = SEEN
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
"""


class ApiModeTest(unittest.TestCase):
    def test_api_mode_end_to_end(self):
        env = dict(os.environ, ZUHUI_HOME=tempfile.mkdtemp(prefix="zuhui_home_past_api_"), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", API, str(ROOT)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        self.assertIn(r["ask"]["sources"][0]["mid"], ("20261101-100000", "20261108-100000"))
        self.assertIn("02:10", r["seen"][0]["user"])                        # 片段带着哪一场、几分几秒、谁说的
        self.assertIn("王老师：第三章的消融实验不够", r["seen"][0]["user"])
        self.assertEqual(r["digest"]["state"], "done")
        self.assertIn("## 还没做完的待办", r["digest"]["markdown"])
        seen = r["seen"]
        self.assertEqual(seen[0]["model"], "fake-fast")                     # 提问走快速那档（省钱）
        self.assertEqual(seen[1]["model"], "fake-main")                     # 阶段汇总走主力那档
        fast = next(x for x in seen if x["stream"])
        self.assertIn("【往期组会】", fast["system"])                        # 往期要点在固定前缀里（缓存得住）
        self.assertIn("待办：补消融实验", fast["system"])
        self.assertIn("往期会议里和这句话最相关的几段", fast["user"])          # 问到"上次"：这一轮附上往期片段
        self.assertIn("王老师：第三章的消融实验不够", fast["user"])
        self.assertIn(["sentence", "11月1日那次说要补消融。"], r["reply"])
        deep = seen[-1]
        self.assertEqual(r["deep"][0], "#ask\n11月1日那次说要补消融。")
        self.assertIn("往期组会（同一篇论文以前几场会", deep["system"])


SERVER = r"""
import json, os, sys, socket, time, http.client
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from zuhui import relay_cfg, share
share.PUBLIC_RELAY = "https://127.0.0.1:1/"
relay_cfg.set_mode("off")
from zuhui.config import STATE, CURRENT_PAPER
from zuhui.meetings import paper_entry
paper = STATE / "papers-test" / "ibp.md"
paper.parent.mkdir(parents=True, exist_ok=True)
paper.write_text("# 1 引言\n\n正文。\n", encoding="utf-8")
CURRENT_PAPER.write_text(json.dumps({"path": str(paper), "name": "FooBench 论文"}, ensure_ascii=False), encoding="utf-8")
pe = paper_entry(paper, "FooBench 论文")
for mid, todos in (("20261101-100000", ["补消融实验"]), ("20261108-100000", ["重跑表 4"])):
    d = STATE / "meetings" / mid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": mid, "title": "组会", "started": mid[:4] + "-11-" + mid[6:8] + "T10:00:00",
                                             "ended": None, "state": "ended", "duration_s": 600, "papers": [pe]}, ensure_ascii=False), encoding="utf-8")
    (d / "transcript.jsonl").write_text(json.dumps({"t": "00:05", "sec": 5, "who": "teacher", "text": "第三章消融实验要补"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (d / "summary.json").write_text(json.dumps({"title": "x", "speakers": [], "abstract": "a", "conclusions": [], "sections": [], "todos": todos}, ensure_ascii=False), encoding="utf-8")
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
out["carry"] = req("GET", "/api/carry")
first = out["carry"][1]["items"][0]["id"]
out["tick"] = req("POST", "/api/carry", {"id": first, "done": True})
out["carry2"] = req("GET", "/api/carry")
out["tick_bad"] = req("POST", "/api/carry", {"id": "../../x", "done": True})
out["tick_404"] = req("POST", "/api/carry", {"id": "20991231-000000/todo:abc", "done": True})
out["ask_empty"] = req("POST", "/api/past/ask", {"question": "  "})
out["ask_long"] = req("POST", "/api/past/ask", {"question": "问" * 301})
out["ask_none"] = req("POST", "/api/past/ask", {"question": "完全无关的问题 zzqq", "mids": []})
out["ask_model"] = req("POST", "/api/past/ask", {"question": "上次第三章消融实验怎么说的"})
out["meetings"] = req("GET", "/api/meetings")
out["dg_few"] = req("POST", "/api/digests", {"mids": ["20261101-100000"]})
out["dg"] = req("POST", "/api/digests", {"mids": ["20261108-100000", "20261101-100000"]})
for _ in range(100):                 # 没设模型来源：后台马上失败（不调任何接口）
    lst = req("GET", "/api/digests")[1]
    if lst and lst[0]["state"] != "running":
        break
    time.sleep(0.1)
out["dg_list"] = lst
out["dg_one"] = req("GET", "/api/digests/" + out["dg"][1]["digest"]["id"])
out["dg_404"] = req("GET", "/api/digests/20991231-000000")
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class HttpTest(unittest.TestCase):
    def test_http(self):
        env = dict(os.environ, ZUHUI_HOME=tempfile.mkdtemp(prefix="zuhui_home_past_"), PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", SERVER, str(ROOT)], env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-1500:] + p.stderr[-2500:])
        r = json.loads(line[7:])
        self.assertEqual(r["carry"][0], 200)
        self.assertEqual(r["carry"][1]["paper"], "FooBench 论文")
        self.assertEqual([x["text"] for x in r["carry"][1]["items"]], ["重跑表 4", "补消融实验"])
        self.assertEqual(r["tick"][0], 200)
        self.assertEqual([x["text"] for x in r["carry2"][1]["items"]], ["补消融实验"])
        self.assertEqual(r["tick_bad"], [400, {"ok": False, "error": "bad_id"}])
        self.assertEqual(r["tick_404"], [404, {"ok": False, "error": "not_found"}])
        self.assertEqual(r["ask_empty"], [400, {"ok": False, "error": "empty"}])
        self.assertEqual(r["ask_long"], [400, {"ok": False, "error": "too_long"}])
        self.assertEqual(r["ask_none"], [200, {"ok": True, "answer": "", "sources": [], "found": False}])
        self.assertEqual(r["ask_model"][0], 502)                          # 没设模型来源：说清原因
        self.assertIn("模型来源", r["ask_model"][1]["error"])
        self.assertEqual(r["meetings"][1][0]["papers"][0]["name"], "FooBench 论文")
        self.assertEqual(r["dg_few"], [400, {"ok": False, "error": "too_few"}])
        self.assertEqual(r["dg"][0], 200)
        self.assertEqual(r["dg_list"][0]["state"], "failed")
        self.assertIn("模型来源", r["dg_list"][0]["error"])
        self.assertEqual(r["dg_one"][1]["mids"], ["20261101-100000", "20261108-100000"])
        self.assertEqual(r["dg_404"][0], 404)


if __name__ == "__main__":
    unittest.main()
