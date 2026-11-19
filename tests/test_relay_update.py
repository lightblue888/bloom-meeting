# -*- coding: utf-8 -*-
"""中继自动更新（relay/bloom_update.py）和 /healthz 的 live，全离线，不碰任何真的服务器。

在临时目录里摆一份假的 /opt/bloom（token_server.py、www、VERSION、update.env、bloom_update.py），
起一个本地 HTTP 服务假装 GitHub（releases/latest、raw、发布附件）和本机中继的 /healthz，
systemctl 换成只记调用的假命令。假 /healthz 看临时目录里装着的 token_server.py：里面有 HEALTH_FAIL 就回 500（新版起不来）。

    python -X utf8 -m unittest tests.test_relay_update -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "relay" / "bloom_update.py"
REPO = "test/bloom"
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _ts(tag: str, extra: str = "") -> bytes:
    """假的 token_server.py：能编译，里面有 /healthz。"""
    return f'# token_server {tag}\nHEALTH = "/healthz"\n{extra}'.encode()


def _load_updater():
    spec = importlib.util.spec_from_file_location("bloom_update_under_test", UPDATER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeGitHub:
    """假的 GitHub（API、raw、发布附件）和假的本机中继 /healthz，记下每个请求的路径和 Authorization。"""

    def __init__(self):
        self.releases: dict[str, dict] = {}
        self.latest: str | None = None
        self.live = 0
        self.ts_path: Path | None = None
        self.seen: list[tuple[str, str | None]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.seen.append((self.path, self.headers.get("Authorization")))
                code, body, extra = fake.route(self.path)
                self.send_response(code)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def reset(self, ts_path: Path):
        self.releases, self.latest, self.live, self.seen, self.ts_path = {}, None, 0, [], ts_path

    def release(self, tag, ts=None, updater=None, zip_bytes=None):
        files = {"token_server.py": _ts(tag) if ts is None else ts}
        if updater is not None:
            files["bloom_update.py"] = updater
        if zip_bytes is None:
            zip_bytes = _zip({"g/guest.html": f"guest {tag}", f"g/app-{tag}.js": "js", "livekit-client.umd.min.js": f"lk {tag}"})
        self.releases[tag] = {"files": files, "zip": zip_bytes}
        self.latest = tag

    def route(self, path):
        p = path.split("?")[0]
        parts = p.strip("/").split("/")
        if p == "/healthz":
            ts = self.ts_path.read_bytes() if self.ts_path and self.ts_path.exists() else b""
            if b"HEALTH_FAIL" in ts:
                return 500, b"{}", {}
            return 200, json.dumps({"ok": True, "public": False, "live": self.live}).encode(), {}
        if p == f"/api/repos/{REPO}/releases/latest" and self.latest:
            t = self.latest
            return 200, json.dumps({"tag_name": t, "assets": [
                {"name": "notes.txt", "url": f"{self.base}/api/assets/x", "browser_download_url": f"{self.base}/x"},
                {"name": "bloom-guest.zip", "url": f"{self.base}/api/assets/{t}",
                 "browser_download_url": f"{self.base}/download/{t}/bloom-guest.zip"}]}).encode(), {}
        if parts[0] == "download" and len(parts) == 3:          # 和真的 GitHub 一样先跳到下载服务器
            return 302, b"", {"Location": f"{self.base}/objects/{parts[1]}.zip"}
        if (parts[0] == "objects" or parts[:2] == ["api", "assets"]) and len(parts) >= 2:
            rel = self.releases.get(parts[-1].removesuffix(".zip"))
            if rel:
                return 200, rel["zip"], {}
        if p.startswith(f"/raw/{REPO}/") and len(parts) == 6 and parts[4] == "relay":
            data = (self.releases.get(parts[3]) or {}).get("files", {}).get(parts[5])
            if data is not None:
                return 200, data, {}
        return 404, b"not found", {}


class UpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gh = _FakeGitHub()

    @classmethod
    def tearDownClass(cls):
        cls.gh.srv.shutdown()
        cls.gh.srv.server_close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bloom_update_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        d = self.dir = self.tmp / "bloom"
        (d / "www" / "g").mkdir(parents=True)
        (d / "token_server.py").write_bytes(_ts("v1.0.0"))
        (d / "www" / "g" / "guest.html").write_text("guest v1.0.0", encoding="utf-8")
        (d / "www" / "g" / "app-old.js").write_text("old js", encoding="utf-8")
        (d / "www" / "livekit-client.umd.min.js").write_text("lk v1.0.0", encoding="utf-8")
        (d / "www" / "keep.txt").write_text("不在包里的文件", encoding="utf-8")
        (d / "VERSION").write_text("v1.0.0\n", encoding="utf-8")
        (d / "update.env").write_text(f"BLOOM_REPO={REPO}\n", encoding="utf-8")
        (d / "bloom_update.py").write_text("# old updater\n", encoding="utf-8")
        self.calls_log = self.tmp / "systemctl.log"
        fake = self.tmp / "fake_systemctl.py"
        fake.write_text("import sys\nwith open(sys.argv[1], 'a', encoding='utf-8') as f:\n"
                        "    f.write(' '.join(sys.argv[2:]) + '\\n')\n", encoding="utf-8")
        self.systemctl = f'"{Path(sys.executable).as_posix()}" "{fake.as_posix()}" "{self.calls_log.as_posix()}"'
        self.gh.reset(d / "token_server.py")
        self.gh.release("v1.0.0")
        self.snap = self.snapshot()

    # ---------- 小工具 ----------
    def run_update(self, *args, env=None, wait="2"):
        gh = self.gh
        e = {k: v for k, v in os.environ.items() if not k.upper().startswith("BLOOM_") and k.upper() != "GITHUB_TOKEN"}
        no_proxy = ",".join(x for x in (e.get("NO_PROXY", ""), "127.0.0.1", "localhost") if x)
        e.update(NO_PROXY=no_proxy, ZUHUI_HOME=str(self.tmp / "zuhui"), PYTHONIOENCODING="utf-8",
                 BLOOM_DIR=str(self.dir), BLOOM_GITHUB_API=gh.base + "/api", BLOOM_GITHUB_RAW=gh.base + "/raw",
                 BLOOM_HEALTHZ=gh.base + "/healthz", BLOOM_SYSTEMCTL=self.systemctl, BLOOM_HEALTH_WAIT=wait)
        e.update(env or {})
        p = subprocess.run([sys.executable, str(UPDATER), *args], env=e, capture_output=True, timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")

    def snapshot(self) -> dict:
        return {f.relative_to(self.dir).as_posix(): f.read_bytes()
                for f in sorted(self.dir.rglob("*")) if f.is_file() and f.name != ".update.lock"}

    def calls(self) -> list[str]:
        return self.calls_log.read_text(encoding="utf-8").splitlines() if self.calls_log.exists() else []

    def version(self) -> str:
        return (self.dir / "VERSION").read_text(encoding="utf-8").strip()

    def downloads(self) -> list[str]:
        return [p for p, _ in self.gh.seen if p.startswith(("/raw/", "/download/", "/objects/", "/api/assets/"))]

    def assert_untouched(self):
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.snapshot(), self.snap)
        self.assertEqual([x.name for x in self.dir.iterdir() if x.name.startswith(".update-")], [])

    # ---------- 测试 ----------
    def test_already_latest(self):
        rc, out = self.run_update()
        self.assertEqual(rc, 0, out)
        self.assertIn("已是最新", out)
        self.assert_untouched()
        self.assertEqual(self.downloads(), [])
        self.assertNotIn("/healthz", [p for p, _ in self.gh.seen])

    def test_update(self):
        self.gh.release("v1.1.0", updater=b"# new updater v1.1.0\n")
        rc, out = self.run_update()
        self.assertEqual(rc, 0, out)
        d = self.dir
        self.assertEqual(self.version(), "v1.1.0")
        self.assertEqual((d / "token_server.py").read_bytes(), _ts("v1.1.0"))
        self.assertEqual((d / "www/g/guest.html").read_text(encoding="utf-8"), "guest v1.1.0")
        self.assertTrue((d / "www/g/app-v1.1.0.js").is_file())
        self.assertFalse((d / "www/g/app-old.js").exists())                       # g/ 整个换掉
        self.assertEqual((d / "www/livekit-client.umd.min.js").read_text(encoding="utf-8"), "lk v1.1.0")
        self.assertEqual((d / "www/keep.txt").read_text(encoding="utf-8"), "不在包里的文件")   # 别的文件留着
        self.assertEqual((d / "bloom_update.py").read_bytes(), b"# new updater v1.1.0\n")    # 自己也换了
        bak = d / "backup" / "v1.0.0"
        self.assertEqual((bak / "token_server.py").read_bytes(), _ts("v1.0.0"))
        self.assertEqual((bak / "www/g/guest.html").read_text(encoding="utf-8"), "guest v1.0.0")
        self.assertTrue((bak / "www/g/app-old.js").is_file())
        self.assertEqual((bak / "bloom_update.py").read_text(encoding="utf-8"), "# old updater\n")
        self.assertEqual(self.calls(), ["restart bloom-token"])
        self.assertEqual([x.name for x in d.iterdir() if x.name.startswith(".update-")], [])
        paths = [p for p, _ in self.gh.seen]
        self.assertIn("/download/v1.1.0/bloom-guest.zip", paths)                     # 公开仓库走下载链接（跟着跳转）
        self.assertIn("/objects/v1.1.0.zip", paths)
        self.assertTrue(all(a is None for _, a in self.gh.seen))                     # 没设 GITHUB_TOKEN 就不带
        rc, out = self.run_update()                                                  # 再跑一次：已是最新
        self.assertEqual(rc, 0, out)
        self.assertIn("已是最新", out)
        self.assertEqual(self.calls(), ["restart bloom-token"])

    def test_live_meeting_skips(self):
        self.gh.release("v1.1.0")
        self.gh.live = 2
        rc, out = self.run_update()
        self.assertEqual(rc, 0, out)
        self.assertIn("有 2 场会在开", out)
        self.assert_untouched()
        self.assertEqual(self.downloads(), [])                                       # 有会在开连下载都不下
        self.gh.live = 0                                                             # 散会后下一次就换
        rc, out = self.run_update()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.version(), "v1.1.0")

    def test_relay_down_skips(self):
        self.gh.release("v1.1.0")
        rc, out = self.run_update(env={"BLOOM_HEALTHZ": "http://127.0.0.1:1/healthz"})
        self.assertNotEqual(rc, 0, out)
        self.assertIn("连不上", out)
        self.assert_untouched()

    def test_bad_download_not_installed(self):
        cases = {
            "token_server.py 语法错": dict(ts=b'HEALTH = "/healthz"\ndef (:\n'),
            "token_server.py 是空的": dict(ts=b""),
            "包里没有 g/guest.html": dict(zip_bytes=_zip({"g/index.html": "x", "livekit-client.umd.min.js": "lk"})),
            "包坏了": dict(zip_bytes=b"PK\x03\x04 not really a zip"),
            "包里有往外跳的路径": dict(zip_bytes=_zip({"g/guest.html": "x", "../evil.txt": "x"})),
            "bloom_update.py 语法错": dict(updater=b"def (:\n"),
        }
        for why, kw in cases.items():
            with self.subTest(why):
                self.gh.release("v1.1.0", **kw)
                rc, out = self.run_update()
                self.assertNotEqual(rc, 0, out)
                self.assertIn("这次不更新", out)
                self.assert_untouched()
                self.assertFalse((self.tmp / "evil.txt").exists())

    def test_rollback_when_new_version_does_not_come_up(self):
        self.gh.release("v1.1.0", ts=_ts("v1.1.0", "HEALTH_FAIL = True\n"), updater=b"# new updater\n")
        rc, out = self.run_update()
        self.assertEqual(rc, 1, out)
        self.assertIn("已换回", out)
        d = self.dir
        self.assertEqual((d / "token_server.py").read_bytes(), _ts("v1.0.0"))
        self.assertEqual((d / "www/g/guest.html").read_text(encoding="utf-8"), "guest v1.0.0")
        self.assertTrue((d / "www/g/app-old.js").is_file())
        self.assertEqual(self.version(), "v1.0.0")
        self.assertEqual((d / "bloom_update.py").read_text(encoding="utf-8"), "# old updater\n")   # 没成功就不换自己
        self.assertEqual(self.calls(), ["restart bloom-token", "reset-failed bloom-token", "restart bloom-token"])
        self.assertEqual([x.name for x in d.iterdir() if x.name.startswith(".update-")], [])

        rc, out = self.run_update()                                                  # 同一个版本 24 小时内不再试
        self.assertEqual(rc, 0, out)
        self.assertIn("不再试", out)
        self.assertEqual(len(self.calls()), 3)

        rc, out = self.run_update("--force")                                         # --force 再试：还是起不来，还是换回
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual((d / "token_server.py").read_bytes(), _ts("v1.0.0"))

        self.gh.release("v1.1.1")                                                    # 修好的新版照常换
        rc, out = self.run_update()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.version(), "v1.1.1")
        self.assertFalse((d / "update-failed").exists())

    def test_backups_keep_two(self):
        for tag in ("v1.1.0", "v1.2.0", "v1.3.0"):
            self.gh.release(tag)                       # 这几个版本没有 bloom_update.py（404）：自己不换，别的照常
            rc, out = self.run_update()
            self.assertEqual(rc, 0, out)
            time.sleep(0.05)
        self.assertEqual(self.version(), "v1.3.0")
        self.assertEqual(sorted(p.name for p in (self.dir / "backup").iterdir()), ["v1.1.0", "v1.2.0"])
        self.assertEqual((self.dir / "backup/v1.2.0/token_server.py").read_bytes(), _ts("v1.2.0"))
        self.assertEqual((self.dir / "bloom_update.py").read_text(encoding="utf-8"), "# old updater\n")
        self.assertEqual(self.calls(), ["restart bloom-token"] * 3)

    def test_lock_blocks_second_run(self):
        self.gh.release("v1.1.0")
        f = _load_updater()._lock(str(self.dir / ".update.lock"))
        self.assertIsNotNone(f)
        try:
            rc, out = self.run_update()
        finally:
            f.close()
        self.assertEqual(rc, 0, out)
        self.assertIn("在跑", out)
        self.assertEqual(self.gh.seen, [])
        self.assert_untouched()

    def test_private_repo_token_from_env_file(self):
        (self.dir / "update.env").write_text(f"BLOOM_REPO={REPO}\nGITHUB_TOKEN=tok123\n", encoding="utf-8")
        self.gh.release("v1.1.0")
        rc, out = self.run_update()
        self.assertEqual(rc, 0, out)
        github = {p: a for p, a in self.gh.seen if p != "/healthz"}
        self.assertIn("/api/assets/v1.1.0", github)                                  # 私有仓库：附件走 API
        self.assertTrue(all(a == "Bearer tok123" for a in github.values()), github)
        self.assertTrue(all(a is None for p, a in self.gh.seen if p == "/healthz"))  # 本机中继不带
        self.assertNotIn("tok123", out)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req(method, url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with _LOCAL.open(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, {}
    except OSError:
        return 0, {}


class HealthzLiveTest(unittest.TestCase):
    """真的 token_server.py（公共模式、没有 LiveKit）：/healthz 的 live 只算主持人 15 分钟内联系过、状态是在开的会。"""

    def test_live_counts_meetings_in_progress(self):
        tmp = Path(tempfile.mkdtemp(prefix="bloom_healthz_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "livekit.env").write_text("LIVEKIT_API_KEY=APItest123\nLIVEKIT_API_SECRET=" + "s" * 40 +
                                         "\nLIVEKIT_URL=wss://relay.test\nBLOOM_PUBLIC=1\n", encoding="utf-8")
        now = time.time()
        rooms = {
            "liveRECENT1": {"state": {"live": True, "title": "a"}, "sv": 1, "touched": now, "host_seen": now - 60},    # 主持人 1 分钟前还在：算
            "liveQUIET01": {"state": {"live": True, "title": "q"}, "sv": 1, "touched": now, "host_seen": now - 300},   # 5 分钟没联系、没人碰：算
            "liveSTALE01": {"state": {"live": True, "title": "b"}, "sv": 1, "touched": now, "host_seen": now - 1200},  # 20 分钟没联系：不算
            "idleNEXT001": {"state": {"live": False, "upcoming": True}, "sv": 1, "touched": now, "host_seen": now},     # 没开会、开着邀请：不算
            "endedROOM01": {"state": {"live": False, "ended": "2026-11-19T10:00:00"}, "sv": 2, "touched": now, "host_seen": now - 60},
        }
        (tmp / "rooms.json").write_text(json.dumps(rooms), encoding="utf-8")
        port = _free_port()
        env = dict(os.environ, BLOOM_ENV=str(tmp / "livekit.env"), BLOOM_PORT=str(port), STATE_DIRECTORY=str(tmp),
                   ZUHUI_HOME=str(tmp / "zuhui"))
        proc = subprocess.Popen([sys.executable, str(ROOT / "relay" / "token_server.py")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            if _req("GET", base + "/healthz")[0] == 200:
                break
            time.sleep(0.1)
        c, h = _req("GET", base + "/healthz")
        self.assertEqual(c, 200)
        self.assertEqual((h.get("ok"), h.get("public"), h.get("live")), (True, True, 2))
        self.assertEqual(_req("GET", base + "/healthz")[1].get("live"), 2)                 # 查健康不改东西

        c, v = _req("GET", base + "/view/liveQUIET01/state")                               # 访客一碰：主持人超过 2 分钟没联系，标成结束
        self.assertEqual((c, (v.get("state") or {}).get("live")), (200, False))
        self.assertEqual(_req("GET", base + "/healthz")[1].get("live"), 1)

        c, d = _req("POST", base + "/dev/register", {}, {"X-Forwarded-For": "10.0.0.1"})
        self.assertEqual(c, 200)
        dev = {"Authorization": f"Device {d['device']}:{d['secret']}"}
        self.assertEqual(_req("POST", base + "/push/nextROOM01/state", {"live": False, "upcoming": True}, dev)[0], 200)
        self.assertEqual(_req("GET", base + "/healthz")[1].get("live"), 1)                 # 下一场"未开始"不算
        self.assertEqual(_req("POST", base + "/push/liveNEW0001/state", {"live": True, "title": "c"}, dev)[0], 200)
        self.assertEqual(_req("GET", base + "/healthz")[1].get("live"), 2)
        self.assertEqual(_req("POST", base + "/push/liveNEW0001/clear", {}, dev)[0], 200)   # 散会
        self.assertEqual(_req("GET", base + "/healthz")[1].get("live"), 1)


if __name__ == "__main__":
    unittest.main()
