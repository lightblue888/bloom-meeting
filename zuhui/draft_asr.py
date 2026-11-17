# -*- coding: utf-8 -*-
"""快识别（初稿）的后端这一头：拉起 draft_asr_worker.py（GPT-SoVITS 的 python，CPU），按 id 收发请求。

用法：你一停顿（还没等到 Fun-ASR 的准确文字），耳朵把还没转写的那截录音交给它，约 0.2 秒出初稿，
让模型先按初稿想着；Fun-ASR 的结果到了，draft_match.same_meaning() 判是不是同一句，是就直接用想好的回话。
起不来（没有整合包、模型、pypinyin）就什么都不做，和以前一样。配置 cfg["ears"]["draft"]：
    {"enabled": true, "python": "", "model": "", "threads": 4}   空 = 用 voice.json 的 gsv.python / 整合包自带模型"""
from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from .config import ROOT, STATE, local_path

log = logging.getLogger("zuhui.draft")

WORKER = ROOT / "zuhui" / "draft_asr_worker.py"
GSV_DIR = local_path("gsv_dir")             # GPT-SoVITS 整合包（带 Paraformer 模型）；空 = 不开初稿抢跑
MODEL_REL = r"tools\asr\models\speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
CREATE_NO_WINDOW = 0x08000000


def _gsv() -> dict:
    try:
        with open(STATE / "voice.json", encoding="utf-8-sig") as f:
            return (json.load(f) or {}).get("gsv") or {}
    except Exception:
        return {}


class DraftASR:
    def __init__(self, cfg: dict | None = None):
        d = dict(((cfg or {}).get("ears") or {}).get("draft") or {})
        g = _gsv()
        root = str(g.get("dir") or GSV_DIR)
        self.enabled = d.get("enabled", True) is not False
        self.python = str(d.get("python") or g.get("python") or os.path.join(root, "runtime", "python.exe"))
        self.model = str(d.get("model") or os.path.join(root, MODEL_REL))
        self.threads = int(d.get("threads") or 4)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._wait: dict[int, list] = {}         # id -> [Event, 回答]
        self._ready = threading.Event()
        self._starting = False
        self.error: str | None = None

    # ---------------------------------------------------------------- 进程

    @property
    def ready(self) -> bool:
        p = self._proc
        return self._ready.is_set() and p is not None and p.poll() is None

    def start(self) -> None:
        """后台拉起（加载约 5-8 秒）；已经在跑或正在起就不管。"""
        if not self.enabled or self.ready or self._starting:
            return
        if not (os.path.exists(self.python) and os.path.isdir(self.model) and WORKER.exists()):
            self.error = f"快识别没装：{self.python} / {self.model}"
            log.info(self.error)
            return
        self._starting = True
        threading.Thread(target=self._launch, name="draft-asr-start", daemon=True).start()

    def _launch(self) -> None:
        try:
            self._ready.clear()
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = ""
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
                env.pop(k, None)
            logf = open(STATE / "draft_asr.log", "a", encoding="utf-8", errors="replace")
            logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start =====\n")
            logf.flush()
            # -I：不读用户 site-packages 和 PYTHON* 变量（和 GPT-SoVITS 自己的启动方式一样）
            self._proc = subprocess.Popen([self.python, "-I", "-X", "utf8", "-u", str(WORKER), self.model, str(self.threads)],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logf,
                                          cwd=os.path.dirname(self.model), env=env, creationflags=CREATE_NO_WINDOW)
            threading.Thread(target=self._read, args=(self._proc,), name="draft-asr-read", daemon=True).start()
            if not self._ready.wait(90):
                self.error = "快识别 90 秒没起来"
                log.warning(self.error)
        except Exception as e:
            self.error = repr(e)
            log.warning("快识别起不来：%r", e)
        finally:
            self._starting = False

    def _read(self, proc) -> None:
        for raw in proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            if msg.get("ready"):
                log.info("快识别就绪（pid %s，加载 %ss）", msg.get("pid"), msg.get("load_s"))
                self._ready.set()
                continue
            w = self._wait.pop(msg.get("id"), None)
            if w is not None:
                w[1] = msg
                w[0].set()
        self._ready.clear()
        for w in list(self._wait.values()):
            w[0].set()
        self._wait.clear()

    def stop(self) -> None:
        p, self._proc = self._proc, None
        self._ready.clear()
        if p is not None and p.poll() is None:
            try:
                p.stdin.write(b'{"op":"quit"}\n')
                p.stdin.flush()
                p.wait(3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    # ---------------------------------------------------------------- 请求

    def _call(self, req: dict, timeout: float) -> dict | None:
        if not self.ready:
            return None
        rid = next(self._ids)
        req["id"] = rid
        w = [threading.Event(), None]
        self._wait[rid] = w
        try:
            with self._lock:
                self._proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
                self._proc.stdin.flush()
        except Exception:
            self._wait.pop(rid, None)
            return None
        if not w[0].wait(timeout):
            self._wait.pop(rid, None)
            return None
        return w[1]

    def asr(self, pcm: str | Path, s: float, e: float, timeout: float = 2.0) -> tuple[str, int] | None:
        """录音 pcm（16 kHz s16le）里 s..e 秒的初稿 → (文字, 毫秒)；没起来、超时、出错 → None。"""
        r = self._call({"op": "asr", "pcm": str(pcm), "s": round(s, 3), "e": round(e, 3)}, timeout)
        if not r or "error" in r:
            return None
        return r.get("text") or "", int(r.get("ms") or 0)

    def asr_pcm(self, pcm: bytes, timeout: float = 2.0) -> tuple[str, int] | None:
        """内存里的一段（16 kHz s16le 单声道）的初稿；访客那一路用。"""
        import base64
        r = self._call({"op": "asr", "b64": base64.b64encode(pcm).decode("ascii")}, timeout)
        if not r or "error" in r:
            return None
        return r.get("text") or "", int(r.get("ms") or 0)

    def pinyin(self, text: str) -> list[str]:
        r = self._call({"op": "pinyin", "text": text}, 0.5)
        if not r or "pinyin" not in r:
            raise RuntimeError("pinyin unavailable")
        return r["pinyin"]
