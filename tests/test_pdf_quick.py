# -*- coding: utf-8 -*-
"""新 PDF 先出文字、后台补全（zuhui/pdf_full.py、mineru_svc.py，2026-11-20）。
跑：python -X utf8 -m unittest tests.test_pdf_quick -v （仓库根目录；临时 ZUHUI_HOME，MinerU 用假的，不真跑、不起服务）

覆盖：
  1. 装了 MinerU：先出纯文本（1 秒内、meta 记 pymupdf + pending_full），后台转完换成完整版（.md、figures、meta、
     对比页基线和"开会那一刻的原文"一起换）；同一篇再导入不重复排；
  2. 转换期间手改过：不换，留着改过的，meta 记 full_skipped；
  3. MinerU 失败：留着纯文本，去掉 pending_full，meta 记 full_error；
  4. 改稿任务等到换完才开始（别的文档上的任务照常）；开会中换的："开会前"的版本快照也换成完整版、对比页重做；
  5. 没装 MinerU：和以前一样，导入时直接 PyMuPDF，没有 pending_full、不排队；
  6. MinerU 常驻服务的空闲回收规则（不真起进程）。
"""
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("BLOOM_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules:
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-pdfq-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

HOME = Path(zconfig.STATE)
if os.path.normcase(str(HOME.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import board as boardmod  # noqa: E402
from zuhui import mineru_svc, papers, pdf_full, pdfconv, paper_versions, view  # noqa: E402
from zuhui.hands import Hands  # noqa: E402
from zuhui.meetings import Meetings, paper_entry, paper_id  # noqa: E402

WORK = HOME / "work" / "pdfq"
FULL_MD = ("# Full Paper\n\n## 1 Introduction\n\nWe study time series generation.\n\n"
           "```latex\nL = \\sum_{t} \\|x_t - \\hat{x}_t\\|^2 \\tag{1}\n```\n\n"
           "| Model | MAE |\n| --- | --- |\n| TimeGAN | 0.12 |\n\n[image: 图 1]\n\nFigure 1: Overview.\n")
BODY = ("Time series generation benchmarks compare models on fidelity, diversity and utility. "
        "We describe the protocol, the datasets and the metrics used in this study, and we report "
        "results for twelve generators across seven domains with three random seeds each. ")


def sha1(p: Path) -> str:
    return hashlib.sha1(Path(p).read_bytes()).hexdigest()


def make_pdf(path: Path, pages: int = 2) -> Path:
    import pymupdf as fitz
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 520, 760), f"Section {i + 1}. " + BODY * 3, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


class FakeMinerU:
    """代替 pdfconv.convert_pdf：等 gate 放行，写一张图，返回完整版 Markdown；fail=True 时抛 ConvertError。"""

    def __init__(self, fail: bool = False):
        self.gate = threading.Event()
        self.started = threading.Event()
        self.fail = fail
        self.calls = []

    def __call__(self, pdf, stage, progress=None, remote_url=None):
        self.calls.append({"pdf": str(pdf), "remote_url": remote_url})
        self.started.set()
        if not self.gate.wait(20):
            raise pdfconv.ConvertError("测试：gate 没放行")
        if self.fail:
            raise pdfconv.ConvertError("MinerU 失败（退出码 1）：显存不足（测试）")
        figs = Path(stage) / "figures"
        figs.mkdir(parents=True, exist_ok=True)
        (figs / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (figs / "figures.json").write_text(json.dumps({"1": {"file": "figures/fig1.png", "source": "mineru"}}), encoding="utf-8")
        return FULL_MD, {"engine": "mineru", "tier": "standard", "figures": 1, "tables": 1, "equations": 1}


class Base(unittest.TestCase):
    kit = True

    def setUp(self):
        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir(parents=True, exist_ok=True)
        view.BASE_DIR.mkdir(parents=True, exist_ok=True)
        self._saved = (pdfconv.mineru_kit, pdfconv.convert_pdf, mineru_svc.use, list(pdf_full.listeners), pdf_full.guard,
                       pdf_full.log, papers.QUICK, mineru_svc.in_meeting, mineru_svc.log)
        pdfconv.mineru_kit = (lambda: Path("C:/fake/mineru-kit.exe")) if self.kit else (lambda: None)
        mineru_svc.use = lambda start=True: contextlib.nullcontext(None)      # 不起常驻服务
        self.fake = FakeMinerU()
        pdfconv.convert_pdf = self.fake
        pdf_full.listeners[:] = []
        pdf_full.guard = None
        self.logs = []
        pdf_full.log = self.logs.append
        papers.QUICK = True

    def tearDown(self):
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(30), "后台队列没转完")
        (pdfconv.mineru_kit, pdfconv.convert_pdf, mineru_svc.use, listeners, pdf_full.guard, pdf_full.log,
         papers.QUICK, mineru_svc.in_meeting, mineru_svc.log) = self._saved
        pdf_full.listeners[:] = listeners
        shutil.rmtree(WORK, ignore_errors=True)

    def import_pdf(self, name="paper.pdf"):
        src = make_pdf(WORK / name.rsplit(".", 1)[0] / name)
        t0 = time.perf_counter()
        info = papers.import_file(src)
        return src, info, time.perf_counter() - t0


class QuickThenFull(Base):
    def test_text_first_then_swapped(self):
        src, info, dt = self.import_pdf()
        dst = Path(info["path"])
        self.assertLess(dt, 1.0, f"纯文本应该 1 秒内装好：{dt:.2f}s")
        self.assertTrue(info.get("pending_full"))
        self.assertEqual(dst.parent.name, "paper-bloom")
        meta = pdf_full.read_meta(dst)
        self.assertEqual(meta.get("engine"), "pymupdf")
        self.assertTrue(meta.get("pending_full"))
        self.assertEqual(meta.get("md_sha1"), sha1(dst))
        self.assertIn("Time series generation", dst.read_text(encoding="utf-8"))
        self.assertTrue(pdf_full.pending(dst))
        self.assertTrue(self.fake.started.wait(5), "MinerU 应该已经在后台开始")
        # 摊开着（对比页有基线）：换的时候基线一起换
        b = view.base_for(dst)
        b.parent.mkdir(parents=True, exist_ok=True)
        view._copy_doc(dst, b)
        view._copy_doc(b, Path(str(b) + ".orig"))
        # 同一篇再导入：沿用纯文本版、不重复排
        info2 = papers.import_file(src)
        self.assertEqual(Path(info2["path"]), dst)
        self.assertTrue(info2.get("pending_full"))
        self.assertEqual(len(pdf_full.pending_paths()), 1)
        seen = []
        pdf_full.listeners.append(lambda d, o, i: seen.append((d, o, pdf_full.pending(d))))
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(20))
        self.assertEqual(len(self.fake.calls), 1, "同一篇只转一次")
        self.assertEqual(dst.read_text(encoding="utf-8"), FULL_MD)
        self.assertTrue((dst.parent / "figures" / "fig1.png").is_file())
        meta = pdf_full.read_meta(dst)
        self.assertEqual(meta.get("engine"), "mineru")
        self.assertNotIn("pending_full", meta)
        self.assertEqual(meta.get("md_sha1"), sha1(dst))
        self.assertEqual((meta.get("figures"), meta.get("tables"), meta.get("equations")), (1, 1, 1))
        self.assertEqual(b.read_text(encoding="utf-8"), FULL_MD, "基线换成完整版")
        self.assertEqual(Path(str(b) + ".orig").read_text(encoding="utf-8"), FULL_MD, "开会那一刻的原文也换")
        self.assertEqual((Path(info["dir"]) / "original.md").read_text(encoding="utf-8"), FULL_MD, "导入时的原样也换")
        self.assertEqual(seen, [(dst, "swapped", False)], "收尾时这篇已经不算等着了")
        self.assertFalse(pdf_full.pending(dst))
        # 换完以后再导入：用转好的完整版，不再排
        info3 = papers.import_file(src)
        self.assertFalse(info3.get("pending_full"))
        self.assertEqual(Path(info3["path"]).read_text(encoding="utf-8"), FULL_MD)
        self.assertFalse(pdf_full.pending_paths())

    def test_edited_meanwhile_not_swapped(self):
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        self.assertTrue(self.fake.started.wait(5))
        edited = dst.read_text(encoding="utf-8") + "\n手改的一段。\n"
        dst.write_text(edited, encoding="utf-8")
        out = []
        pdf_full.listeners.append(lambda d, o, i: out.append((o, i.get("why"))))
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(20))
        self.assertEqual(dst.read_text(encoding="utf-8"), edited, "改过的不换")
        self.assertFalse((dst.parent / "figures" / "fig1.png").exists())
        meta = pdf_full.read_meta(dst)
        self.assertEqual(meta.get("engine"), "pymupdf")
        self.assertNotIn("pending_full", meta)
        self.assertEqual(meta.get("full_skipped"), "edited")
        self.assertEqual(out, [("kept", "edited")])

    def test_revised_by_ai_not_swapped(self):
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        pdf_full.guard = lambda d: "revised"
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(20))
        self.assertNotEqual(dst.read_text(encoding="utf-8"), FULL_MD)
        self.assertEqual(pdf_full.read_meta(dst).get("full_skipped"), "revised")

    def test_failure_keeps_text(self):
        self.fake.fail = True
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        text = dst.read_text(encoding="utf-8")
        out = []
        pdf_full.listeners.append(lambda d, o, i: out.append(o))
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(20))
        self.assertEqual(dst.read_text(encoding="utf-8"), text, "失败留着纯文本")
        meta = pdf_full.read_meta(dst)
        self.assertEqual(meta.get("engine"), "pymupdf")
        self.assertNotIn("pending_full", meta)
        self.assertIn("显存不足", meta.get("full_error", ""))
        self.assertEqual(out, ["failed"])
        self.assertTrue(any("失败" in x for x in self.logs), self.logs)
        self.assertFalse(pdf_full.pending(dst))


class TasksWait(Base):
    def test_edit_tasks_wait_for_full_version(self):
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        other = WORK / "other" / "b.md"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("# B\n\nB 正文。\n", encoding="utf-8")
        b = boardmod.Board(path=WORK / "board.json")
        ta = b.add("把引言改短", "引言改短", paper=paper_entry(dst, "paper"))
        tb = b.add("B 的标题改一下", "B 标题", paper=paper_entry(other, "B"))
        h = object.__new__(Hands)
        h.board, h._paper_file = b, dst
        got = b.claim_next(skip=h.waiting_doc)
        self.assertEqual(got["id"], tb["id"], "别的文档上的任务照常开始")
        self.assertIsNone(b.claim_next(skip=h.waiting_doc), "这篇的任务排着，不开始")
        self.assertEqual(b.get(ta["id"])["status"], "queued")
        # 旧任务（没记是哪篇）：算屏幕上那篇
        old = b.add("旧任务", "旧任务")
        self.assertTrue(h.waiting_doc(b.get(old["id"])))
        # "AI 在干什么"：等文档识别完的单独数，不算进排队
        from zuhui.app import Meeting
        app = Meeting.__new__(Meeting)
        app._mute, app._talk, app._deep_jobs, app.pending_answers = False, None, [], []
        app.mouth = types.SimpleNamespace(speaking=False)
        app.cfg = {"fast": {}, "hands": {}, "deep": {}}
        app.board, app.hands = b, h
        st = app.brain_state()
        self.assertEqual((st["waiting"], st["queued"]), (2, 0), st)
        b.add("屏幕外那篇再改一条", "B 再改", paper=paper_entry(other, "B"))
        st = app.brain_state()
        self.assertEqual((st["waiting"], st["queued"]), (2, 1), st)
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(20))
        got = b.claim_next(skip=h.waiting_doc)
        self.assertEqual(got["id"], ta["id"], "换成完整版之后开始")

    def test_cli_worker_thread_waits(self):
        """真的工人线程（backend=cli，用 Claude Code 账号时就是它）：这篇换成完整版之前不领，换完马上领，改的是完整版。"""
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        b = boardmod.Board(path=WORK / "board.json")
        h = Hands({"hands": {"backend": "cli", "workers": 1}}, b, lambda t: None, dst)
        self.assertEqual(h.backend, "cli")
        ran = []

        def fake_run(task):
            ran.append((task["id"], dst.read_text(encoding="utf-8") == FULL_MD))
            b.finish(task["id"], result="已改")
        h._run = fake_run
        pdf_full.listeners.append(lambda d, o, i: h.notify())       # app 的收尾也是这样叫醒工人
        t = b.add("把引言改短", "引言改短", paper=paper_entry(dst, "paper"))
        h.start()
        try:
            time.sleep(1.5)
            self.assertEqual(ran, [], "还在识别：不改")
            self.assertEqual(b.get(t["id"])["status"], "queued")
            t0 = time.time()
            self.fake.gate.set()
            self.assertTrue(pdf_full.wait_idle(20))
            while not ran and time.time() - t0 < 5:
                time.sleep(0.05)
            self.assertEqual(ran, [(t["id"], True)], "换成完整版后才改，改的是完整版")
            self.assertLess(time.time() - t0, 3.0, "换完马上开始（叫醒，不等下一轮）")
        finally:
            h.stop()

    def test_ccd_worker_waits(self):
        """CCD 工人（另一个进程，python -m zuhui worker watch / claim）：看 meta 里的 pending_full，换完才报、才领。"""
        from zuhui import __main__ as zmain
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        other = WORK / "other" / "b.md"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("# B\n\nB 正文。\n", encoding="utf-8")
        b = boardmod.Board(path=WORK / "board.json")
        ta = b.add("把引言改短", "引言改短", paper=paper_entry(dst, "paper"))
        tb = b.add("B 的标题改一下", "B 标题", paper=paper_entry(other, "B"))
        self.assertTrue(pdf_full.waiting_on_disk(dst))
        self.assertFalse(pdf_full.waiting_on_disk(other))
        seen = set()
        self.assertEqual([e["id"] for e in zmain.ccd_events(b, seen)], [tb["id"]], "还在识别的先不报")
        r = zmain.ccd_claim(b, ta["id"])
        self.assertEqual((r["status"], r.get("waiting")), ("queued", "doc"), "还在识别的不领")
        self.assertEqual(b.get(ta["id"])["status"], "queued")
        self.assertEqual(zmain.ccd_claim(b, tb["id"])["status"], "working", "别的文档照常")
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(20))
        self.assertFalse(pdf_full.waiting_on_disk(dst))
        self.assertEqual([e["id"] for e in zmain.ccd_events(b, seen)], [ta["id"]], "换完再报")
        self.assertEqual(zmain.ccd_claim(b, ta["id"])["status"], "working")

    def test_failure_also_releases_tasks(self):
        self.fake.fail = True
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        b = boardmod.Board(path=WORK / "board.json")
        ta = b.add("改一下", "改一下", paper=paper_entry(dst, "paper"))
        h = object.__new__(Hands)
        h.board, h._paper_file = b, dst
        self.assertIsNone(b.claim_next(skip=h.waiting_doc))
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(20))
        self.assertEqual(b.claim_next(skip=h.waiting_doc)["id"], ta["id"])


class InMeetingSwap(Base):
    def test_swap_during_meeting_rebases_page_and_versions(self):
        from zuhui.app import Meeting
        from zuhui.paper import Paper
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        ms = Meetings(root=WORK / "meetings")
        b = boardmod.Board(path=WORK / "board.json")
        v = view.Viewer.__new__(view.Viewer)
        v.board, v._sig, v.on_edit, v.focus = b, "old-sig", None, None
        v.lock, v._gen_lock = threading.Lock(), threading.RLock()
        v.mirror, v.base = dst, view.base_for(dst)
        v._pages = {paper_id(dst): ("sig", "<html>纯文本版的页面</html>")}
        v._urgent, v._wake = threading.Event(), threading.Event()
        v.ensure_base(rebase=True)                       # 开会：以现在（纯文本）为原文
        entry = paper_entry(dst, "paper")
        m = ms.start(session="s-pdfq", title="paper", papers=[entry], active=entry["id"])
        before = paper_versions.snapshot_meeting(m, "before")
        self.assertEqual(before[0]["sha"], sha1(dst))
        app = Meeting.__new__(Meeting)
        app.meetings, app.board, app.viewer = ms, b, v
        app.paper = Paper(dst, "paper")
        woke = []
        app.hands = types.SimpleNamespace(notify=lambda: woke.append(1), waiting_doc=lambda t: False)
        app.fast = types.SimpleNamespace(_recall=object(), warm=lambda: True)
        app._paper_lock = threading.Lock()
        app.cfg = {"meeting": {"lang": "zh"}}
        logs = []
        app.log = lambda *a, **k: logs.append(k)
        app._attach_pdf_full()
        try:
            self.assertIsNone(app._full_guard(dst))
            b.add("改一下", "改一下", paper=entry)          # 排着队的不算"已完成的修订"
            self.assertIsNone(app._full_guard(dst))
            self.fake.gate.set()
            self.assertTrue(pdf_full.wait_idle(20))
            self.assertEqual(dst.read_text(encoding="utf-8"), FULL_MD)
            self.assertEqual(v.base0.read_text(encoding="utf-8"), FULL_MD, "开会那一刻的原文换成完整版")
            self.assertNotIn(paper_id(dst), v._pages, "纯文本版的旧页面丢掉")
            self.assertIsNone(v._sig)
            self.assertTrue(v._urgent.is_set() and v._wake.is_set(), "对比页马上重做")
            self.assertIsNone(app.fast._recall)
            self.assertIn("Full Paper", "\n".join(app.paper.lines))
            self.assertTrue(woke, "叫醒工人")
            items = paper_versions.info(paper_id(dst))["items"]
            mine = [x for x in items if x.get("kind") == "before" and x.get("mid") == m["id"]]
            self.assertEqual(len(mine), 1)
            self.assertEqual(mine[0]["sha"], sha1(dst), "开会前的版本换成完整版")
            self.assertTrue(mine[0]["same"])
        finally:
            ms.end(board_tasks=[])
            shutil.rmtree(paper_versions.ROOT / paper_id(dst), ignore_errors=True)
            for f in (v.base, v.base0):
                try:
                    f.unlink()
                except OSError:
                    pass

    def test_guard_sees_finished_revision(self):
        from zuhui.app import Meeting
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        b = boardmod.Board(path=WORK / "board.json")
        app = Meeting.__new__(Meeting)
        app.board = b
        t = b.add("改", "改", paper=paper_entry(dst, "paper"))
        b.finish(t["id"], result="已改")
        self.assertEqual(app._full_guard(dst), "revised")
        b.resolve(t["id"], rejected=True)
        self.assertIsNone(app._full_guard(dst), "撤回了的不算")


class NoMinerU(Base):
    kit = False

    def test_unchanged_without_mineru(self):
        pdfconv.convert_pdf = self._saved[1]           # 真的 convert_pdf：没装 MinerU 时抛 ConvertError，退回 PyMuPDF
        src, info, dt = self.import_pdf()
        dst = Path(info["path"])
        self.assertNotIn("pending_full", info)
        self.assertEqual(info["convert"]["engine"], "pymupdf")
        self.assertIn("没装 MinerU", info["convert"].get("warning", ""))
        meta = pdf_full.read_meta(dst)
        self.assertEqual(meta.get("engine"), "pymupdf")
        self.assertNotIn("pending_full", meta)
        self.assertFalse(pdf_full.pending_paths())
        self.assertEqual(dst.read_text(encoding="utf-8"), papers._pdf_text(src))
        self.assertLess(dt, 2.0)
        self.assertEqual(pdf_full.resume([dst]), 0)

    def test_quick_off_waits_for_mineru(self):
        """ZUHUI_PDF_QUICK=0：退回以前（导入时等 MinerU 转完）。"""
        pdfconv.mineru_kit = lambda: Path("C:/fake/mineru-kit.exe")
        papers.QUICK = False
        self.fake.gate.set()
        _, info, _ = self.import_pdf()
        self.assertNotIn("pending_full", info)
        self.assertEqual(Path(info["path"]).read_text(encoding="utf-8"), FULL_MD)


class Resume(Base):
    def test_resume_after_restart(self):
        _, info, _ = self.import_pdf()
        dst = Path(info["path"])
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(20))
        # 假装后端重启前还没转：meta 写回纯文本 + pending_full
        dst.write_text("# paper\n\n纯文本。\n", encoding="utf-8")
        pdf_full._update_meta(dst, engine="pymupdf", pending_full=True, md_sha1=sha1(dst))
        self.fake.gate.clear()
        self.assertEqual(pdf_full.resume([dst, dst]), 1)
        self.assertTrue(pdf_full.pending(dst))
        self.fake.gate.set()
        self.assertTrue(pdf_full.wait_idle(20))
        self.assertEqual(dst.read_text(encoding="utf-8"), FULL_MD)
        # 被改过的不再排
        dst.write_text("# paper\n\n改过。\n", encoding="utf-8")
        pdf_full._update_meta(dst, pending_full=True)
        self.assertEqual(pdf_full.resume([dst]), 0)
        self.assertEqual(pdf_full.read_meta(dst).get("full_skipped"), "edited")


class ServerIdle(unittest.TestCase):
    """常驻服务的回收规则：不真起进程，只摆状态。"""

    def setUp(self):
        self.saved = (mineru_svc._proc, mineru_svc._pid, mineru_svc._port, mineru_svc._ready, mineru_svc._busy,
                      mineru_svc._last_used, mineru_svc._lobby_at, mineru_svc.in_meeting, mineru_svc._stop_proc,
                      mineru_svc._adopted)
        self.stopped = []
        mineru_svc._stop_proc = lambda p: self.stopped.append(p)
        mineru_svc._adopted = True

    def tearDown(self):
        (mineru_svc._proc, mineru_svc._pid, mineru_svc._port, mineru_svc._ready, mineru_svc._busy, mineru_svc._last_used,
         mineru_svc._lobby_at, mineru_svc.in_meeting, mineru_svc._stop_proc, mineru_svc._adopted) = self.saved

    def up(self, last_used, lobby_at=0.0, meeting=False, busy=0):
        mineru_svc._proc, mineru_svc._pid, mineru_svc._port, mineru_svc._ready = None, 999999, 1, True
        mineru_svc._last_used, mineru_svc._lobby_at, mineru_svc._busy = last_used, lobby_at, busy
        mineru_svc.in_meeting = lambda: meeting

    def test_idle_rules(self):
        now = time.time()
        self.up(now - 9 * 60, lobby_at=now - 1)
        self.assertFalse(mineru_svc.reap_once(now), "空闲不到 10 分钟不关")
        self.up(now - 11 * 60, busy=1)
        self.assertFalse(mineru_svc.reap_once(now), "正在转不关")
        self.up(now - 11 * 60, lobby_at=now - 1)
        self.assertTrue(mineru_svc.reap_once(now), "10 分钟没有转换就关，大厅开着也关")
        self.assertIsNone(mineru_svc.url())
        self.up(now - 3 * 60, lobby_at=now - 1, meeting=True)
        self.assertTrue(mineru_svc.reap_once(now), "开会中转完空闲 2 分钟就关")

    def test_lobby_starts_only_when_it_comes_into_view(self):
        saved = (pdfconv.mineru_kit, mineru_svc._kick, mineru_svc._failed_at, mineru_svc._starting)
        kicks = []
        pdfconv.mineru_kit = lambda: Path("C:/fake/mineru-kit.exe")
        mineru_svc._kick = lambda: kicks.append(1)
        mineru_svc._failed_at, mineru_svc._starting = 0.0, None
        mineru_svc._proc = mineru_svc._pid = None
        try:
            mineru_svc._lobby_at = 0.0
            self.assertTrue(mineru_svc.lobby_seen(), "大厅第一次可见：起")
            self.assertFalse(mineru_svc.lobby_seen(), "大厅一直可见（1.5 秒一次）：不再起")
            for _ in range(5):                                  # 服务到点关了，大厅还开着：不再起
                mineru_svc._lobby_at = time.time() - 1.5
                self.assertFalse(mineru_svc.lobby_seen())
            self.assertEqual(len(kicks), 1)
            mineru_svc._lobby_at = time.time() - 60             # 切走一分钟又回到大厅：重新可见，再起
            self.assertTrue(mineru_svc.lobby_seen())
            mineru_svc._lobby_at = time.time() - 60
            self.assertFalse(mineru_svc.lobby_seen(meeting=True), "开会中不因为大厅起")
            self.assertEqual(len(kicks), 2)
        finally:
            pdfconv.mineru_kit, mineru_svc._kick, mineru_svc._failed_at, mineru_svc._starting = saved

    def test_meeting_start_releases_idle_server(self):
        self.up(time.time())
        mineru_svc.on_meeting_start()
        self.assertEqual(len(self.stopped), 1)
        self.up(time.time(), busy=1)
        mineru_svc.on_meeting_start()
        self.assertEqual(len(self.stopped), 1, "正在转的不关")

    def test_lobby_does_nothing_without_mineru(self):
        saved = pdfconv.mineru_kit
        pdfconv.mineru_kit = lambda: None
        try:
            mineru_svc._proc = mineru_svc._pid = None
            mineru_svc.lobby_seen()
            self.assertFalse(mineru_svc.status()["running"])
            self.assertFalse(mineru_svc.status()["starting"])
            self.assertIsNone(mineru_svc.ensure())
        finally:
            pdfconv.mineru_kit = saved


if __name__ == "__main__":
    unittest.main()
