# -*- coding: utf-8 -*-
"""有新版本时提示、点了才更新（zuhui/updates.py，GET/POST /api/update）。

全离线：GitHub 换成本机一个假服务（releases/latest 和 raw 上的 install.ps1），PowerShell 打桩（不真起），
ZUHUI_HOME 和后端根目录都是临时目录，不碰真实的 ~/.zuhui，也不碰仓库根目录（开发机的后端从源码跑，没有 .bloom-version）。

    python -X utf8 -m unittest tests.test_updates -v
"""
from __future__ import annotations

import http.server
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
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-updates-")      # 必须在 import zuhui 之前
from zuhui import config as zconfig  # noqa: E402

if os.path.normcase(str(Path(zconfig.STATE).resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

from zuhui import updates  # noqa: E402

REPO = "lightblue888/bloom-meeting"
SCRIPT = ("# Bloom installer (test stand-in)\n$Dir = $env:BLOOM_DIR\n" + "# filler line\n" * 300).encode("ascii")


class FakeGitHub:
    """releases/latest 回 tag（fail=True 时 500）；/<repo>/<tag>/install.ps1 回 SCRIPT。记下每个请求。"""

    def __init__(self):
        self.tag = "v0.6.2"
        self.fail = False
        self.hits: list[str] = []
        gh = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                gh.hits.append(self.path)
                if self.path == f"/repos/{REPO}/releases/latest":
                    if gh.fail:
                        body, code = b'{"message":"boom"}', 500
                    else:
                        body, code = json.dumps({"tag_name": gh.tag, "draft": False, "prerelease": False}).encode(), 200
                elif self.path == f"/{REPO}/{gh.tag}/install.ps1":
                    body, code = SCRIPT, 200
                else:
                    body, code = b"not found", 404
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def checks(self) -> int:
        return sum(1 for h in self.hits if h.endswith("/releases/latest"))

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class FakeMeetings:
    def __init__(self, cur=None, rows=None):
        self.cur, self.rows = cur, rows or []

    def current(self):
        return dict(self.cur) if self.cur else None

    def list(self):
        return [dict(r) for r in self.rows]


class FakeViewer:
    def __init__(self, cur=None, rows=None, processing=()):
        self.meetings = FakeMeetings(cur, rows)
        self._proc = set(processing)

    def processing(self, mid):
        return mid in self._proc


class FakePopen:
    """subprocess.Popen 的替身：记下参数，不起任何进程。"""
    calls: list = []

    def __init__(self, args, **kw):
        FakePopen.calls.append((list(args), kw))
        self.pid = 4242


IDLE = FakeViewer(rows=[{"id": "m1"}])


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zuhui-test-updates-"))
        self.app = self.tmp / "bloom-meeting" / "app"          # install.ps1 的布局：<BLOOM_DIR>\app
        self.app.mkdir(parents=True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.gh = FakeGitHub()
        self.patches = [mock.patch.object(updates, "ROOT", self.app), mock.patch.object(updates, "STATE", self.state),
                        mock.patch.object(updates, "API", self.gh.url), mock.patch.object(updates, "RAW", self.gh.url),
                        mock.patch.object(updates, "REPO", REPO)]
        for p in self.patches:
            p.start()
        updates._cache.update(at=0.0, latest=None, ok=False)
        FakePopen.calls = []
        self.tmp_before = set(Path(tempfile.gettempdir()).glob("bloom-update-*"))

    def tearDown(self):
        # 后台还在查的那一下先等它查完（假服务还开着），免得它把结果写进下一个测试的缓存
        if updates._checker is not None:
            updates._checker.join(10)
        for th in threading.enumerate():
            if th.name == "update-run":
                th.join(10)
        for p in reversed(self.patches):
            p.stop()
        self.gh.close()
        updates._cache.update(at=0.0, latest=None, ok=False)
        shutil.rmtree(self.tmp, ignore_errors=True)
        for d in set(Path(tempfile.gettempdir()).glob("bloom-update-*")) - self.tmp_before:   # 这个测试下载 install.ps1 的临时目录
            shutil.rmtree(d, ignore_errors=True)

    def installed(self, tag="v0.6.1"):
        (self.app / ".bloom-version").write_text(tag, encoding="utf-8")

    def fresh(self, viewer=IDLE):
        """查一次新版（等后台线程查完），再要状态。"""
        updates.latest(wait=5)
        return updates.status(viewer)

    def wait_job(self, key, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            j = updates._load_job()
            if j.get(key):
                return j
            time.sleep(0.02)
        self.fail(f"update.json 里一直没有 {key}：{updates._load_job()}")

    # ------------------------------------------------------------ 从源码跑的
    def test_dev_never_checks(self):
        st = updates.status(IDLE)
        self.assertEqual(st["state"], "dev")
        out, code = updates.start(IDLE)
        self.assertEqual((code, out["error"]), (400, "dev"))
        time.sleep(0.3)
        self.assertEqual(self.gh.hits, [], "开发机（没有 .bloom-version）不该去查 GitHub")
        self.assertEqual(FakePopen.calls, [])
        self.assertFalse((self.state / "update.json").exists())

    def test_repo_root_is_dev(self):
        """真的仓库根目录（开发机的后端就在这儿跑）没有 .bloom-version。"""
        self.assertFalse((ROOT / ".bloom-version").exists())

    # ------------------------------------------------------------ 查新版
    def test_newer_release_available(self):
        self.installed("v0.6.1")
        st = self.fresh()
        self.assertEqual((st["state"], st["current"], st["latest"]), ("available", "v0.6.1", "v0.6.2"))
        self.assertIsNone(st["busy"])

    def test_same_or_older_release_is_none(self):
        self.installed("v0.6.2")
        self.assertEqual(self.fresh()["state"], "none")
        self.gh.tag = "v0.6.0"
        updates._cache.update(at=0.0)
        self.assertEqual(self.fresh()["state"], "none")

    def test_branch_install_is_none(self):
        self.installed("main")                  # BLOOM_REF=main 装的：认不出版本号，不提示
        self.assertEqual(self.fresh()["state"], "none")

    def test_check_failure_counts_as_none(self):
        self.installed("v0.6.1")
        self.gh.fail = True
        st = self.fresh()
        self.assertEqual((st["state"], st["latest"]), ("none", None))

    def test_result_cached(self):
        self.installed("v0.6.1")
        self.fresh()
        for _ in range(5):
            updates.status(IDLE)
        time.sleep(0.2)
        self.assertEqual(self.gh.checks(), 1, "6 小时内只查一次")
        updates._cache["at"] -= updates.CHECK_EVERY + 1        # 过了 6 小时
        self.fresh()
        self.assertEqual(self.gh.checks(), 2)

    def test_numeric_compare(self):
        self.assertTrue(updates.newer("v0.10.0", "v0.9.9"))
        self.assertTrue(updates.newer("0.6.2", "v0.6.1"))
        self.assertFalse(updates.newer("v0.6.1", "v0.6.1"))
        self.assertFalse(updates.newer(None, "v0.6.1"))
        self.assertFalse(updates.newer("v0.6.2", "main"))

    # ------------------------------------------------------------ 开会中不更新
    def test_refused_while_live(self):
        self.installed("v0.6.1")
        live = FakeViewer(cur={"id": "m2", "state": "live"})
        self.assertEqual(self.fresh(live)["busy"], "live")
        out, code = updates.start(live)
        self.assertEqual((code, out), (409, {"ok": False, "error": "live"}))
        self.assertEqual(FakePopen.calls, [])
        self.assertFalse((self.state / "update.json").exists())

    def test_refused_while_processing(self):
        self.installed("v0.6.1")
        post = FakeViewer(rows=[{"id": "m1"}, {"id": "m0"}], processing={"m1"})
        out, code = updates.start(post)
        self.assertEqual((code, out["error"]), (409, "processing"))
        self.assertEqual(self.fresh(post)["busy"], "processing")

    def test_refused_when_nothing_newer(self):
        self.installed("v0.6.2")
        self.fresh()
        out, code = updates.start(IDLE)
        self.assertEqual((code, out["error"]), (409, "none"))

    # ------------------------------------------------------------ 点了更新
    def test_post_starts_powershell(self):
        self.installed("v0.6.1")
        self.fresh()
        with mock.patch.object(updates.subprocess, "Popen", FakePopen), mock.patch.object(updates, "_alive", lambda pid: True):
            out, code = updates.start(IDLE)
            self.assertEqual(code, 200)
            self.assertEqual((out["state"], out["latest"]), ("updating", "v0.6.2"))
            job = self.wait_job("pid")
            self.assertEqual(len(FakePopen.calls), 1)
            args, kw = FakePopen.calls[0]
            self.assertTrue(args[0].lower().endswith("powershell.exe"))
            for a in ("-NoProfile", "-ExecutionPolicy", "Bypass", "-File"):
                self.assertIn(a, args)
            script = Path(args[args.index("-File") + 1])
            self.assertEqual(script.name, "install.ps1")
            self.assertEqual(script.read_bytes(), SCRIPT, "跑的是这个 tag 的 install.ps1")
            self.assertIn(f"/{REPO}/v0.6.2/install.ps1", self.gh.hits)
            env = kw["env"]
            self.assertEqual(env["BLOOM_REF"], "v0.6.2")
            self.assertEqual(env["BLOOM_DIR"], str(self.app.parent), "BLOOM_DIR 是 app 的上一层（install.ps1 装到 <BLOOM_DIR>\\app）")
            self.assertEqual(env["BLOOM_RESTART"], "1")
            self.assertEqual(env["BLOOM_LOG"], str(self.state / "update.log"))
            if sys.platform == "win32":
                flags = kw["creationflags"]
                self.assertTrue(flags & 0x08000000, "CREATE_NO_WINDOW")
                self.assertTrue(flags & 0x00000200, "CREATE_NEW_PROCESS_GROUP")
            for k in ("stdin", "stdout", "stderr"):
                self.assertEqual(kw[k], subprocess.DEVNULL)
            self.assertEqual(kw["cwd"], str(script.parent))
            self.assertEqual((job["state"], job["target"], job["from"]), ("updating", "v0.6.2", "v0.6.1"))

            st = updates.status(IDLE)
            self.assertEqual((st["state"], st["latest"]), ("updating", "v0.6.2"))
            again, code = updates.start(IDLE)                     # 再点一次：不再起第二个
            self.assertEqual((code, again["state"]), (200, "updating"))
            self.assertEqual(len(FakePopen.calls), 1)

            # 装好了：install.ps1 最后写 .bloom-version，Bloom 重开后的后端看到版本对上
            self.installed("v0.6.2")
            st = updates.status(IDLE)
            self.assertEqual((st["state"], st["current"]), ("none", "v0.6.2"))
            self.assertFalse((self.state / "update.json").exists())
            self.assertFalse(script.parent.exists(), "下载 install.ps1 的临时目录删掉了")

    def test_download_failure(self):
        self.installed("v0.6.1")
        self.fresh()
        self.gh.tag = "v0.6.2"
        with mock.patch.object(updates.subprocess, "Popen", FakePopen), \
                mock.patch.object(updates, "RAW", self.gh.url + "/nothing-here"):
            out, code = updates.start(IDLE)
            self.assertEqual(code, 200)
            self.wait_job("ended")
        st = updates.status(IDLE)
        self.assertEqual((st["state"], st["error"]), ("failed", "download"), updates._load_job())
        self.assertEqual(FakePopen.calls, [])

    # ------------------------------------------------------------ 失败
    def job(self, **kw):
        j = {"state": "updating", "target": "v0.6.2", "from": "v0.6.1", "started": time.time(), "pid": 4242,
             "pid_of": 1, "dir": None, "error": None, "token": "t"}
        j.update(kw)
        (self.state / "update.json").write_text(json.dumps(j), encoding="utf-8")

    def transcript(self, *lines, footer=True):
        head = ["**********************", "Windows PowerShell 脚本开始", "开始时间: 20261119120000", "**********************"]
        foot = ["**********************", "Windows PowerShell 脚本结束", "结束时间: 20261119121000", "**********************"]
        text = "\r\n".join(head + list(lines) + (foot if footer else [])) + "\r\n"
        (self.state / "update.log").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

    def test_timeout_is_failure_with_last_log_line(self):
        self.installed("v0.6.1")
        self.job(started=time.time() - updates.GIVE_UP - 5)
        self.transcript("  下载 Bloom（v0.6.2）...", "  Downloading Bloom (v0.6.2)...", "    412.3 / 802.7 MB   51%", footer=False)
        with mock.patch.object(updates, "_alive", lambda pid: True):
            st = updates.status(IDLE)
        self.assertEqual((st["state"], st["latest"]), ("failed", "v0.6.2"))
        self.assertEqual(st["error"], "412.3 / 802.7 MB   51%")
        self.assertEqual(json.loads((self.state / "update.json").read_text(encoding="utf-8"))["state"], "failed")

    def test_timeout_without_log(self):
        self.installed("v0.6.1")
        self.job(started=time.time() - updates.GIVE_UP - 5)
        with mock.patch.object(updates, "_alive", lambda pid: True):
            st = updates.status(IDLE)
        self.assertEqual((st["state"], st["error"]), ("failed", "timeout"))

    def test_powershell_exited_without_new_version(self):
        self.installed("v0.6.1")
        self.job()
        self.transcript("  [ok] Bloom -> C:\\x\\app", "  Opening Bloom...", "  [x] uv sync failed")
        with mock.patch.object(updates, "_alive", lambda pid: False):
            st = updates.status(IDLE)
        self.assertEqual((st["state"], st["error"]), ("failed", "uv sync failed"))

    def test_still_running_is_updating(self):
        self.installed("v0.6.1")
        self.job(started=time.time() - 60)
        with mock.patch.object(updates, "_alive", lambda pid: True):
            self.assertEqual(updates.status(IDLE)["state"], "updating")

    def test_failed_can_retry_and_expires(self):
        self.installed("v0.6.1")
        self.fresh()
        self.job(state="failed", error="uv sync failed", ended=time.time() - 60)
        self.assertEqual(updates.status(IDLE)["state"], "failed")
        with mock.patch.object(updates.subprocess, "Popen", FakePopen), mock.patch.object(updates, "_alive", lambda pid: True):
            out, code = updates.start(IDLE)
            self.assertEqual((code, out["state"]), (200, "updating"))
            self.wait_job("pid")
        self.job(state="failed", error="uv sync failed", ended=time.time() - updates.FAILED_KEEP - 5)
        self.assertEqual(updates.status(IDLE)["state"], "available")

    def test_alive_real_process(self):
        self.assertTrue(updates._alive(os.getpid()))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(updates._alive(p.pid))


if __name__ == "__main__":
    unittest.main()
