# -*- coding: utf-8 -*-
"""设置 → 存储（zuhui/storage.py）：占用怎么算、录屏怎么清（逐字稿、截图、纪要留着）、自动删除、不用的模型、临时文件，
以及 /api/storage 那几个接口。

全在临时目录里：ZUHUI_HOME（导入 zuhui 之前设；和别的测试模块一起跑时用已经在用的那个，再把 storage.STATE 换成每个
测试自己的）、BLURT_HOME、BLURT_WORKSPACE 都指到临时目录；"回收站"换成挪进临时目录的假的，不碰 Windows 回收站，
也不碰真实的 ~/.zuhui、~/.blurt、~/Blurt。HTTP 那部分在子进程里起一个独立的对比页服务（随便一个空端口，不碰 47330），
只发不会删东西的请求。

    python -X utf8 -m unittest tests.test_storage -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-storage-")      # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import storage  # noqa: E402

DAY = 86400


def _write(p: Path, n: int = 0, text: str | None = None, age_days: float = 0.0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        p.write_text(text, encoding="utf-8")
    else:
        p.write_bytes(b"\0" * n)
    if age_days:
        t = time.time() - age_days * DAY
        os.utime(p, (t, t))
    return p


class FakeMeetings:
    def __init__(self, root: Path):
        self.root = root
        self.cur = None

    def current(self):
        return dict(self.cur) if self.cur else None


class FakeViewer:
    def __init__(self, root: Path, cfg: dict | None = None):
        self.meetings = FakeMeetings(root)
        self.cfg = cfg or {}
        self.busy = None
        self.processing_ids: set[str] = set()

    def busy_session(self):
        return self.busy

    def processing(self, mid: str) -> bool:
        return mid in self.processing_ids


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zuhui-storage-case-"))
        self.state = self.tmp / "zuhui"
        self.blurt = self.tmp / "blurt"
        self.ws = self.tmp / "ws"
        self.trash = self.tmp / "recycle"
        for d in (self.state, self.blurt, self.ws, self.trash):
            d.mkdir(parents=True)
        self._env = {k: os.environ.get(k) for k in ("BLURT_HOME", "BLURT_WORKSPACE", "BLURT_SCRIPTS")}
        os.environ["BLURT_HOME"] = str(self.blurt)
        os.environ["BLURT_WORKSPACE"] = str(self.ws)
        os.environ.pop("BLURT_SCRIPTS", None)
        self._saved = (storage.STATE, storage.SETTINGS, storage._recycle)
        storage.STATE = self.state
        storage.SETTINGS = self.state / "storage.json"
        self.recycled: list[Path] = []

        def fake_recycle(paths):          # 假回收站：挪进临时目录，记下来
            gone = []
            for p in paths:
                p = Path(p)
                if not p.exists():
                    continue
                dst = self.trash / f"{len(self.recycled)}-{p.name}"
                shutil.move(str(p), str(dst))
                self.recycled.append(p)
                gone.append(p)
            return gone

        storage._recycle = fake_recycle
        self.sessions = self.ws / "inbox" / "sessions"
        self.mroot = self.state / "meetings"
        self.mroot.mkdir(parents=True)
        self.viewer = FakeViewer(self.mroot)

    def tearDown(self):
        storage.STATE, storage.SETTINGS, storage._recycle = self._saved
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 造数据 ----------
    def session(self, name: str, age_days: float, video: int = 1000, audio: int = 500, pcm: int = 200) -> Path:
        sd = self.sessions / name
        _write(sd / "meta.json", text=json.dumps({"started_at": "2026-01-01T10:00:00+08:00", "video": "recording.mp4"}))
        _write(sd / "transcript.json", text='{"segments": []}')
        _write(sd / "live" / "transcript.jsonl", text='{"start": 1, "end": 2, "text": "你好"}\n')
        _write(sd / "meeting" / "shot-0001.png", 300)
        if video:
            _write(sd / "recording.mp4", video, age_days=age_days)
        if audio:
            _write(sd / "audio.wav", audio, age_days=age_days)
        if pcm:
            _write(sd / "live" / "audio.pcm", pcm, age_days=age_days)
        return sd

    def meeting(self, mid: str, session: Path | None, state: str = "ended") -> Path:
        d = self.mroot / mid
        _write(d / "meta.json", text=json.dumps({"id": mid, "title": "组会", "state": state,
                                                 "session": str(session) if session else None}, ensure_ascii=False))
        _write(d / "transcript.jsonl", text='{"t": "00:01", "sec": 1, "who": "teacher", "text": "你好"}\n')
        _write(d / "summary.json", text='{"title": "纪要"}')
        return d

    def media_left(self, sd: Path) -> list[str]:
        return sorted(f.name for f in storage.media_files(sd))

    # ---------- 录屏 ----------
    def test_scan_counts_and_split(self):
        old = self.session("20260101-100000", 100)
        new = self.session("20260801-100000", 10)
        orphan = self.session("20260102-100000", 200, video=0, audio=700, pcm=0)
        self.meeting("m-old", old)
        self.meeting("m-new", new)
        self.meeting("m-none", None)                            # 没录屏的会
        _write(self.state / "papers" / "p1" / "paper.md", 50)
        _write(self.state / "cache" / "a.wav", 40)
        _write(self.state / "backend.log", 30)
        s = storage.scan(self.viewer)
        r = s["recordings"]
        self.assertEqual(r["meetings"], 2)
        self.assertEqual(r["bytes"], 2 * 1700)
        self.assertEqual(r["older"]["30"], {"count": 1, "bytes": 1700})
        self.assertEqual(r["older"]["90"], {"count": 1, "bytes": 1700})
        self.assertEqual(r["older"]["180"], {"count": 0, "bytes": 0})
        self.assertEqual(r["older"]["all"], {"count": 2, "bytes": 3400})
        self.assertEqual(r["others"]["count"], 1)
        self.assertEqual(r["others"]["bytes"], 700)
        self.assertEqual(r["auto_preview"]["90"], {"count": 2, "bytes": 1700 + 700})   # 自动删除：没有会议的那段也算
        self.assertEqual(r["auto_days"], 0)
        self.assertEqual(os.path.normcase(r["folder"]), os.path.normcase(str(self.sessions)))
        self.assertEqual(s["records"]["meetings"]["count"], 3)
        self.assertEqual(s["records"]["papers"]["count"], 1)
        self.assertGreaterEqual(s["records"]["meetings"]["bytes"], 2 * 300)      # 会话里留下的截图算进会议记录
        self.assertEqual(s["caches"]["bytes"], 40)
        self.assertEqual(s["logs"]["bytes"], 30)
        self.assertEqual(s["split"]["recordings"], 3400 + 700)
        self.assertEqual(sum(s["split"].values()), s["total"])
        self.assertTrue(s["total"] >= 4100)

    def test_trash_keeps_transcripts_and_review(self):
        old = self.session("20260101-100000", 100)
        new = self.session("20260801-100000", 10)
        mdir = self.meeting("m-old", old)
        self.meeting("m-new", new)
        r = storage.trash_recordings(self.viewer, 90, "meetings")
        self.assertEqual(r, {"count": 1, "bytes": 1700})
        self.assertEqual(self.media_left(old), [])
        self.assertEqual(self.media_left(new), ["audio.pcm", "audio.wav", "recording.mp4"])
        for keep in ("transcript.json", "live/transcript.jsonl", "meeting/shot-0001.png", "meta.json"):
            self.assertTrue((old / keep).exists(), keep)
        for keep in ("meta.json", "transcript.jsonl", "summary.json"):
            self.assertTrue((mdir / keep).exists(), keep)
        self.assertIn("video_removed", json.loads((old / "meta.json").read_text(encoding="utf-8")))
        # 回顾：录屏没了就当没录屏（不报错）
        from zuhui.view import _session_of, _video_of
        self.assertIsNone(_video_of(_session_of({"session": str(old)})))
        self.assertIsNotNone(_video_of(_session_of({"session": str(new)})))
        # 再扫一遍：这场会不再算有录屏
        s = storage.scan(self.viewer)
        self.assertEqual(s["recordings"]["meetings"], 1)

    def test_others_only_touch_recordings_without_meeting(self):
        linked = self.session("20260101-100000", 100)
        orphan = self.session("20260102-100000", 100)
        self.meeting("m1", linked)
        r = storage.trash_recordings(self.viewer, None, "others")
        self.assertEqual(r["count"], 1)
        self.assertEqual(self.media_left(orphan), [])
        self.assertEqual(len(self.media_left(linked)), 3)
        self.assertTrue((orphan / "transcript.json").exists())

    def test_busy_recordings_are_never_touched(self):
        live = self.session("20260101-100000", 100)
        post = self.session("20260102-100000", 100)
        rec = self.session("20260103-100000", 100)
        job = self.session("20260104-100000", 100)
        fresh = self.session("20260105-100000", 0)            # 刚写过：当它还在录
        crashed = self.session("20260106-100000", 100)
        free = self.session("20260107-100000", 100)
        self.meeting("m-live", live)
        self.meeting("m-post", post)
        self.meeting("m-crash", crashed, state="live")          # 上次没散会就退了的那场
        self.meeting("m-free", free)
        self.viewer.meetings.cur = {"id": "m-live", "session": str(live)}
        self.viewer.processing_ids = {"m-post"}
        self.viewer.busy = str(rec)
        _write(self.blurt / "jobs" / "j-1.json", text=json.dumps({"type": "record", "state": "running", "session": str(job)}))
        r = storage.trash_recordings(self.viewer, None, "all")
        self.assertEqual(r["count"], 1)
        self.assertEqual(self.media_left(free), [])
        for sd in (live, post, rec, job, fresh, crashed):
            self.assertEqual(len(self.media_left(sd)), 3, sd.name)

    def test_outside_sandbox_is_never_touched(self):
        elsewhere = Path(tempfile.mkdtemp(prefix="zuhui-storage-elsewhere-"))
        try:
            sd = elsewhere / "sessions" / "20260101-100000"
            _write(sd / "meta.json", text="{}")
            _write(sd / "recording.mp4", 100, age_days=100)
            self.meeting("m1", sd)
            self.assertEqual(storage.scan(self.viewer)["recordings"]["meetings"], 1)     # 大小照算
            self.assertEqual(storage.trash_recordings(self.viewer, None, "all")["count"], 0)
            self.assertTrue((sd / "recording.mp4").exists())
        finally:
            shutil.rmtree(elsewhere, ignore_errors=True)

    # ---------- 自动删除 ----------
    def test_auto_delete(self):
        old = self.session("20260101-100000", 100)
        new = self.session("20260801-100000", 10)
        orphan = self.session("20260102-100000", 200)
        self.meeting("m-old", old)
        self.meeting("m-new", new)
        with self.assertRaises(ValueError):
            storage.set_auto(self.viewer, 7)
        self.assertEqual(storage.set_auto(self.viewer, 0), {"auto_days": 0, "count": 0, "bytes": 0})
        self.assertEqual(len(self.media_left(old)), 3)
        r = storage.set_auto(self.viewer, 90)
        self.assertEqual((r["auto_days"], r["count"]), (90, 2))
        self.assertEqual(self.media_left(old), [])
        self.assertEqual(self.media_left(orphan), [])
        self.assertEqual(len(self.media_left(new)), 3)
        self.assertEqual(json.loads(storage.SETTINGS.read_text(encoding="utf-8"))["auto_days"], 90)
        self.assertEqual(storage.auto_days(), 90)
        self.assertEqual(storage.scan(self.viewer)["recordings"]["auto_days"], 90)
        # 每天那一次：新的一段过了期限，后台线程清掉
        older = self.session("20260103-100000", 120)
        self.meeting("m-older", older)
        storage._auto_started = False
        storage.start_auto_clean(self.viewer, every=3600, delay=0)
        for _ in range(100):
            if not self.media_left(older):
                break
            time.sleep(0.05)
        self.assertEqual(self.media_left(older), [])
        self.assertEqual(len(self.media_left(new)), 3)

    # ---------- 模型 ----------
    def models_fixture(self):
        bm = self.blurt / "models"
        _write(bm / "fun-asr-nano-int8" / "llm.int8.onnx", 3_000_000)
        _write(bm / "qwen3-asr-0.6b-int8" / "encoder.int8.onnx", 2_500_000)
        _write(bm / storage.VOICE_FILE, 2_100_000)
        _write(bm / storage.TURN_FILE, 2_050_000)
        _write(bm / storage.VAD_FILE, 600_000)
        _write(self.state / "models" / "addr" / "model.bin", 4_000_000)
        _write(self.state / "models" / "addr_en" / "model.bin", 4_000_000)
        _write(self.state / "models" / "addr_old" / "model.bin", 3_000_000)
        _write(self.state / "asr_train" / "models" / "tuned" / "llm.int8.onnx", 3_000_000)
        _write(self.state / "asr_train" / "models" / "candidate" / "llm.int8.onnx", 2_200_000)
        _write(self.state / "asr_train" / "active.json",
               text=json.dumps({"engine": "fun-asr-nano", "model_dir": str(self.state / "asr_train" / "models" / "tuned")}))
        self.viewer.cfg = {"addr": {"enabled": True, "path": str(self.state / "models" / "addr"),
                                    "path_en": str(self.state / "models" / "addr_en")}}

    def test_models_used_and_unused(self):
        self.models_fixture()
        m = storage.models(self.viewer)
        roles = [g["role"] for g in m["used"]]
        self.assertEqual(roles, ["live", "after", "addr", "voice", "turn"])
        addr = next(g for g in m["used"] if g["role"] == "addr")
        self.assertEqual((addr["bytes"], addr["langs"]), (8_000_000, ["zh", "en"]))
        self.assertEqual(sorted(u["name"] for u in m["unused"]), ["Qwen3-ASR 0.6B", "addr_old", "candidate"])
        self.assertNotIn("Silero VAD", [g["name"] for g in m["used"]])                 # 小的内部文件不单列
        self.assertEqual(m["bytes"], 3_000_000 + 2_500_000 + 2_100_000 + 2_050_000 + 600_000 + 11_000_000 + 5_200_000)
        # 接话判断没开：那两个也算不用的
        self.viewer.cfg = {"addr": {"enabled": False}}
        names = sorted(u["name"] for u in storage.models(self.viewer)["unused"])
        self.assertIn("addr", names)

    def test_trash_models_only_unused(self):
        self.models_fixture()
        live = str(self.blurt / "models" / "fun-asr-nano-int8")
        old = str(self.state / "models" / "addr_old")
        with self.assertRaises(ValueError):
            storage.trash_models(self.viewer, [old, live])            # 有一个在用：整批不动
        self.assertTrue(Path(old).exists())
        with self.assertRaises(ValueError):
            storage.trash_models(self.viewer, [str(self.tmp / "nope")])
        r = storage.trash_models(self.viewer, [old])
        self.assertEqual(r, {"count": 1, "bytes": 3_000_000})
        self.assertFalse(Path(old).exists())
        self.assertTrue(Path(live).exists())

    # ---------- 缓存 ----------
    def test_clear_caches(self):
        boot = storage._proc_start()
        self.assertIsNotNone(boot)
        tmp = self.state / "tmp"
        dead = _write(tmp / "mouth-dead" / "a.wav", 100)
        t = boot - 3600
        os.utime(dead.parent, (t, t))
        alive = _write(tmp / "mouth-alive" / "a.wav", 100)              # 这个进程起来以后建的：正在用
        stale = _write(tmp / "retranscribe" / "x.pcm", 100)
        t2 = time.time() - 2 * DAY
        os.utime(stale.parent, (t2, t2))
        recent = _write(tmp / "other" / "y.pcm", 100)
        guest = _write(tmp / "guest_asr" / "job.1.json", 10)
        t3 = time.time() - 5 * DAY
        os.utime(guest.parent, (t3, t3))
        tts = _write(self.state / "cache" / "a.wav", 100)
        old_cache = _write(self.state / "cache.old" / "b.wav", 100)
        build = _write(self.blurt / "build" / "c.bin", 100)
        index = _write(self.blurt / "index.db", 100)
        s = storage.scan(self.viewer)["caches"]
        self.assertEqual(s["clearable"], 400)
        # 开着会：tmp 不动
        self.viewer.meetings.cur = {"id": "m1", "session": None}
        self.assertEqual(storage.clear_caches(self.viewer)["bytes"], 200)
        self.assertTrue(dead.exists())
        self.viewer.meetings.cur = None
        self.assertEqual(storage.clear_caches(self.viewer)["bytes"], 200)
        self.assertFalse(dead.exists())
        self.assertFalse(stale.exists())
        for keep in (alive, recent, guest, tts, index):
            self.assertTrue(keep.exists(), keep)
        self.assertFalse(old_cache.exists())
        self.assertFalse(build.exists())

    # ---------- 接口 ----------
    def test_post_validation(self):
        self.assertEqual(storage.post(self.viewer, "/api/storage/recordings", {"days": 5})[1], 400)
        self.assertEqual(storage.post(self.viewer, "/api/storage/recordings", {"which": "x"})[1], 400)
        self.assertEqual(storage.post(self.viewer, "/api/storage/auto", {"days": "x"})[1], 400)
        self.assertEqual(storage.post(self.viewer, "/api/storage/models", {"paths": ["C:/Windows"]}), ({"error": "in_use"}, 400))
        self.assertEqual(storage.post(self.viewer, "/api/storage/nope", {})[1], 404)
        out, code = storage.post(self.viewer, "/api/storage/auto", {"days": 30})
        self.assertEqual((code, out["ok"], out["auto_days"]), (200, True, 30))


SERVER = r"""
import json, os, sys, socket, http.client
sys.path.insert(0, sys.argv[1])
from zuhui.view import Viewer
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
v = Viewer(port=port)
v.serve(open_browser=False)
def req(method, path, body=None, origin=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Content-Type": "application/json"}
    if origin:
        h["Origin"] = origin
    c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
    r = c.getresponse(); data = r.read()
    try:
        return r.status, json.loads(data.decode("utf-8"))
    except ValueError:
        return r.status, None
out = {}
out["get"] = req("GET", "/api/storage")
out["bad_days"] = req("POST", "/api/storage/recordings", {"days": 5})
out["auto_bad"] = req("POST", "/api/storage/auto", {"days": 7})
out["auto"] = req("POST", "/api/storage/auto", {"days": 0})
out["models_in_use"] = req("POST", "/api/storage/models", {"paths": [os.path.join(os.environ["BLURT_HOME"], "models", "fun-asr-nano-int8")]})
out["evil"] = req("POST", "/api/storage/caches", {}, origin="https://evil.example")
out["caches"] = req("POST", "/api/storage/caches", {})
print("RESULT " + json.dumps(out, ensure_ascii=False), flush=True)
os._exit(0)
"""


class StorageHttpTest(unittest.TestCase):
    def test_routes(self):
        tmp = Path(tempfile.mkdtemp(prefix="zuhui-storage-http-"))
        try:
            env = dict(os.environ, ZUHUI_HOME=str(tmp / "zuhui"), BLURT_HOME=str(tmp / "blurt"),
                       BLURT_WORKSPACE=str(tmp / "ws"), PYTHONIOENCODING="utf-8")
            _write(tmp / "blurt" / "models" / "fun-asr-nano-int8" / "llm.int8.onnx", 3_000_000)
            p = subprocess.run([sys.executable, "-c", SERVER, str(ROOT)], env=env, capture_output=True, text=True,
                               encoding="utf-8", timeout=120)
            line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
            self.assertIsNotNone(line, p.stdout[-2000:] + p.stderr[-2000:])
            r = json.loads(line[7:])
            code, body = r["get"]
            self.assertEqual(code, 200)
            for k in ("total", "free", "split", "recordings", "models", "records", "caches", "logs"):
                self.assertIn(k, body)
            self.assertEqual([g["role"] for g in body["models"]["used"]], ["live"])
            self.assertEqual(r["bad_days"], [400, {"error": "days"}])
            self.assertEqual(r["auto_bad"], [400, {"error": "days"}])
            self.assertEqual(r["auto"], [200, {"ok": True, "auto_days": 0, "count": 0, "bytes": 0}])
            self.assertEqual(r["models_in_use"], [400, {"error": "in_use"}])
            self.assertEqual(r["evil"][0], 403)
            self.assertEqual(r["caches"][0], 200)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
