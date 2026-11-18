# -*- coding: utf-8 -*-
"""第 7 批：AI 念词的读法（voice_prefs.pron / say_as，mouth.speakable）——写法 → 读法，一行一条，从 txt / csv 导入的
"写法,读法" 也认；最多 200 条、每边最多 40 字；英文按整词换，大小写混写的不分大小写；长的先换、换过的不再换；
所有声音引擎都过 speakable；逐字稿上还是原文。

全离线：ZUHUI_HOME 指到临时目录，不出声、不调模型。

    python -X utf8 -m unittest tests.test_b7_pron -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-b7-pron-")     # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import mouth  # noqa: E402
from zuhui import voice_prefs as vp  # noqa: E402


class PronTest(unittest.TestCase):
    def tearDown(self):
        vp.save({"pron": []})
        mouth.set_lang("zh")

    def test_parse(self):
        got = vp._clean_pron("BERT = 伯特\nLaTeX → 雷泰克\n\nIT,爱踢\nGPT-4o -> 鸡皮提四欧\n张老师\t张老湿\nBERT = 波特")
        self.assertEqual(got, [{"w": "BERT", "r": "波特"}, {"w": "LaTeX", "r": "雷泰克"}, {"w": "IT", "r": "爱踢"},
                               {"w": "GPT-4o", "r": "鸡皮提四欧"}, {"w": "张老师", "r": "张老湿"}])   # 同一个写法：后写的算
        self.assertEqual(vp._clean_pron([["a", "b"], {"w": " c ", "r": " d "}]), [{"w": "a", "r": "b"}, {"w": "c", "r": "d"}])
        for bad, code in [("只有写法", "pron_bad"), ("= 只有读法", "pron_bad"), ([{"w": "x"}], "pron_bad"), (3, "pron_bad"),
                          ("a" * 41 + " = b", "pron_too_long"), ("a = " + "b" * 41, "pron_too_long"),
                          ("\n".join(f"w{k} = r" for k in range(vp.PRON_MAX + 1)), "pron_too_many")]:
            with self.assertRaisesRegex(ValueError, code):
                vp._clean_pron(bad)
        self.assertEqual(len(vp._clean_pron("\n".join(f"w{k} = r" for k in range(vp.PRON_MAX)))), vp.PRON_MAX)

    def test_say_as(self):
        self.assertEqual(vp.say_as("没填读法原样念 BERT"), "没填读法原样念 BERT")
        vp.save({"pron": "BERT = 伯特\nLaTeX = 雷泰克\nIT = 爱踢\nGPT-4 = 鸡皮提四\nGPT-4o = 鸡皮提四欧\n张老师 = 张老湿\n伯特 = 不该再换"})
        self.assertEqual(vp.say_as("BERT 和 BERTopic"), "伯特 和 BERTopic")            # 整词
        self.assertEqual(vp.say_as("latex、LATEX、LaTeX"), "雷泰克、雷泰克、雷泰克")      # 大小写混写的不分大小写
        self.assertEqual(vp.say_as("IT、it、ITEM"), "爱踢、it、ITEM")                    # 全大写缩写照原样找
        self.assertEqual(vp.say_as("GPT-4o 比 GPT-4 好"), "鸡皮提四欧 比 鸡皮提四 好")   # 长的先换
        self.assertEqual(vp.say_as("张老师说"), "张老湿说")
        self.assertEqual(vp.say_as("BERT"), "伯特")                                     # 换出来的字不再换第二遍
        self.assertEqual(vp.public({})["pron"][0], {"w": "BERT", "r": "伯特"})

    def test_all_engines_go_through_speakable(self):
        vp.save({"pron": "BERT = 伯特"})
        self.assertEqual(mouth.speakable("第2段用了 BERT"), "第二段用了 伯特")
        mouth.set_lang("en")
        self.assertEqual(mouth.speakable("We used BERT"), "We used 伯特")               # 英文组会也换
        self.assertEqual(mouth.speakable_list(["BERT 很好。"]), ["伯特 很好。"])

        class M(mouth.Mouth):
            def __init__(self):                                                       # 不起嗓子服务、不放声音
                import queue
                self._synth_q = queue.Queue()
                self._gen, self._pending = 0, 0
                import threading
                self._lock = threading.Lock()

            def _pref(self):
                return "auto"

        m = M()
        said = []
        m.say("BERT 很好。", on_start=said.append)
        _, text, cb = m._synth_q.get_nowait()
        self.assertEqual(text, "伯特 很好。")                                           # 送去合成的是读法
        cb(text)
        self.assertEqual(said, ["BERT 很好。"])                                          # 逐字稿记的还是原文


if __name__ == "__main__":
    unittest.main()
