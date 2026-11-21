# -*- coding: utf-8 -*-
"""论文页上就地改一段，在真浏览器里（无头 Edge / Chrome）看（2026-11-21 第二场组会图 5："保存不了"）。

以前存好后编辑框一直开着、写"已保存"、保存按钮变灰点不动，要等新版本页面换上来才收起；论文一长、外壳换页慢，
看着就像没存上，接着打的字换页时也丢了。现在：
  · 点保存：存好了编辑框马上收起，那一段先按纯文字显示改后的样子；新版本换上来后是带格式的改后样子；
  · 存不上（稿子在这期间被别处改过）：编辑框留着，写出原因，保存按钮能再点；
  · 正改着一段时页面被换掉（外壳换上新版本、刷新）：换上来的页面接着打开这一段，打了一半的字还在；取消以后不再重开；
  · 逐字稿上记一行"谁改了哪一段"（zuhui/doc_edits.py）。
临时 ZUHUI_HOME、空闲端口起一份对比页服务（不碰 47330），论文是编的。
跑：python -X utf8 -m unittest tests.test_doc_edits_browser -v（没有 playwright / 浏览器时跳过）"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules:
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-docedit-b-")
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

PAPER = ("# 城市共享单车调度评测\n\n## 5 结果\n\n### 5.1 调度偏差\n\n本节回答调度规则是否让缺车率偏离人工复核。\n\n"
         "（1）调度规则足以把站点的缺车判断弄反。默认规则把其中 31.4% 报告为充足。按站点统计，偏差在 12.2% 到 13.9% 之间。\n\n"
         "（2）偏差的方向由规则决定。\n\n### 5.2 稳健性\n\n换一个城区划分，结论不变。\n")


def _free_port() -> int:
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


@unittest.skipUnless(sync_playwright and BROWSER, "没有 playwright / 浏览器")
class EditInBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from zuhui import view
        from zuhui.meetings import Meetings, paper_entry
        cls.tmp = Path(tempfile.mkdtemp(prefix="deb-", dir=str(HOME)))
        cls.paper = cls.tmp / "单车调度评测.md"
        cls.paper.write_bytes(PAPER.encode("utf-8"))
        cls.lib = Meetings(root=cls.tmp / "meetings")
        cls.port = _free_port()
        v = view.Viewer(cfg={"paper": {"file": str(cls.paper)}, "meeting": {"me": "林晓"}}, port=cls.port, meetings=cls.lib)
        v.mirror = cls.paper
        v.base = view.base_for(cls.paper)
        v.ensure_base(rebase=True)
        cls.v = v
        cls.srv = v.serve(open_browser=False)
        t0 = time.time()
        while time.time() - t0 < 60 and v.build < 1:
            time.sleep(0.1)
        cls.mid = cls.lib.start(papers=[paper_entry(cls.paper, "单车调度评测")])["id"]
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=BROWSER, headless=True)
        cls.page = cls.browser.new_page(viewport={"width": 1280, "height": 800}, color_scheme="dark")
        cls.url = f"http://127.0.0.1:{cls.port}/?embed=1&ui=zh"

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

    def open_page(self):
        p = self.page
        p.goto(self.url, wait_until="load")
        p.wait_for_selector(".docs .para")
        if p.get_attribute("#zh-editbtn", "aria-pressed") != "true":
            p.click("#zh-editbtn")
        return p

    def open_para(self, start: str):
        p = self.page
        p.locator(".docs .para", has_text=start).first.click()
        p.wait_for_selector("textarea.zh-edit")

    def para_text(self, start: str) -> str:
        return self.page.evaluate("s => (Array.from(document.querySelectorAll('.docs .para')).find(e => e.textContent.includes(s)) || {}).textContent || ''", start)

    def test_1_save_closes_at_once_and_shows_new_text(self):
        p = self.open_page()
        build0 = self.v.build
        self.open_para("（1）调度规则")
        val = p.input_value("textarea.zh-edit")
        p.fill("textarea.zh-edit", val.replace("按站点统计", "改成按周看，按站点统计"))
        p.evaluate("() => { window.__old = 1; }")
        t0 = time.time()
        p.click(".zh-save")
        p.wait_for_selector("textarea.zh-edit", state="detached", timeout=1500)       # 存好就收起，不等新版本
        self.assertLess(time.time() - t0, 1.5)
        self.assertEqual(self.v.build, build0)                                          # 新版本还没换上来
        self.assertIn("改成按周看，按站点统计", self.para_text("（1）调度规则"))           # 先按纯文字显示改后的样子
        self.assertIn("改成按周看", self.paper.read_text(encoding="utf-8"))
        # 新版本换上来（页面自己刷新）后还是改后的样子
        p.wait_for_function("() => !window.__old && document.querySelector('.docs .para')", timeout=15000)
        self.assertGreater(self.v.build, build0)
        self.assertIn("改成按周看，按站点统计", self.para_text("（1）调度规则"))
        self.assertEqual(p.locator("textarea.zh-edit").count(), 0)                        # 存好的不再重开
        rows = [ln for ln in self.lib.current_lines() if ln.get("tag") == "#doc_edit"]
        self.assertEqual(rows[-1]["text"], f"{self.v.me_name()} 改了 5.1 第（1）段")

    def test_2_failed_save_keeps_editor(self):
        p = self.open_page()
        self.open_para("（2）偏差的方向")
        p.fill("textarea.zh-edit", "（2）偏差的方向完全由规则决定。")
        src = self.paper.read_text(encoding="utf-8")                                     # 这期间别处改了这一段
        self.paper.write_text(src.replace("（2）偏差的方向由规则决定。", "（2）偏差的方向主要由规则决定。"), encoding="utf-8")
        p.click(".zh-save")
        p.wait_for_function("() => document.querySelector('.zh-edithint').textContent.includes('此段已被修改')")
        self.assertTrue(p.is_visible("textarea.zh-edit"))
        self.assertFalse(p.is_disabled(".zh-save"))                                      # 还能再点
        self.assertEqual(p.input_value("textarea.zh-edit"), "（2）偏差的方向完全由规则决定。")   # 打的字还在
        p.click(".zh-cancel")
        p.wait_for_selector("textarea.zh-edit", state="detached")

    def test_3_draft_survives_page_swap(self):
        p = self.open_page()
        self.open_para("换一个城区划分")
        p.fill("textarea.zh-edit", "换一个城区划分，结论基本不变，只有")
        p.reload(wait_until="load")                                                       # 外壳换上新版本 / 刷新
        p.wait_for_selector("textarea.zh-edit", timeout=5000)
        self.assertEqual(p.input_value("textarea.zh-edit"), "换一个城区划分，结论基本不变，只有")
        p.click(".zh-cancel")
        p.wait_for_selector("textarea.zh-edit", state="detached")
        p.reload(wait_until="load")
        p.wait_for_selector(".docs .para")
        p.wait_for_timeout(500)
        self.assertEqual(p.locator("textarea.zh-edit").count(), 0)                        # 取消了：不再重开

    def test_4_back_slot_follows_typing(self):
        """外壳的两个槽：前面那页正改着，后面那页先接着打开这一段；前面那页又打的字跟过去，换上来（zhHello）后有焦点。"""
        p = self.page
        p.goto(f"http://127.0.0.1:{self.port}/version", wait_until="load")
        src = self.url + "&host=bloom"
        p.evaluate("u => { document.body.innerHTML = '<iframe id=a style=\"width:900px;height:700px\"></iframe>"
                   "<iframe id=b style=\"width:900px;height:700px\"></iframe>'; document.getElementById('a').src = u; }", src)
        a = p.frame_locator("#a")
        a.locator(".docs .para").first.wait_for()
        if a.locator("#zh-editbtn").get_attribute("aria-pressed") != "true":
            a.locator("#zh-editbtn").click()
        a.locator(".docs .para", has_text="本节回答").first.click()
        a.locator("textarea.zh-edit").fill("本节回答调度规则是否让缺车率偏离人工复核，")
        p.evaluate("u => { document.getElementById('b').src = u; }", src)                # 外壳在后面那个槽里加载新版本
        b = p.frame_locator("#b")
        b.locator("textarea.zh-edit").wait_for(timeout=5000)
        self.assertEqual(b.locator("textarea.zh-edit").input_value(), "本节回答调度规则是否让缺车率偏离人工复核，")
        a.locator("textarea.zh-edit").press("End")
        a.locator("textarea.zh-edit").type("以及偏多少")                                    # 换上来之前又打的字
        p.wait_for_timeout(300)
        self.assertEqual(b.locator("textarea.zh-edit").input_value(), "本节回答调度规则是否让缺车率偏离人工复核，以及偏多少")
        p.evaluate("() => document.getElementById('b').contentWindow.postMessage({zhHello: 1}, '*')")
        p.wait_for_timeout(200)
        self.assertTrue(b.locator("textarea.zh-edit").evaluate("t => document.activeElement === t"))
        b.locator(".zh-cancel").click()
        # 前面那页取消了（或者存了）：后面那页接着开的也收起，换上来时不再冒出一个编辑框
        a.locator(".docs .para", has_text="（2）偏差的方向").first.click()
        a.locator("textarea.zh-edit").wait_for()
        p.evaluate("u => { document.getElementById('b').src = u + '&again=1'; }", src)
        b.locator("textarea.zh-edit").wait_for(timeout=5000)
        a.locator(".zh-cancel").click()
        b.locator("textarea.zh-edit").wait_for(state="detached", timeout=2000)


if __name__ == "__main__":
    unittest.main()
