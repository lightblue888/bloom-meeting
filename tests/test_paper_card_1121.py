# -*- coding: utf-8 -*-
"""论文页上的批注卡片（2026-11-21 组会后）：
  · 卡片太长："固定大小 加上滚动"：卡片最高 400px，头一行不动，原话、结果在卡片里滚；
  · 结果前面署 AI 现在的名字（设置里起的），不写死"AI"；原话后面附的"（当场复述的改法：…）"不上卡片；
  · 一处好几条（分组）：头上只写这一节，状态只写字（不再左边画勾），× 在每条那一行、✓ 只在头上，按钮、状态字上下对齐。
数据全是编的（会议原话的样子：口语、重复、带括号的复述）。临时 ZUHUI_HOME、空闲端口起一份对比页服务（不碰 47330）。
跑：python -X utf8 -m unittest tests.test_paper_card_1121 -v（没有 playwright / 浏览器时浏览器那几条跳过）；
截图：设 BLOOM_SHOTS=<目录>。"""
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
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-card-")
from zuhui import config as zconfig  # noqa: E402

HOME = Path(zconfig.STATE)
if os.path.normcase(str(HOME.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("不在真实状态目录里跑")

from zuhui import view  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except Exception:                                    # noqa: BLE001
    sync_playwright = None
BROWSER = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                            r"C:\Program Files\Google\Chrome\Application\chrome.exe") if Path(p).is_file()), None)

PAPER = ("# 城市短时降雨预测\n\n## 1 引言\n\n短时降雨预测关系到城市排水调度，现有方法在强对流天气下误差偏大。\n\n"
         "## 2 方法\n\n我们用雷达回波序列训练一个轻量的时空模型，输入过去一小时的十二帧回波。\n\n"
         "预测误差的区间由分块自助法给出，区间 1.20 至 3.45 毫米。\n\n"
         "## 3 实验\n\n在三个城市的汛期数据上测试，平均绝对误差下降了百分之十八。\n\n"
         "表 2 列出了各城市在不同提前量下的误差，提前量越长误差越大。\n")

AI_NAME = "Aster"                                   # 设置里起的名字（编的）
ME = "林老师"
# 原话：口语、重复；后面附着快嘴当场复述的改法（复述里自己带括号）
SAID = "那这一段你就，嗯，你改吧你改吧，就是那个区间的写法，有没有打断你，你改吧。"
BACK = ("（当场复述的改法：好，那我改。2 节第（2）段那个“区间 1.20 至 3.45”，改成标准的 95% 置信区间写法，"
        "标明是按分块自助法得到的，记下了，交给后台排队改。）")
LONG_RESULT = ("我改了 2 节第（2）段（“区间 1.20 至 3.45”其实在第（2）段，不在第（3）段），把误差区间那句改成了"
               "“按分块自助法得到的 95% 置信区间为 [1.20, 3.45] 毫米”，这个说法和 3 节“显著性检验”里写的区间算法一致。"
               "同一段里提前量那句“0.5 至 1.5 小时”我没动，全文其他区间也还是“X 至 Y”的写法，要不要统一改成这种格式，需要你来定。") * 3


def _task(i, title, status, remark, result=None, section=None, focus=None):
    t = {"id": f"T{i}", "idx": i, "title": title, "remark": remark, "status": status, "created": "2026-11-21T15:00:00",
         "section": section, "focus": focus or {}}
    if result:
        t["result"] = result
    return t


def _free_port() -> int:
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


class SaidBack(unittest.TestCase):
    """原话后面附的复述：配括号去掉，别的附注留着。"""

    def test_nested_parens(self):
        self.assertEqual(view._drop_said_back(SAID + BACK), SAID)

    def test_old_form_and_supplement(self):
        s = "把表 2 的标题改一下（Claude 当场复述的改法：改成“各城市误差（毫米）”。）（又补充了：单位也写上）（当场复述的补充：好。）"
        self.assertEqual(view._drop_said_back(s), "把表 2 的标题改一下（又补充了：单位也写上）")

    def test_cut_short(self):                            # 复述被截断、括号配不上：去到末尾
        self.assertEqual(view._drop_said_back("改一下（当场复述的改法：2 节第（2…"), "改一下")

    def test_brief(self):
        b = view._task_brief(_task(1, "区间写法", "done", SAID + BACK), ME)
        self.assertEqual(b["said"], SAID)
        b = view._task_brief(_task(2, "区间写法", "done", "参会的老师周明说：" + SAID + BACK), ME)
        self.assertEqual((b["who"], b["said"]), ("周明", SAID))

    def test_inject_names_the_ai(self):
        view._AI_NAME[0] = AI_NAME
        html = view._inject(3, [], ME, {}, "zh", paper="x")
        self.assertIn('AI = {"name": "Aster"', html)
        self.assertNotIn("<b>AI</b>", html)


@unittest.skipUnless(sync_playwright and BROWSER, "没有 playwright / 浏览器")
class CardInBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from zuhui.board import Board
        cls.tmp = Path(tempfile.mkdtemp(prefix="card-", dir=str(HOME)))
        cls.paper = cls.tmp / "论文.md"
        cls.paper.write_bytes(PAPER.encode("utf-8"))
        at2 = {"section": "2 方法", "text": "预测误差的区间由分块自助法给出"}
        at3 = {"section": "3 实验", "text": "在三个城市的汛期数据上测试"}
        tasks = [
            _task(1, "2 节第（2）段误差区间改成标准的 95% 置信区间写法", "done", SAID + BACK, LONG_RESULT, "2 方法", at2),
            _task(2, "3 节第一段补上数据的起止年份", "done", "这个数据是哪几年的，写上吧，起止年份写上。",
                  "我在 3 节第一段补了“2019 至 2023 年汛期”。无。", "3 实验", at3),
            _task(3, "3 节第一段“百分之十八”改成阿拉伯数字", "done", "百分之十八写成 18%，全文都这样。",
                  "我把 3 节的“百分之十八”改成了“18%”，全文另外两处也一起改了。无。", "3 实验", at3),
            _task(4, "3 节第一段说明误差是怎么平均的", "working", "平均绝对误差是按城市平均还是按时次平均，说清楚。",
                  None, "3 实验", at3),
            # 会上改不了的标注（小旗）：卡片底下一排胶囊
            dict(_task(5, "表 2 补上 2024 年的数据", "done", "表 2 最好把今年的也跑一下，补上。", None, "3 实验",
                       {"section": "3 实验", "text": "表 2 列出了各城市在不同提前量下的误差"}),
                 note=True, plan="重新跑 2024 年汛期的数据，表 2 加一列。", tags=["rerun", "data"]),
        ]
        cls.board_file = cls.tmp / "board.json"
        cls.board_file.write_text(json.dumps({"tasks": tasks, "next_idx": 5}, ensure_ascii=False), encoding="utf-8")
        cls.port = _free_port()
        v = view.Viewer(cfg={"paper": {"file": str(cls.paper)}, "meeting": {"me": ME}, "assistant": {"name": AI_NAME}},
                        port=cls.port, board=Board(cls.board_file))
        v.mirror = cls.paper
        v.base = view.base_for(cls.paper)
        v.ensure_base(rebase=True)
        cls.v = v
        cls.srv = v.serve(open_browser=False)
        t0 = time.time()
        while time.time() - t0 < 60 and v.build < 1:
            time.sleep(0.1)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=BROWSER, headless=True)
        cls.url = f"http://127.0.0.1:{cls.port}/?embed=1&host=bloom&ui=zh"
        cls.shots = os.environ.get("BLOOM_SHOTS")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.pw.stop()
        finally:
            cls.v._stop.set()
            try:
                cls.srv.shutdown()
                cls.srv.server_close()
            except Exception:                        # noqa: BLE001
                pass
            time.sleep(0.5)
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def open(self, scheme="dark", height=800):
        p = self.browser.new_page(viewport={"width": 1280, "height": height}, color_scheme=scheme)
        p.goto(self.url, wait_until="load")
        p.wait_for_selector(".zh-pin")
        return p

    def card(self, p, n_items, has=""):
        """点开那一处的圆标（n_items 条、名字里带 has 的那一处），返回卡片"""
        pins = p.locator(".zh-pin")
        for i in range(pins.count()):
            label = pins.nth(i).get_attribute("aria-label")
            if len(label.split("，")) == n_items and has in label:
                pins.nth(i).scroll_into_view_if_needed()
                pins.nth(i).click()
                p.wait_for_selector(".zh-note")
                return p.locator(".zh-note")
        self.fail(f"没有 {n_items} 条的那一处")

    def shot(self, p, name):
        if self.shots:
            p.locator(".zh-note").screenshot(path=str(Path(self.shots) / name))

    def test_long_card_scrolls_inside(self):
        p = self.open()
        c = self.card(p, 1)
        g = p.evaluate("""() => { const c = document.querySelector('.zh-note'), b = c.querySelector('.zh-note-body'),
                                   h = c.querySelector('.zh-note-head');
                                   const r = c.getBoundingClientRect(), hr = h.getBoundingClientRect();
                                   b.scrollTop = b.scrollHeight;
                                   const r2 = c.getBoundingClientRect(), hr2 = h.getBoundingClientRect();
                                   return {h: r.height, body: [b.scrollHeight, b.clientHeight], head: [hr.top, hr2.top], card: [r.top, r2.top],
                                           over: getComputedStyle(b).overflowY, page: window.scrollY}; }""")
        self.assertLessEqual(g["h"], 401)                              # 最高 400px
        self.assertGreater(g["body"][0], g["body"][1])                 # 原话、结果比卡片长：在卡片里滚
        self.assertEqual(g["over"], "auto")
        self.assertEqual(g["head"][0], g["head"][1])                   # 滚的时候头一行不动
        self.assertEqual(g["card"][0], g["card"][1])
        text = c.inner_text()
        self.assertNotIn("当场复述", text)                              # 复述的改法不上卡片
        self.assertIn(SAID, text)
        whos = p.eval_on_selector_all(".zh-note .zh-who", "xs => xs.map(x => [x.textContent, x.title])")
        self.assertEqual(whos, [[ME, ME], [AI_NAME, AI_NAME]])        # 结果前面署 AI 的名字
        self.assertEqual(p.evaluate("() => document.querySelectorAll('.zh-note-no, .zh-item-no').length"), 0)
        # 头一行：标题 · 状态 · × · ✓，按钮一样大
        sizes = p.eval_on_selector_all(".zh-note-head button", "bs => bs.map(b => [b.className, Math.round(b.getBoundingClientRect().width)])")
        self.assertEqual(sizes, [["zh-note-x", 24], ["zh-note-ok", 24]])
        p.evaluate("() => { document.querySelector('.zh-note-body').scrollTop = 160; }")
        self.shot(p, "card-long-dark-scrolled.png")
        p.evaluate("() => { document.querySelector('.zh-note-body').scrollTop = 0; }")
        self.shot(p, "card-long-dark.png")
        # 窗口矮：卡片跟着矮，不出窗口
        p2 = self.open(height=420)
        self.card(p2, 1)
        h = p2.evaluate("() => document.querySelector('.zh-note').getBoundingClientRect().height")
        self.assertLessEqual(h, 420 - 80 + 1)
        p2.close()
        p.close()

    def test_group_rows_line_up(self):
        p = self.open()
        c = self.card(p, 3)
        g = p.evaluate("""() => {
          const c = document.querySelector('.zh-note'), R = (e) => e.getBoundingClientRect();
          const head = c.querySelector('.zh-note-head'), rows = [...c.querySelectorAll('.zh-item-head')];
          return {
            headSt: head.querySelectorAll('.zh-note-st').length,
            headBtns: [...head.querySelectorAll('button')].map(b => b.className),
            ok: R(head.querySelector('.zh-note-ok')),
            acts: rows.map(r => R(r.querySelector('.zh-acts'))),
            xs: rows.map(r => { const x = r.querySelector('.zh-note-x'); return x ? R(x) : null; }),
            st: rows.map(r => R(r.querySelector('.zh-note-st')).right),
            titles: rows.map(r => R(r.querySelector('.zh-item-title')).left),
            headTitle: R(head.querySelector('.zh-note-title')).left,
          };
        }""")
        self.assertEqual(g["headSt"], 0)                               # 分组头上只写那一节，不写状态
        self.assertEqual(g["headBtns"], ["zh-note-ok"])                # ✓ 只在头上
        self.assertEqual(len(g["acts"]), 3)
        for a in g["acts"]:                                             # 每条的按钮列和头上的一样宽、右边对齐
            self.assertAlmostEqual(a["right"], g["ok"]["right"], delta=0.5)
        xs = [x for x in g["xs"] if x]
        self.assertEqual(len(xs), 2)                                    # 改好的两条能撤回，修改中的那条没有 ×
        for x in xs:
            self.assertAlmostEqual(x["left"], xs[0]["left"], delta=0.5)
            self.assertLess(x["right"], g["ok"]["left"])               # × 在 ✓ 左边那一列
        for s in g["st"]:                                               # 状态字右边对齐
            self.assertAlmostEqual(s, g["st"][0], delta=0.5)
        for left in g["titles"]:                                        # 标题左边和头上的对齐（左边不再有勾）
            self.assertAlmostEqual(left, g["headTitle"], delta=0.5)
        self.assertIn("修改中", c.inner_text())
        self.shot(p, "card-group-dark.png")
        p.close()

    def test_note_card(self):
        p = self.open()
        c = self.card(p, 1, "2024")
        self.assertEqual(p.evaluate("() => document.querySelectorAll('.zh-note .zh-note-st').length"), 0)   # 标注不写状态，状态在底下胶囊里
        self.assertEqual(c.locator(".zh-tag").count(), 3)              # 会后改 + 需重跑实验 + 需补数据
        self.assertIn("重新跑 2024 年", c.inner_text())
        self.shot(p, "card-note-dark.png")
        p.close()

    def test_light_theme_names_readable(self):
        p = self.open("light")
        self.card(p, 1)
        cols = p.eval_on_selector_all(".zh-note .zh-who", "xs => xs.map(x => getComputedStyle(x).color)")
        for c in cols:                                                  # 和黑色对半混过：每个通道都不超过一半
            nums = [float(x) for x in c[c.index("(") + 1:c.index(")")].replace("srgb", "").replace(",", " ").split()[:3]]
            if max(nums) > 1:
                nums = [x / 255 for x in nums]
            self.assertLessEqual(max(nums), 0.51, c)
        self.shot(p, "card-long-light.png")
        p.close()


if __name__ == "__main__":
    unittest.main()
