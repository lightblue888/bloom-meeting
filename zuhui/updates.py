# -*- coding: utf-8 -*-
"""有新版本时提示，点了才更新（大厅"准备情况"那一行）。

只管用一行命令（oss/install.ps1）装的 Bloom：后端根目录有 .bloom-version，写着装的是哪个 tag。从仓库源码跑的
（开发机）没有这个文件，状态一律 dev：不查 GitHub，也不让更新。

    GET  /api/update  → {state: dev|none|available|updating|failed, current, latest, error?, busy}
    POST /api/update  → 开始更新；开会中、会后整理没做完时 409 {ok: false, error: live|processing}

查新版：GitHub 的 releases/latest，在后台线程里查，超时短；查到的留 6 小时，没查到（断网、限流）当没有新版，
半小时后再查。走用户原来的代理：后端一启动就把代理变量从自己的环境里拿掉了（netenv.py），这里按 netenv.SAVED
加回去，NO_PROXY 照样认。不改任何代理设置。

更新：下载这个 tag 的 install.ps1 到临时目录，起一个隐藏、不跟着后端退出的 PowerShell 跑它，带上
BLOOM_REF=tag、BLOOM_DIR=装的地方、BLOOM_RESTART=1（先关掉 Bloom 和后端，装完再打开）、BLOOM_LOG=~/.zuhui/update.log。
进度记在 ~/.zuhui/update.json，后端被关掉重开以后照样接得上：.bloom-version 变成目标版本 = 装好了；PowerShell
已经退出而版本没变、或者 15 分钟还没装完 = 失败，原因取日志最后一行。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import netenv
from .config import ROOT, STATE

REPO = os.environ.get("BLOOM_REPO") or "lightblue888/bloom-meeting"
API = (os.environ.get("BLOOM_API") or "https://api.github.com").rstrip("/")
RAW = "https://raw.githubusercontent.com"

CHECK_EVERY = 6 * 3600      # 查到的结果留多久
RETRY_AFTER = 30 * 60       # 没查到时多久再查
CHECK_TIMEOUT = 8           # 查新版的超时（秒）
FETCH_TIMEOUT = 30          # 下载 install.ps1 的超时
GIVE_UP = 15 * 60           # 超过这么久版本还没变：算失败
NO_PID_GIVE_UP = 3 * 60     # 还没起 PowerShell 就没了下文（下载时后端被关了）：算失败
FAILED_KEEP = 24 * 3600     # 失败留多久；之后又显示"新版本"

_lock = threading.Lock()            # 查新版的缓存
_job_lock = threading.RLock()       # update.json 的读改写
_cache: dict = {"at": 0.0, "latest": None, "ok": False}
_checker: threading.Thread | None = None


# ---------------------------------------------------------------- 文件
def version_file() -> Path:
    return ROOT / ".bloom-version"


def job_file() -> Path:
    return STATE / "update.json"


def log_file() -> Path:
    return STATE / "update.log"


def current() -> str | None:
    """装的是哪个 tag；不是 install.ps1 装的（没有 .bloom-version）：None。"""
    try:
        v = version_file().read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return v[:64] or None


def _ver(tag) -> tuple | None:
    m = re.fullmatch(r"v?(\d+(?:\.\d+){0,3})", str(tag or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def newer(latest, cur) -> bool:
    """latest 比 cur 新。装的是分支（main）之类认不出版本号的：不算有新版。"""
    a, b = _ver(latest), _ver(cur)
    return bool(a and b and a > b)


# ---------------------------------------------------------------- 网络
def _opener(url: str):
    """按用户原来的代理（netenv.SAVED 加回去的）开连接；NO_PROXY 里的、本机地址直连。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    env = netenv.with_proxy()
    proxies: dict[str, str] = {}
    no = ""
    for k in sorted(env):                  # 大写的先、小写的后：两种都有时小写的算数（和 urllib 一样）
        kl, v = k.lower(), env[k]
        if not v:
            continue
        if kl == "no_proxy":
            no = v
        elif kl in ("http_proxy", "https_proxy"):
            proxies[kl[:-6]] = v
    direct = (host in ("127.0.0.1", "localhost", "::1") or not proxies
              or (no and urllib.request.proxy_bypass_environment(host, {"no": no})))
    return urllib.request.build_opener(urllib.request.ProxyHandler({} if direct else proxies))


def _get(url: str, timeout: float, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "bloom-updater", "Accept": accept})
    with _opener(url).open(req, timeout=timeout) as r:
        return r.read(4_000_000)


def _check() -> None:
    latest, ok = None, False
    try:
        d = json.loads(_get(f"{API}/repos/{REPO}/releases/latest", CHECK_TIMEOUT).decode("utf-8"))
        tag = str(d.get("tag_name") or "").strip() if isinstance(d, dict) else ""
        if tag and not d.get("draft") and not d.get("prerelease"):
            latest, ok = tag[:64], True
    except Exception as e:                 # 断网、超时、限流：当没有新版
        print(f"[update] 查新版失败：{e!r}"[:300], flush=True)
    with _lock:
        _cache.update(at=time.time(), latest=latest, ok=ok)


def latest(wait: float = 0) -> str | None:
    """最新 release 的 tag（缓存的）。过期了就在后台线程里再查；wait > 0 时最多等这么久拿新结果。"""
    global _checker
    with _lock:
        age = time.time() - _cache["at"]
        stale = age > (CHECK_EVERY if _cache["ok"] else RETRY_AFTER)
        if stale and not (_checker and _checker.is_alive()):
            _checker = threading.Thread(target=_check, name="update-check", daemon=True)
            _checker.start()
        t = _checker if stale else None
    if t is not None and wait > 0:
        t.join(wait)
    with _lock:
        return _cache["latest"]


# ---------------------------------------------------------------- 开会中不更新
def busy(viewer) -> str | None:
    """开着会 → "live"；散会后的整理（转写、整理、纪要）还在做 → "processing"；都没有 → None。
    看的和 /api/current 一样：meetings.current() 和 viewer.processing(上一场)。"""
    ms = getattr(viewer, "meetings", None) if viewer is not None else None
    if not ms:
        return None
    try:
        cur = ms.current()
    except Exception:
        cur = None
    if cur:
        return "live" if cur.get("state") == "live" else "processing"
    try:
        rows = ms.list() or []
        if rows and viewer.processing(rows[0].get("id")):
            return "processing"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- update.json
def _load_job() -> dict:
    with _job_lock:                 # Windows 上别人正读着时 os.replace 会被拒：本进程里读写都排队
        try:
            d = json.loads(job_file().read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}


def _save_job(d: dict | None) -> None:
    f = job_file()
    with _job_lock:
        if not d:
            try:
                f.unlink()
            except OSError:
                pass
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        for i in range(20):         # 另一个进程（刚重开的后端）正读着：等一下再换
            try:
                os.replace(tmp, f)
                return
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.05)


def _patch_job(token: str, **kw) -> None:
    """只改这一次更新的记录（token 对得上）：中途又点了一次，旧线程别把新记录改了。"""
    with _job_lock:
        j = _load_job()
        if j.get("token") == token:
            j.update(kw)
            _save_job(j)


def _cleanup(job: dict) -> None:
    """删掉下载 install.ps1 的那个临时目录（只删 bloom-update- 开头、在临时目录里的）。"""
    d = job.get("dir")
    if not d:
        return
    p = Path(d)
    try:
        if p.name.startswith("bloom-update-") and p.resolve().parent == Path(tempfile.gettempdir()).resolve():
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5        # 拒绝访问：进程在，只是打不开
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259                   # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _sep(s: str) -> bool:
    return re.fullmatch(r"\*{10,}", s.strip()) is not None


def last_log_line(path: Path | None = None) -> str:
    """更新日志里最后一行有内容的话，去掉 install.ps1 的 [x] / [!] 记号。

    日志是 Start-Transcript 写的：一行 ****、头（按系统语言，中文 Windows 上是"Windows PowerShell 脚本开始"）、****、
    正文；正常停下时再有 ****、两行结尾、****。头和结尾都不算。"""
    f = path or log_file()
    try:
        with open(f, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            raw = fh.read()
    except OSError:
        return ""
    text = raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    lines = re.split(r"\r\n|\n|\r", text.replace("\ufeff", ""))
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _sep(lines[-1]):                   # 正常停下的：去掉最后那段结尾
        j = len(lines) - 2
        while j >= 0 and not _sep(lines[j]) and len(lines) - j <= 8:
            j -= 1
        if j >= 0 and _sep(lines[j]):
            lines = lines[:j]
    for line in reversed(lines):
        s = line.strip()
        if _sep(s):                                 # 到了头那段：正文里没有字
            break
        s = re.sub(r"^\[(x|!|ok)\]\s*", "", s).strip()
        if s:
            return s[:300]
    return ""


# ---------------------------------------------------------------- 状态
def _settle(cur: str) -> dict | None:
    """update.json 现在算什么。装好了（版本变成目标）：删掉记录，None；PowerShell 已经退出而版本没变、或者超时：
    记成失败；失败过了一天：删掉。返回还算数的记录（updating / failed）。"""
    with _job_lock:
        job = _load_job()
        st = job.get("state")
        if st not in ("updating", "failed"):
            return None
        tgt = job.get("target")
        now = time.time()
        if tgt and cur == tgt:
            _cleanup(job)
            _save_job(None)
            return None
        if st == "updating":
            try:
                started = float(job.get("started") or 0)
            except (TypeError, ValueError):
                started = 0.0
            why = None
            if now - started > GIVE_UP:
                why = last_log_line() or "timeout"
            elif job.get("pid") and not _alive(job["pid"]):
                if current() == tgt:               # 刚好在这一下装完
                    _cleanup(job)
                    _save_job(None)
                    return None
                why = last_log_line() or "failed"
            elif not job.get("pid") and job.get("pid_of") != os.getpid() and now - started > NO_PID_GIVE_UP:
                why = "launch"
            if why:
                job.update(state="failed", error=why, ended=now)
                _cleanup(job)
                _save_job(job)
            return job
        try:
            ended = float(job.get("ended") or 0)
        except (TypeError, ValueError):
            ended = 0.0
        if now - ended > FAILED_KEEP:
            _save_job(None)
            return None
        return job


def status(viewer=None) -> dict:
    """GET /api/update。"""
    cur = current()
    if not cur:
        return {"state": "dev", "current": None, "latest": None, "busy": None}
    out = {"current": cur, "latest": None, "busy": busy(viewer)}
    job = _settle(cur)
    lt = latest()
    if job and job.get("state") == "updating":
        out.update(state="updating", latest=job.get("target"))
        return out
    if job and job.get("state") == "failed" and not newer(lt, job.get("target")):
        out.update(state="failed", latest=job.get("target"), error=str(job.get("error") or "failed"))
        return out
    out.update(latest=lt, state="available" if newer(lt, cur) else "none")
    return out


# ---------------------------------------------------------------- 更新
def start(viewer=None) -> tuple[dict, int]:
    """POST /api/update：→ (回给页面的, HTTP 状态码)。"""
    cur = current()
    if not cur:
        return {"ok": False, "error": "dev"}, 400
    b = busy(viewer)
    if b:
        return {"ok": False, "error": b}, 409
    lt = latest(wait=CHECK_TIMEOUT + 2)
    with _job_lock:
        job = _settle(cur)
        if job and job.get("state") == "updating":
            return {"ok": True, **status(viewer)}, 200
        if not newer(lt, cur):
            return {"ok": False, "error": "none"}, 409
        token = f"{os.getpid()}-{time.time():.6f}"
        _save_job({"state": "updating", "target": lt, "from": cur, "started": time.time(), "pid": None,
                   "pid_of": os.getpid(), "dir": None, "error": None, "token": token})
    threading.Thread(target=_run, args=(lt, token), name="update-run", daemon=True).start()
    return {"ok": True, "state": "updating", "current": cur, "latest": lt, "busy": None}, 200


def _run(tag: str, token: str) -> None:
    """下载这个 tag 的 install.ps1，起 PowerShell。出错就记成失败（原因是代码：download / launch）。"""
    try:
        d = Path(tempfile.mkdtemp(prefix="bloom-update-"))
        _patch_job(token, dir=str(d))
        try:
            body = _get(f"{RAW}/{REPO}/{urllib.parse.quote(tag, safe='')}/install.ps1", FETCH_TIMEOUT, accept="text/plain")
        except Exception as e:
            print(f"[update] 下载 install.ps1 失败：{e!r}"[:300], flush=True)
            shutil.rmtree(d, ignore_errors=True)
            return _patch_job(token, state="failed", error="download", ended=time.time())
        if len(body) < 2000 or b"BLOOM_DIR" not in body:
            shutil.rmtree(d, ignore_errors=True)
            return _patch_job(token, state="failed", error="download", ended=time.time())
        script = d / "install.ps1"
        script.write_bytes(body)
        try:
            log_file().unlink()                     # 上一次的日志：这次没写出东西时别拿它的最后一行当原因
        except OSError:
            pass
        try:
            p = launch(script, tag)
        except Exception as e:
            print(f"[update] 起 PowerShell 失败：{e!r}"[:300], flush=True)
            shutil.rmtree(d, ignore_errors=True)
            return _patch_job(token, state="failed", error="launch", ended=time.time())
        _patch_job(token, pid=p.pid)
    except Exception as e:
        print(f"[update] {e!r}"[:300], flush=True)
        _patch_job(token, state="failed", error="failed", ended=time.time())


def install_dir() -> Path:
    """install.ps1 的 BLOOM_DIR：后端装在 <BLOOM_DIR>\\app，所以是后端根目录的上一层。"""
    return ROOT.parent


def launch(script: Path, tag: str) -> subprocess.Popen:
    """隐藏地跑 install.ps1：自己一个进程组、不开窗口、不接后端的输入输出，后端被它关掉也不受影响。"""
    env = netenv.with_proxy()                       # 下载走用户原来的代理
    # 后端自己的 Python 环境变量别带给安装脚本里的 uv / python
    for k in ("BLOOM_SOURCE", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__", "PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)
    env.update({"BLOOM_REF": tag, "BLOOM_DIR": str(install_dir()), "BLOOM_RESTART": "1", "BLOOM_LOG": str(log_file())})
    ps = shutil.which("powershell.exe") or os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                                                         "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    args = [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)]
    kw = dict(cwd=str(script.parent), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    if sys.platform != "win32":
        return subprocess.Popen(args, start_new_session=True, **kw)
    flags = 0x08000000 | 0x00000200                 # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:                                            # 再脱离后端所在的作业对象（不允许脱离时退回不带这个）
        return subprocess.Popen(args, creationflags=flags | 0x01000000, **kw)
    except OSError:
        return subprocess.Popen(args, creationflags=flags, **kw)


# ---------------------------------------------------------------- HTTP（view.py 调）
def http_get(viewer) -> tuple[dict, int]:
    try:
        return status(viewer), 200
    except Exception as e:
        return {"state": "none", "current": current(), "latest": None, "busy": None, "error": repr(e)[:200]}, 200


def http_post(viewer, body: dict | None = None) -> tuple[dict, int]:
    try:
        return start(viewer)
    except Exception as e:
        return {"ok": False, "error": repr(e)[:200]}, 500
