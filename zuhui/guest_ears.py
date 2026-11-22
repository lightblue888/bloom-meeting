# -*- coding: utf-8 -*-
"""访客的话直接转写：会议语音桥（bridge.py）把每位访客自己那一路声音（LiveKit 订阅下来，16 kHz 单声道）喂进来，
按人分段、转写，转出来的字交给 app.py 的 _guest_segment（按停顿并句、整形、记进逐字稿、决定接不接话）。

为什么：以前靠主持人的麦克风录到扬声器放出来的访客声音，由主持人的实时转写转出来、再按时间认成访客；
主持人戴耳机（AirPods Max）时麦克风根本听不到访客，访客说什么都没有。

- 分段：每位访客一个 Silero 判停（和耳朵同一个模型，onnxruntime 单线程，几位访客共用一个会话、各留各的状态），
  开口时带上前 0.3 秒；停顿 END_S 秒算一段说完。一段已经说了 PIECE_MIN_S 秒以上，停顿 PIECE_S 秒就先切下来送去转
  （人多半还要接着说，后面的另起一段：长话不用等全说完才开始转）。一段最长 MAX_S 秒，到了就在最近 3 秒里声音最小的地方切。
- 抢先转：短的一段（不到 PIECE_MIN_S 秒）停顿到 SPEC_S 秒就先送去转；停到 END_S 秒还没再开口，就直接用这次的结果，又开口了就作废。
  长的那段不抢：它停 PIECE_S 秒本来就切下来送去转，抢先只早 0.1 秒，他一接着说就白转一整段（转写进程只有一个，
  白转的那几秒后面的段全得排着；2026-11-14 那场手机访客停顿多，积压到 20 秒）。
- 判停的时间线（给 app.py 判"这一轮说完没有"、出声那道门用，不靠字什么时候转出来）：每段第一个有声帧 vs、最后一个 we、
  转完没有（spans）；累计出声多久（voiced_since）；最近的停顿（quiet_need：他句中停顿的 85 分位数，夹在 0.8～2 秒）。
- 积压：按最近几段的转写速度估排着的要转多久（AsrWorker.backlog_s），超过 lite_backlog_s 秒，新送的段少带热词
  （干活进程里另一个识别器，见 guest_asr_worker.py）。每段的转写用时 / 语音时长（rtf）记进 info 和日志。
- 转写：一个常驻的干活进程（zuhui/guest_asr_worker.py：Fun-ASR-Nano int8，Blurt 的 uv 环境，低优先级，≤4 线程），
  第一位访客进来时起，模型只加载一次；没有访客 IDLE_STOP_S 秒后停掉，省内存。一次只送一段，排着的段作废了就不送。
- 访客连续出声 BARGE_S 秒：通知一次 on_barge（学生正在出声就停下来听）。学生正在出声时访客那一路的声音比他平时说话小一半以上，
  多半是他开着外放、学生的声音漏回来了，不算插话。

实测（2026-11-11，金标准 32 句 / 199 秒你的真实录音；混合错误率 = 中文按字、英文按词，参考和结果过同样的整形）：
  直接喂模型、整句（不经网络）    带 24 个热词 0.80%，不带 0.93%；转写约 0.25 秒 / 每秒语音（带热词，4 线程），不带约 0.20
  经中继 LiveKit（Opus）端到端    带热词 1.5%～2.1%，不带 2.9%（不带热词时"DeepSeek V4 Pro"这种英文名会整个丢掉）
  说完 → 出字（端到端，从他最后一个字算起）
                                    处理器有些别的负载（转写约 0.37 秒 / 每秒语音）：中位数 1.8 秒，4～6.5 秒的句子 1.3～2.2 秒；
                                    按不忙时的速度推算，5 秒一句 ≈ 0.3（判停）+ 1.1～1.3（转写）+ 0.1（网络）≈ 1.5～1.7 秒；
                                    别的进程把处理器占到 70%～90% 时，低优先级的转写排队，中位数 4.6 秒、最慢二十几秒，
                                    改普通优先级（guest_asr.priority = "normal"）中位数 2.1 秒
  热词每段固定多算约 0.3～0.45 秒（24 个）。只带 12 个（同一时间交替测，20 句）：快约 8%，错误率 0.67% → 1.12%，
  所以默认还是 24 个（guest_asr.hotwords 可以改成数字 / 列表 / False）。
SenseVoice 本机没有；SeACo-Paraformer 要 torch、进程 5 GB 以上，开会时不合适。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import components
from .config import STATE

log = logging.getLogger(__name__)

SR = 16000
FRAME = 512                         # Silero 一帧 32 毫秒
FRAME_S = FRAME / SR
HERE = Path(__file__).resolve().parent
UV_WITH = components.UV_WITH
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_BELOW_NORMAL = 0x00004000 if os.name == "nt" else 0

# 默认值（config 的 guest_asr 一节可以改；priority: "normal" 改普通优先级）。实测见文件头
DEFAULTS = {
    "enabled": True,
    "threads": 4,
    "end_s": 0.7,          # 停顿这么久 = 一段说完
    "spec_s": 0.3,         # 短的一段停顿这么久就抢先送去转
    "piece_min_s": 3.0,    # 说了这么久以上的一段……
    "piece_s": 0.4,        # ……停顿这么久就先切下来转
    "max_s": 12.0,         # 一段最长
    "barge_s": 0.6,        # 连续出声这么久 = 在插话
    "idle_stop_s": 120.0,  # 没有访客这么久就停掉干活进程
    "hotwords": True,      # 带 24 个热词（软件名 + 论文术语，和散会后重听同一份）：英文名字准得多，每段多转一点（见文件头）
    "hotwords_lite": 8,    # 积压时这一段只带几个热词（软件名在前）；False = 积压时也照常带
    "lite_backlog_s": 3.0, # 排着要转的超过这么多秒算积压
    "priority": "normal",  # 2026-11-11 主会话定：访客说完到出字要快，开会时别被别的程序挤到后面（低优先级时满载会积压到十几秒）
}
QUIET_MIN_S, QUIET_MAX_S = 0.8, 2.0  # 出声那道门：访客至少静这么久才开口（按他句中停顿的 85 分位数，夹在这中间）
PAUSE_LO, PAUSE_HI = 0.2, 3.0        # 算"句中停顿"的空当（两个有声帧之间）：短于这个是字间，长于这个多半是在等回话
TH_ON, TH_OFF = 0.5, 0.35           # Silero：过 0.5 算开口，说话中掉到 0.35 以下才算停
START_FRAMES = 2                    # 连续两帧（64 毫秒）过线才算开口
PRE_FRAMES = 10                     # 开口前带上 0.32 秒
TAIL_FRAMES = 6                     # 结尾多留 0.19 秒
MIN_VOICED_S = 0.25                 # 有声不到这么久的一段（咳嗽、碰麦）不转
DRAFT_S = 0.15                      # 停顿这么久（smart-turn 也说多半说完了）就先出初稿，让模型先想着


def _model_dir() -> Path:
    """转写模型：训练流程验收过的那份（~/.zuhui/asr_train/active.json，同一个模型换了更好的量化）；没有就用 Blurt 的。"""
    try:
        a = json.loads((STATE / "asr_train" / "active.json").read_text(encoding="utf-8"))
        if a.get("engine") == "fun-asr-nano" and a.get("model_dir") and (Path(a["model_dir"]) / "llm.int8.onnx").exists():
            return Path(a["model_dir"])
    except (OSError, ValueError):
        pass
    return Path(os.environ.get("BLURT_HOME") or Path.home() / ".blurt") / "models" / "fun-asr-nano-int8"


def _hotwords(n: int = 24) -> list[str]:
    """热词：软件名（Claude、DeepSeek、Opus、Bloom……）+ 当前论文的术语，n=24 时和散会后重听（retranscribe）同一份。
    每个热词都占模型的提示、每段都要多算一遍：24 个每段多 0.3～0.45 秒；只带 12 个快约 8%，但错字多（见文件头）。"""
    try:
        if n >= 24:
            from .retranscribe import _hotwords as full
            return full()
        from .lexicon import CURRENT, merged_hotwords
        try:
            paper = json.loads(CURRENT.read_text(encoding="utf-8")).get("hotwords") or []
        except (OSError, ValueError):
            paper = []
        return merged_hotwords(paper, n=n)
    except Exception:
        return []


class AsrWorker:
    """一个常驻的转写进程。submit(16 kHz int16, cb) → 转完在读结果的线程里 cb(text|None, ms)。一次只有一段在转。
    lite_hotwords（列表）：干活进程里再建一个只带这几个热词的识别器，积压时 submit(..., lite=True) 用它（快约 12%，见文件头）。
    作废的段：还没送出去的直接不送；正在转的停不下来（sherpa-onnx 的 decode_stream 是一次阻塞调用，没有中止的口子，
    只能杀进程，重新加载模型要 7 秒以上，比转完还慢），转完把结果扔掉——所以长段不抢先转，白转的最多一段 3 秒。"""

    RTF0 = 0.5                                      # 还没转过时按这个速度估积压

    def __init__(self, model_dir: Path, threads: int = 4, hotwords: list[str] | None = None, normal_priority: bool = False,
                 lite_hotwords: list[str] | None = None):
        self.model_dir, self.threads, self.hotwords = Path(model_dir), min(4, max(1, int(threads))), list(hotwords or [])
        self.normal_priority = bool(normal_priority)      # 默认低优先级（BELOW_NORMAL）
        self.lite_hotwords = None if lite_hotwords is None else list(lite_hotwords)
        self.lite_ready = False                     # 干活进程说少带热词的那个识别器建好了
        self.ready = False
        self.error: str | None = None
        self.load_s: float | None = None
        self.rtf: float | None = None               # 最近几段的转写用时 / 语音时长（滑动平均）
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._todo: deque = deque()                 # (id, int16 bytes, samples, cb, lite) 还没送出去的
        self._busy: tuple | None = None             # (id, cb, samples, 送出去的时刻) 正在转的
        self._dropped: set[int] = set()
        self._n = 0
        self._offline = True                        # uv 先只用缓存起（不碰网）；起不来再联网起一次（_read 末尾）

    def _cmd(self, job: Path) -> list[str]:
        return [*components.uv_run(self._offline), "python", "-I", "-X", "utf8", str(HERE / "guest_asr_worker.py"), str(job)]

    def backlog_s(self) -> float:
        """排着的 + 正在转的，按最近的速度估还要转多少秒。"""
        r = self.rtf or self.RTF0
        with self._lock:
            q = sum(x[2] for x in self._todo) / SR * r
            if self._busy is not None:
                q += max(0.0, self._busy[2] / SR * r - (time.time() - self._busy[3]))
        return q

    def start(self) -> None:
        work = STATE / "tmp" / "guest_asr"
        work.mkdir(parents=True, exist_ok=True)
        job = work / f"job.{os.getpid()}.json"
        jd = {"model_dir": str(self.model_dir), "threads": self.threads, "hotwords": self.hotwords,
              "normal_priority": self.normal_priority}
        if self.lite_hotwords is not None and self.lite_hotwords != self.hotwords:
            jd["hotwords_lite"] = self.lite_hotwords
        job.write_text(json.dumps(jd, ensure_ascii=False), encoding="utf-8")
        cmd = self._cmd(job)
        self._t0 = time.time()
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=open(work / "worker.log", "a", encoding="utf-8"),
                                          cwd=str(work),
                                          creationflags=_NO_WINDOW | (0 if self.normal_priority else _BELOW_NORMAL))
        except OSError as e:
            self.error = f"转写进程起不来：{e!r}"
            log.warning(self.error)
            return
        threading.Thread(target=self._read, name="guest-asr-read", daemon=True).start()

    def stop(self) -> None:
        p, self._proc = self._proc, None
        with self._lock:
            todo, self._todo = list(self._todo), deque()
            busy, self._busy = self._busy, None
        for item in todo:
            self._call(item[3], None, None)
        if busy:
            self._call(busy[1], None, None)
        if p is not None and p.poll() is None:
            try:
                p.stdin.write(b'{"cmd": "quit"}\n')
                p.stdin.flush()
                p.wait(3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def submit(self, pcm, cb, lite: bool = False) -> int:
        """pcm：16 kHz 单声道 int16 的 numpy 数组。lite：用少带热词的识别器（干活进程建好了才用，没建好照常转）。"""
        with self._lock:
            self._n += 1
            rid = self._n
            self._todo.append((rid, pcm.astype("<i2").tobytes(), int(len(pcm)), cb, bool(lite)))
        self._pump()
        return rid

    def drop(self, rid: int) -> None:
        """这一段不要了：还没送出去就不送；正在转的转完扔掉结果。"""
        with self._lock:
            for i, item in enumerate(self._todo):
                if item[0] == rid:
                    del self._todo[i]
                    return
            if self._busy and self._busy[0] == rid:
                self._dropped.add(rid)

    def pending(self) -> int:
        with self._lock:
            return len(self._todo) + (1 if self._busy else 0)

    def _pump(self) -> None:
        with self._lock:
            p = self._proc
            if not self.ready or self._busy is not None or not self._todo or p is None:
                return
            rid, raw, n, cb, lite = self._todo.popleft()
            self._busy = (rid, cb, n, time.time())
            head = {"id": rid, "samples": n}
            if lite and self.lite_ready:
                head["hw"] = "lite"
            try:
                p.stdin.write((json.dumps(head) + "\n").encode() + raw)
                p.stdin.flush()
            except (OSError, ValueError) as e:
                self._busy = None
                self.error = f"转写进程写不进去：{e!r}"
                todo, self._todo = list(self._todo), deque()
                cb_fail = [cb] + [x[3] for x in todo]
            else:
                return
        for c in cb_fail:
            self._call(c, None, None)

    @staticmethod
    def _call(cb, text, ms) -> None:
        try:
            cb(text, ms)
        except Exception:
            log.exception("访客转写回调出错")

    def _read(self) -> None:
        p = self._proc
        heard = False
        try:
            for raw in p.stdout:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                heard = True
                if "ready" in r:
                    if not r.get("ready"):
                        self.error = f"转写模型没加载起来：{r.get('error')}"
                        log.warning(self.error)
                        break
                    self.ready, self.load_s = True, round(time.time() - self._t0, 1)
                    log.info("访客转写：模型加载好了（%.1f 秒，%s）", self.load_s, self.model_dir.name)
                    self._pump()
                    continue
                if "lite" in r and "id" not in r:          # 少带热词的识别器建好了（或者建不起来）
                    self.lite_ready = bool(r.get("lite"))
                    log.info("访客转写：积压时用的少热词识别器%s", "建好了" if self.lite_ready else f"没建起来：{r.get('error')}")
                    continue
                with self._lock:
                    busy, self._busy = self._busy, None
                    dropped = busy is not None and busy[0] in self._dropped
                    self._dropped.discard(busy[0] if busy else -1)
                    try:                                   # 转写速度：长于半秒的段才算（太短的固定开销占大头）
                        if busy is not None and r.get("ms") and busy[2] >= SR // 2:
                            x = float(r["ms"]) / 1000.0 / (busy[2] / SR)
                            self.rtf = x if self.rtf is None else 0.7 * self.rtf + 0.3 * x
                    except (TypeError, ValueError):
                        pass
                if busy is not None and busy[0] == r.get("id") and not dropped:
                    self._call(busy[1], None if r.get("error") else (r.get("text") or ""), r.get("ms"))
                self._pump()
        except Exception as e:
            self.error = f"读转写结果出错：{e!r}"
        if self._proc is p and not heard and self._offline and self.error is None:
            # 一句话没说就退了：多半是 uv 缓存里没有 sherpa-onnx（新装的机器、缓存清过），联网再起一次；排着的段留着
            self._offline = False
            log.warning("访客转写：只用缓存起不来，联网再起一次（见 %s）", STATE / "tmp" / "guest_asr" / "worker.log")
            self.start()
            return
        if self._proc is p and self.error is None:
            self.error = "转写进程退出了"
        with self._lock:                            # 没转完的都算失败（文字为 None）
            todo, self._todo = list(self._todo), deque()
            busy, self._busy = self._busy, None
        for c in ([busy[1]] if busy else []) + [x[3] for x in todo]:
            self._call(c, None, None)


class _Silero:
    """Silero 判停（~/.blurt/models/silero_vad.onnx，v4 / v5 都认）。几位访客共用一个会话，每人各自的状态。"""

    def __init__(self):
        import numpy as np
        self.np = np
        self.sess = None
        try:
            import onnxruntime as ort
            model = Path(os.environ.get("BLURT_HOME") or Path.home() / ".blurt") / "models" / "silero_vad.onnx"
            if model.exists():
                so = ort.SessionOptions()
                so.intra_op_num_threads = so.inter_op_num_threads = 1
                self.sess = ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
                self.names = {i.name for i in self.sess.get_inputs()}
        except Exception as e:
            log.warning("访客判停：Silero 起不来（%r），退回音量门限", e)
            self.sess = None

    def state(self):
        np = self.np
        if self.sess is not None and "state" in self.names:
            return {"state": np.zeros((2, 1, 128), np.float32)}
        return {"h": np.zeros((2, 1, 64), np.float32), "c": np.zeros((2, 1, 64), np.float32)}

    def prob(self, frame, st: dict) -> float:
        np = self.np
        if self.sess is None:                       # 没有模型：音量门限（浏览器已经降过噪）
            return 1.0 if float(np.sqrt((frame * frame).mean())) > 0.015 else 0.0
        x = frame.reshape(1, -1)
        if "state" in self.names:
            out, st["state"] = self.sess.run(None, {"input": x, "state": st["state"], "sr": np.array(SR, np.int64)})[:2]
        else:
            feeds = {"x" if "x" in self.names else "input": x, "h": st["h"], "c": st["c"]}
            if "sr" in self.names:
                feeds["sr"] = np.array(SR, np.int64)
            out, st["h"], st["c"] = self.sess.run(None, feeds)[:3]
        return float(np.asarray(out).reshape(-1)[0])


class _Guest:
    def __init__(self, ident: str, name: str, vad_state: dict):
        self.ident, self.name, self.st = ident, name, vad_state
        self.rest = None                            # 不够一帧的尾巴（int16）
        self.pre: deque = deque(maxlen=PRE_FRAMES)  # 开口前的几帧
        self.frames: list = []                      # 这一段：(int16 帧, 概率, 帧末的墙上时间)
        self.on = False                             # 正在说
        self.run = 0                                # 没开口时连续过线几帧
        self.last_v = -1                            # 这一段最后一个有声帧的下标
        self.sil = 0                                # 最后一个有声帧之后几帧没声
        self.voiced = 0                             # 这一段有声帧数
        self.vrun = 0.0                             # 连续出声多久（中间短停 0.25 秒内不算断）
        self.barged = False
        self.spec: dict | None = None               # 抢先转的那次：{"rid","end","text","done","final"}
        self.last_voice_t = 0.0
        self.last_frame_t = 0.0
        self.waiting = 0                            # 送去转、还没交出文字的段数
        self.seq = 0
        self.lvl = None                             # 他平时说话的音量（学生没出声时有声帧的 RMS，慢慢跟）
        self.run_rms: list[float] = []              # 这一阵连续出声每帧的 RMS
        self.draft_v = -2                           # 出过初稿的那次停顿（最后有声帧的下标）
        self.draft_seq = 0                          # 每次出初稿 / 作废 +1：晚到的初稿认不认
        self.draft_live = False                     # 报过一次初稿、还没作废
        self.vs_open: float | None = None           # 这一段第一个有声帧的时刻
        self.vcum = 0.0                             # 一共出声多少秒
        self.vhist: deque = deque(maxlen=6000)      # (有声帧时刻, 到这一帧累计出声秒数)：算某时刻之后又说了多久
        self.spans: deque = deque(maxlen=300)       # 送去转的每一段：{"seq","vs","we","voiced","done"}
        self.pauses: deque = deque(maxlen=150)      # 最近的句中停顿（秒）：出声那道门按它自适应
        self.host_sims: deque = deque(maxlen=8)     # 最近几段和主持人声纹的余弦（认"主持人在用这台设备参会"）


# 主持人用另一台设备（手机）以访客身份进会：这位访客的声音和主持人登记的声纹（~/.zuhui/voiceprint.json，耳朵同一个 CAM++）对得上，
# 就记进 ears.PLAYBACK.host_elsewhere —— 家里那台电脑的麦克风不再当主持人的话（只会录到扬声器的回声），也不再推给访客。
# 离线（2026-11-15，tests/echo_replay.py 同一批数据）：主持人自己的话过 Opus 16 kbps 编码 中位 0.55、最低 0.33（110 段）；
# 2026-11-14 那场 x（就是主持人）经手机→扬声器→麦克风 中位 0.52、最低 0.33（158 段）；别的访客经扬声器 ≤ 0.23，学生的配音 ≤ 0.19。
HOST_SIM = 0.35         # 一段像主持人
HOST_NEED = 3           # 至少这么多段像、最近几段的中位数也过线，才认定
HOST_MIN_S = 1.25       # 有声不到这么久的段不判


def _pct(xs, q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


class GuestEars:
    """每位访客的声音 → 分段 → 转写 → on_text(名字, 文字, info)。info：ws/we（这段话的墙上起止）、dur、asr_ms、lag（最后一个有声帧
    到文字出来）、spec（用的是抢先转的结果）、piece（长话中途切下来的一块）。"""

    def __init__(self, cfg: dict | None = None, on_text=None, on_barge=None, audible=None):
        c = dict(DEFAULTS)
        c.update((cfg or {}).get("guest_asr") or {})
        self.c = c
        self.enabled = c.get("enabled", True) is not False
        self.on_text = on_text
        self.on_barge = on_barge
        self.audible = audible or (lambda: False)    # 学生的声音此刻在放吗（访客那边外放、回声漏回来时别当成插话）
        self.recent: deque = deque(maxlen=60)        # 最近转出来的段：(名字, 起, 止, 文字)
        self.worker: AsrWorker | None = None
        self._vad: _Silero | None = None
        self._q: queue.Queue = queue.Queue(maxsize=6000)
        self._guests: dict[str, _Guest] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_guest = 0.0
        self._fail_at = 0.0
        self.stats: deque = deque(maxlen=300)       # 每段：{"name","dur","asr_ms","lag","spec","piece","chars"}
        self.draft = None                            # 快识别（DraftASR，和你那一路共用）；None = 不出初稿
        self.on_draft = None                         # on_draft(名字, 初稿 或 None=作废, info)
        self.host_check = c.get("host_voice", True) is not False   # 认"这位访客就是主持人本人"（见 HOST_SIM）
        self._hq: queue.Queue = queue.Queue(maxsize=20)
        self._ht: threading.Thread | None = None

    # ---------- 对外（桥的线程里调，都不阻塞） ----------
    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="guest-ears", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(("stop",))
        except queue.Full:
            pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        w, self.worker = self.worker, None
        if w is not None:
            w.stop()

    def joined(self, ident: str, name: str) -> None:
        """一位访客的声音订阅上了：干活进程先起来（加载模型要几秒，等他开口时已经好了）。"""
        self._put(("join", ident, name))

    def left(self, ident: str) -> None:
        self._put(("left", ident))

    def feed(self, ident: str, name: str, pcm: bytes, t: float) -> None:
        """一小块声音（16 kHz 单声道 s16le），t = 收到它的墙上时间。"""
        self._put(("audio", ident, name, pcm, t))

    @property
    def healthy(self) -> bool:
        """直接转写这条路能用（干活进程在跑或者正在起）。起不来就是 False，桥退回老办法（麦克风录到的认成访客）。"""
        if not self.enabled or not (self._thread and self._thread.is_alive()):
            return False
        w = self.worker
        return w is None or w.error is None

    def _now(self, g: _Guest, now: float) -> float:
        """判停处理到的时刻：处理跟不上（排着半秒以上的声音还没判）时只算到判过的那一帧，没判的不能当成静音。"""
        return now if self._q.qsize() < 50 or not g.last_frame_t else min(now, g.last_frame_t)

    def talking(self, within: float = 0.5) -> list[str]:
        """这会儿正在出声的访客：最后一个有声帧离现在不到 within 秒。
        只看最后一个有声帧（以前还要求"一段没说完"：停 0.4 秒长话就切块、段就关了，within 形同虚设，0.4 秒的换气就算没在说）。"""
        now = time.time()
        with self._lock:
            return [g.name for g in self._guests.values() if g.last_voice_t and self._now(g, now) - g.last_voice_t < within]

    def names(self) -> list[str]:
        """在会上（订阅上了声音）的访客名字。"""
        with self._lock:
            return list({g.name for g in self._guests.values()})

    def last_voice(self, name: str) -> float:
        """这位访客最后一个有声帧的时刻（没出过声 0）。"""
        with self._lock:
            return max([g.last_voice_t for g in self._guests.values() if g.name == name] or [0.0])

    def quiet_for(self, name: str) -> float:
        """这位访客已经静了多久（判停处理到的时刻算起；没出过声 = 很久）。"""
        now = time.time()
        with self._lock:
            gs = [g for g in self._guests.values() if g.name == name and g.last_voice_t]
            return min([self._now(g, now) - g.last_voice_t for g in gs] or [1e9])

    def voiced_since(self, name: str, t: float) -> float:
        """这位访客 t 之后一共出声了多少秒（判停的有声帧）。"""
        tot = 0.0
        with self._lock:
            for g in self._guests.values():
                if g.name != name or not g.vhist:
                    continue
                base = None
                for tt, cum in reversed(g.vhist):
                    if tt <= t:
                        base = cum
                        break
                if base is None:                    # t 比记着的都早：记着的全算
                    base = g.vhist[0][1] - FRAME_S
                tot += max(0.0, g.vcum - base)
        return tot

    def after(self, name: str, t: float, min_voiced: float = MIN_VOICED_S) -> dict | None:
        """t 之后这位访客又开口的第一段：{"vs": 第一个有声帧, "seq": 送去转的编号（还在说、没送 = None）, "done": 字出来了没有}。
        有声不到 min_voiced 秒、还在说的那段不算（咳嗽、碰麦）；没再开口 = None。"""
        with self._lock:
            best = None
            for g in self._guests.values():
                if g.name != name:
                    continue
                for s in g.spans:
                    if s["vs"] > t + 0.02 and (best is None or s["vs"] < best["vs"]):
                        best = {"vs": s["vs"], "we": s["we"], "seq": s["seq"], "done": s["done"]}
                        break
                if (g.on and g.vs_open is not None and g.vs_open > t + 0.02 and g.voiced * FRAME_S >= min_voiced
                        and (best is None or g.vs_open < best["vs"])):
                    best = {"vs": g.vs_open, "we": g.last_voice_t, "seq": None, "done": False}
            return best

    def quiet_need(self, name: str) -> float:
        """出声那道门：这位访客至少静多久才算停下来——他句中停顿的 85 分位数，夹在 QUIET_MIN_S～QUIET_MAX_S；
        停顿样本不到 8 个按 QUIET_MIN_S。手机那场（2026-11-14）句中停顿 0.3～1.9 秒，按 0.5 秒开口次次插在换气里。"""
        with self._lock:
            ps = [p for g in self._guests.values() if g.name == name for p in g.pauses]
        if len(ps) < 8:
            return QUIET_MIN_S
        return min(QUIET_MAX_S, max(QUIET_MIN_S, _pct(ps, 0.85)))

    def busy(self, name: str) -> bool:
        """这位访客还在说（一段没说完），或者有话送去转了还没出字：先别把他的话当成说完了。"""
        now = time.time()
        with self._lock:
            return any(g.name == name and ((g.last_voice_t and self._now(g, now) - g.last_voice_t < 1.0) or g.waiting > 0)
                       for g in self._guests.values())

    def pending(self, name: str) -> int:
        """这位访客送去转、还没出字的段数。"""
        with self._lock:
            return sum(g.waiting for g in self._guests.values() if g.name == name)

    def texts_near(self, name: str, ws: float, we: float, slack: float = 1.0) -> list[str]:
        """这位访客在 [ws, we] 前后直接转出来的字（耳朵拿麦克风那一路的同一段话来比对）。"""
        return [t for n, s, e, t in list(self.recent) if n == name and s - slack < we and e + slack > ws]

    # ---------- 线程 ----------
    def _put(self, item) -> None:
        if not self.enabled:
            return
        try:
            self._q.put_nowait(item)
        except queue.Full:                          # 线程卡住了：丢这一块，不能卡住桥
            pass

    def _loop(self) -> None:
        import numpy as np
        self._np = np
        try:
            self._vad = _Silero()
        except Exception as e:
            log.warning("访客判停起不来：%r", e)
            return
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.1)
            except queue.Empty:
                item = None
            try:
                if item is None:
                    self._tick()
                    continue
                kind = item[0]
                if kind == "stop":
                    break
                if kind == "audio":
                    self._audio(*item[1:])
                elif kind == "join":
                    self._guest(item[1], item[2])
                    self._ensure_worker()
                elif kind == "left":
                    self._left(item[1])
                if self._q.empty():
                    self._tick()
            except Exception:
                log.exception("访客转写出错")
        with self._lock:
            gs = list(self._guests.values())
        for g in gs:
            if g.on:
                self._close(g)
        with self._lock:                            # 下一场会从头来
            self._guests.clear()
        self.recent.clear()
        try:
            from . import ears
            ears.PLAYBACK.host_elsewhere = None
        except Exception:
            pass
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def _guest(self, ident: str, name: str) -> _Guest:
        g = self._guests.get(ident)
        if g is None:
            g = _Guest(ident, name, self._vad.state())
            with self._lock:
                self._guests[ident] = g
        elif name and g.name != name:
            g.name = name
        self._last_guest = time.time()
        return g

    def _left(self, ident: str) -> None:
        g = self._guests.get(ident)
        if g is None:
            return
        if g.on:
            self._close(g)
        with self._lock:
            self._guests.pop(ident, None)
        try:                                         # 用另一台设备参会的主持人走了：家里这台的麦克风恢复成主持人
            from . import ears
            if ears.PLAYBACK.host_elsewhere == g.name and not any(x.name == g.name for x in self._guests.values()):
                ears.PLAYBACK.host_elsewhere = None
        except Exception:
            pass

    def _ensure_worker(self) -> AsrWorker | None:
        w = self.worker
        if w is not None and w.error is None:
            return w
        if w is not None and time.time() - self._fail_at < 30:
            return None                             # 刚挂过：30 秒内不反复重起
        if w is not None:
            log.warning("访客转写进程挂了（%s），重起", w.error)
            w.stop()
        hw = self.c.get("hotwords", True)                # 列表 = 就用这些；数字 = 带几个；True = 默认 24 个；False = 不带
        hw = list(hw) if isinstance(hw, list) else (_hotwords(hw if isinstance(hw, int) and hw is not True else 24)
                                                     if hw else [])
        lite = self.c.get("hotwords_lite", 8)            # 积压时带几个（热词表前面是软件名）；False / None = 积压时也照常带
        lite = None if (lite is None or lite is False or not hw) else hw[:max(0, int(lite))]
        w = AsrWorker(Path(self.c["model_dir"]) if self.c.get("model_dir") else _model_dir(), int(self.c.get("threads") or 4),
                      hw,
                      normal_priority=str(self.c.get("priority") or "").lower() == "normal",
                      lite_hotwords=lite)
        self._fail_at = time.time()
        w.start()
        self.worker = w
        return w

    def _tick(self) -> None:
        """声音断了（访客关麦、网络卡）也要能判出"说完了"：补上静音帧。没有访客一阵子就停掉干活进程。"""
        now = time.time()
        np = self._np
        for g in list(self._guests.values()):
            if g.on and g.last_frame_t and now - g.last_frame_t > 0.6:     # 平时 10 毫秒一块，断这么久是真没声音了
                n = min(int((now - g.last_frame_t) / FRAME_S), int(1.5 / FRAME_S))
                z = np.zeros(FRAME, np.int16)
                for k in range(n):
                    if not g.on:
                        break
                    self._frame(g, z, g.last_frame_t + FRAME_S)
        if self._guests:
            self._last_guest = now
        w = self.worker
        if w is not None and not self._guests and now - self._last_guest > float(self.c["idle_stop_s"]) and not w.pending():
            log.info("访客转写：%d 秒没有访客，停掉转写进程", int(now - self._last_guest))
            self.worker = None
            w.stop()

    def _audio(self, ident: str, name: str, pcm: bytes, t: float) -> None:
        np = self._np
        g = self._guest(ident, name)
        x = np.frombuffer(pcm, dtype=np.int16)
        if g.rest is not None and len(g.rest):
            x = np.concatenate([g.rest, x])
        n = len(x) // FRAME
        for i in range(n):
            self._frame(g, x[i * FRAME:(i + 1) * FRAME], t - (len(x) - (i + 1) * FRAME) / SR)
        g.rest = x[n * FRAME:].copy()

    def _frame(self, g: _Guest, fr, t: float) -> None:
        np = self._np
        g.last_frame_t = t
        x = fr.astype(np.float32) / 32768.0
        p = self._vad.prob(x, g.st)
        if not g.on:
            g.pre.append((fr, p, t))
            g.run = g.run + 1 if p >= TH_ON else 0
            if g.run >= START_FRAMES:
                g.on, g.frames = True, list(g.pre)
                g.pre.clear()
                t1 = t - (g.run - 1) * FRAME_S              # 这一段第一个有声帧
                self._voice_mark(g, t1, t, g.run)
                g.vs_open = t1
                g.last_v, g.sil, g.voiced = len(g.frames) - 1, 0, g.run
                g.vrun, g.barged, g.last_voice_t, g.run_rms = g.run * FRAME_S, False, t, []
            return
        g.frames.append((fr, p, t))
        if p >= TH_OFF:
            self._voice_mark(g, t, t, 1)
            if g.vs_open is None:
                g.vs_open = t
            g.last_v, g.sil, g.last_voice_t = len(g.frames) - 1, 0, t
            g.voiced += 1
            g.vrun += FRAME_S
            rms = float(np.sqrt((x * x).mean()))
            g.run_rms.append(rms)
            if g.spec is not None:                  # 又开口了：抢先转的那次作废
                self._drop_spec(g)
            if g.draft_v != -2:                     # 出过初稿以后又开口了：初稿作废
                self._draft_drop(g)
            try:
                playing = bool(self.audible())
            except Exception:
                playing = False
            if not playing and p >= TH_ON:          # 学生没出声时他说话的音量：判插话时拿来比
                g.lvl = rms if g.lvl is None else 0.98 * g.lvl + 0.02 * rms
            if not g.barged and g.vrun >= float(self.c["barge_s"]):
                g.barged = True
                # 学生正在出声时访客那边"有声音"：可能是他开着外放、学生的声音漏回来了（回声消除没消干净，一般比他自己说话小得多）。
                # 比他平时说话小一半以上的不算插话
                loud = sorted(g.run_rms)[len(g.run_rms) // 2] if g.run_rms else 0.0
                quiet_echo = playing and g.lvl is not None and loud < 0.5 * g.lvl
                if self.on_barge is not None and not quiet_echo:
                    try:
                        self.on_barge(g.name)
                    except Exception:
                        log.exception("on_barge 出错")
        else:
            g.sil += 1
            if g.sil * FRAME_S > 0.25:
                g.vrun, g.run_rms = 0.0, []
                g.barged = False if g.sil * FRAME_S > 0.6 else g.barged
        sil = g.sil * FRAME_S
        dur = (g.last_v + 1) * FRAME_S
        if sil >= float(self.c["end_s"]) or (dur >= float(self.c["piece_min_s"]) and sil >= float(self.c["piece_s"])):
            self._close(g, piece=sil < float(self.c["end_s"]))
        elif (sil >= float(self.c["spec_s"]) and g.spec is None and g.voiced * FRAME_S >= MIN_VOICED_S
              and dur < float(self.c["piece_min_s"])):      # 长的那段不抢先转（见文件头）
            self._speculate(g)
        elif sil >= DRAFT_S and g.draft_v != g.last_v and g.voiced * FRAME_S >= MIN_VOICED_S:
            self._draft(g)
        elif len(g.frames) * FRAME_S >= float(self.c["max_s"]):
            self._cut_long(g)

    def _voice_mark(self, g: _Guest, t_first: float, t_last: float, n: int) -> None:
        """记有声帧：累计出声多久；和上一个有声帧隔了 PAUSE_LO～PAUSE_HI 秒的，记一个句中停顿。"""
        gap = t_first - g.last_voice_t - FRAME_S if g.last_voice_t else -1.0
        with self._lock:
            if PAUSE_LO <= gap <= PAUSE_HI:
                g.pauses.append(round(gap, 3))
            g.vcum += n * FRAME_S
            g.vhist.append((t_last, g.vcum))

    # ---------- 初稿抢跑（快识别） ----------
    def _turn_p(self, frames) -> float | None:
        """smart-turn 给这段"说完了"打分（最后 8 秒）；模型没有就 None。"""
        try:
            from .turn import model
            m = model()
            if m is None or not frames:
                return None
            return round(m.p_done(self._pcm(frames).astype(self._np.float32) / 32768.0), 3)
        except Exception:
            log.exception("访客 smart-turn 打分出错")
            return None

    def _draft(self, g: _Guest) -> None:
        """他停了 DRAFT_S 秒：这段先送快识别出初稿，让模型先想着（准确文字还是 Fun-ASR 的，后端会对一遍）。"""
        d = self.draft
        if d is None or self.on_draft is None or not d.ready or g.voiced * FRAME_S < MIN_VOICED_S:
            return
        end = min(len(g.frames), g.last_v + 1 + TAIL_FRAMES)
        g.draft_v = g.last_v
        p = self._turn_p(g.frames[:end])
        if p is not None and p < 0.5:
            return                                  # 多半没说完：不抢跑
        pcm = self._pcm(g.frames[:end]).tobytes()
        we = g.frames[min(g.last_v, end - 1)][2]
        ticket = g.draft_seq = g.draft_seq + 1

        def work():
            r = d.asr_pcm(pcm, timeout=2.0)
            if not r or not r[0] or g.draft_seq != ticket or g.draft_v != g.last_v:
                return                              # 没出字 / 他又开口了（段关了也照样交：准确文字还要一会儿）
            g.draft_live = True
            try:
                self.on_draft(g.name, r[0], {"we": we, "p": p, "ms": r[1]})
            except Exception:
                log.exception("on_draft 出错")
        threading.Thread(target=work, name="guest-draft", daemon=True).start()

    def _draft_drop(self, g: _Guest) -> None:
        g.draft_seq += 1
        g.draft_v = -2
        if g.draft_live:
            g.draft_live = False
            if self.on_draft is not None:
                try:
                    self.on_draft(g.name, None, None)
                except Exception:
                    log.exception("on_draft 出错")

    # ---------- 送去转 ----------
    def _pcm(self, frames):
        return self._np.concatenate([f[0] for f in frames]) if frames else self._np.zeros(0, self._np.int16)

    def _speculate(self, g: _Guest) -> None:
        w = self._ensure_worker()
        if w is None or not w.ready or w.pending():
            return                                  # 还没加载好 / 手上还有别的段：抢先转只会排在后面，白占处理器
        end = min(len(g.frames), g.last_v + 1 + TAIL_FRAMES)
        spec = {"end": end, "last_v": g.last_v, "text": None, "ms": None, "done": False, "final": None}

        def cb(text, ms, spec=spec):
            with self._lock:                        # 和 _submit 抢：谁后到谁交出文字
                spec["text"], spec["ms"], spec["done"] = text, ms, True
                fin = spec.get("final")
            if fin is not None:
                self._deliver(g, fin, text, ms, spec=True)
        spec["rid"] = w.submit(self._pcm(g.frames[:end]), cb)
        g.spec = spec

    def _drop_spec(self, g: _Guest) -> None:
        spec, g.spec = g.spec, None
        w = self.worker
        if spec and w is not None and not spec["done"]:
            w.drop(spec["rid"])

    def _close(self, g: _Guest, piece: bool = False) -> None:
        """这一段说完了（或者长话中途停顿，先切下来）：送去转。"""
        end = min(len(g.frames), g.last_v + 1 + TAIL_FRAMES)
        seg, rest = g.frames[:end], g.frames[end:]
        voiced = g.voiced * FRAME_S
        spec, g.spec = g.spec, None
        vs, g.vs_open = g.vs_open, None
        g.on, g.run, g.frames = False, 0, []
        g.pre = deque(rest[-PRE_FRAMES:], maxlen=PRE_FRAMES)
        if voiced < MIN_VOICED_S or not seg:
            if spec:
                g.spec = spec
                self._drop_spec(g)
            return
        info = {"ws": seg[0][2] - FRAME_S, "we": seg[min(g.last_v, len(seg) - 1)][2], "dur": round(len(seg) * FRAME_S, 2),
                "piece": piece, "closed": time.time(), "voiced": round(voiced, 2)}
        info["vs"] = vs if vs is not None else info["ws"]
        if not piece:
            info["p"] = self._turn_p(seg)            # 后端判"这句说完没有"用（turn.need）
        self._submit(g, seg, info, spec)             # 初稿（报过的话）留在后端等核对；他下次开口才作废

    def _cut_long(self, g: _Guest) -> None:
        """一段说到 MAX_S 秒还没停：在最近 3 秒里声音最小的那一帧后面切开，前面的送去转，后面的接着攒。"""
        k0 = max(1, len(g.frames) - int(3.0 / FRAME_S))
        cut = min(range(k0, len(g.frames)), key=lambda k: g.frames[k][1]) + 1
        seg, g.frames = g.frames[:cut], g.frames[cut:]
        spec, g.spec = g.spec, None
        if spec:
            g.spec = spec
            self._drop_spec(g)
        g.last_v = max(-1, g.last_v - cut)
        g.voiced = sum(1 for f in g.frames if f[1] >= TH_OFF)
        info = {"ws": seg[0][2] - FRAME_S, "we": seg[-1][2], "dur": round(len(seg) * FRAME_S, 2), "piece": True,
                "closed": time.time(), "voiced": round(sum(1 for f in seg if f[1] >= TH_OFF) * FRAME_S, 2)}
        info["vs"] = g.vs_open if g.vs_open is not None else info["ws"]
        g.vs_open = next((f[2] for f in g.frames if f[1] >= TH_OFF), None)     # 后半截第一个有声帧
        self._submit(g, seg, info, None)

    def _host_check(self, g: _Guest, seg) -> None:
        """这一段有声的部分（最多最后 4 秒）和主持人的声纹比一比（另一个线程里算，约 30–60 毫秒，不卡判停）。"""
        np = self._np
        if self.host_check is False or g.ident == "app-audio":   # 会议软件那一路是好几个人混在一起，不拿来认主持人
            return
        voiced = [f[0] for f in seg if f[1] >= TH_OFF]
        if len(voiced) * FRAME_S < HOST_MIN_S:
            return
        x = np.concatenate(voiced)[-SR * 4:].astype(np.float32) / 32768.0
        try:
            self._hq.put_nowait((g, x))
        except queue.Full:
            return
        t = self._ht
        if t is None or not t.is_alive():
            self._ht = threading.Thread(target=self._host_loop, name="guest-hostvoice", daemon=True)
            self._ht.start()

    def _host_loop(self) -> None:
        from . import ears
        while not self._stop.is_set():
            try:
                g, x = self._hq.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                chk = ears._voice_check()
                sim = chk.sim(x) if chk.ok else None
            except Exception:
                log.exception("访客声纹比对出错")
                sim = None
            if sim is None:
                continue
            g.host_sims.append(round(sim, 3))
            hs = list(g.host_sims)
            recent = sorted(hs[-5:])[len(hs[-5:]) // 2]
            pb = ears.PLAYBACK
            here = time.time() - float(getattr(pb, "host_here_at", 0.0) or 0.0) < 600
            if (pb.host_elsewhere != g.name and not here         # 家里麦克风刚证实你就在电脑前（手机也进了会在试）：十分钟内不再认
                    and sum(1 for s in hs if s >= HOST_SIM) >= HOST_NEED and recent >= HOST_SIM):
                pb.host_elsewhere_sim = recent
                pb.host_elsewhere = g.name
                log.info("访客 %s 的声音像主持人（最近 %s）：主持人在用另一台设备参会", g.name, hs[-5:])
            elif pb.host_elsewhere == g.name and len(hs) >= 5 and recent < HOST_SIM - 0.1:
                pb.host_elsewhere = None                 # 换人在用这台设备说话了
                log.info("访客 %s 最近的声音不像主持人了（%s）", g.name, hs[-5:])

    def _submit(self, g: _Guest, seg, info: dict, spec: dict | None) -> None:
        try:
            self._host_check(g, seg)
        except Exception:
            log.exception("访客声纹比对出错")
        g.seq += 1
        info["seq"] = g.seq
        with self._lock:
            g.waiting += 1
            prev = g.spans[-1] if g.spans else None
            info["gap"] = round(info["vs"] - prev["we"], 2) if prev is not None and "vs" in info else None
            g.spans.append({"seq": g.seq, "vs": info.get("vs", info["ws"]), "we": info["we"],
                            "voiced": info.get("voiced"), "done": False})
        if spec is not None and spec["end"] == len(seg):
            # 抢先转的就是这一段（之后没再开口）：用它的结果
            with self._lock:
                done = spec["done"]
                if not done:
                    spec["final"] = info
            if done:
                self._deliver(g, info, spec["text"], spec["ms"], spec=True)
            return
        if spec is not None and not spec["done"] and self.worker is not None:
            self.worker.drop(spec["rid"])
        w = self._ensure_worker()
        if w is None:
            with self._lock:
                g.waiting -= 1
                self._span_done(g, info["seq"])
            log.warning("访客转写：转写进程不可用，%s 这一段（%.1f 秒）没转", g.name, info["dur"])
            return
        try:                                         # 积压了：这一段少带热词（快一些）
            info["backlog"] = round(w.backlog_s(), 2)
            info["lite"] = bool(w.lite_ready and info["backlog"] > float(self.c.get("lite_backlog_s") or 3.0))
        except Exception:
            info["lite"] = False
        w.submit(self._pcm(seg), lambda text, ms, info=info: self._deliver(g, info, text, ms, spec=False), lite=info["lite"])

    @staticmethod
    def _span_done(g: _Guest, seq) -> None:
        for s in reversed(g.spans):
            if s["seq"] == seq:
                s["done"] = True
                return

    def _deliver(self, g: _Guest, info: dict, text, ms, spec: bool) -> None:
        try:
            self._deliver1(g, info, text, ms, spec)
        finally:
            with self._lock:                           # 字交出去以后才标"转完了"（转出来是空的也标：后端别等它）——
                self._span_done(g, info.get("seq"))    # 后端看到"转完了"时，这块字一定已经接进它那一轮了

    def _deliver1(self, g: _Guest, info: dict, text, ms, spec: bool) -> None:
        with self._lock:
            g.waiting = max(0, g.waiting - 1)
        now = time.time()
        info.update(asr_ms=ms, lag=round(now - info["we"], 2), spec=spec, name=g.name)
        try:
            info["rtf"] = round(float(ms) / 1000.0 / max(0.05, float(info["dur"])), 2) if ms is not None else None
        except (TypeError, ValueError):
            info["rtf"] = None
        text = (text or "").strip()
        self.stats.append({k: info.get(k) for k in ("name", "dur", "asr_ms", "lag", "spec", "piece", "rtf", "lite", "backlog")}
                          | {"chars": len(text)})
        if text:
            self.recent.append((g.name, info["ws"], info["we"], text))
        log.info("访客转写 %s：%.1f 秒 → %d 字，转写 %s 毫秒（%s 倍语音时长%s），说完到出字 %.2f 秒%s", g.name, info["dur"], len(text),
                 ms, info.get("rtf"), "，少带热词" if info.get("lite") else "", info["lag"], "（抢先）" if spec else "")
        if text and self.on_text is not None:
            try:
                self.on_text(g.name, text, info)
            except Exception:
                log.exception("on_text 出错")
