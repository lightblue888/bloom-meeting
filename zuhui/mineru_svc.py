# -*- coding: utf-8 -*-
"""MinerU 常驻服务（2026-11-20）：`mineru-kit api-server --preload-models`，模型只载一次，之后每篇 PDF 用
`parse --remote-url` 交给它（pdfconv.run_mineru）。实测 13 页 35–44 秒 → 27 秒，38 页 57 → 47 秒，结果一样。
常驻约占 3 GB 显存（用户会让 Bloom 停在大厅一整天、同时打游戏），所以：
    · 起：大厅从不可见变成可见时起一次（前端只在大厅可见时带 ?lobby=1 轮询，隔了 LOBBY_GAP_S 以上再来就算"重新可见"，
      lobby_seen），或者有 PDF 要转时起（use / ensure，开会中也起）；
    · 关：IDLE_S（10 分钟）没有转换任务就关，不管大厅是不是还开着；关了以后大厅再次变可见、或者又来了 PDF 再起；
      开会中转完空闲 IDLE_MEETING_S（2 分钟）就关（配音、转写也在显卡上）；开会那一刻没在转的直接关；
    · 没装 MinerU（pdfconv.mineru_kit() 为 None）：什么都不做。
起不来、连不上：调用方照旧本地起进程转（run_mineru 不带 remote_url）。
端口让系统挑一个空闲的（不和 47330、47407、1420 这些固定端口撞）。起好的记在 STATE/mineru_server.json：
后端崩了没来得及关的，下次起来还健康就接着用、到点照样关。"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager

from . import pdfconv
from .config import STATE

IDLE_S = float(os.environ.get("ZUHUI_MINERU_IDLE") or 600)   # 这么久没有转换任务就关（大厅开着也关）
IDLE_MEETING_S = 120.0       # 开会中：转完空闲这么久就关
LOBBY_GAP_S = 15.0           # 大厅可见时前端 1.5 秒带一次 ?lobby=1；隔了这么久才又来 = 大厅刚从不可见变成可见
START_TIMEOUT = 120.0        # 起服务最多等这么久（平时约 8 秒；显卡被别的程序占着时慢）
RETRY_AFTER = 300.0          # 起失败了：这么久之内不再自己重试（转换时还是本地转）
STATE_FILE = STATE / "mineru_server.json"
LOG_FILE = STATE / "mineru_server.log"

in_meeting = lambda: False   # noqa: E731  app 设：现在是不是在开会
log = lambda text: None      # noqa: E731  app 设：记一行日志

_lock = threading.RLock()
_proc: subprocess.Popen | None = None
_pid: int | None = None       # 接过来的（上一个后端起的）只有 pid
_port: int | None = None
_ready = False
_starting: threading.Thread | None = None
_failed_at = 0.0
_last_used = 0.0
_lobby_at = 0.0
_busy = 0
_reaper: threading.Thread | None = None
_adopted = False


def available() -> bool:
    return pdfconv.mineru_kit() is not None


def _open(url: str, timeout: float):
    """连本机：不经代理。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=timeout)


def _healthy(port: int | None) -> bool:
    if not port:
        return False
    try:
        with _open(f"http://127.0.0.1:{port}/v1/health", 2) as r:
            return r.status < 400
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _running() -> bool:
    """起着（或正在起）。调用方拿着 _lock。"""
    if _proc is not None:
        return _proc.poll() is None
    return _pid is not None


def _image_name(pid: int) -> str:
    if os.name != "nt":
        return ""
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], capture_output=True,
                           timeout=10, creationflags=pdfconv.NO_WINDOW)
        line = r.stdout.decode("utf-8", "replace").strip().splitlines()
        return line[0].split(",")[0].strip('"').lower() if line and line[0].startswith('"') else ""
    except Exception:
        return ""


def _adopt() -> None:
    """上一个后端起的服务（崩了没来得及关）：还健康就接着用；还在、却不应答的是 mineru-kit 就关掉。只做一次。"""
    global _adopted, _pid, _port, _ready, _last_used
    with _lock:
        if _adopted:
            return
        _adopted = True
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        pid, port = int(d.get("pid") or 0), int(d.get("port") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return
    if pid and port and _healthy(port):
        with _lock:
            if not _running():
                _pid, _port, _ready, _last_used = pid, port, True, time.time()
                _start_reaper()
                return
    if pid and _image_name(pid).startswith("mineru"):
        pdfconv._kill_tree(type("P", (), {"pid": pid})())
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


def url() -> str | None:
    """起好了的服务地址；没起、还在起：None（不会去起）。"""
    with _lock:
        if _ready and _port and _running():
            return f"http://127.0.0.1:{_port}"
    return None


def _start_blocking() -> bool:
    global _proc, _pid, _port, _ready, _failed_at, _last_used
    kit = pdfconv.mineru_kit()
    if not kit:
        return False
    port = _free_port()
    up = STATE / "mineru-api"
    t0 = time.time()
    try:
        up.mkdir(parents=True, exist_ok=True)
        cmd = [str(kit), "api-server", "--host", "127.0.0.1", "--port", str(port), "--tier", pdfconv.TIER,
               "--preload-models", "--upload-dir", str(up), "--retention-seconds", "900", "--log-level", "warning"]
        with open(LOG_FILE, "wb") as lf:
            proc = subprocess.Popen(cmd, cwd=str(up), stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=pdfconv.mineru_env(), creationflags=pdfconv.NO_WINDOW)
    except OSError as e:
        with _lock:
            _failed_at = time.time()
        log(f"MinerU 服务启动失败：{e!r}")
        return False
    with _lock:
        _proc, _pid, _port, _ready = proc, proc.pid, port, False
    try:
        STATE_FILE.write_text(json.dumps({"pid": proc.pid, "port": port, "at": time.strftime("%Y-%m-%d %H:%M:%S")}),
                              encoding="utf-8")
    except OSError:
        pass
    while time.time() - t0 < START_TIMEOUT:
        if proc.poll() is not None:
            break
        if _healthy(port):
            with _lock:
                if _proc is proc:
                    _ready, _last_used = True, time.time()
                    _start_reaper()
            log(f"MinerU 服务已就绪（{time.time() - t0:.1f} 秒，端口 {port}）")
            return True
        time.sleep(0.3)
    why = f"退出码 {proc.returncode}" if proc.poll() is not None else f"{START_TIMEOUT:.0f} 秒没起来"
    try:
        tail = LOG_FILE.read_bytes()[-400:].decode("utf-8", "replace").strip().replace("\n", " | ")
    except OSError:
        tail = ""
    log(f"MinerU 服务启动失败：{why} {tail}".strip())
    _stop_proc(proc)
    with _lock:
        if _proc is proc:
            _clear()
        _failed_at = time.time()
    return False


def _clear() -> None:
    """调用方拿着 _lock。"""
    global _proc, _pid, _port, _ready
    _proc, _pid, _port, _ready = None, None, None, False
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


def _stop_proc(proc) -> None:
    if proc is None:
        return
    pdfconv._kill_tree(proc)
    try:
        if hasattr(proc, "wait"):
            proc.wait(timeout=15)
    except Exception:
        pass


def _kick() -> threading.Thread | None:
    """没在跑、也没在起：后台起。返回起服务的线程（已经起着的返回 None）。"""
    global _starting
    with _lock:
        if _starting is not None and _starting.is_alive():
            return _starting
        if _running():
            return None

        def run():
            global _starting
            try:
                _start_blocking()
            finally:
                with _lock:
                    _starting = None
        _starting = threading.Thread(target=run, name="mineru-server-start", daemon=True)
        _starting.start()
        return _starting


def lobby_seen(meeting: bool = False) -> bool:
    """前端在大厅、窗口可见时的轮询（/api/current?lobby=1）。只有大厅刚从不可见变成可见（上一次隔了 LOBBY_GAP_S 以上，
    或者后端起来后第一次）、又没在开会时，才把服务起好等着；一直开着的大厅不会让服务一直占着显存（到点照样关，关了不再起）。
    返回这次有没有去起。"""
    global _lobby_at
    now = time.time()
    with _lock:
        edge = now - _lobby_at > LOBBY_GAP_S
        _lobby_at = now
    if meeting or not edge or not available():
        return False
    _adopt()
    with _lock:
        if url() or (_starting is not None and _starting.is_alive()):
            return False
        if time.time() - _failed_at < RETRY_AFTER:
            return False
    _kick()
    return True


def ensure(timeout: float = START_TIMEOUT) -> str | None:
    """要转一篇 PDF：服务没在跑就起（开会中也起），等它好；起不来 / 等不到返回 None（调用方本地转）。"""
    if not available():
        return None
    _adopt()
    u = url()
    if u:
        return u
    with _lock:
        if time.time() - _failed_at < RETRY_AFTER and not (_starting is not None and _starting.is_alive()):
            return None
    th = _kick()
    if th is not None:
        th.join(timeout)
    return url()


@contextmanager
def use(start: bool = True):
    """转一篇 PDF 期间：算忙（不会被空闲回收），给出服务地址（没有就是 None）。"""
    global _busy, _last_used
    with _lock:
        _busy += 1
    try:
        yield ensure() if start else url()
    finally:
        with _lock:
            _busy -= 1
            _last_used = time.time()


def busy() -> bool:
    with _lock:
        return _busy > 0


def stop(why: str = "") -> bool:
    """关掉（放显存）。正在转的不关。返回关没关。"""
    with _lock:
        if _busy > 0 or not _running():
            return False
        proc = _proc if _proc is not None else (type("P", (), {"pid": _pid})() if _pid else None)
        _clear()
    _stop_proc(proc)
    if why:
        log(why)
    return True


def on_meeting_start() -> None:
    """开会了：显存留给配音和转写。没在转的服务现在就关。"""
    stop("开会：已关闭 MinerU 服务，释放显存")


def reap_once(now: float | None = None) -> bool:
    """到点就关：没有转换任务够久了（起好那一刻也算一次）。大厅开不开着不算。返回关没关。"""
    now = time.time() if now is None else now
    with _lock:
        if not _running() or _busy > 0 or (_starting is not None and _starting.is_alive()):
            return False
        meeting = False
        try:
            meeting = bool(in_meeting())
        except Exception:
            pass
        limit = IDLE_MEETING_S if meeting else IDLE_S
        if now - _last_used < limit:
            return False
    mins = round(limit / 60, 1)
    return stop(f"MinerU 服务空闲 {mins:g} 分钟，已关闭，释放显存（下次导入 PDF 时自动启动）")


def _start_reaper() -> None:
    """调用方拿着 _lock。"""
    global _reaper
    if _reaper is not None and _reaper.is_alive():
        return

    def loop():
        global _reaper
        while True:
            time.sleep(15)
            try:
                reap_once()
            except Exception:
                pass
            with _lock:
                if not _running() and not (_starting is not None and _starting.is_alive()):
                    _reaper = None
                    return
    _reaper = threading.Thread(target=loop, name="mineru-server-reaper", daemon=True)
    _reaper.start()


def shutdown() -> None:
    """后端退出：关掉自己起的服务（正在转也关）。"""
    with _lock:
        proc = _proc
        if proc is None and _pid:
            proc = type("P", (), {"pid": _pid})()
        if proc is None:
            return
        _clear()
    _stop_proc(proc)


def status() -> dict:
    with _lock:
        return {"running": _running(), "ready": bool(_ready), "port": _port, "busy": _busy,
                "starting": bool(_starting is not None and _starting.is_alive())}


import atexit  # noqa: E402

atexit.register(shutdown)
