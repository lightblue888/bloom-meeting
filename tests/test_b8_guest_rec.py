# -*- coding: utf-8 -*-
"""第 8 批：访客页的录制告知。主持人这边录屏软件的 record 任务在跑 = 在录（share.rec_mode → 推给中继的状态里的 rec），
停了 = 没在录；每秒问一次，结果留几秒。不碰真实的录屏任务（换成假的）。

    python -X utf8 -m unittest tests.test_b8_guest_rec -v
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
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-guestrec-")
from zuhui import ears, share  # noqa: E402


class RecModeTest(unittest.TestCase):
    def setUp(self):
        self._saved = ears.running_record_job
        self.job = None
        self.calls = 0

        def fake():
            self.calls += 1
            return self.job

        ears.running_record_job = fake
        share._rec_cache = (0.0, None)

    def tearDown(self):
        ears.running_record_job = self._saved
        share._rec_cache = (0.0, None)

    def test_screen_audio_none(self):
        self.assertIsNone(share.rec_mode(now=1000.0))
        self.job = {"type": "record", "state": "running", "session": "x"}
        self.assertEqual(share.rec_mode(now=1010.0), "screen")
        self.job = {"type": "record", "state": "running", "session": "x", "mode": "audio"}
        self.assertEqual(share.rec_mode(now=1020.0), "audio")
        self.job = None
        self.assertIsNone(share.rec_mode(now=1030.0))

    def test_cached_for_a_few_seconds(self):
        self.job = {"type": "record", "state": "running", "session": "x"}
        self.assertEqual(share.rec_mode(now=2000.0), "screen")
        self.job = None
        self.assertEqual(share.rec_mode(now=2001.0), "screen")          # 还在缓存里
        self.assertEqual(self.calls, 1)
        self.assertIsNone(share.rec_mode(now=2000.0 + share.REC_CACHE_S + 0.1))
        self.assertEqual(self.calls, 2)

    def test_broken_job_reader_means_not_recording(self):
        def boom():
            raise OSError("jobs")
        ears.running_record_job = boom
        self.assertIsNone(share.rec_mode(now=3000.0))


if __name__ == "__main__":
    unittest.main()
