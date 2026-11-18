# -*- coding: utf-8 -*-
"""第 8 批：设置 → 存储 → 自动删除逐字稿和纪要（zuhui/storage.py 的 records_* / trash_records / set_records_auto）。

到期的会：逐字稿（各版本、删改清单、重转写记录、录屏文件夹里那份转写）、纪要、书签、遗留勾选送进回收站；
会议名（meta.json）、谁来了、议程、修改清单、修订对比、录屏、截图都留着。正在开的、还在收尾的会不动。

沙盒和假回收站跟 tests/test_storage.py 一样（借它的 setUp / tearDown / session），不碰真实的 ~/.zuhui、Windows 回收站。

    python -X utf8 -m unittest tests.test_b8_retention -v
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests import test_storage as ts  # noqa: E402  （先设好 ZUHUI_HOME 再 import zuhui）
from zuhui import storage  # noqa: E402

KEEP = ("meta.json", "participants.json", "agenda.json", "board.json", "diff.html")


def _iso(days_ago: float) -> str:
    return (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")


class RetentionTest(unittest.TestCase):
    setUp = ts.StorageTest.setUp
    tearDown = ts.StorageTest.tearDown
    session = ts.StorageTest.session

    def meeting(self, mid: str, days_ago: float, session: Path | None = None, state: str = "ended", **meta) -> Path:
        d = self.mroot / mid
        m = {"id": mid, "title": f"组会 {mid} · 11月01日 10:00", "state": state, "started": _iso(days_ago + 0.05),
             "ended": _iso(days_ago) if state != "live" else None, "session": str(session) if session else None, **meta}
        ts._write(d / "meta.json", text=json.dumps(m, ensure_ascii=False))
        for n in storage.RECORD_FILES:
            ts._write(d / n, text='{"x": 1}\n')
        for n in KEEP[1:]:
            ts._write(d / n, text='{"tasks": []}')
        return d

    def test_preview_lists_only_old_finished_meetings(self):
        self.meeting("20260101-100000", 200)
        self.meeting("20260601-100000", 100)
        self.meeting("20261101-100000", 5)
        self.meeting("20260102-100000", 300, state="live")                 # 还在开（崩了没收尾）：不动
        self.meeting("20260103-100000", 300)
        self.viewer.processing_ids.add("20260103-100000")                    # 还在收尾：不动
        s = storage.scan(self.viewer)
        r = s["records"]["retention"]
        self.assertEqual(r["days"], 0)
        self.assertEqual(r["preview"]["30"]["count"], 2)
        self.assertEqual([m["id"] for m in r["preview"]["30"]["meetings"]], ["20260601-100000", "20260101-100000"])
        self.assertEqual(r["preview"]["180"]["count"], 1)
        self.assertEqual(r["preview"]["365"]["count"], 0)
        self.assertEqual(r["preview"]["30"]["meetings"][0]["title"], "组会 20260601-100000")    # 去掉标题后面的日期
        self.assertGreater(r["preview"]["30"]["bytes"], 0)

    def test_turning_on_deletes_records_and_keeps_the_rest(self):
        sd = self.session("20260101-100000", 200)
        old = self.meeting("20260101-100000", 200, session=sd)
        new = self.meeting("20261101-100000", 5)
        out = storage.set_records_auto(self.viewer, 90)
        self.assertEqual(out["days"], 90)
        self.assertEqual(out["count"], 1)
        self.assertGreater(out["bytes"], 0)
        for n in storage.RECORD_FILES:
            self.assertFalse((old / n).exists(), n)
            self.assertTrue((new / n).exists(), n)
        for n in KEEP:
            self.assertTrue((old / n).exists(), n)
        # 录屏文件夹：录屏软件那份转写删了，录屏、截图、meta 留着
        self.assertFalse((sd / "transcript.json").exists())
        self.assertFalse((sd / "live" / "transcript.jsonl").exists())
        for n in ("recording.mp4", "audio.wav", "meeting/shot-0001.png", "meta.json"):
            self.assertTrue((sd / n).exists(), n)
        m = json.loads((old / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(m["records_days"], 90)
        self.assertTrue(m["records_removed"])
        self.assertEqual(m["title"], "组会 20260101-100000 · 11月01日 10:00")
        self.assertEqual(json.loads(storage.SETTINGS.read_text(encoding="utf-8"))["records_days"], 90)
        self.assertEqual(storage.records_days(), 90)
        # 再扫：那场不再列着；再清一次什么也不动
        self.assertEqual(storage.scan(self.viewer)["records"]["retention"]["preview"]["30"]["count"], 0)
        self.assertEqual(storage.apply_records_auto(self.viewer), {"count": 0, "bytes": 0})

    def test_cleared_meeting_still_opens(self):
        from zuhui.meetings import Meetings
        self.meeting("20260101-100000", 200)
        storage.set_records_auto(self.viewer, 30)
        mt = Meetings(self.mroot)
        g = mt.get("20260101-100000")
        self.assertIsNotNone(g)
        self.assertEqual(g["transcript"], [])
        self.assertIsNone(g["summary"])
        self.assertTrue(g["meeting"]["records_removed"])
        self.assertTrue([x for x in mt.list() if x["id"] == "20260101-100000"])

    def test_shared_recording_waits_for_every_meeting(self):
        sd = self.session("20260101-100000", 200)
        self.meeting("20260101-100000", 200, session=sd)
        self.meeting("20260101-100500", 10, session=sd)        # 同一段录屏上还有一场没到期的会
        storage.set_records_auto(self.viewer, 90)
        self.assertTrue((sd / "transcript.json").exists())
        self.assertFalse((self.mroot / "20260101-100000" / "transcript.jsonl").exists())

    def test_busy_recording_session_is_left_alone(self):
        sd = self.session("20260101-100000", 200)
        self.meeting("20260101-100000", 200, session=sd)
        self.viewer.busy = str(sd)                              # 这段录屏正在用
        storage.set_records_auto(self.viewer, 90)
        self.assertTrue((sd / "transcript.json").exists())
        self.assertFalse((self.mroot / "20260101-100000" / "summary.json").exists())

    def test_off_and_validation(self):
        d = self.meeting("20260101-100000", 400)
        self.assertEqual(storage.set_records_auto(self.viewer, 0), {"days": 0, "count": 0, "bytes": 0})
        self.assertTrue((d / "transcript.jsonl").exists())
        for bad in (7, 60, -1, "x"):
            body, code = storage.post(self.viewer, "/api/storage/retention", {"days": bad})
            self.assertEqual(code, 400, bad)
        self.assertTrue((d / "transcript.jsonl").exists())
        body, code = storage.post(self.viewer, "/api/storage/retention", {"days": 365})
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 1)
        self.assertFalse((d / "transcript.jsonl").exists())

    def test_auto_loop_applies_both(self):
        self.meeting("20260101-100000", 200)
        storage.set_records_auto(self.viewer, 0)
        s = json.loads(storage.SETTINGS.read_text(encoding="utf-8"))
        s["records_days"] = 180
        storage.SETTINGS.write_text(json.dumps(s), encoding="utf-8")
        self.assertEqual(storage.apply_records_auto(self.viewer)["count"], 1)

    def test_settings_keep_each_other(self):
        storage.set_auto(self.viewer, 90)
        storage.set_records_auto(self.viewer, 365)
        storage.set_auto(self.viewer, 30)
        s = json.loads(storage.SETTINGS.read_text(encoding="utf-8"))
        self.assertEqual((s["auto_days"], s["records_days"]), (30, 365))


if __name__ == "__main__":
    unittest.main()
