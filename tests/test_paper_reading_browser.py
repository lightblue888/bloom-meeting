# -*- coding: utf-8 -*-
"""论文区（2026-11-21）在真浏览器里（无头 Edge / Chrome）：论文页嵌在一个假外壳里（顶层页 + iframe，和 Bloom 窗口一样）。
临时 ZUHUI_HOME、空闲端口起一份对比页服务（不碰 47330），开一场沙盒会，论文是编出来的：
  · 选中一段：外壳说能引用（zhQuote）才弹「引用」，弹在选区结尾旁边、不挤动正文；点了才报给外壳（zhSel），选区清掉；
    Esc、点别处收起；外壳没说能引用（参会者、散会后）就不弹；
  · 读到哪：在屏幕上的这一份边读边记，整页重来（外壳连不上后端几秒后又回来）回到同一段；
    外壳换版本前问它（zhReadAsk）：读到哪、是不是正在写批注；让新的一份停到那一段（zhGo → zhGone）；
  · 写批注的框：一行起，回车发送，快到 500 字才显示字数，出错不改变框的大小。
跑：python -X utf8 -m unittest tests.test_paper_reading_browser -v（没有 playwright / 浏览器时跳过）"""
import json
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
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-read-")
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


def _paper() -> str:
    out = ["# 一篇编出来的论文\n"]
    for s, name in enumerate(["引言", "方法", "实验", "讨论"]):
        out.append(f"## {s + 1} {name}\n")
        for i in range(6):
            out.append(f"第{s + 1}节第{i + 1}段：这里描述一个编出来的步骤，样本量为 {100 + s * 6 + i} 条，窗口取 {5 + i} 秒，"
                       "这一段只是占位，写得长一些，好让每段都占两三行，方便看页面停在哪一段。\n")
    return "\n".join(out)


def _free_port() -> int:
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


# 假外壳：一个 iframe 装论文页；论文页发上来的消息都记在 window.__got 里
SHELL = """<!doctype html><html><body style="margin:0">
<iframe id="paper" src="%s" style="border:0;width:1000px;height:760px"></iframe>
<script>window.__got = []; addEventListener('message', function(e){ window.__got.push(e.data); });</script></body></html>"""


@unittest.skipUnless(sync_playwright and BROWSER, "没有 playwright / 浏览器")
class PaperReading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from zuhui import view
        from zuhui.meetings import Meetings, paper_entry
        cls.tmp = Path(tempfile.mkdtemp(prefix="read-", dir=str(HOME)))
        cls.paper = cls.tmp / "论文.md"
        cls.paper.write_bytes(_paper().encode("utf-8"))
        cls.lib = Meetings(root=cls.tmp / "meetings")
        cls.port = _free_port()
        v = view.Viewer(cfg={"paper": {"file": str(cls.paper)}, "meeting": {"me": "主持人"}, "hands": {}}, port=cls.port, meetings=cls.lib)
        v.mirror = cls.paper
        v.base = view.base_for(cls.paper)
        v.ensure_base(rebase=True)
        cls.v = v
        cls.srv = v.serve(open_browser=False)
        t0 = time.time()
        while time.time() - t0 < 60 and v.build < 1:
            time.sleep(0.1)
        cls.lib.start(papers=[paper_entry(cls.paper, "论文")])
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=BROWSER, headless=True)
        cls.url = f"http://127.0.0.1:{cls.port}/?embed=1&host=bloom&ui=zh"

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

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 1000, "height": 760}, color_scheme="dark")
        self.page = self.ctx.new_page()
        self.page.goto(f"http://127.0.0.1:{self.port}/version")       # 外壳和论文页同一个源：sessionStorage 共用一份，和 Bloom 里一样
        self.page.set_content(SHELL % self.url)
        self.f = self.frame()

    def tearDown(self):
        self.ctx.close()

    def frame(self):
        self.page.wait_for_function("() => { const f = document.getElementById('paper'); return f && f.contentDocument && f.contentDocument.querySelector('.docs .row'); }")
        f = self.page.frame_locator("#paper").owner.element_handle().content_frame()
        f.wait_for_load_state("load")
        return f

    def tell(self, msg):
        """外壳 → 论文页"""
        self.page.evaluate("m => document.getElementById('paper').contentWindow.postMessage(m, '*')", msg)
        self.page.wait_for_timeout(120)

    def got(self, key):
        return [m[key] for m in self.page.evaluate("() => window.__got") if isinstance(m, dict) and key in m]

    def top(self):
        return self.f.evaluate("""() => { const rows = document.querySelectorAll('.docs .row');
            for (let i = 0; i < rows.length; i++) { const r = rows[i].getBoundingClientRect(); if (r.bottom > 8 && r.height > 0) return [i, Math.round(r.top)]; } }""")

    def drag(self, row, dx=300, dy=30):
        """拖选第 row 段第一行起的一截（顶层页坐标）"""
        r = self.f.evaluate("k => { const p = document.querySelectorAll('.docs .row')[k].querySelector('.para'); p.scrollIntoView({block: 'center'});"
                            " const b = p.getBoundingClientRect(); return [b.x, b.y]; }", row)
        self.page.mouse.move(r[0] + 20, r[1] + 12)
        self.page.mouse.down()
        self.page.mouse.move(r[0] + 20 + dx, r[1] + 12 + dy, steps=6)
        self.page.mouse.up()
        self.page.wait_for_timeout(150)
        return r[0] + 20 + dx, r[1] + 12 + dy

    def quote_shown(self):
        return self.f.evaluate("() => document.getElementById('zh-quote').classList.contains('show')")

    # ------------------------------------------------------------------ 引用
    def test_quote_only_after_click(self):
        self.assertTrue(self.got("zhQuoteAsk"))                       # 论文页一加载就问能不能引用
        self.drag(3)
        self.assertFalse(self.quote_shown())                           # 外壳没说能：不弹
        self.assertEqual(self.got("zhSel"), [])
        self.tell({"zhQuote": {"can": True}})
        self.page.mouse.click(5, 700)
        nxt = self.f.evaluate("() => document.querySelectorAll('.docs .row')[4].getBoundingClientRect().top")
        end = self.drag(3)
        self.assertTrue(self.quote_shown())
        self.assertEqual(self.got("zhSel"), [])                        # 选中了还不算引用
        q = self.f.evaluate("() => { const r = document.getElementById('zh-quote').getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; }")
        self.assertLess(abs((q[0] + q[2]) / 2 - end[0]), 40, q)        # 在选区结尾旁边（下面一行）
        self.assertTrue(0 < q[1] - end[1] < 60, (q, end))
        self.assertEqual(self.f.evaluate("() => document.querySelectorAll('.docs .row')[4].getBoundingClientRect().top"), nxt)   # 不挤动正文
        self.f.click("#zh-quote button")
        self.page.wait_for_timeout(150)
        sel = self.got("zhSel")
        self.assertEqual(len(sel), 1)
        self.assertIn("第4段：这里描述一个编出来的步骤", sel[0]["text"])      # 第 1 节第 4 段那一截（不带段头、批注按钮的字）
        self.assertNotIn("段 · ", sel[0]["text"])
        self.assertEqual(sel[0]["section"], "1 引言")
        self.assertFalse(self.quote_shown())
        self.assertEqual(self.f.evaluate("() => String(getSelection())"), "")
        # Esc 收起；点别处收起
        self.drag(5)
        self.assertTrue(self.quote_shown())
        self.page.keyboard.press("Escape")
        self.assertFalse(self.quote_shown())
        self.page.mouse.click(5, 700)
        self.drag(5)
        self.assertTrue(self.quote_shown())
        self.page.mouse.click(5, 700)
        self.assertFalse(self.quote_shown())
        self.assertEqual(len(self.got("zhSel")), 1)
        # 散会了 / 参会者：外壳说不能，弹着的也收起
        self.drag(5)
        self.tell({"zhQuote": {"can": False}})
        self.assertFalse(self.quote_shown())

    # ------------------------------------------------------------------ 读到哪
    def test_reading_place_survives_a_reload(self):
        self.tell({"zhHello": 1})                                      # 外壳：这一份在屏幕上
        self.f.evaluate("() => { const r = document.querySelectorAll('.docs .row')[14]; window.scrollTo(0, r.getBoundingClientRect().top + scrollY - 40); }")
        self.page.wait_for_timeout(500)
        was = self.top()
        self.assertIn(was[0], (13, 14))
        # 外壳连不上后端几秒、整页重来：新加载的页面回到同一段
        self.page.set_content(SHELL % (self.url + "&v=2"))
        self.f = self.frame()
        self.page.wait_for_timeout(300)
        self.assertEqual(self.top(), was)

    def test_hand_over_to_a_new_version(self):
        self.tell({"zhHello": 1})
        self.f.evaluate("() => { const r = document.querySelectorAll('.docs .row')[9]; window.scrollTo(0, r.getBoundingClientRect().top + scrollY - 2); }")
        self.page.wait_for_timeout(300)
        was = self.top()
        self.assertEqual(was[0], 9)
        self.tell({"zhReadAsk": "s1"})
        ans = self.got("zhRead")[-1]
        self.assertEqual(ans["id"], "s1")
        self.assertEqual(ans["busy"], "")
        self.assertEqual(ans["at"]["i"], 9)
        self.assertTrue(ans["at"]["text"].startswith("第2节第4段"), ans)
        self.assertNotIn("段 · ", ans["at"]["text"])                   # 段头"第 N 段 · 已修改"不算进那一段的字
        # 新的一份（这里就是同一页滚回顶上）：停到那一段，回 zhGone
        self.f.evaluate("() => window.scrollTo(0, 0)")
        self.tell({"zhGo": ans["at"], "id": "s1"})
        self.assertEqual(self.got("zhGone"), ["s1"])
        self.assertEqual(self.top(), was)
        # 别的一篇（编号不一样）：不往这篇上套，照样回 zhGone
        self.f.evaluate("() => window.scrollTo(0, 0)")
        self.tell({"zhGo": dict(ans["at"], d="another"), "id": "s2"})
        self.assertEqual(self.got("zhGone"), ["s1", "s2"])
        self.assertEqual(self.top()[0], 0)
        # 正在写批注：外壳先别换
        self.tell({"zhComments": {"items": [], "can": {"add": True, "del": True}, "colors": {}, "paper": None}})
        self.f.hover(".docs .row >> nth=2")
        self.f.click("#zh-cm-add")
        self.f.fill("#zh-cm-card textarea", "写到一半")
        self.page.wait_for_timeout(800)
        self.tell({"zhReadAsk": "s3"})
        self.assertEqual(self.got("zhRead")[-1]["busy"], "comment")

    # ------------------------------------------------------------------ 写批注的框
    def test_comment_box(self):
        self.tell({"zhComments": {"items": [], "can": {"add": True, "del": True}, "colors": {}, "paper": None}})
        self.f.hover(".docs .row >> nth=4")
        self.f.click("#zh-cm-add")
        self.f.wait_for_selector("#zh-cm-card textarea")
        size = lambda: self.f.evaluate("() => { const r = document.getElementById('zh-cm-card').getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }")
        empty = size()
        self.assertLess(empty[1], 100, empty)                          # 一行的框 + 发送那一行
        self.assertEqual(self.f.evaluate("() => document.querySelectorAll('#zh-cm-card [data-a=cancel]').length"), 0)
        self.assertTrue(self.f.evaluate("() => document.querySelector('.zh-cm-n').classList.contains('off')"))
        self.f.fill("#zh-cm-card textarea", "长" * 420)
        self.assertEqual(self.f.evaluate("() => { const n = document.querySelector('.zh-cm-n'); return n.classList.contains('off') ? '' : n.textContent; }"), "420/500")
        self.f.fill("#zh-cm-card textarea", "这一段要补一个例子")
        self.assertEqual(size(), empty)
        # 出错：原因出在框底下那一行，框不变大
        self.page.route("**/api/comment", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": False, "error": "failed"})))
        self.f.press("#zh-cm-card textarea", "Enter")
        self.f.wait_for_selector(".zh-cm-err:not(.off)")
        self.assertEqual(self.f.evaluate("() => document.querySelector('.zh-cm-err').textContent"), "发送失败：请重试")
        self.assertEqual(size(), empty)
        self.page.unroute("**/api/comment")
        # Shift+回车换行，回车发出去；发出去以后报错清掉
        self.f.press("#zh-cm-card textarea", "Shift+Enter")
        self.assertIn("\n", self.f.evaluate("() => document.querySelector('#zh-cm-card textarea').value"))
        self.f.press("#zh-cm-card textarea", "Enter")
        self.f.wait_for_selector(".zh-cm-pin")
        self.assertTrue(self.f.evaluate("() => document.querySelector('.zh-cm-err').classList.contains('off')"))
        saved = json.loads((self.lib.root / self.lib.current()["id"] / "comments.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(saved[-1]["text"], "这一段要补一个例子")
        shots = os.environ.get("BLOOM_SHOTS")
        if shots:
            self.page.screenshot(path=str(Path(shots) / "paper-comment-box.png"))


if __name__ == "__main__":
    unittest.main()
