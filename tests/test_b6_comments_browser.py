# -*- coding: utf-8 -*-
"""第 6 批：论文上打字批注，在真浏览器里（无头 Edge / Chrome）看论文页（zuhui/comments.py 注入的那段脚本）。
临时 ZUHUI_HOME、空闲端口起一份对比页服务（不碰 47330），开一场沙盒会：
  · 外壳推批注进来（postMessage zhComments）→ 那一段左边出气泡；悬停一段出"批注"按钮；
  · 在页面上写一条、回复、标为已解决、删掉（真的走 /api/comment 存进会议目录）；
  · 气泡、按钮、卡片都不挤动正文（那一段的位置前后一样）；卡片在那一段下面或上面，不盖住它；
  · 输入框最多 500 字；章节名里不混进气泡的条数（不影响"翻到哪一节"的上报）；
  · PPT 讲稿：工具栏的"编辑"去掉。
跑：python -X utf8 -m unittest tests.test_b6_comments_browser -v（没有 playwright / 浏览器时跳过）"""
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
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-cmb-")
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

PAPER = ("# 组会论文实时修订助手\n\n## 引言\n\n组会上老师的意见常常口头给出，学生会后凭记忆修改论文，容易遗漏。\n\n"
         "本文提出 Bloom，一个会上实时改稿的助手。\n\n## 方法\n\n转写模块在本机运行，延迟约 310 毫秒。\n\n"
         "判断何时发言用一个小分类器。\n\n## 实验\n\n在公开数据集上，准确率由 75.1% 提高到 94.9%。\n")


def _free_port() -> int:
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


@unittest.skipUnless(sync_playwright and BROWSER, "没有 playwright / 浏览器")
class CommentsInBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from zuhui import view
        from zuhui.meetings import Meetings, paper_entry, paper_id
        cls.tmp = Path(tempfile.mkdtemp(prefix="cmb-", dir=str(HOME)))
        cls.paper = cls.tmp / "论文.md"
        cls.paper.write_bytes(PAPER.encode("utf-8"))
        cls.lib = Meetings(root=cls.tmp / "meetings")
        cls.port = _free_port()
        v = view.Viewer(cfg={"paper": {"file": str(cls.paper)}, "meeting": {"me": "王老师"}}, port=cls.port, meetings=cls.lib)
        v.mirror = cls.paper
        v.base = view.base_for(cls.paper)
        v.ensure_base(rebase=True)
        cls.v = v
        cls.srv = v.serve(open_browser=False)
        t0 = time.time()
        while time.time() - t0 < 60 and v.build < 1:
            time.sleep(0.1)
        cls.mid = cls.lib.start(papers=[paper_entry(cls.paper, "论文")])["id"]
        cls.pid = paper_id(cls.paper)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=BROWSER, headless=True)
        cls.page = cls.browser.new_page(viewport={"width": 1280, "height": 800}, color_scheme="dark")
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

    def push(self, items, add=True, dele=True):
        msg = {"zhComments": {"items": items, "can": {"add": add, "del": dele}, "colors": {"王老师": "#60a5fa"}, "paper": self.pid}}
        self.page.evaluate("m => window.postMessage(m, '*')", msg)
        self.page.wait_for_timeout(150)

    def rect(self, i):
        return self.page.evaluate("i => { const r = document.querySelectorAll('.docs .row')[i].querySelector('.para').getBoundingClientRect();"
                                  " return [r.x, r.y, r.width, r.height]; }", i)

    def test_write_reply_resolve_delete(self):
        p = self.page
        p.goto(self.url, wait_until="load")
        p.wait_for_selector(".docs .row")
        self.push([])
        before = self.rect(1)
        sect0 = p.evaluate("() => document.querySelector('.sect.lv2').textContent")
        p.hover(".docs .row >> nth=1")
        p.wait_for_selector("#zh-cm-add.show")
        self.assertEqual(self.rect(1), before)                         # 按钮浮在边上，正文不动
        p.click("#zh-cm-add")
        p.wait_for_selector("#zh-cm-card textarea")
        p.fill("#zh-cm-card textarea", "x" * 600)
        self.assertEqual(p.eval_on_selector("#zh-cm-card textarea", "t => t.value.length"), 500)
        p.fill("#zh-cm-card textarea", "这一段要补一个例子")
        p.click('#zh-cm-card [data-a="send"]')
        p.wait_for_selector(".zh-cm-pin")
        self.assertEqual(self.rect(1), before)                         # 气泡、卡片都不挤动正文
        saved = json.loads((self.lib.root / self.mid / "comments.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(saved[0]["text"], "这一段要补一个例子")
        self.assertEqual(saved[0]["by"], "王老师")
        self.assertEqual(saved[0]["paper"], self.pid)
        self.assertTrue(saved[0]["key"].startswith("本文提出Bloom"))
        # 卡片不盖住那一段
        box = p.evaluate("() => { const c = document.getElementById('zh-cm-card').getBoundingClientRect();"
                         " const r = document.querySelector('.zh-cm-pin').parentNode.getBoundingClientRect(); return [c.top, c.bottom, r.top, r.bottom]; }")
        self.assertTrue(box[0] >= box[3] - 1 or box[1] <= box[2] + 1, box)
        # 回复、解决
        p.click('#zh-cm-card [data-a="reply"]')
        p.fill("#zh-cm-card textarea", "好，下周补")
        p.click('#zh-cm-card [data-a="send"]')
        p.wait_for_function("() => document.querySelectorAll('#zh-cm-card .zh-cm-rep').length === 1")
        p.click('#zh-cm-card [data-a="res"]')
        p.wait_for_selector(".zh-cm-pin.is-done")
        saved = json.loads((self.lib.root / self.mid / "comments.json").read_text(encoding="utf-8"))["items"]
        self.assertTrue(saved[0]["resolved"])
        self.assertEqual(saved[0]["replies"][0]["text"], "好，下周补")
        # 章节名不混进条数（报给后端的"翻到哪一节"靠它）
        self.assertEqual(p.evaluate("() => document.querySelector('.sect.lv2').textContent"), sect0)
        # 外壳再推一次（下一次轮询）：照样画出来；回顾里只读：没有输入框、没有删除
        self.push(saved, add=False, dele=False)
        self.assertEqual(p.evaluate("() => document.querySelectorAll('.zh-cm-pin').length"), 1)
        p.click(".zh-cm-pin")
        self.assertEqual(p.evaluate("() => document.querySelectorAll('#zh-cm-card textarea, #zh-cm-card [data-a=del]').length"), 0)
        p.keyboard.press("Escape")
        # 主持人删掉
        self.push(saved, add=True, dele=True)
        p.click(".zh-cm-pin")
        p.click('#zh-cm-card .zh-cm-th [data-a="del"]')
        p.wait_for_function("() => !document.querySelector('.zh-cm-pin')")
        self.assertEqual(json.loads((self.lib.root / self.mid / "comments.json").read_text(encoding="utf-8"))["items"], [])
        shots = os.environ.get("BLOOM_SHOTS")
        if shots:
            p.screenshot(path=str(Path(shots) / "b6-comments.png"))

    def test_readonly_hides_edit(self):
        from zuhui import view
        html = view._inject(1, [], "我", {}, "zh", paper="x", readonly=True)
        self.assertIn("var READONLY = true", html)
        p = self.page
        p.set_content("<html><body><div class='bar'><button id='zh-editbtn'>编辑</button></div><div class='docs'>"
                      "<div class='row'><div class='para'>第一段</div></div></div>"
                      + __import__("zuhui.comments", fromlist=["inject"]).inject("zh", True) + "</body></html>")
        self.assertEqual(p.evaluate("() => !!document.getElementById('zh-editbtn')"), False)


if __name__ == "__main__":
    unittest.main()
