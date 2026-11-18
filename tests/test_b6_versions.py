# -*- coding: utf-8 -*-
"""第 6 批：论文跨会议的版本历史（zuhui/paper_versions.py、zuhui/paper_api.py）。
沙盒 ZUHUI_HOME：开会前 / 散会后各存一份、同内容不重复存（两篇论文同内容也只一份 blob）、gzip 存、超过上限删最旧的
并清掉没人用的 blob、任选两版对比（复用修订对比生成器）、恢复到某一版（先存恢复前的，字节原样换回去）、开会时不能恢复。
跑：python -X utf8 -m unittest tests.test_b6_versions -v"""
import gzip
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ZUHUI_HOME"] = os.environ.get("ZUHUI_HOME") or tempfile.mkdtemp(prefix="zuhui-test-home-")

from zuhui import paper_api, paper_versions as pv  # noqa: E402
from zuhui.meetings import Meetings, paper_entry, paper_id  # noqa: E402


class VersionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ver-case-"))
        shutil.rmtree(pv.ROOT, ignore_errors=True)
        self.paper = self.tmp / "论文.md"
        self.paper.write_bytes("# 论文\n\n第一段原文。\n\n第二段原文。\n".encode("utf-8"))
        self.pid = paper_id(self.paper)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_before_after_dedupe(self):
        m = {"id": "m1", "title": "第一次组会 · 11月10日 09:00", "papers": [paper_entry(self.paper, "论文")]}
        a = pv.snapshot_meeting(m, "before")
        b = pv.snapshot_meeting(m, "after")           # 会上没改：不再记一条
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["v"], b[0]["v"])
        self.paper.write_text("# 论文\n\n第一段改过。\n\n第二段原文。\n", encoding="utf-8")
        c = pv.snapshot_meeting(m, "after")
        info = pv.info(self.pid)
        self.assertEqual([x["kind"] for x in info["items"]], ["after", "before"])   # 新的在前
        self.assertEqual(info["items"][1]["title"], "第一次组会")                   # 去掉名字后面的日期
        self.assertTrue(info["items"][0]["same"])
        self.assertFalse(info["items"][1]["same"])
        self.assertEqual(info["paper"]["name"], "论文")
        # 下一场会开会前没动过：不再记
        m2 = {"id": "m2", "title": "第二次", "papers": m["papers"]}
        pv.snapshot_meeting(m2, "before")
        self.assertEqual(len(pv.info(self.pid)["items"]), 2)
        # blob 是 gzip、按内容存：另一篇同样的内容不多一份
        other = self.tmp / "另一篇.md"
        other.write_bytes(self.paper.read_bytes())
        pv.snapshot(other, "另一篇", "before", "m2")
        blobs = list(pv.BLOBS.glob("*/*.gz"))
        self.assertEqual(len(blobs), 2)
        self.assertTrue(gzip.decompress(blobs[0].read_bytes()))
        self.assertEqual(pv.read(self.pid, c[0]["v"]), self.paper.read_bytes().decode("utf-8"))

    def test_restore_backs_up_first(self):
        raw0 = self.paper.read_bytes()
        v1 = pv.snapshot(self.paper, "论文", "before", "m1")["v"]
        self.paper.write_bytes("﻿# 论文\r\n\r\n全改了。\r\n".encode("utf-8"))
        pv.snapshot(self.paper, "论文", "after", "m1")
        it = pv.restore(self.pid, v1)
        self.assertEqual(self.paper.read_bytes(), raw0)                      # 字节原样换回去
        kinds = [x["kind"] for x in pv.info(self.pid)["items"]]
        self.assertEqual(kinds, ["restore", "after", "before"])               # 恢复前那份和散会后的一样：不重复记
        self.assertEqual(it["kind"], "restore")
        self.assertEqual(it["of"], v1)
        backup = next(x for x in pv.info(self.pid)["items"] if x["kind"] == "after")
        self.assertIn("全改了", pv.read(self.pid, backup["v"]))
        # 恢复前的稿子没存过：先存一份 backup
        self.paper.write_text("# 论文\n\n又改了。\n", encoding="utf-8")
        pv.restore(self.pid, v1)
        kinds = [x["kind"] for x in pv.info(self.pid)["items"]]
        self.assertEqual(kinds[:2], ["restore", "backup"])
        self.assertIn("又改了", pv.read(self.pid, pv.info(self.pid)["items"][1]["v"]))
        with self.assertRaises(ValueError):
            pv.restore(self.pid, "999")

    def test_prune_and_gc(self):
        old = pv.MAX_ITEMS
        pv.MAX_ITEMS = 3
        try:
            for i in range(6):
                self.paper.write_text(f"# 论文\n\n第 {i} 版。\n", encoding="utf-8")
                pv.snapshot(self.paper, "论文", "after", f"m{i}")
            items = pv.info(self.pid)["items"]
            self.assertEqual(len(items), 3)
            self.assertEqual(len(list(pv.BLOBS.glob("*/*.gz"))), 3)          # 删掉的那几版的内容也清掉了
            self.assertIn("第 5 版", pv.read(self.pid, items[0]["v"]))
        finally:
            pv.MAX_ITEMS = old

    @unittest.skipUnless(__import__("zuhui.view", fromlist=["HTMLDIFF"]).HTMLDIFF.exists(), "没有修订对比生成器")
    def test_diff_page(self):
        v1 = pv.snapshot(self.paper, "论文", "before", "m1")["v"]
        self.paper.write_text("# 论文\n\n第一段改过了。\n\n第二段原文。\n", encoding="utf-8")
        page, why = pv.diff_page(self.pid, "cur", v1)                       # 谁早谁在左边
        self.assertIsNotNone(page, why)
        self.assertIn("改过了", page)
        self.assertIn("<ins", page)
        self.assertEqual(pv.diff_page(self.pid, "77", v1), (None, "not_found"))

    def test_api_restore_refused_while_live(self):
        v1 = pv.snapshot(self.paper, "论文", "before", "m1")["v"]
        lib = Meetings(root=self.tmp / "meetings")
        board = SimpleNamespace(counts=lambda: {})
        calls = []
        viewer = SimpleNamespace(meetings=lib, board=board, mirror=self.paper, ui_lang="zh",
                                 ensure_base=lambda rebase=False: calls.append("rebase"), rebase_paper=lambda p: None,
                                 paper_info=lambda: {"name": "论文"}, on_edit=lambda: calls.append("edit"), _sig="x")
        lib.start(papers=[paper_entry(self.paper, "论文")])
        out, code = paper_api.post(viewer, "/api/paper/versions/restore", {"paper": self.pid, "v": v1})
        self.assertEqual((code, out["error"]), (409, "live"))
        lib.end()
        self.paper.write_text("# 论文\n\n改了。\n", encoding="utf-8")
        out, code = paper_api.post(viewer, "/api/paper/versions/restore", {"paper": self.pid, "v": v1})
        self.assertEqual(code, 200, out)
        self.assertEqual(calls, ["rebase", "edit"])
        self.assertIsNone(viewer._sig)
        code, data, ctype = paper_api.get(viewer, f"/api/paper/versions?paper={self.pid}")
        self.assertEqual(code, 200)
        self.assertEqual(data["items"][0]["kind"], "restore")
        code, data, ctype = paper_api.get(viewer, "/api/paper/versions")       # 不给编号 = 屏幕上那篇
        self.assertEqual(data["paper"]["id"], self.pid)
        out, code = paper_api.post(viewer, "/api/paper/versions/restore", {"paper": self.pid, "v": v1}, by="小王", guest=True)
        self.assertEqual(code, 403)                                           # 访客不能恢复

    def test_prefs_api(self):
        viewer = SimpleNamespace(meetings=None)
        self.assertEqual(paper_api.post(viewer, "/api/paper/prefs", {"docx_mode": "direct"})[0]["docx_mode"], "direct")
        self.assertEqual(paper_api.get(viewer, "/api/paper/prefs")[1]["docx_mode"], "direct")
        self.assertEqual(paper_api.post(viewer, "/api/paper/prefs", {"docx_mode": "x"})[1], 400)
        paper_api.post(viewer, "/api/paper/prefs", {"docx_mode": "track"})


if __name__ == "__main__":
    unittest.main()
