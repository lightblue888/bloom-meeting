# -*- coding: utf-8 -*-
"""论文页上手改一段，在真浏览器里（无头 Edge / Chrome）看：存好后这一段不再标成修订（2026-11-21 第二场组会）。

那一段先被两条任务改过（都没打勾、带着红删除线绿下划线），作者在编辑框里整段重写后保存：
  · 换上来的新页面里这一段没有修订标记（不是改过的段落，没有插入、删除）；
  · 那两条任务的图钉还在，贴在这一段上，点开卡片照常显示、没有报错；
  · 在卡片上撤回其中一条：写出"撤回失败：此处已手动修改"，作者的字一个不少；
  · 页面上没有脚本报错。
临时 ZUHUI_HOME、端口 47531 起一份对比页服务（不碰 47330），论文是编的。
跑：python -X utf8 -m unittest tests.test_hand_edit_base_browser -v（没有 playwright / 浏览器时跳过）"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-handbase-b-")
from zuhui import config as zconfig  # noqa: E402

HOME = Path(zconfig.STATE)
if os.path.normcase(str(HOME.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("不在真实状态目录里跑")

try:
    from playwright.sync_api import sync_playwright
except Exception:                                    # noqa: BLE001
    sync_playwright = None
BROWSER = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                            r"C:\Program Files\Google\Chrome\Application\chrome.exe") if Path(p).is_file()), None)
PORT = 47531

PAPER = ("# 乡村入户调查方法\n\n## 4 方法\n\n### 4.2 抽样\n\n"
         "（1）抽样规则会改变结论。我们在四个县各抽了二十户，每户访谈一位成年人。\n\n"
         "（2）样本量够用，置信区间的宽度在可接受范围内。\n\n### 4.3 访谈\n\n访谈在农闲时进行，每次约四十分钟。\n")
HAND = "（1）抽样规则足以改变结论。我们在四个县按住址各抽了二十五户，户籍不再作为依据。"

ROW_JS = """s => { const r = Array.from(document.querySelectorAll('.docs .row')).find(e => {
    const c = e.cloneNode(true); c.querySelectorAll('del, .zh-pins, .zh-note').forEach(x => x.remove()); return c.textContent.includes(s); });
  return r ? {chg: r.classList.contains('chg'), ins: r.querySelectorAll('ins').length, del: r.querySelectorAll('del').length,
              pins: r.querySelectorAll('.zh-pins .zh-pin').length} : null; }"""


@unittest.skipUnless(sync_playwright and BROWSER, "没有 playwright / 浏览器")
class HandEditInBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from zuhui import hands as handsmod
        from zuhui import share, view
        from zuhui.meetings import Meetings, paper_entry
        share.PUBLIC_RELAY = "https://127.0.0.1:1/"
        for d in ("changes", "base", "view"):
            (HOME / d).mkdir(parents=True, exist_ok=True)
        cls.tmp = Path(tempfile.mkdtemp(prefix="hb-", dir=str(HOME)))
        cls.paper = cls.tmp / "入户调查.md"
        cls.paper.write_bytes(PAPER.encode("utf-8"))
        cls.lib = Meetings(root=cls.tmp / "meetings")
        v = view.Viewer(cfg={"paper": {"file": str(cls.paper)}, "meeting": {"me": "林晓"}}, port=PORT, meetings=cls.lib)
        v.mirror = cls.paper
        v.base = view.base_for(cls.paper)
        v.ensure_base(rebase=True)
        cls.v = v
        entry = paper_entry(cls.paper, "入户调查")
        v.papers = [entry]
        cls.ids = []
        for title, find, rep in (("改第一句", "抽样规则会改变结论。", "抽样规则足以改变结论。"),
                                 ("补两点", "每户访谈一位成年人。", "每户访谈一位成年人。一是按户籍抽，二是按住址抽。")):
            before = cls.paper.read_text(encoding="utf-8")
            after = before.replace(find, rep, 1)
            t = v.board.add(remark="改一下", title=title, paper=entry)
            v.board.claim(t["id"])
            cls.paper.write_bytes(after.encode("utf-8"))
            handsmod.Hands._capture_changes(None, t, before, after)
            time.sleep(0.01)
            v.board.finish(t["id"], result=f"已{title}。无")
            cls.ids.append(t["id"])
        cls.srv = v.serve(open_browser=False)
        t0 = time.time()
        while time.time() - t0 < 60 and v.build < 1:
            time.sleep(0.1)
        cls.lib.start(papers=[entry])
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=BROWSER, headless=True)
        cls.page = cls.browser.new_page(viewport={"width": 1280, "height": 800}, color_scheme="dark")
        cls.errors = []
        cls.page.on("pageerror", lambda e: cls.errors.append(str(e)))
        cls.page.on("console", lambda m: cls.errors.append(m.text)                   # 测试服务没有 favicon：不算
                    if m.type == "error" and not str((m.location or {}).get("url", "")).endswith("/favicon.ico") else None)
        cls.url = f"http://127.0.0.1:{PORT}/?embed=1&ui=zh"

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.pw.stop()
        finally:
            try:
                cls.lib.end()
            except Exception:                        # noqa: BLE001
                pass
            cls.v._stop.set()
            try:
                cls.srv.shutdown()
                cls.srv.server_close()
            except Exception:                        # noqa: BLE001
                pass
            time.sleep(0.5)
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def row(self, s: str):
        return self.page.evaluate(ROW_JS, s)

    def test_1_rewrite_clears_marks(self):
        from zuhui import view
        p = self.page
        p.goto(self.url, wait_until="load")
        p.wait_for_selector(".docs .para")
        before = self.row("（1）抽样规则足以改变结论")
        self.assertTrue(before and before["chg"] and before["ins"] > 0, before)          # 手改之前：两条任务的修订标记
        if p.get_attribute("#zh-editbtn", "aria-pressed") != "true":
            p.click("#zh-editbtn")
        p.locator(".docs .para", has_text="（1）抽样规则").first.click()
        p.wait_for_selector("textarea.zh-edit")
        p.fill("textarea.zh-edit", HAND)
        p.evaluate("() => { window.__old = 1; }")
        p.click(".zh-save")
        p.wait_for_selector("textarea.zh-edit", state="detached", timeout=3000)
        p.wait_for_function("() => !window.__old && document.querySelector('.docs .para')", timeout=20000)   # 新版本换上来
        after = self.row("户籍不再作为依据")
        self.assertIsNotNone(after)
        self.assertFalse(after["chg"], after)                                            # 不再是改过的段落
        self.assertEqual((after["ins"], after["del"]), (0, 0), after)
        self.assertGreaterEqual(after["pins"], 1, after)                                 # 两条任务的图钉贴在这一段上
        spans = lambda f: [k for _, _, k in view.Viewer._spans(f.read_text(encoding="utf-8"))]   # noqa: E731
        self.assertEqual(spans(self.v.base), spans(self.paper))
        self.assertEqual(self.errors, [])

    def test_2_card_opens_and_undo_keeps_text(self):
        p = self.page
        if not p.query_selector(".docs .para"):
            p.goto(self.url, wait_until="load")
            p.wait_for_selector(".docs .para")
        pin = p.locator(".docs .row", has_text="户籍不再作为依据").locator(".zh-pins .zh-pin").first
        pin.click()
        card = p.locator(".zh-note")
        card.wait_for()
        text = card.inner_text()
        for title in ("改第一句", "补两点"):
            self.assertIn(title, text)
        self.assertEqual(p.locator(".zh-note-bad").count(), 0)
        src = self.paper.read_bytes()
        card.locator(f'.zh-item[data-id="{self.ids[1]}"] .zh-note-x').click()           # 撤回"补两点"
        p.wait_for_function("() => (document.querySelector('.zh-note-bad') || {}).textContent === '撤回失败：此处已手动修改'",
                            timeout=5000)
        self.assertEqual(self.paper.read_bytes(), src)                                    # 作者的字一个不少
        self.assertFalse(self.v.board.get(self.ids[1]).get("rejected"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
