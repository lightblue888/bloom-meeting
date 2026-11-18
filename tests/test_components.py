# -*- coding: utf-8 -*-
"""基础部件在不在（zuhui/components.py）：status() 的形状、模型文件在 / 不在、which() 找到 ~/.blurt/bin 里的工具、
pandoc 先用随后端装的那份、ffmpeg 有没有 gfxcapture 只问一次；转写干活进程 uv 只用缓存起不来时联网再起一次
（retranscribe、guest_ears）。

全在临时目录里：BLURT_HOME、ZUHUI_HOME、UV_CACHE_DIR、家目录、LOCALAPPDATA 都指到临时目录，PATH 只留系统目录，
不碰真的 ~/.blurt、~/.zuhui，也不联网。

    python -m unittest tests.test_components -v
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-home-")    # 别碰真的 ~/.zuhui

from zuhui import components as C  # noqa: E402

WIN = os.name == "nt"
ENV_KEYS = ("BLURT_HOME", "UV_CACHE_DIR", "PATH", "USERPROFILE", "HOME", "LOCALAPPDATA", "PYPANDOC_PANDOC", "XDG_CACHE_HOME")


def _exe(d: Path, name: str, out: str = "") -> Path:
    """一个假的可执行文件：Windows 上是 .bat（PATHEXT 认），别处是 sh 脚本。运行时打印 out。"""
    d.mkdir(parents=True, exist_ok=True)
    if WIN:
        p = d / f"{name}.bat"
        p.write_text("@echo off\r\n" + (f"echo {out}\r\n" if out else ""), encoding="ascii")
    else:
        p = d / name
        p.write_text("#!/bin/sh\n" + (f"echo '{out}'\n" if out else ""), encoding="ascii")
        p.chmod(0o755)
    return p


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bloom-components-"))
        self.old_env = {k: os.environ.get(k) for k in ENV_KEYS}
        self.blurt = self.tmp / "blurt"
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["BLURT_HOME"] = str(self.blurt)
        os.environ["UV_CACHE_DIR"] = str(self.tmp / "uvcache")
        os.environ["USERPROFILE"] = os.environ["HOME"] = str(self.home)
        os.environ["LOCALAPPDATA"] = str(self.tmp / "localappdata")
        os.environ["PATH"] = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32") if WIN else "/usr/bin:/bin"
        os.environ.pop("PYPANDOC_PANDOC", None)
        os.environ.pop("XDG_CACHE_HOME", None)
        self._bundled = C.bundled_pandoc
        C.bundled_pandoc = lambda: None                      # 本机 Python 里装没装 pypandoc-binary 不影响结果
        C._which.clear()
        C._gfx.clear()

    def tearDown(self):
        C.bundled_pandoc = self._bundled
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        C._which.clear()
        C._gfx.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)


class StatusTest(Sandbox):
    def test_shape_and_nothing_installed(self):
        st = C.status()
        self.assertEqual(set(st), set(C.BASIC) | set(C.OPTIONAL))
        for k, v in st.items():
            self.assertEqual(set(v), {"ok", "path", "detail"}, k)
            self.assertIsInstance(v["ok"], bool, k)
            self.assertTrue(v["path"] is None or isinstance(v["path"], str), k)
            self.assertIsInstance(v["detail"], str, k)
            self.assertTrue(v["detail"].strip(), k)
        for k in ("asr_model", "vad", "smart_turn", "asr_runtime", "uv", "ffmpeg", "ffplay", "pandoc"):
            self.assertFalse(st[k]["ok"], k)
            self.assertIsNone(st[k]["path"], k)
        self.assertEqual(C.missing(st), list(C.BASIC))
        json.dumps(st, ensure_ascii=False)                    # 能直接从 HTTP 回出去

    def test_models_present(self):
        models = self.blurt / "models"
        asr = models / C.ASR_DIR
        (asr / "Qwen3-0.6B").mkdir(parents=True)
        for n in ("encoder_adaptor.int8.onnx", "llm.int8.onnx"):
            (asr / n).write_bytes(b"x")
        st = C.status()
        self.assertFalse(st["asr_model"]["ok"])
        self.assertIn("不完整", st["asr_model"]["detail"])
        (asr / "embedding.int8.onnx").write_bytes(b"x")
        (models / "silero_vad.onnx").write_bytes(b"x")
        (models / C.SMART_TURN).write_bytes(b"x")
        st = C.status()
        for k in ("asr_model", "vad", "smart_turn"):
            self.assertTrue(st[k]["ok"], k)
        self.assertEqual(Path(st["asr_model"]["path"]), asr)
        self.assertEqual(Path(st["smart_turn"]["path"]), models / C.SMART_TURN)
        (models / C.SMART_TURN).write_bytes(b"")              # 空文件（下到一半）不算
        self.assertFalse(C.status()["smart_turn"]["ok"])

    def test_asr_runtime_in_uv_cache(self):
        self.assertFalse(C.status()["asr_runtime"]["ok"])
        w = self.tmp / "uvcache" / "wheels-v6" / "pypi" / "sherpa-onnx"
        w.mkdir(parents=True)
        for ext in (".http", ".msgpack"):                     # 只有下载元数据不算
            (w / f"{C.SHERPA}-cp314-cp314-win_amd64{ext}").write_bytes(b"")
        (w / f"sherpa_onnx-{C.SHERPA}-cp314-cp314-win_amd64.lock").write_bytes(b"")
        self.assertFalse(C.status()["asr_runtime"]["ok"])
        (w / f"{C.SHERPA}-cp314-cp314-win_amd64").write_bytes(b"x" * 27)   # Windows 上：指针小文件
        self.assertTrue(C.status()["asr_runtime"]["ok"])
        (w / f"{C.SHERPA}-cp314-cp314-win_amd64").unlink()
        (w / f"{C.SHERPA}-cp312-cp312-manylinux_2_17_x86_64").mkdir()        # 别的系统上：目录
        self.assertTrue(C.status()["asr_runtime"]["ok"])

    def test_relay_and_voice(self):
        orig = C._spec
        try:
            C._spec = lambda name: name != "livekit.rtc"
            r = C.status()["relay"]
            self.assertFalse(r["ok"])
            self.assertIn("livekit", r["detail"])
            C._spec = lambda name: True
            self.assertTrue(C.status()["relay"]["ok"])
        finally:
            C._spec = orig
        p = _exe(self.blurt / "bin", "edge-tts")
        st = C.status(refresh=True)
        self.assertTrue(st["voice"]["ok"])
        self.assertEqual(Path(st["voice"]["path"]).resolve(), p.resolve())

    @unittest.skipUnless(WIN, "SAPI 只有 Windows 有")
    def test_sapi(self):
        s = C.status()["sapi"]
        self.assertIsInstance(C.sapi_voices(), list)
        self.assertEqual(s["ok"], bool(C.sapi_voices()))


class WhichTest(Sandbox):
    def test_blurt_bin_after_path(self):
        mine = _exe(self.blurt / "bin", "uv")
        self.assertEqual(Path(C.which("uv")).resolve(), mine.resolve())
        self.assertTrue(C.status()["uv"]["ok"])
        first = _exe(self.tmp / "onpath", "uv")
        os.environ["PATH"] = str(self.tmp / "onpath") + os.pathsep + os.environ["PATH"]
        self.assertEqual(Path(C.which("uv", refresh=True)).resolve(), first.resolve())   # PATH 上的优先

    def test_local_bin_and_cache(self):
        self.assertIsNone(C.which("ffplay"))
        p = _exe(self.home / ".local" / "bin", "ffplay")
        self.assertIsNone(C.which("ffplay"))                  # 找不到也缓存着
        self.assertEqual(Path(C.which("ffplay", refresh=True)).resolve(), p.resolve())
        p.unlink()
        self.assertIsNone(C.which("ffplay"))                  # 缓存的路径没了就重找

    def test_extend_path(self):
        (self.blurt / "bin").mkdir(parents=True)
        (self.home / ".local" / "bin").mkdir(parents=True)
        before = os.environ["PATH"]
        added = C.extend_path()
        self.assertEqual([Path(x) for x in added], [self.blurt / "bin", self.home / ".local" / "bin"])
        self.assertTrue(os.environ["PATH"].startswith(before))   # 接在后面，不抢原来的
        self.assertEqual(C.extend_path(), [])                 # 再来一次不重复加
        _exe(self.blurt / "bin", "ffprobe")
        self.assertIsNotNone(shutil.which("ffprobe"))         # 子进程、别处的 shutil.which 也找得到


class PandocTest(Sandbox):
    def test_order(self):
        sysp = _exe(self.tmp / "onpath", "pandoc")
        os.environ["PATH"] = str(self.tmp / "onpath") + os.pathsep + os.environ["PATH"]
        self.assertEqual(Path(C.pandoc()).resolve(), sysp.resolve())
        self.assertFalse("随后端" in C.status()["pandoc"]["detail"])
        # 随后端装的 pypandoc-binary：site-packages/pypandoc/files/pandoc(.exe)，找文件不 import
        C.bundled_pandoc = self._bundled
        site = self.tmp / "site"
        pkg = site / "pypandoc"
        (pkg / "files").mkdir(parents=True)
        (pkg / "__init__.py").write_text("raise RuntimeError('不该 import 它')\n", encoding="utf-8")
        exe = pkg / "files" / ("pandoc.exe" if WIN else "pandoc")
        exe.write_bytes(b"x")
        sys.path.insert(0, str(site))
        importlib.invalidate_caches()
        try:
            self.assertEqual(Path(C.pandoc()), exe)
            st = C.status()["pandoc"]
            self.assertTrue(st["ok"])
            self.assertIn("随后端", st["detail"])
            forced = _exe(self.tmp / "forced", "pandoc")
            os.environ["PYPANDOC_PANDOC"] = str(forced)
            self.assertEqual(Path(C.pandoc()), forced)
        finally:
            sys.path.remove(str(site))
            importlib.invalidate_caches()
        self.assertNotIn("pypandoc", sys.modules)


class FfmpegTest(Sandbox):
    def _count_runs(self):
        calls = []
        orig = subprocess.run

        def run(*a, **kw):
            calls.append(a[0] if a else kw.get("args"))
            return orig(*a, **kw)
        subprocess.run = run
        self.addCleanup(setattr, subprocess, "run", orig)
        return calls

    @unittest.skipUnless(WIN, "gfxcapture 只在 Windows 上查")
    def test_gfxcapture_asked_once(self):
        _exe(self.blurt / "bin", "ffmpeg", "Filter gfxcapture")
        _exe(self.blurt / "bin", "ffplay")
        calls = self._count_runs()
        st = C.status()
        self.assertTrue(st["ffmpeg"]["ok"], st["ffmpeg"])
        self.assertTrue(st["ffplay"]["ok"])
        self.assertEqual(len(calls), 1)
        t0 = time.perf_counter()
        st2 = C.status()                                      # 第二次：不再起子进程
        self.assertEqual(len(calls), 1)
        self.assertEqual(st2["ffmpeg"], st["ffmpeg"])
        self.assertLess(time.perf_counter() - t0, 2.0)

    @unittest.skipUnless(WIN, "gfxcapture 只在 Windows 上查")
    def test_old_ffmpeg_without_gfxcapture(self):
        _exe(self.blurt / "bin", "ffmpeg", "Unknown filter 'gfxcapture'.")
        st = C.status()["ffmpeg"]
        self.assertFalse(st["ok"])
        self.assertIsNotNone(st["path"])
        self.assertIn("gfxcapture", st["detail"])


class UvRunTest(Sandbox):
    def test_command(self):
        uv = _exe(self.blurt / "bin", "uv")
        on = C.uv_run(True)
        self.assertEqual(Path(on[0]).resolve(), uv.resolve())
        self.assertEqual(on[1:3], ["run", "--offline"])
        self.assertEqual(on[-len(C.UV_WITH):], C.UV_WITH)
        self.assertNotIn("--offline", C.uv_run(False))

    def test_retranscribe_retries_online(self):
        from zuhui import retranscribe as R
        work = self.tmp / "rt"
        work.mkdir()
        job, status = work / "job.json", work / "status.json"
        job.write_text("{}", encoding="utf-8")
        seen = []
        orig = R._worker_cmd

        def fake(engine, j, offline=True):
            seen.append(offline)
            return [sys.executable, "-c", f"import sys; sys.exit({3 if offline else 0})"]
        R._worker_cmd = fake
        try:
            R._run_worker("fun-asr-nano", job, status, yield_to_meeting=False)
            self.assertEqual(seen, [True, False])            # 只用缓存起不来 → 联网再起一次 → 成了
            seen.clear()
            with self.assertRaises(RuntimeError):            # 不走 uv 的那种：不重试，照旧报错
                R._worker_cmd = lambda engine, j, offline=True: (seen.append(offline), [sys.executable, "-c", "raise SystemExit(2)"])[1]
                R._run_worker("seaco-paraformer", job, status, yield_to_meeting=False)
            self.assertEqual(len(seen), 1)
        finally:
            R._worker_cmd = orig

    def test_guest_worker_retries_online(self):
        import numpy as np
        from zuhui import guest_ears as G
        fake_worker = (
            "import json, sys\n"
            "print(json.dumps({'ready': True, 'load_s': 0}), flush=True)\n"
            "inp = sys.stdin.buffer\n"
            "while True:\n"
            "    h = inp.readline()\n"
            "    if not h: break\n"
            "    h = json.loads(h)\n"
            "    if h.get('cmd') == 'quit': break\n"
            "    inp.read(int(h['samples']) * 2)\n"
            "    print(json.dumps({'id': h['id'], 'text': 'ok', 'ms': 1}), flush=True)\n")
        tried = []

        def cmd(self_, job):
            tried.append(self_._offline)
            if self_._offline:
                return [sys.executable, "-c", "import sys; sys.exit(1)"]    # 缓存里没有：一句话没说就退了
            return [sys.executable, "-c", fake_worker]
        orig = G.AsrWorker._cmd
        G.AsrWorker._cmd = cmd
        w = G.AsrWorker(self.tmp / "model")
        try:
            got = []
            w.submit(np.zeros(1600, dtype=np.int16), lambda text, ms: got.append(text))   # 起来之前就排着的段
            w.start()
            deadline = time.time() + 20
            while not got and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(tried, [True, False])
            self.assertTrue(w.ready)
            self.assertIsNone(w.error)
            self.assertEqual(got, ["ok"])                     # 排着的段没丢，新进程转了
        finally:
            w.stop()
            G.AsrWorker._cmd = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
