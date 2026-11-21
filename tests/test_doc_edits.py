# -*- coding: utf-8 -*-
"""会上有人在对比页里直接改了一段（zuhui/doc_edits.py），以及改稿工人能看到往期会议（2026-11-21 第二场组会之后加的）。

- 存好一段：逐字稿上记一行系统行（谁改了哪一节第几段，改前改后的片段）；同一个人接着改同一段并成一行；没在开会不记；
  共同主持人（访客）改的记他的名字；英文会用英文；
- 这一行快嘴看得到（hand_edits_fn）、改稿工人看得到（talk 里的系统行、"直接改过的地方"那一节）、纪要的材料里有、
  /bloom 技能（watch.py、history.py）打得出来；
- 改稿工人拿到同一篇文档往期几场会的要点（past.brief），文件名换了版本号（第 12 版 → 第 13 版）也认得是同一篇；
  纪要里单列的"写作要求"排在往期要点最前面。

论文、会议内容都是编的。全离线、不花钱：ZUHUI_HOME 指到临时目录，模型用假的顶替。

    python -X utf8 -m unittest tests.test_doc_edits -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import deque
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-docedits-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

HOME = Path(zconfig.STATE)
if os.path.normcase(str(HOME.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import doc_edits, past, worker_ctx  # noqa: E402
from zuhui import meetings as mt  # noqa: E402

PAPER = ("# 城市共享单车调度评测\n\n## 1 引言\n\n共享单车的调度好坏，直接决定早高峰有没有车可骑。\n\n"
         "## 5 结果\n\n### 5.1 调度偏差\n\n本节回答调度规则是否让缺车率偏离人工复核。表 4 给出六个城区上的结果。\n\n"
         "（1）调度规则足以把站点的缺车判断弄反。288 个组合中，201 个在人工复核下缺车，默认规则却把其中 31.4% 报告为充足。"
         "按站点统计，固定顺序的偏差在 12.2% 到 13.9% 之间。\n\n"
         "| 城区 | 偏差 |\n|---|---|\n| 东城 | 12.2% |\n\n"
         "（2）偏差的方向由规则决定。\n\n没有编号的一段。\n\n### 5.2 稳健性\n\n换一个城区划分，结论不变。\n")


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"bloom_{name}", ROOT / "skills" / "bloom" / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 哪一节、第几段、改前改后 ----------
class Locate(unittest.TestCase):
    def test_labels(self):
        ps = doc_edits._paras(PAPER)
        lab = lambda k: doc_edits.locate(PAPER, next(i for i, p in enumerate(ps) if p.startswith(k)))      # noqa: E731
        self.assertEqual(lab("（1）调度规则")["where"], "5.1 第（1）段")               # 段首有编号：照编号说
        self.assertEqual(lab("（1）调度规则")["where_en"], "5.1 paragraph (1)")
        self.assertEqual(lab("（1）调度规则")["section"], "5.1 调度偏差")
        self.assertEqual(lab("本节回答调度")["where"], "5.1 第 1 段")
        self.assertEqual(lab("没有编号的一")["where"], "5.1 第 4 段")                   # 表格不算段
        self.assertEqual(lab("| 城区 | ")["where"], "5.1 的表格")
        self.assertEqual(lab("### 5.1")["where"], "5.1 的标题")
        self.assertEqual(lab("共享单车的调")["where"], "1 引言第 1 段")                   # 一级编号连标题说
        self.assertEqual(lab("换一个城区划")["section"], "5.2 稳健性")

    def test_snippets(self):
        old = "（1）调度规则足以把站点的缺车判断弄反。按站点统计，固定顺序的偏差在 12.2% 到 13.9% 之间。"
        new = old.replace("按站点统计", "改成按周看，按站点统计")
        b, a = doc_edits.snippets(old, new)
        self.assertIn("按站点统计", b)
        self.assertIn("改成按周看，按站点统计", a)
        self.assertNotIn("（1）调度规则足以", a)                                         # 只截改动附近
        self.assertTrue(a.startswith("…") and a.endswith("…"))
        self.assertEqual(doc_edits.snippets(old, old), ("", ""))
        b, a = doc_edits.snippets(old, "")
        self.assertEqual(a, "")
        long_old = "甲" * 600
        b, a = doc_edits.snippets(long_old, "乙" * 600)
        self.assertLessEqual(len(b), doc_edits.SNIP + 2)
        self.assertLessEqual(len(a), doc_edits.SNIP + 2)


# ---------- 对比页存好一段 → 逐字稿上一行 ----------
class Record(unittest.TestCase):
    def setUp(self):
        from zuhui import view
        self.tmp = Path(tempfile.mkdtemp(prefix="docedit-", dir=str(HOME)))
        self.paper = self.tmp / "单车调度评测.md"
        self.paper.write_bytes(PAPER.encode("utf-8"))
        self.lib = mt.Meetings(root=self.tmp / "meetings")
        v = view.Viewer(cfg={"paper": {"file": str(self.paper)}, "meeting": {"me": "林晓"}}, port=0, meetings=self.lib)
        v.mirror = self.paper
        v.base = view.base_for(self.paper)
        v.ensure_base(rebase=True)
        self.v = v
        doc_edits._last.clear()

    def tearDown(self):
        try:
            self.lib.end()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def edit(self, start: str, old: str, new: str, by=None):
        """页面上点开以 start 开头的那一段，把 old 换成 new 存起来（页面送来的是整段的字）。"""
        full = next(p for p in doc_edits._paras(self.paper.read_text(encoding="utf-8")) if p.startswith(start))
        d = self.v.para_source(full)
        self.assertTrue(d["ok"], d)
        src = d["source"]
        out = self.v.para_save(d["index"], src, src.replace(old, new), by=by)
        self.assertTrue(out["ok"], out)
        return out

    def hand(self):
        return doc_edits.edits(self.lib.current_lines())

    def test_line_written_and_merged(self):
        self.lib.start(papers=[mt.paper_entry(self.paper, "单车调度评测")])
        me = self.v.me_name()
        self.edit("（1）调度规则足以把站点的缺车判断弄反", "按站点统计", "改成按周看，按站点统计")
        rows = self.hand()
        self.assertEqual(len(rows), 1)
        ln = rows[0]
        self.assertEqual(ln["who"], "system")
        self.assertEqual(ln["text"], f"{me} 改了 5.1 第（1）段")
        self.assertEqual(ln["section"], "5.1 调度偏差")
        self.assertIn("改成按周看", ln["after"])
        self.assertNotIn("改成按周看", ln["before"])
        self.assertEqual(ln["paper"], mt.paper_id(self.paper))
        self.assertIn("改成按周看", self.paper.read_text(encoding="utf-8"))           # 真存进稿子了
        on_disk = [json.loads(x) for x in (self.lib.root / self.lib.current()["id"] / "transcript.jsonl")
                   .read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(on_disk[-1]["tag"], doc_edits.TAG)
        # 接着改同一段：并进同一行，改前留最早的、改后是最新的
        self.edit("（1）调度规则足以把站点的缺车判断弄反", "默认规则却把其中", "默认规则把其中")
        rows = self.hand()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("改成按周看", rows[0]["before"])
        self.assertIn("默认规则把其中", rows[0]["after"])
        self.assertIn("改成按周看", rows[0]["after"])
        # 改另一段：另起一行
        self.edit("（2）偏差的方向由规则决定", "由规则决定", "完全由规则决定")
        rows = self.hand()
        self.assertEqual([r["text"] for r in rows], [f"{me} 改了 5.1 第（1）段", f"{me} 改了 5.1 第（2）段"])

    def test_guest_name_delete_and_no_meeting(self):
        self.edit("（2）偏差的方向由规则决定", "由规则决定", "由规则决定了")      # 没在开会：存了，但不记
        self.assertIn("由规则决定了", self.paper.read_text(encoding="utf-8"))
        self.assertEqual(self.lib.current_lines(), [])
        self.lib.start(papers=[mt.paper_entry(self.paper, "单车调度评测")])
        self.edit("（2）偏差的方向由规则决定", "由规则决定了", "由规则决定", by="王一凡")
        self.assertEqual(self.hand()[-1]["text"], "王一凡 改了 5.1 第（2）段")
        d = self.v.para_source("没有编号的一段")
        self.v.para_save(d["index"], d["source"], "")                                 # 删掉一段
        self.assertEqual(self.hand()[-1]["text"], f"{self.v.me_name()} 删了 5.1 第 4 段")
        self.assertNotIn("after", self.hand()[-1])

    def test_english_meeting(self):
        self.lib.start(lang="en", papers=[mt.paper_entry(self.paper, "单车调度评测")])
        self.edit("（2）偏差的方向由规则决定", "由规则决定", "完全由规则决定")
        self.assertEqual(self.hand()[-1]["text"], f"{self.v.me_name()} edited 5.1 paragraph (2)")

    def test_guest_act_passes_relay_name(self):
        self.lib.start(papers=[mt.paper_entry(self.paper, "单车调度评测")])
        seen = []
        self.v._local_post = lambda path, body, timeout=60: seen.append((path, body)) or {"ok": True}
        with mock.patch("zuhui.roles.is_cohost", return_value=True):
            self.v.guest_act({"path": "/api/para/save", "by": "王一凡", "body": {"index": 1, "old": "a", "text": "b", "by": "冒名"}},
                             fetch_upload=None)
        self.assertEqual(seen[0][1]["by"], "王一凡")                                   # 记中继认出来的名字，不信页面报的


# ---------- 谁看得到 ----------
def L(sec, who, text, **kw):
    return dict({"t": worker_ctx.mmss(sec), "sec": float(sec), "who": who, "text": text}, **kw)


EDIT = L(130, "system", "林晓 改了 5.1 第（1）段", tag=doc_edits.TAG, by="林晓", where="5.1 第（1）段", section="5.1 调度偏差",
         before="…按站点统计，固定顺序…", after="…改成按周看，按站点统计，固定顺序…", paper="p1")
LINES = [
    L(100, "teacher", "这一段我自己来改，你别动。"),
    L(105, "student", "好，这段你自己改。"),
    EDIT,
    L(140, "system", "已切换到 别的一篇", tag="#paper"),
    L(150, "teacher", "把这一节的数字格式统一一下。"),
]
TASK = {"id": "T3", "idx": 3, "title": "统一 5.1 的数字格式", "remark": "把这一节的数字格式统一一下。", "status": "working",
        "paper": "p1", "paper_name": "单车调度评测第13版", "ask_sec": 150.0, "said_sec": 152.0}


class WhoSees(unittest.TestCase):
    def test_worker_talk_and_block(self):
        s = worker_ctx.talk(LINES, TASK, 150.0, 152.0, me="林晓", ai="小B")
        self.assertIn("02:10 （直接改文档）林晓 改了 5.1 第（1）段：改前「…按站点统计，固定顺序…」，改后「…改成按周看，按站点统计，固定顺序…」", s)
        self.assertNotIn("已切换到", s)                                                # 别的系统行照旧不带
        e = worker_ctx.hand_edits(LINES, TASK)
        self.assertIn("林晓 改了 5.1 第（1）段", e)
        self.assertEqual(worker_ctx.hand_edits(LINES, dict(TASK, paper="p2")), "")     # 别的文档上的不算

    def test_worker_prompt_blocks(self):
        from zuhui.hands import Hands
        h = object.__new__(Hands)
        h.cfg = {"meeting": {"lang": "zh"}}
        blk = Hands._meeting_block(h, {"edits": worker_ctx.hand_edits(LINES, TASK), "past": "◆ 11月20日《组会》\n写作要求：不用冒号和分号"})
        self.assertIn("本场有人在文档里直接改过的地方", blk)
        self.assertIn("别改回去", blk)
        self.assertIn("往期会议（同一篇文档前几场会的纪要要点和遗留项）", blk)
        self.assertIn("写作要求：不用冒号和分号", blk)
        for f in ("worker.md", "worker_api.md"):
            self.assertIn("往期会上定下的写法要求", (ROOT / "prompts" / f).read_text(encoding="utf-8"))

    def test_fast_turn_has_hand_edits(self):
        from zuhui import app_facts
        from zuhui.fast import FastBrain
        tmp = Path(tempfile.mkdtemp(prefix="docedit-fast-", dir=str(HOME)))
        paper = tmp / "p.md"
        paper.write_text(PAPER, encoding="utf-8")
        fb = object.__new__(FastBrain)
        fb.cfg = {"meeting": {"lang": "zh"}, "hands": {}}
        fb.fcfg = {}
        fb.system_prompt = "SYS"
        fb._facts_sig = app_facts.signature()
        fb.history = deque()
        fb.paper = SimpleNamespace(path=str(paper), title="单车", outline=lambda: "5.1", find=lambda t: None, current=None)
        fb.board = SimpleNamespace(summary=lambda: "（空）", tasks=lambda: [])
        self.assertNotIn("直接改过的地方", fb._messages("这段怎么样")[-1]["content"])
        fb.hand_edits_fn = lambda: doc_edits.note(LINES, paper="p1")
        turn = fb._messages("这段怎么样")[-1]["content"]
        self.assertIn("本场有人在文档里直接改过的地方", turn)
        self.assertIn("02:10 林晓 改了 5.1 第（1）段：改前「…按站点统计", turn)
        fb.hand_edits_fn = lambda: 1 / 0                                              # 出错：不放，整轮照常
        self.assertNotIn("直接改过的地方", fb._messages("这段怎么样")[-1]["content"])
        shutil.rmtree(tmp, ignore_errors=True)

    def test_note_caps(self):
        many = [dict(EDIT, sec=float(i), t=worker_ctx.mmss(i), text=f"林晓 改了 5.1 第 {i} 段") for i in range(20)]
        n = doc_edits.note(many, limit=8)
        self.assertIn("（更早的 12 处略）", n)
        self.assertIn("第 19 段", n)
        self.assertNotIn("第 3 段", n)

    def test_summary_gets_hand_edits(self):
        seen = []

        def fake(user, system=None, **kw):
            seen.append((user, system))
            return {"title": "调度评测", "abstract": "改了 5.1。", "conclusions": [], "sections": [], "people": [], "todos": []}
        with mock.patch("zuhui.claude_text.ask_json", fake):
            mt.summarize({"meeting": {"me": "林晓"}}, {"title": "组会", "duration_s": 600}, LINES, [])
        user, system = seen[0]
        self.assertIn("[02:10] 系统：林晓 改了 5.1 第（1）段", user)
        self.assertIn("会上有人直接在文档里改的", user)
        self.assertIn("改后「…改成按周看", user)
        self.assertIn("写作要求：", system)

    def test_bloom_skill_scripts(self):
        w = _load_script("watch")
        self.assertEqual(w.fmt(EDIT, "林晓"), "[02:10] 系统: 林晓 改了 5.1 第（1）段（直接改文档：改前「…按站点统计，固定顺序…」，"
                                             "改后「…改成按周看，按站点统计，固定顺序…」）")
        pushed = {w._key(EDIT): w._body(EDIT)}
        self.assertIsNone(w._delta(EDIT, pushed))
        again = dict(EDIT, after="…改成按周看，按城区统计…")                             # 接着改同一段：再推一次整行
        self.assertEqual(w._delta(again, pushed), (EDIT["text"], False))
        cur = {"meeting": {"state": "live", "title": "组会", "id": "m1"}, "participants": [], "transcript": LINES, "board": {}}
        w.current = lambda timeout=4.0: cur
        buf = io.StringIO()
        with redirect_stdout(buf):
            w.catchup()
        self.assertIn("直接在论文里改过的地方（1 处，不是 AI 改的）", buf.getvalue())
        # 往期会议：history.py show / transcript
        h = _load_script("history")
        tmp = Path(tempfile.mkdtemp(prefix="docedit-hist-", dir=str(HOME)))
        d = tmp / "meetings" / "20261120-100000"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": d.name, "title": "组会", "started": "2026-11-20T10:00:00", "state": "ended"}),
                                     encoding="utf-8")
        (d / "transcript.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in LINES), encoding="utf-8")
        h.ROOT = tmp / "meetings"
        buf = io.StringIO()
        with redirect_stdout(buf):
            h.cmd_show(["last"])
            h.cmd_transcript(["last"])
        out = buf.getvalue()
        self.assertIn("## 直接在论文里改的（不是 AI 改的）", out)
        self.assertIn("[02:10] 系统: 林晓 改了 5.1 第（1）段（直接改文档：", out)
        self.assertIn("[02:20] 系统: 已切换到 别的一篇", out)                             # 系统行不再写成"参会者"
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 改稿工人看到往期会议 ----------
class PastForWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docedit-past-", dir=str(HOME)))
        self.lib = mt.Meetings(root=self.tmp / "meetings")
        past._INDEX.__init__()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, mid, paper, summary):
        d = self.lib.root / mid
        d.mkdir(parents=True)
        started = f"{mid[:4]}-{mid[4:6]}-{mid[6:8]}T{mid[9:11]}:{mid[11:13]}:{mid[13:15]}"
        (d / "meta.json").write_text(json.dumps({"id": mid, "title": "组会", "started": started, "ended": started, "state": "ended",
                                                 "duration_s": 1800, "papers": [paper]}, ensure_ascii=False), encoding="utf-8")
        (d / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")

    def test_name_stem(self):
        s = past.name_stem
        self.assertEqual(s("单车调度评测第12版"), s("单车调度评测第13版"))
        self.assertEqual(s("单车调度评测第12版"), "单车调度评测")
        self.assertEqual(s("dispatch_eval_v3"), "dispatch_eval")
        self.assertEqual(s("单车调度评测-20261121"), "单车调度评测")
        self.assertEqual(s("单车调度评测_v2_终稿"), "单车调度评测")
        self.assertEqual(s("论文终稿"), "")                                            # 剩下的太常见：不拿它认
        self.assertEqual(s("draft_v3"), "")
        self.assertNotEqual(s("chapter3"), s("chapter4"))                              # 不是版本号的数字不去
        self.assertEqual(s("申报书修订助手-说明"), "申报书修订助手-说明")                 # 中间的"修订"不去

    def test_new_version_finds_old_meetings(self):
        old = {"id": "aaaa1111", "name": "单车调度评测第12版", "path": "C:/p/v12/单车调度评测第12版.md"}
        new = {"id": "bbbb2222", "name": "单车调度评测第13版", "path": "C:/p/v13/单车调度评测第13版.md"}
        other = {"id": "cccc3333", "name": "别的论文第13版", "path": "C:/p/o/别的论文第13版.md"}
        self.make("20261120-100000", old, {
            "conclusions": [{"text": "3.1 拆成两段"}, {"text": "3.2 第一段改成任务句"}, {"text": "全文多余的冒号和分号已清理"},
                            {"text": "4.3 小标题保留"}, {"text": "写作要求：正文不用冒号和分号，并列的词之间用顿号"}],
            "todos": [{"text": "检查拆句后不通顺的地方", "who": "林晓"}]})
        self.make("20261119-100000", other, {"conclusions": [{"text": "别的论文的结论"}]})
        self.assertEqual([m["id"] for m in past.related(self.lib, new)], ["20261120-100000"])
        task = {"id": "T2", "paper": new["id"], "paper_path": new["path"], "paper_name": new["name"]}
        text = worker_ctx.past_meetings(self.lib, task, exclude="20261121-150000")
        self.assertFalse(text.startswith(past.BRIEF_HEAD))                             # 写给快嘴的那句开头去掉
        self.assertIn("11月20日《组会》", text)
        self.assertIn("写作要求：正文不用冒号和分号，并列的词之间用顿号", text)
        self.assertLess(text.index("写作要求"), text.index("结论："))                    # 写作要求排最前，不被截掉
        self.assertIn("检查拆句后不通顺的地方", text)
        self.assertNotIn("别的论文的结论", text)
        self.assertEqual(worker_ctx.past_meetings(self.lib, {"id": "T9"}), "")          # 不知道是哪篇：不给

    def test_worker_context_from_app(self):
        from zuhui.app import Meeting as App
        new = {"id": "bbbb2222", "name": "单车调度评测第13版", "path": str(self.tmp / "v13.md")}
        self.make("20261120-100000", dict(new, id="aaaa1111", name="单车调度评测第12版", path="C:/p/v12.md"),
                  {"conclusions": [{"text": "写作要求：不用分号"}]})
        self.lib.start(title="组会", papers=[new])
        lib_lines = self.lib
        lib_lines.add_line("system", "林晓 改了 5.1 第（1）段", tag=doc_edits.TAG,
                           extra={"before": "旧", "after": "新", "paper": new["id"]})
        fake = SimpleNamespace(meetings=self.lib, viewer=None, cfg={"meeting": {"lang": "zh"}}, _hline=None)
        task = {"id": "T1", "idx": 1, "paper": new["id"], "paper_path": new["path"], "paper_name": new["name"],
                "meeting": self.lib.current()["id"], "said_sec": 1.0, "ask_sec": 1.0, "remark": "改一下"}
        ctx = App._worker_context(fake, task)
        self.assertIn("写作要求：不用分号", ctx["past"])
        self.assertIn("林晓 改了 5.1 第（1）段：改前「旧」，改后「新」", ctx["edits"])
        self.lib.end()


if __name__ == "__main__":
    unittest.main()
