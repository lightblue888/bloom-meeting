# -*- coding: utf-8 -*-
"""多人会里"这句该不该 Claude 接"：本机训的分类模型（2026-11-13）。

为什么：以前靠代码规则（正则、"叫了谁""刚说完多久"的判断）加快嘴回话时顺带判断，八轮下来卡在 85–93%，
错的多是多嘴：叫它停然后转问别人、老师问学生它抢答、半截话。规则一条条补，补一处坏一处。
现在拿 Opus 标好的句子训了一个分类模型（scratch_ab/sim/addr_train.py），只看：这句话、谁说的什么身份、
Claude 多久前说完、之前几句逐字稿。没有手写规则。固定考卷测试集：现行流程 92.1%，加上它拦截后 96.1%。

模型跑在单独的子进程里（addr_worker.py）：在后端进程里载入 torch 会占着全局锁几十秒，接口全卡住；
torch 本地崩溃也会带走后端。输入的拼法必须和训练时一模一样（encode，在这边拼好再发过去）。
子进程没就绪、超时、崩了，prob() 都返回 None，调用方照旧走老办法。"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import STATE

log = logging.getLogger(__name__)
WORKER = Path(__file__).with_name("addr_worker.py")
WORKER_LOG = STATE / "addr_worker.log"        # 子进程的输出、每次拉起和退出（退出码）。测试的 ZUHUI_HOME 里写自己那份


def _rc(p: subprocess.Popen | None) -> str:
    """子进程的退出码（等它最多 5 秒）。Windows 上的崩溃码写成十六进制（0xc0000005）。"""
    if p is None:
        return "未知"
    try:
        rc = p.wait(timeout=5)
    except Exception:
        return "未知"
    return hex(rc & 0xFFFFFFFF) if rc < 0 or rc > 255 else str(rc)


def ago(s: float | None) -> str:
    if s is None:
        return "Claude还没说过话"
    s = float(s)
    return "Claude刚说完" if s < 5 else "Claude半分钟内说过" if s < 30 else "Claude两分钟内说过" if s < 120 else "Claude很久没说话"


def encode(before: list[str], name: str, role: str, text: str, since_claude: float | None) -> tuple[str, str]:
    """(这句, 之前的逐字稿)。before: ["[mm:ss] 名字：话", ...]（含 Claude 说的），旧的在前。"""
    a = f"{ago(since_claude)}。{name}（{role}）说：{text}"
    return a, "\n".join(reversed([l[:120] for l in before[-6:]]))


class AddrModel:
    def __init__(self, path: str | Path, device: str = "auto", timeout: float = 1.0):
        self.path = Path(path)
        self.device = device
        self.timeout = timeout                    # 判一句一般 20 多毫秒；超过这个就不等了，照旧走老办法
        self.ready = False
        self.error: str | None = None
        self.last_ms: float | None = None
        self.dev: str | None = None
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._seq = 0
        self._proc: subprocess.Popen | None = None
        self._closed = False                      # release() 关过：还在载入的子进程起来后也马上关掉，不留孤儿
        self._stop = threading.Event()            # close() 置位：等着重试的马上不等了
        self.tries = 0                            # 重试过几次（不算第一次）
        self.gave_up = False                      # 重试用完了还是起不来（下一场会开会时 get(fresh=True) 换一个新的再试）
        threading.Thread(target=self._run, name="addr-load", daemon=True).start()

    # 子进程起不来、或者开着会退出了：过 RETRY_S 秒再拉起，最多再试 RETRIES 次。
    # 以前一次不成就整场不再试，日志里只有"子进程退出了"、没有退出码（2026-11-19 组会上开会时退出过一次）
    RETRIES = 2
    RETRY_S = 5.0

    def _run(self) -> None:
        try:
            while not self._closed:
                p, again = self._start()
                if p is not None:
                    self._read(p)                 # 一直读到子进程退出
                if self._closed or not again or self.tries >= self.RETRIES:
                    break
                self.tries += 1
                log.warning("addr model: retry %d/%d in %.0fs", self.tries, self.RETRIES, self.RETRY_S)
                if self._stop.wait(self.RETRY_S):
                    break
        finally:
            self.gave_up = not self._closed and not self.ready

    def _note(self, text: str) -> None:
        try:
            with open(WORKER_LOG, "a", encoding="utf-8") as f:
                f.write(f"== {time.strftime('%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass

    def _start(self) -> tuple[subprocess.Popen | None, bool]:
        """拉起一次子进程、等它报就绪。→ (就绪的子进程, 起不来时值不值得再试)。"""
        p = None
        try:
            if not self.path.exists():
                self.error = repr(FileNotFoundError(str(self.path)))[:300]
                log.warning("addr model failed: %s", self.error)
                return None, False                # 模型文件不在：再试也没用
            self.error = None                     # 界面上这一阵显示"加载中"
            try:
                WORKER_LOG.parent.mkdir(parents=True, exist_ok=True)
                errlog = open(WORKER_LOG, "a", encoding="utf-8")   # 模拟会几场同时开，追加不覆盖
            except OSError:
                errlog = subprocess.DEVNULL
            try:
                if errlog is not subprocess.DEVNULL:
                    errlog.write(f"\n== {time.strftime('%m-%d %H:%M:%S')} 后端进程 {os.getpid()} 拉起"
                                 + (f"（第 {self.tries} 次重试）" if self.tries else "") + "\n")
                    errlog.flush()
                p = self._proc = subprocess.Popen(
                    [sys.executable, "-X", "utf8", "-u", str(WORKER), str(self.path), self.device],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog, text=True, encoding="utf-8",
                    bufsize=1, creationflags=0x08000000 if os.name == "nt" else 0)      # CREATE_NO_WINDOW
            finally:
                if errlog is not subprocess.DEVNULL:
                    errlog.close()                # 子进程拿着自己那份句柄，这边不留着（以前每拉一次漏一个）
            first = p.stdout.readline()
            if not first.strip():
                raise RuntimeError(f"子进程退出了，退出码 {_rc(p)}（看 {WORKER_LOG}）")
            msg = json.loads(first)
            if not msg.get("ready"):
                raise RuntimeError(msg.get("error"))
            self.dev = msg.get("dev")
            self.ready = True
            if self._closed:
                self.close()
                return None, False
            log.info("addr model ready on %s", self.dev)
            return p, True
        except Exception as e:                    # 没装 torch / 子进程起不来：照旧走老办法，过几秒再试
            self.ready = False
            if self._closed:                      # 载入时被 release() 关掉的：不算起不来
                return None, False
            self.error = repr(e)[:300]
            log.warning("addr model failed: %s", self.error)
            self._note(f"起不来：{self.error}")
            if p is not None:
                if self._proc is p:
                    self._proc = None
                try:
                    p.kill()
                except Exception:
                    pass
            return None, True

    def _read(self, p: subprocess.Popen) -> None:
        try:
            for line in p.stdout:
                try:
                    self._q.put(json.loads(line))
                except ValueError:
                    pass
        except Exception:
            pass
        self.ready = False
        if self._proc is p:
            self._proc = None                     # 退出了：散会释放显存时不算它
        if self._closed:
            return
        self.error = f"子进程退出了，退出码 {_rc(p)}"
        log.warning("addr model exited: %s", self.error)
        self._note(self.error)

    def prob(self, before: list[str], name: str, role: str, text: str, since_claude: float | None) -> float | None:
        """该接的概率（0–1）；模型没好、超时、出错都返回 None。"""
        if not self.ready or self._proc is None:
            return None
        t0 = time.perf_counter()
        a, b = encode(before, name, role, text, since_claude)
        with self._lock:
            while not self._q.empty():            # 上一句超时后才到的回答：扔掉
                self._q.get_nowait()
            self._seq += 1
            sid = self._seq
            try:
                self._proc.stdin.write(json.dumps({"id": sid, "a": a, "b": b}, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                self.ready, self.error = False, repr(e)[:300]
                return None
            while True:
                left = t0 + self.timeout - time.perf_counter()
                if left <= 0:
                    return None
                try:
                    r = self._q.get(timeout=left)
                except queue.Empty:
                    return None
                if r.get("id") == sid:
                    break
        self.last_ms = (time.perf_counter() - t0) * 1000
        return float(r["p"]) if "p" in r else None

    def close(self) -> None:
        self._closed = True
        self._stop.set()
        self.ready = False
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass


_insts: dict[str, AddrModel] = {}


def release() -> int:
    """散会后空闲一阵：关掉已载入的分类模型子进程，把显存还回去（下次开会 get() 重新载入）。返回关掉了几个。"""
    n = 0
    for k in list(_insts):
        m = _insts.pop(k, None)
        if m is None:
            continue
        if m._proc is not None:
            n += 1
        m.close()
    return n


def model_path(cfg: dict) -> str:
    """按会议语言挑模型：英文组会用 addr_en（中英双语底座训的），没有就退回中文那个；中文会用 addr。"""
    a = cfg.get("addr") or {}
    zh = a.get("path") or str(Path.home() / ".zuhui" / "models" / "addr")
    if str((cfg.get("meeting") or {}).get("lang") or "zh") == "en":
        en = a.get("path_en") or str(Path.home() / ".zuhui" / "models" / "addr_en")
        if Path(en).exists():
            return en
    return zh


def get(cfg: dict, fresh: bool = False) -> AddrModel | None:
    """配置里开了才加载（addr.enabled）；路径见 model_path。测试用环境变量 ZUHUI_ADDR_DEVICE 指定 cpu / cuda。
    fresh=True（开会时）：上次重试用完了还没起来的，换一个新的从头再试。"""
    a = cfg.get("addr") or {}
    if not a.get("enabled"):
        return None
    path = model_path(cfg)
    old = _insts.get(path)
    if fresh and old is not None and old.gave_up:
        old.close()
        _insts.pop(path, None)
    if path not in _insts:
        _insts[path] = AddrModel(path, os.environ.get("ZUHUI_ADDR_DEVICE") or a.get("device") or "auto")
    return _insts[path]
