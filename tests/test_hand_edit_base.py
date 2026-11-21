# -*- coding: utf-8 -*-
"""论文页上手改一段：存好就算看过了，这一段不再标成修订（2026-11-21 第二场组会："这是我自己打进去的字"）。

那一段先被两条任务改过（都没打勾），作者又在段落编辑框里把它整段重写。以前保存后整段标成修订（红删除线、绿下划线），
现在底稿里对应的那段换成同样的新文字。另外核对后果：
  · 被手改盖掉的那几条：之后撤回好好失败（不撤掉作者的字）；还改了别处的，只撤别处；手改之后的任务照常撤；
  · 打勾更早的任务：不把 AI 的字塞回底稿里作者的段落，别处照常并；
  · AI 新加的段、几段被并成一段、整段删掉、CRLF + BOM；
  · 散会快照从"原文 + 手改"比起（只算 AI 的改动），开会时的原文（.orig）一个字节不动；重新开会时作废；
  · 图钉贴回手改后的那一段；逐字稿上照样记"谁改了哪一段"。
全离线：临时 ZUHUI_HOME，论文是编的。

    python -X utf8 -m unittest tests.test_hand_edit_base -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-handbase-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

HOME = Path(zconfig.STATE)
if os.path.normcase(str(HOME.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import board as boardmod  # noqa: E402
from zuhui import doc_edits, share, view  # noqa: E402
from zuhui import hands as handsmod  # noqa: E402
from zuhui.meetings import Meetings, paper_entry  # noqa: E402

share.PUBLIC_RELAY = "https://127.0.0.1:1/"

PARAS = ["# 乡村入户调查方法", "## 4 方法", "### 4.2 抽样",
         "（1）抽样规则会改变结论。我们在四个县各抽了二十户，每户访谈一位成年人。",
         "（2）样本量够用，置信区间的宽度在可接受范围内。",
         "### 4.3 访谈", "访谈在农闲时进行，每次约四十分钟，由两名调查员同去。",
         "## 5 结论", "抽样方式对结论有实质影响。"]
P1, P2, Q = PARAS[3], PARAS[4], PARAS[6]
HAND = "（1）抽样规则足以改变结论。我们在四个县按住址各抽了二十五户，户籍不再作为依据。"


def fresh() -> None:
    for d in ("changes", "base", "view", "work"):
        shutil.rmtree(HOME / d, ignore_errors=True)
    for d in ("changes", "base", "view"):
        (HOME / d).mkdir(parents=True, exist_ok=True)
    for f in ("board.json", "board.json.tmp"):
        try:
            (HOME / f).unlink()
        except OSError:
            pass


def enc(text: str, nl: str = "\n", bom: bool = False) -> bytes:
    return (b"\xef\xbb\xbf" if bom else b"") + text.replace("\n", nl).encode("utf-8")


def paras(path: Path) -> list[str]:
    """一份文件按空行切出的段落（对比页按这个比：两边段落一样 = 这里没有修订标记）。"""
    return [k for _, _, k in view.Viewer._spans(path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n"))]


class Doc:
    """一篇测试稿：按指定换行和 BOM 落盘；工人那样改（读时换行统一成 \\n，写回照原样），改动记录照 hands 记。"""

    def __init__(self, path: Path, nl: str = "\n", bom: bool = False):
        self.path, self.nl, self.bom = path, nl, bom
        self.orig = "\n\n".join(PARAS) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(enc(self.orig, nl, bom))

    def text(self) -> str:
        return self.path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")

    def edit(self, b, pairs, entry) -> dict:
        before = after = self.text()
        for find, rep in pairs:
            assert after.count(find) == 1, (find, after)
            after = after.replace(find, rep, 1)
        t = b.add(remark="改一下", title="改 " + pairs[0][0][:6], paper=entry)
        b.claim(t["id"])
        self.path.write_bytes(enc(after, self.nl, self.bom))
        handsmod.Hands._capture_changes(None, t, before, after)
        time.sleep(0.01)
        b.finish(t["id"], result="改好了。无")
        return t


class Base(unittest.TestCase):
    nl, bom = "\n", False

    def setUp(self):
        fresh()
        self.doc = Doc(HOME / "work" / "入户调查.md", self.nl, self.bom)
        self.b = boardmod.Board(path=HOME / "board.json")
        self.entry = paper_entry(self.doc.path, "入户调查")
        v = view.Viewer.__new__(view.Viewer)
        v.board, v._sig, v.on_edit, v.focus, v.meetings = self.b, None, None, None, None
        v.lock, v._gen_lock = threading.Lock(), threading.RLock()
        v.ui_lang, v.cfg = "zh", {"paper": {}, "meeting": {}}
        v.papers = [self.entry]
        v.build, v.building, v.html, v._stop = 0, False, "", threading.Event()
        v.mirror, v.base = self.doc.path, view.base_for(self.doc.path)
        v.ensure_base(rebase=True)
        self.v = v
        self.orig_bytes = v.base0.read_bytes()

    def tearDown(self):
        fresh()

    def edit(self, *pairs) -> dict:
        return self.doc.edit(self.b, list(pairs), self.entry)

    def hand(self, start: str, new: str, by=None) -> dict:
        """页面上点开以 start 开头的那一段，整段换成 new 存起来。"""
        full = next(k for k in paras(self.doc.path) if k.startswith(start))
        d = self.v.para_source(full)
        self.assertTrue(d["ok"], d)
        r = self.v.para_save(d["index"], d["source"], new, by=by)
        self.assertTrue(r["ok"] and r["changed"], r)
        time.sleep(0.01)
        return r

    def clean(self):
        """底稿和稿子段落一样：论文页上这篇没有修订标记。"""
        self.assertEqual(paras(self.v.base), paras(self.doc.path))

    def two_tasks_then_rewrite(self):
        """5.2 第（1）段那样：一条改第一句、一条补两点，都没打勾；作者把这一段整段重写（补的两点删了）。"""
        t11 = self.edit(("抽样规则会改变结论。", "抽样规则足以改变结论。"))
        t12 = self.edit(("每户访谈一位成年人。", "每户访谈一位成年人。一是按户籍抽，二是按住址抽。"))
        self.hand("（1）抽样规则", HAND)
        return t11, t12


class RewriteSwallowsTwoTasks(Base):
    def test_paragraph_is_clean_and_orig_untouched(self):
        t11, t12 = self.two_tasks_then_rewrite()
        want = enc(self.doc.orig.replace(P1, HAND), self.nl, self.bom)
        self.assertEqual(self.doc.path.read_bytes(), want)
        self.assertEqual(self.v.base.read_bytes(), want)                       # 底稿换成同样的字：换行、BOM 照原样
        self.assertEqual(self.v.base0.read_bytes(), self.orig_bytes)          # 开会时的原文一个字节不动
        self.assertEqual(view.Viewer._own_of(self.v.base).read_bytes(), want)   # 原文 + 手改
        rec = self.v._hand_edits()[-1]
        self.assertEqual(set(rec["tasks"]), {t11["id"], t12["id"]})
        self.assertEqual(rec["new"], HAND)
        # 两条任务还在（没打勾），图钉按手改后这一段的开头找
        briefs = {x["id"]: x for x in view._briefs(self.v._active_tasks(), "")}
        head = view._KEY_RE.sub("", HAND)[:20]
        for t in (t11, t12):
            self.assertEqual(briefs[t["id"]]["hint"]["heads"][0], head)
            self.assertFalse(self.b.get(t["id"]).get("resolved"))

    def test_other_pending_change_keeps_its_mark(self):
        t5 = self.edit(("每次约四十分钟", "每次约一小时"))
        self.two_tasks_then_rewrite()
        self.assertEqual(paras(self.v.base), [HAND if k == P1 else k for k in PARAS])   # 访谈那一段还是改前：修订标记还在
        self.assertIn("一小时", self.doc.text())
        self.assertFalse(self.b.get(t5["id"]).get("resolved"))

    def test_undo_swallowed_tasks_fails_and_keeps_text(self):
        t11, t12 = self.two_tasks_then_rewrite()
        mirror, base = self.doc.path.read_bytes(), self.v.base.read_bytes()
        for t in (t12, t11):
            r = self.v.reject_task(t["id"])
            self.assertFalse(r["ok"], r)
            self.assertEqual(r["error"], "撤回失败：此处已手动修改")
            self.assertEqual(self.doc.path.read_bytes(), mirror)             # 作者的字一个不少
            self.assertEqual(self.v.base.read_bytes(), base)
            self.assertFalse(self.b.get(t["id"]).get("rejected"))
        self.v.ui_lang = "en"
        self.assertEqual(self.v.reject_task(t12["id"])["error"], "Undo failed: edited by hand")

    def test_undo_reverts_only_elsewhere(self):
        self.edit(("抽样规则会改变结论。", "抽样规则足以改变结论。"))
        t12 = self.edit(("每户访谈一位成年人。", "每户访谈一位成年人。一是按户籍抽，二是按住址抽。"),
                        ("每次约四十分钟", "每次约四十五分钟"))
        self.hand("（1）抽样规则", HAND)
        r = self.v.reject_task(t12["id"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.doc.path.read_bytes(), enc(self.doc.orig.replace(P1, HAND), self.nl, self.bom))
        self.clean()

    def test_task_after_hand_edit_undoes_normally(self):
        self.two_tasks_then_rewrite()
        after_hand = self.doc.path.read_bytes()
        t13 = self.edit(("二十五户", "三十户"))
        r = self.v.reject_task(t13["id"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.doc.path.read_bytes(), after_hand)

    def test_undo_older_task_after_later_task_touched_hand_paragraph(self):
        """手改之后又有任务改了这一段（不再和手改一字不差）：撤更早的那几条照样不碰这一段。"""
        t11, t12 = self.two_tasks_then_rewrite()
        self.edit(("二十五户", "三十户"))
        mirror = self.doc.path.read_bytes()
        for t in (t12, t11):
            self.assertEqual(self.v.reject_task(t["id"])["error"], "撤回失败：此处已手动修改")
            self.assertEqual(self.doc.path.read_bytes(), mirror)

    def test_tick_older_tasks_keeps_hand_paragraph(self):
        t11, t12 = self.two_tasks_then_rewrite()
        base = self.v.base.read_bytes()
        for t in (t12, t11):
            self.assertTrue(self.v.resolve_task(t["id"])["ok"])
        self.assertEqual(self.v.base.read_bytes(), base)                      # AI 的字没被塞回作者的段落
        self.clean()

    def test_tick_merges_elsewhere_only(self):
        t = self.edit(("每户访谈一位成年人。", "每户访谈一位成年人，"), ("每次约四十分钟", "每次约一小时"))
        self.hand("（1）抽样规则", HAND)
        self.assertNotEqual(paras(self.v.base), paras(self.doc.path))         # 访谈那一段还有修订
        r = self.v.resolve_task(t["id"])
        self.assertTrue(r["ok"] and r["accepted"], r)
        self.clean()

    def test_final_snapshot_counts_only_ai(self):
        self.edit(("每次约四十分钟", "每次约一小时"))
        self.two_tasks_then_rewrite()
        sp = self.v.spec(final=True)
        own = view.Viewer._own_of(self.v.base)
        self.assertEqual(sp["docs"][0]["before"], str(own))
        self.assertEqual(paras(own), [HAND if k == P1 else k for k in PARAS])
        if not view.HTMLDIFF.exists():
            self.skipTest("没有对比页生成器")
        ok, page = self.v._generate(final=True)
        self.assertTrue(ok, page[-500:])
        ins = "".join(re.findall(r"<ins>(.*?)</ins>", page, re.S))
        dele = "".join(re.findall(r"<del>(.*?)</del>", page, re.S))
        self.assertIn("一小时", ins)                                           # AI 的改动照样在
        for s in ("按住址", "户籍不再作为依据", "二十五"):                       # 作者自己打的不算
            self.assertNotIn(s, ins)
        self.assertNotIn("每户访谈一位成年人", dele)
        self.assertIn("户籍不再作为依据", page)

    def test_new_meeting_drops_own(self):
        self.two_tasks_then_rewrite()
        own = view.Viewer._own_of(self.v.base)
        self.assertTrue(own.is_file())
        self.v.ensure_base(rebase=True)
        self.assertFalse(own.exists())
        self.assertEqual(self.v.spec(final=True)["docs"][0]["before"], str(self.v.base0))
        # 原文后来整份换过（比如 PDF 换成完整版）：旧的 .own 作废
        self.hand("（1）抽样规则", HAND + "就这样。")
        time.sleep(0.02)
        view._copy_doc(self.doc.path, self.v.base0)
        self.assertFalse(view.Viewer._own_ok(self.v.base))

    def test_broken_base_heals_with_hand_edit(self):
        self.two_tasks_then_rewrite()
        self.v.base.write_bytes(enc(self.doc.orig)[:-4] + b"\xe5\xaf")       # 尾巴上半个字
        self.assertTrue(self.v._heal_base(self.doc.path))
        self.assertIn(HAND, paras(self.v.base))
        self.clean()


class RewriteCrlfBom(RewriteSwallowsTwoTasks):
    nl, bom = "\r\n", True


class OtherShapes(Base):
    def test_ai_added_paragraph(self):
        self.edit((P2, P2 + "\n\n（3）补充：拒访率低于百分之五。"))
        self.hand("（3）补充", "（3）补充：拒访率低于百分之三，已剔除。")
        self.clean()
        self.assertEqual(paras(self.v.base)[5], "（3）补充：拒访率低于百分之三，已剔除。")   # 插在对齐的位置
        self.assertEqual(self.v.base0.read_bytes(), self.orig_bytes)

    def test_delete_ai_added_paragraph(self):
        self.edit((P2, P2 + "\n\n（3）补充：拒访率低于百分之五。"))
        base = self.v.base.read_bytes()
        self.hand("（3）补充", "")
        self.assertEqual(self.v.base.read_bytes(), base)                      # 底稿本来就没有这一段
        self.clean()

    def test_delete_ai_modified_paragraph(self):
        t = self.edit(("每次约四十分钟", "每次约一小时"))
        self.hand("访谈在农闲时", "")
        self.clean()
        self.assertNotIn(Q, paras(self.v.base))
        r = self.v.reject_task(t["id"])                                        # 撤回：不把作者删掉的段落放回来
        self.assertFalse(r["ok"], r)
        self.assertNotIn("访谈在农闲时", self.doc.text())

    def test_merged_paragraphs(self):
        self.edit((P1 + "\n\n" + P2, P1 + P2))
        merged = "（1）抽样规则会改变结论。我们在四个县各抽了二十户，样本量够用。"
        self.hand("（1）抽样规则", merged)
        self.clean()
        self.assertNotIn(P2, paras(self.v.base))

    def test_unchanged_paragraph_still_synced(self):
        self.hand("（2）样本量够用", "（2）样本量够用。")
        self.clean()
        self.assertEqual(self.v.base.read_bytes(), self.doc.path.read_bytes())

    def test_transcript_line_kept(self):
        lib = Meetings(root=HOME / "work" / "meetings")
        self.v.meetings = lib
        lib.start(papers=[self.entry])
        doc_edits._last.clear()
        try:
            self.two_tasks_then_rewrite()
            rows = doc_edits.edits(lib.current_lines())
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["text"].endswith("改了 4.2 第（1）段"), rows[0])
        finally:
            lib.end()

    def test_hand_file_is_safe_for_other_readers(self):
        """changes/ 下多了 hand.json：按任务读改动记录的那几处（快嘴、Word 写回）不会把它当成一条任务。"""
        self.two_tasks_then_rewrite()
        d = json.loads((view.CHANGES_DIR / "hand.json").read_text(encoding="utf-8"))
        self.assertIsInstance(d, dict)
        for k in ("id", "changed", "before", "after", "ops"):
            self.assertNotIn(k, d)


if __name__ == "__main__":
    unittest.main()
