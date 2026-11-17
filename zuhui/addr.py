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

log = logging.getLogger(__name__)
WORKER = Path(__file__).with_name("addr_worker.py")


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
        threading.Thread(target=self._start, name="addr-load", daemon=True).start()

    def _start(self) -> None:
        try:
            if not self.path.exists():
                raise FileNotFoundError(str(self.path))
            errlog = open(Path.home() / ".zuhui" / "addr_worker.log", "a", encoding="utf-8")   # 模拟会几场同时开，追加不覆盖
            errlog.write(f"\n== {time.strftime('%m-%d %H:%M:%S')} 后端进程 {os.getpid()} 拉起\n")
            errlog.flush()
            self._proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-u", str(WORKER), str(self.path), self.device],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog, text=True, encoding="utf-8",
                bufsize=1, creationflags=0x08000000 if os.name == "nt" else 0)      # CREATE_NO_WINDOW
            first = self._proc.stdout.readline()
            msg = json.loads(first) if first.strip() else {"ready": False, "error": "子进程退出了，看 ~/.zuhui/addr_worker.log"}
            if not msg.get("ready"):
                raise RuntimeError(msg.get("error"))
            self.dev = msg.get("dev")
            threading.Thread(target=self._read, name="addr-read", daemon=True).start()
            self.ready = True
            if self._closed:
                self.close()
                return
            log.info("addr model ready on %s", self.dev)
        except Exception as e:                    # 没装 torch / 模型文件不在 / 子进程起不来：照旧走老办法
            self.error = repr(e)[:300]
            log.warning("addr model failed: %s", self.error)
            self.close()

    def _read(self) -> None:
        p = self._proc
        try:
            for line in p.stdout:
                try:
                    self._q.put(json.loads(line))
                except ValueError:
                    pass
        except Exception:
            pass
        self.ready = False
        self.error = self.error or "子进程退出了"

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


def get(cfg: dict) -> AddrModel | None:
    """配置里开了才加载（addr.enabled）；路径见 model_path。测试用环境变量 ZUHUI_ADDR_DEVICE 指定 cpu / cuda。"""
    a = cfg.get("addr") or {}
    if not a.get("enabled"):
        return None
    path = model_path(cfg)
    if path not in _insts:
        _insts[path] = AddrModel(path, os.environ.get("ZUHUI_ADDR_DEVICE") or a.get("device") or "auto")
    return _insts[path]
