# -*- coding: utf-8 -*-
"""嘴：把文字念出来（克隆音色），带"接话缓存"做到立刻应声。

合成优先级：克隆音色常驻服务 → edge-tts（联网）→ Windows SAPI（离线）。
克隆音色服务由 ~/.zuhui/voice.json 的 "engine" 选：
  "gptsovits"（默认推荐，快）：zuhui/tts_gsv.py 适配服务（端口 voice.json gsv.port，默认 47322），
              它再拉起 GPT-SoVITS 整合包的 api_v2.py；参考音频 / 参考文本在 voice.json 的 "gsv" 段。
  "indextts"：zuhui/tts_server.py（IndexTTS2，端口 cfg mouth.tts_port，默认 47321），参考音频是顶层 "ref"。
两个服务接口一样（GET /health，POST /say {"text","play":false,"wav"}）；选了哪个就只拉起哪个（显存）。
不管哪种后端，最终都由播放线程用 ffplay 播，所以打断只要清队列 + 杀 ffplay。
gptsovits 边合成边播：嗓子服务 /stream 一块一块吐 PCM，ffplay 从管道读（首块约 0.3 秒就出声，和句子长短无关）；
/stream 不通就退回整句 wav 文件。

流水线：say(text) → 按 。！？；\\n 切句 → 合成线程逐句合成 → 播放队列 → 播放线程逐句播（边合成边播）。
接话：warm() 把 cfg["mouth"]["backchannels"] 预合成到 ~/.zuhui/cache/<md5>.wav
     （key = 文本 + 音色标识：indextts 是参考音频路径，gptsovits 是 引擎+参考音频+参考文本+模型版本），
     ack() 在没在说话时立刻随机播一条。

手动测试：
  python -m zuhui.mouth 老师好，这是一句测试。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from .config import ROOT, STATE, local_path
from .ears import PLAYBACK   # 学生每一句按出声时刻记进去：耳朵拿它认扬声器漏进麦克风的回声（见 ears.py）

log = logging.getLogger("zuhui.mouth")

SERVER_PY = ROOT / "zuhui" / "tts_server.py"
GSV_PY = ROOT / "zuhui" / "tts_gsv.py"
RUN_FILE = STATE / "tts_server.json"           # 服务就绪后写 {"pid","port","started"(,"engine")}
VOICE_FILE = STATE / "voice.json"              # {"engine", "ref": 参考音频, "emo": ..., "emo_alpha": ..., "gsv": {...}}
OLD_MEET = Path.home() / ".claude" / "meeting"  # 旧版状态目录（voice.json 回退用）
PROGRESS_FILE = Path.home() / ".claude" / "progress" / "indextts2_tts_server.json"
GSV_PROGRESS_FILE = Path.home() / ".claude" / "progress" / "gptsovits_tts_server.json"
DEFAULT_REF = local_path("voice_ref")       # 克隆音色的参考录音（~/.zuhui/paths.json；空 = 用语音服务自己的）
GSV_PORT = 47322
ENGINES = ("gptsovits", "indextts")

SENT_SPLIT = re.compile(r"(?<=[。！？；!?\n])")   # 切句，标点留在前一句
SOFT_SPLIT = re.compile(r"(?<=，)|(?<=,)(?!\d)")   # 长句再按逗号切；数字里的千分位逗号不切（"144,000"切开后逐字稿成了"144, 000"）
MD_JUNK = re.compile(r"[*_`#>\[\]|]+")             # 去 markdown 符号
HAS_WORD = re.compile(r"[\w\u4e00-\u9fff]")
MAX_SENT = 80                                      # 单句超过这么多字就按逗号再切（降低首句延迟）
RELAUNCH_COOLDOWN = 120                            # 拉起服务后这么久内不重复拉
EDGE_COOLDOWN = 300                                # edge-tts 失败后这么久内不再试（省掉每句白等几秒）

CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200                 # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机服务不走系统代理


def _http(method: str, port: int, path: str, body=None, timeout: float = 5.0) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with _NO_PROXY.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _unlink(p) -> None:
    try:
        if p:
            os.remove(p)
    except OSError:
        pass


def split_sentences(text: str) -> list[str]:
    """按 。！？；\\n 切句；太长的句子再按逗号切；丢掉没有文字的碎片。"""
    text = MD_JUNK.sub("", text or "")
    out: list[str] = []
    for piece in SENT_SPLIT.split(text):
        piece = piece.strip()
        if not piece or not HAS_WORD.search(piece):
            continue
        if len(piece) <= MAX_SENT:
            out.append(piece)
            continue
        buf = ""
        for sub in SOFT_SPLIT.split(piece):
            if buf and len(buf) + len(sub) > MAX_SENT:
                out.append(buf.strip())
                buf = sub
            else:
                buf += sub
        if buf.strip():
            out.append(buf.strip())
    return out


_CN_D = "零一二三四五六七八九"


def _cn_int(n: int) -> str:
    if n < 10:
        return _CN_D[n]
    if n < 20:
        return "十" + (_CN_D[n % 10] if n % 10 else "")
    if n < 100:
        return _CN_D[n // 10] + "十" + (_CN_D[n % 10] if n % 10 else "")
    return "".join(_CN_D[int(c)] for c in str(n))


LANG = "zh"     # 会议语言（Meeting 开场时 set_lang）：英文组会不把数字改成中文读法（第一次英文测试念成了 "Haiku 五点五"）


def set_lang(lang: str) -> None:
    global LANG
    LANG = "en" if str(lang or "zh") == "en" else "zh"


def speakable(text: str) -> str:
    """念之前把数字写法换成读法：第2段 → 第二段，表 2 → 表二，式 (2) → 式二，3.1 → 三点一，0.25 → 零点二五。
    合成器自己的前端会把"第2段"读成"第两段"、把"2.3.1"读成"二.三点一"。英文组会原样念（英文前端自己会读数字）。"""
    t = text or ""
    if LANG == "en":
        return t
    t = re.sub(r"第\s*(\d{1,3})\s*(?=[节章段条处页行个张部步点句])", lambda m: "第" + _cn_int(int(m.group(1))), t)
    t = re.sub(r"([表图式])\s*[（(]?\s*(\d{1,3})\s*[)）]?(?![\d.])", lambda m: m.group(1) + _cn_int(int(m.group(2))), t)

    def dotted(m):
        parts = m.group(1).split(".")
        nxt = m.group(2)
        if len(parts) == 2 and (parts[0] == "0" or parts[1].startswith("0") or nxt in ("%", "％") or len(parts[1]) >= 3):
            return _cn_int(int(parts[0])) + "点" + "".join(_CN_D[int(c)] for c in parts[1]) + nxt     # 小数
        return "点".join(_cn_int(int(x)) for x in parts) + nxt                                           # 小节号
    t = re.sub(r"(\d{1,3}(?:\.\d{1,3})+)(.?)", dotted, t)
    return t


# 多音字：GPT-SoVITS（g2pW + 多音字词典）把"命令行"念成 mìng lìng xíng、"一行代码"念成 yī xíng、"调参"念成 diào cān……
# 送去合成之前把这些词里的多音字换成同音的单音字（只换送给合成器的字，逐字稿、回声比对用的还是原文）。
# 每条都用 GPT-SoVITS 自己的前端核过（scratchpad/student_fix/g2p_probe.py），再合成、用 Fun-ASR 听回来核过。
_TTS_FIX = [
    (re.compile(r"命令行"), "命令航"),                                   # mìng lìng háng
    (re.compile(r"一行(?![人动为李政星销车驶使事走程善进军止径])"), "一航"),  # 一行代码 / 每一行 / 合并成一行：háng（一行人除外）
    # 这行 / 每行 / 空行 / 首行（"那行"多半是"那行吧"，不动；"这行不行"后面跟"不"，不动）
    (re.compile(r"(?<=[这哪每空双首末多两几])行(?=[，。！？、,.!?\s的里上下中就都也是写太有]|$)"), "航"),
    (re.compile(r"(?<![进运执举实施流发旅推并平步通先可履奉盛风修品德自同横遵暴强言送飞爬慢快难践尝试夜])"
                r"行(?=号|首|尾|末|距)"), "航"),                         # 行号 / 行首 / 行尾 / 行距
    (re.compile(r"换行"), "换航"),
    (re.compile(r"(?<=那)行(?=[，。！？,.!?吧啊呀]|$)"), "形"),            # "那行，就这样"：xíng（g2pW 念成了 háng）
    (re.compile(r"(?<!强)调参"), "条参"),                               # 调参 / 微调参数：tiáo cān（强调参数不动）
    (re.compile(r"调一调"), "条一条"),
    (re.compile(r"多空(?![白闲缺格位隙余])"), "多箜"),                  # 多空（多头空头）：kōng；空白 / 空格是 kòng，不动
    (re.compile(r"(?<=[步笔次个页条项字行子])数(?!一数)"), "树"),         # 步数 / 叶子数 / 笔数：shù
    (re.compile(r"只(?=[有是用给在看写改能会要需说做讲占差剩够])"), "纸"),   # 只有 / 只用 / 只改：zhǐ
    (re.compile(r"(?:(?<![一-鿿])|(?<=[能以会要才就都也没经可便法取获]))得(?=到|出)"), "德"),   # 得到 / 能得出：dé（看得出、听得到不动）
]


# 英文按英文念（2026-11-11 用户在会上定的："要念英文……要用中文去替代英文吗？怎么可能？"）。
# GPT-SoVITS 的 text_lang="zh" 本来就是中英混合：LangSegmenter 把英文段切出来，走英文前端（en_normalization + CMU 词典 +
# g2p_en），全大写的缩写它自己拆成字母（OHLC → O H L C，FooBench → I B P Bench，GPU、AI、Fun-ASR 都对）。用它自己的前端
# 把下面每个词核过音素（scratchpad/helperH/g2p_probe.py）：Claude、DeepSeek、Opus、tick、Bloom、GitHub、Desktop、Word、Python、
# Flash、Pro、Max、Extra、Low、Medium、High、TradingView、Markdown 都是对的英文音素。以前念得不像是声音的口音（中文参考音频、
# 只拿中文微调过的那版声音更明显），再拿中文识别器（Fun-ASR）回听当裁判——中文识别器只认得出中文音译，挑出来的自然全是
# "克劳德""迪普西克"这种。所以英文原样送进去；这里只改英文前端自己拼错的几个，改成能拼对的英文写法（念出来还是英文）：
def _word(w: str) -> str:
    return r"(?<![A-Za-z])" + w + r"(?![A-Za-z])"


_EN_SAY = [(re.compile(_word(w), re.I), rep) for w, rep in [
    (r"La[ \-]?TeX", "Lay tech"),            # 前端拆成 La-T-X（L AA1 T IY1 EH1 K S）
    (r"Meta[ \-]?Trader", "Metta Trader"),   # meta 查到 M IY1 T AH0（"米塔"）
    (r"Dukascopy", "Doo kass copy"),         # 猜成 D UW1 K AH0 Z K AA1 P IY0
    (r"PyTorch", "Pie Torch"),               # P-Y Torch
    (r"NumPy", "Numb Pie"),                  # N-U-M-P-Y
    (r"arXiv", "archive"),                   # A-R-X-I-V
    (r"LoRA", "Lora"),                       # lo-R-A
    # 全大写、但大家当一个词念的：前端会一个字母一个字母拼，换成首字母大写
    (r"BERT", "Bert"), (r"ARIMA", "Arima"), (r"GARCH", "Garch"), (r"CUDA", "Kuda"), (r"JSON", "Jason"),
    (r"LASSO", "Lasso"),
]]

# 有通行中文叫法、大家口头也说中文的，才说中文：币安（Binance）；"Sharpe 比率"说"夏普比率"（金融里的标准叫法），
# 单说 Sharpe 或 Sharpe ratio 还是英文。
_ZH_NAME = [
    (re.compile(_word(r"Binance"), re.I), "币安"),
    (re.compile(r"(?<![A-Za-z])Sharpe\s*(?=比率|比值|比|值|指数)", re.I), "夏普"),
]


def tts_text(text: str) -> str:
    """送给克隆音色合成器的那一份：英文原样（只把前端拼错的几个换成拼得对的英文写法，_EN_SAY；有通行中文叫法的说中文，
    _ZH_NAME），多音字换成同音字（_TTS_FIX）。逐字稿、回声比对用的还是原文。"""
    t = text or ""
    # LaTeX 命令和转换残留（"\cite{x}""maketitle thanks aketitle"）不念出来（三人英文会：把这串原样念了）。逐字稿上照留
    t = re.sub(r"\\[A-Za-z]+\*?(?:\{[^{}]{0,60}\})?", " ", t)
    t = re.sub(r"(?i)(?<![A-Za-z])(?:maketitle|aketitle)(?![A-Za-z])(?:\s+thanks\s+(?:maketitle|aketitle))?", " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    for rx, rep in (_ZH_NAME if LANG != "en" else []):   # 英文组会不换成中文叫法
        t = rx.sub(rep, t)
    for rx, rep in _EN_SAY:
        t = rx.sub(rep, t)
    for rx, rep in _TTS_FIX:
        t = rx.sub(rep, t)
    return t


def speakable_list(texts):
    """预合成的固定句子要和 say() 切出来的句子一字不差，缓存才对得上。"""
    out = []
    for t in texts:
        out += [speakable(s) for s in split_sentences(t)]     # 和 say() 一样：先按原文切句，再逐句换成读法
    return out


class _Stream:
    """一句正在流式合成的话：合成线程往里塞 PCM（s16le 单声道），播放线程和语音桥各自从头读。"""

    def __init__(self, text: str):
        self.text = text
        self.sr = 0
        self.chunks: list[bytes] = []
        self.nbytes = 0
        self.done = False
        self.fallback: Path | None = None      # 流式失败时退回的整句 wav
        self.t0 = time.time()
        self.first: float | None = None         # 第一块到的时刻
        self.first_n = 0                        # 第一块的字节数（算合成速度时不算它：它含文本预处理）
        self._cv = threading.Condition()

    def put(self, b: bytes) -> None:
        with self._cv:
            if self.first is None:
                self.first = time.time()
                self.first_n = len(b)
            self.chunks.append(b)
            self.nbytes += len(b)
            self._cv.notify_all()

    def finish(self) -> None:
        with self._cv:
            self.done = True
            self._cv.notify_all()

    def seconds(self) -> float:
        return self.nbytes / (self.sr * 2.0) if self.sr else 0.0

    def speed(self) -> float | None:
        # 第一块之后每秒合成出多少秒音频；刚开头看不准就 None
        if not self.sr or self.first is None or len(self.chunks) < 2:
            return None
        el = time.time() - self.first
        if el < 0.12:
            return None
        return (self.nbytes - self.first_n) / (self.sr * 2.0) / el

    def wait(self, timeout: float) -> None:
        with self._cv:
            self._cv.wait(timeout)

    def get(self, i: int, timeout: float = 0.5):
        """第 i 块；还没到就等一会儿。→ (块或 None, 是否已经收完)"""
        with self._cv:
            if i >= len(self.chunks) and not self.done:
                self._cv.wait(timeout)
            return (self.chunks[i] if i < len(self.chunks) else None), self.done

    def iter(self, alive=lambda: True):
        """从头读到尾（边到边给）；alive() 变假就停。"""
        i = 0
        while alive():
            b, done = self.get(i)
            if b is not None:
                i += 1
                yield b
            elif done:
                return


def _ref_file(path, t_start: float, rate: float) -> None:
    """整句的 wav（缓存的接话、整句合成的后备）从 t_start 开始放：读出来记进 ears.PLAYBACK 当回声参考。
    不是 wav（edge-tts 没转成的 mp3）就不记——那一句的回声照旧靠文字比对去掉。"""
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return
            ch, sr = w.getnchannels(), w.getframerate()
            data = w.readframes(min(w.getnframes(), sr * 60))
        if ch > 1:
            import numpy as np
            data = np.frombuffer(data, dtype=np.int16).reshape(-1, ch).mean(axis=1).astype(np.int16).tobytes()
        PLAYBACK.put_at("tts", data, t_start, sr, rate)
    except Exception:
        log.debug("回声参考：%s 没读成", path, exc_info=True)


class Mouth:
    def __init__(self, cfg: dict):
        m = dict(cfg.get("mouth") or {})
        self.port_default = int(m.get("tts_port") or 47321)
        self.venv = str(m.get("indextts_venv") or "")
        self.edge_voice = str(m.get("edge_voice") or "zh-CN-YunxiNeural")
        self.edge_rate = str(m.get("edge_rate") or "+8%")
        self.sapi_voice = str(m.get("sapi_voice") or "Microsoft Huihui Desktop")
        self.backchannels = [t.strip() for t in (m.get("backchannels") or []) if (t or "").strip()]
        self.rate = float(m.get("rate") or 1.0)      # 播放语速：1.15 = 快 15%，音调不变（ffplay atempo）
        self.phrases = [t.strip() for t in (m.get("phrases") or []) if (t or "").strip()]
        self.phrases += [t.strip() for t in (m.get("openers") or []) if (t or "").strip()]
        # 英文组会的固定话和开头语（config phrases_en / openers_en）：一样预先合成，英文会第一声不用等
        self.phrases += [t.strip() for t in (list(m.get("phrases_en") or []) + list(m.get("openers_en") or [])) if (t or "").strip()]
        if "好，叫我再说。" not in self.phrases:
            self.phrases.append("好，叫我再说。")         # 静音时那一句（app.py）
        self.played: list[list] = []      # 放过的每一段：[开始, 结束]（墙上时间；正在放的结束为 None），回声判断用
        self.stream = bool(m.get("stream", True))                 # 克隆音色边合成边播
        self.prebuf = float(m.get("stream_prebuffer") or 0.25)    # 攒够这么多秒（或整句收完）才开始放
        self._stream_sr = 32000                                   # 预先拉起 ffplay 用的采样率（v2ProPlus 32k）
        self._stream_off_until = 0.0
        self._stream_conn = None
        self._spare = None                                        # 提前起好、等着的 ffplay：(采样率, 进程)
        self._spare_lock = threading.Lock()

        self.cache_dir = STATE / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_root = STATE / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="mouth-", dir=tmp_root))
        atexit.register(shutil.rmtree, self.tmp, True)

        self.ffplay = shutil.which("ffplay")
        self.ffmpeg = shutil.which("ffmpeg")
        self.edge_tts = shutil.which("edge-tts")
        if not self.ffplay:
            log.warning("找不到 ffplay，退回 PowerShell SoundPlayer（只能放 wav）")

        self.backend: str | None = None        # 最近一次成功的合成后端：gptsovits / indextts / edge / sapi
        self._lock = threading.Lock()
        self._gen = 0                          # stop() 时 +1，旧 gen 的队列项全部作废
        self._pending = 0                      # 当前 gen 里还没播完的句数（合成中 + 排队 + 播放中）
        self._proc: subprocess.Popen | None = None
        self._synth_q: queue.Queue = queue.Queue()
        self._play_q: queue.Queue = queue.Queue()
        self._warm_thread: threading.Thread | None = None
        self._launched_at = 0.0
        self._edge_down_until = 0.0
        threading.Thread(target=self._synth_loop, name="mouth-synth", daemon=True).start()
        threading.Thread(target=self._play_loop, name="mouth-play", daemon=True).start()
        if self.ffplay and self.stream:          # 第一句之前先起好一个 ffplay（冷起要一秒多）
            threading.Thread(target=self._refill_spare, args=(self._stream_sr,), name="mouth-spare", daemon=True).start()

    # ------------------------------------------------------------ 对外接口

    def playing_overlap(self, ws: float, we: float, tail: float = 0.6) -> float:
        """[ws, we] 这段时间里，有多大比例学生的声音正在放（每段往后多算 tail 秒的余音）。"""
        if we <= ws:
            we = ws + 0.05
        now, cover = time.time(), 0.0
        for s, e in list(self.played)[-40:]:
            e = (e if e is not None else now) + tail
            cover += max(0.0, min(we, e) - max(ws, s))
        return min(1.0, cover / (we - ws))

    @property
    def audible(self) -> bool:
        """学生的声音此刻真的从音箱出来（或刚停不到 0.4 秒，算上余音）。合成中、排队中都不算。"""
        if self._proc is not None:
            return True
        ends = [e for _, e in self.played[-3:] if e is not None]
        return bool(ends) and time.time() - max(ends) < 0.4

    def say(self, text: str, on_start=None) -> None:
        """非阻塞：切句后交给合成线程，边合成边播。先按原文切句，每句再换成读法送去合成；on_start 收到的是原文那句
        （逐字稿照原文记：以前切的是读法，逐字稿上同一句里"23 个"和"十四点四 万"并存）。"""
        shown = split_sentences(text)
        if not shown:
            return
        with self._lock:
            gen = self._gen
            self._pending += len(shown)
        for d in shown:
            cb = (lambda _s, d=d, f=on_start: f(d)) if on_start else None
            self._synth_q.put((gen, speakable(d), cb))

    def ack(self) -> None:
        """没在说话时立刻播一条随机缓存接话；正在说/排着队就什么都不做。"""
        if self.speaking:
            return
        cands = [p for p in (self._cached(t) for t in self.backchannels) if p]
        if not cands:
            return
        with self._lock:
            if self._pending:
                return
            gen = self._gen
            self._pending += 1
        last = getattr(self, "_last_ack", None)
        pick = random.choice([c for c in cands if c != last] or cands)     # 不连着说同一句
        self._last_ack = pick
        self.last_ack_text = next((t for t in self.backchannels if self._cached(t) == pick), "")
        self.last_ack_at = time.time()
        self._play_q.put((gen, pick, False, None, None))

    def stop(self) -> None:
        """打断：作废待合成/待播的句子，杀掉正在播的 ffplay。"""
        with self._lock:
            self._gen += 1
            self._pending = 0
            proc, self._proc = self._proc, None
            conn = self._stream_conn
        if conn is not None:                   # 正在收的那句流式合成：断开，把嗓子让给下一句
            try:
                conn.shutdown(2)
            except Exception:
                pass
        for q in (self._synth_q, self._play_q):
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if q is self._play_q and item[2]:
                    _unlink(item[1])
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    @property
    def speaking(self) -> bool:
        return self._pending > 0

    def warm(self) -> None:
        """后台预合成接话缓存；已有就跳过。可以反复调用（服务起来后再调一次补克隆音色版）。"""
        if self._warm_thread and self._warm_thread.is_alive():
            return
        self._warm_thread = threading.Thread(target=self._warm, name="mouth-warm", daemon=True)
        self._warm_thread.start()

    def ensure_server(self) -> bool:
        """当前引擎的服务 /health 通 → True。不通就 DETACHED 拉起它（只拉 voice.json 选中的那个），
        立刻返回 False（不等它加载）。gptsovits → zuhui/tts_gsv.py；indextts → zuhui/tts_server.py。"""
        if self._server_port():
            return True
        engine = self._engine()
        if engine == "gptsovits":
            g = self._gsv()
            if not (GSV_PY.exists() and os.path.isdir(str(g.get("dir") or ""))):
                return False
            launch, what = self._launch_gsv, "GPT-SoVITS 服务已拉起（后台加载约半分钟）"
        else:
            if not (self.venv and os.path.exists(self.venv) and SERVER_PY.exists()):
                return False
            launch, what = self._launch_server, "IndexTTS2 服务已拉起（后台加载 1-3 分钟）"
        if self._server_starting() or time.time() - self._launched_at < RELAUNCH_COOLDOWN:
            return False
        try:
            launch()
            self._launched_at = time.time()
            log.info("%s，期间用备用嗓子", what)
        except Exception as e:
            log.warning("拉起 %s 服务失败: %r", engine, e)
        return False

    def release_server(self, min_idle: float = 0.0) -> bool:
        """关掉配音服务，把显存还回去（下次 ensure_server 再拉起）。服务自己报的空闲时间不到 min_idle 秒就不关
        （别的后端，比如模拟会，可能正在用）。真发了退出 → True。"""
        port = self._server_port()
        if not port:
            return False
        try:
            h = _http("GET", port, "/health", timeout=2)
            if min_idle and float(h.get("idle_s") or 0) < min_idle:
                return False
            _http("POST", port, "/quit", {}, timeout=5)
            log.info("配音服务空闲 %.0f 秒，已关掉释放显存", float(h.get("idle_s") or 0))
            return True
        except Exception as e:
            log.warning("关配音服务失败: %r", e)
            return False

    # ------------------------------------------------------------ 服务

    @staticmethod
    def _voice() -> dict:
        for p in (VOICE_FILE, OLD_MEET / "voice.json"):
            v = _read_json(p)
            if isinstance(v, dict) and v:
                return v
        return {}

    def _engine(self) -> str:
        e = str(self._voice().get("engine") or "indextts").strip().lower()
        return e if e in ENGINES else "indextts"

    def _gsv(self) -> dict:
        g = self._voice().get("gsv")
        return g if isinstance(g, dict) else {}

    def _server_port(self) -> int | None:
        """当前引擎的服务在线 → 端口。/health 里的 engine 要对得上（IndexTTS2 的 /health 不带 engine）。"""
        engine = self._engine()
        if engine == "gptsovits":
            ports = [int(self._gsv().get("port") or GSV_PORT)]
        else:
            run = _read_json(RUN_FILE) or {}
            ports = [int(run.get("port") or self.port_default), self.port_default]
        for port in dict.fromkeys(ports):
            try:
                h = _http("GET", port, "/health", timeout=2)
            except Exception:
                continue
            if h.get("ok") and str(h.get("engine") or "indextts") == engine:
                return port
        return None

    def _server_starting(self) -> bool:
        """进度文件还在跳 = 正在加载权重。"""
        j = _read_json(GSV_PROGRESS_FILE if self._engine() == "gptsovits" else PROGRESS_FILE) or {}
        try:
            return j.get("status") == "running" and time.time() - float(j.get("updated_at") or 0) < 60
        except Exception:
            return False

    def _launch_gsv(self) -> None:
        """tts_gsv.py 只用标准库，用当前 python 跑；它自己再用整合包的 runtime python 拉起 api_v2.py。"""
        py = sys.executable or "python"
        logf = open(STATE / "tts_gsv.log", "a", encoding="utf-8")
        env = dict(os.environ)
        env["ZUHUI_HOME"] = str(STATE)
        subprocess.Popen([py, "-u", str(GSV_PY)], stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                         creationflags=DETACHED, close_fds=True, cwd=str(STATE), env=env)

    def _launch_server(self) -> None:
        logf = open(STATE / "tts_server.log", "a", encoding="utf-8")
        env = dict(os.environ)
        env["ZUHUI_HOME"] = str(STATE)
        try:
            cwd = str(Path(self.venv).parents[2])      # <IndexTTS2 目录>\.venv\Scripts\python.exe → <IndexTTS2 目录>
        except IndexError:
            cwd = None
        if cwd and not os.path.isdir(cwd):
            cwd = None
        subprocess.Popen([self.venv, "-u", str(SERVER_PY)], stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                         creationflags=DETACHED, close_fds=True, cwd=cwd, env=env)

    # ------------------------------------------------------------ 缓存

    @staticmethod
    def _voice_ref() -> str:
        """IndexTTS2 的参考音频（voice.json 顶层 ref）。"""
        for p in (VOICE_FILE, OLD_MEET / "voice.json"):
            v = _read_json(p) or {}
            if v.get("ref"):
                return str(v["ref"])
        return DEFAULT_REF

    def _voice_id(self) -> str:
        """克隆音色缓存用的音色标识。indextts 沿用老 key（= 参考音频路径），切回去老缓存还能用；
        gptsovits 把引擎、参考音频、参考文本、模型版本都算进去，换任何一个都不会放出旧声音。"""
        if self._engine() != "gptsovits":
            return self._voice_ref()
        g = self._gsv()
        return "gptsovits|{}|{}|{}|{}".format(g.get("ref") or "", g.get("prompt_text") or "",
                                              g.get("version") or "", g.get("cache_tag") or "")

    def _cache_key(self, text: str, voice: str) -> Path:
        # 克隆音色念的是 tts_text() 换过多音字的那一份：换法变了，缓存跟着换（没换字的句子 key 不变，老缓存照用）
        said = tts_text(text) if not voice.startswith(("edge:", "sapi:")) else text
        h = hashlib.md5(f"{said}\n{voice}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{h}.wav"

    def _cached(self, text: str) -> Path | None:
        """这条接话已有的缓存：优先克隆音色，其次备用嗓子。"""
        for voice in (self._voice_id(), f"edge:{self.edge_voice}", f"sapi:{self.sapi_voice}"):
            p = self._cache_key(text, voice)
            if p.exists() and p.stat().st_size > 1000:
                return p
            mp3 = p.with_suffix(".mp3")
            if mp3.exists() and mp3.stat().st_size > 1000:
                return mp3
        return None

    def has_clone(self, text: str) -> bool:
        """这句已经有克隆音色的缓存（马上能放，不用等合成）。"""
        return self._clone_cached(text) is not None

    def _clone_cached(self, text: str) -> Path | None:
        """这句话的克隆音色缓存（没有就 None；不拿备用嗓子顶，免得一段话里换了个人）。"""
        p = self._cache_key(text, self._voice_id())
        return p if (p.exists() and p.stat().st_size > 1000) else None

    def _warm(self) -> None:
        ref = self._voice_id()
        engine = self._engine()
        for text in self.backchannels + [t for t in speakable_list(self.phrases)]:
            p = self._cache_key(text, ref)
            if p.exists() and p.stat().st_size > 1000:
                continue
            while self.speaking:                      # 正式回复优先，别跟它抢服务
                time.sleep(0.2)
            tmp = self.tmp / f"warm-{uuid.uuid4().hex[:8]}.wav"   # 先写临时文件，成功再挪进缓存，免得留半截
            port = self._server_port()
            if port and self._synth_server(text, tmp, port):
                os.replace(tmp, p)
                log.info("warm %s: %s", engine, text)
                continue
            # 服务不在：先用备用嗓子垫一份（另一个 key），服务起来后再 warm() 会补克隆音色版
            if self._cached(text):
                continue
            out, key, backend = None, None, None
            if self.edge_tts:
                out = self._synth_edge(text, tmp)
                key, backend = self._cache_key(text, f"edge:{self.edge_voice}"), "edge"
            if not out:
                out = self._synth_sapi(text, tmp)
                key, backend = self._cache_key(text, f"sapi:{self.sapi_voice}"), "sapi"
            if out:
                os.replace(out, key.with_suffix(Path(out).suffix))
                log.info("warm %s: %s", backend, text)

    # ------------------------------------------------------------ 固定短句核对

    @staticmethod
    def _pcm16k(path) -> bytes | None:
        """wav（任意采样率、单/双声道）→ 16 kHz 单声道 s16le。"""
        try:
            import wave
            import numpy as np
            with wave.open(str(path), "rb") as w:
                sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
                raw = w.readframes(w.getnframes())
            if sw != 2:
                return None
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if ch > 1:
                a = a.reshape(-1, ch).mean(axis=1)
            if sr != 16000 and len(a):
                n = int(len(a) * 16000 / sr)
                a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
            return np.clip(a, -32768, 32767).astype(np.int16).tobytes()
        except Exception:
            return None

    def verify_cache(self, asr, same, tries: int = 3, stop=None) -> dict:
        """克隆音色的固定短句缓存逐条转写一遍：和原文对不上就重新合成（最多 tries 次）。
        asr(16k pcm bytes) -> 文字或 None；same(念出来的, 原文) -> bool；stop() 为真就停（开会了）。"""
        port = self._server_port()
        if not port:
            return {"skipped": "嗓子服务不在"}
        ref = self._voice_id()
        stats = {"checked": 0, "fixed": [], "still_bad": []}
        for text in self.backchannels + [t for t in speakable_list(self.phrases)]:
            if text.isascii():
                continue                                   # 英文固定话：中文识别核不了，不核（免得白白重合成三遍）
            if stop is not None and stop():
                stats["stopped"] = True
                break
            p = self._cache_key(text, ref)
            if not (p.exists() and p.stat().st_size > 1000):
                continue
            pcm = self._pcm16k(p)
            got = asr(pcm) if pcm else None
            if got is None:
                continue                                   # 听不了（快识别没起来）：这条不判
            stats["checked"] += 1
            if same(got, text):
                continue
            log.warning("固定短句念得不对：%r 念成了 %r，重新合成", text, got)
            for _ in range(tries):
                while self.speaking:
                    time.sleep(0.2)
                tmp = self.tmp / f"fix-{uuid.uuid4().hex[:8]}.wav"
                if self._synth_server(text, tmp, port):
                    pcm = self._pcm16k(tmp)
                    got = asr(pcm) if pcm else None
                    if got is not None and same(got, text):
                        os.replace(tmp, p)
                        stats["fixed"].append(text)
                        break
                _unlink(tmp)
            else:
                stats["still_bad"].append(text)
        return stats

    # ------------------------------------------------------------ 合成

    def _synthesize(self, text: str) -> Path | None:
        base = self.tmp / f"say-{uuid.uuid4().hex[:8]}.wav"
        port = self._server_port()
        if port and self._synth_server(text, base, port):
            self.backend = self._engine()
            return base
        if not port:
            self.ensure_server()                       # 服务掉了（空闲退出/崩了）：后台拉起，这句先用备用嗓子
        if self.edge_tts:
            p = self._synth_edge(text, base)
            if p:
                self.backend = "edge"
                return p
        p = self._synth_sapi(text, base)
        if p:
            self.backend = "sapi"
            return p
        log.warning("所有 TTS 后端都失败: %r", text)
        return None

    def _too_short(self, text: str, st: _Stream) -> bool:
        """流式收完比字数该念的短一大截（GPT-SoVITS 偶尔吞掉半句）。整句接口有这道检查、会换种子重来；流式已经放出去了，只能事后补念。"""
        try:
            from .tts_gsv import expected_range
            lo, _ = expected_range(tts_text(text))
            speed = float(self._gsv().get("speed") or 1.0)
        except Exception:
            return False
        dur = st.seconds()
        if dur and dur * speed < 0.85 * lo:
            log.warning("流式这句只有 %.2f 秒（按字数至少 %.2f），整句重念：%r", dur, lo, text)
            return True
        return False

    @staticmethod
    def _synth_server(text: str, out: Path, port: int) -> bool:
        """克隆音色服务（tts_gsv.py / tts_server.py，同一套接口）合成一句到 out。"""
        try:
            r = _http("POST", port, "/say", {"text": tts_text(text), "play": False, "wav": str(out)}, timeout=180)
            return bool(r.get("ok")) and out.exists() and out.stat().st_size > 1000
        except Exception as e:
            log.warning("tts :%s /say 失败: %r", port, e)
            return False

    _synth_indextts = _synth_server                    # 旧名字

    def _synth_edge(self, text: str, out: Path) -> Path | None:
        """edge-tts 出 mp3；有 ffmpeg 就转成 out(.wav)，没有就原样留成 .mp3（ffplay 两种都能放）。"""
        if not self.edge_tts or time.time() < self._edge_down_until:
            return None
        txt, mp3 = out.with_suffix(".txt"), out.with_suffix(".mp3")
        txt.write_text(text, encoding="utf-8")
        base = [self.edge_tts, "--voice", self.edge_voice, "--rate", self.edge_rate,
                "--file", str(txt), "--write-media", str(mp3)]
        attempts = [base]
        from .netenv import proxy as _proxy
        proxy = _proxy()                 # 后端环境里的代理被拿掉了（会议连接直连），edge-tts 用原来那个
        if proxy:
            attempts.append(base + ["--proxy", proxy])
        try:
            for cmd in attempts:
                try:
                    r = subprocess.run(cmd, capture_output=True, timeout=40, creationflags=CREATE_NO_WINDOW)
                except Exception as e:
                    log.warning("edge-tts 失败: %r", e)
                    continue
                if r.returncode == 0 and mp3.exists() and mp3.stat().st_size > 1000:
                    if self.ffmpeg:
                        subprocess.run([self.ffmpeg, "-y", "-loglevel", "quiet", "-i", str(mp3), str(out)],
                                       capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
                        if out.exists() and out.stat().st_size > 1000:
                            _unlink(mp3)
                            return out
                    return mp3
                _unlink(mp3)   # 失败时 edge-tts 会留一个 0 字节文件
                err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
                log.warning("edge-tts 退出码 %s: %s", r.returncode, err[-1] if err else "")
            # 两次都失败（常见是微软端 403）：接下来几分钟直接跳过 edge，别让每句都白等
            self._edge_down_until = time.time() + EDGE_COOLDOWN
            return None
        finally:
            _unlink(txt)

    def _synth_sapi(self, text: str, out: Path) -> Path | None:
        txt = out.with_suffix(".txt")
        txt.write_text(text, encoding="utf-8")
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"try {{ $s.SelectVoice('{self.sapi_voice}') }} catch {{ }}; "
            "$s.Rate = 1; "
            f"$s.SetOutputToWaveFile('{out}'); "
            f"$s.Speak([IO.File]::ReadAllText('{txt}', [Text.Encoding]::UTF8)); "
            "$s.Dispose()"
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            log.warning("SAPI 失败: %r", e)
        finally:
            _unlink(txt)
        return out if out.exists() and out.stat().st_size > 1000 else None

    # ------------------------------------------------------------ 流式（边合成边播）

    def _can_stream(self) -> bool:
        return (self.stream and bool(self.ffplay) and self._engine() == "gptsovits"
                and time.time() >= self._stream_off_until)

    def _stream_synth(self, text: str, st: _Stream, port: int, gen: int) -> bool:
        """去嗓子服务 /stream 收这句的 PCM，来一块塞一块。→ 收到过声音就 True。被打断（gen 变了）就断开连接。"""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            conn.connect()
            self._stream_conn = conn.sock          # 打断时直接关这个 socket（响应开始后 conn.sock 会被交给响应、变成 None）
            body = json.dumps({"text": tts_text(text)}, ensure_ascii=False).encode("utf-8")
            conn.request("POST", "/stream", body, {"Content-Type": "application/json; charset=utf-8"})
            r = conn.getresponse()
            if r.status != 200:
                msg = r.read()[:200]
                if r.status == 404:                    # 老版本服务没有 /stream：这一阵子都走整句
                    self._stream_off_until = time.time() + 600
                log.warning("流式合成 %s：%r，这句退回整句", r.status, msg)
                return False
            st.sr = int(r.getheader("X-Sample-Rate") or 32000)
            while gen == self._gen:
                b = r.read1(32768)
                if not b:
                    break
                st.put(b)
            return st.nbytes > 0
        except Exception as e:
            if gen == self._gen:
                log.warning("流式合成断了 %r（已收 %.1f 秒）", e, st.seconds())
            return st.nbytes > 0
        finally:
            self._stream_conn = None
            try:
                conn.close()
            except Exception:
                pass

    def _pipe_player(self, sr: int):
        """从管道读裸 PCM 的 ffplay：有提前起好的就用它（久没运行时冷起要一秒多），再在后台补一个。"""
        with self._spare_lock:
            sp, self._spare = self._spare, None
        proc = sp[1] if (sp and sp[0] == sr and sp[1].poll() is None) else None
        if sp and proc is None:
            self._kill(sp[1])
        if proc is None:
            proc = self._spawn_player(sr)
        threading.Thread(target=self._refill_spare, args=(sr,), name="mouth-spare", daemon=True).start()
        return proc

    def _refill_spare(self, sr: int) -> None:
        try:
            p = self._spawn_player(sr)
        except Exception:
            return
        with self._spare_lock:
            old, self._spare = self._spare, (sr, p)
        if old:
            self._kill(old[1])

    def _spawn_player(self, sr: int):
        cmd = [self.ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
               "-f", "s16le", "-ar", str(sr), "-ch_layout", "mono",
               "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer"]
        if abs(self.rate - 1.0) > 0.01:
            cmd += ["-af", f"atempo={min(2.0, max(0.5, self.rate)):.2f}"]
        cmd += ["-i", "pipe:0"]
        # bufsize=0：写一块就立刻进管道（带缓冲的话每块最后几 KB 要等下一块才送出去，ffplay 中途饿肚子，
        # 实测每句多放 0.4-0.6 秒）
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                bufsize=0, creationflags=CREATE_NO_WINDOW)

    @staticmethod
    def _kill(proc) -> None:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def _play_stream(self, st: _Stream, gen: int, cb=None, said=None) -> None:
        proc = self._pipe_player(self._stream_sr)          # 和合成第一块并行起进程
        finished = False
        try:
            need = self._est_seconds(st.text)
            while gen == self._gen and not self._ready(st, need):
                st.wait(0.05)                              # 攒够再放，免得半路断
            if gen != self._gen:
                return
            if st.nbytes == 0:                             # 流式没出声：放整句后备（没有就算了）
                self._kill(proc)
                proc = None
                if st.fallback is not None:
                    try:
                        self._play(st.fallback, gen, cb, said)
                    finally:
                        _unlink(st.fallback)
                return
            if st.sr != self._stream_sr:                   # 采样率和预先拉起的不一样（换了模型）：重起
                self._kill(proc)
                self._stream_sr = st.sr
                proc = self._pipe_player(st.sr)
            if not self._wait_gate(gen, first=cb is not None):
                return
            if cb is not None:                             # 这一句开始出声了：逐字稿这时才显示它
                try:
                    cb(said)
                except Exception:
                    log.exception("on_start 回调出错")
            rec = [time.time(), None]
            self.played.append(rec)
            del self.played[:-100]
            with self._lock:
                if gen != self._gen:
                    rec[1] = time.time()
                    return
                self._proc = proc
            hook = getattr(self, "on_play_stream", None)   # 会议语音桥：同一句也推给通过链接进来的人
            if hook is not None:
                try:
                    hook(st, self.rate, proc)
                except Exception:
                    log.exception("on_play_stream 回调出错")
            t_start, written, i, dry, gaps = time.time(), 0, 0, 0.0, 0
            t_play = t_start
            bps = st.sr * 2.0 * self.rate
            try:
                while gen == self._gen:
                    b, done = st.get(i)
                    if b is None:
                        if done:
                            break
                        continue
                    i += 1
                    late = time.time() - (t_start + written / bps)   # >0：这一块到的时候前面的已经放完了（断流）
                    if late > 0.05 and written:
                        dry += late
                        gaps += 1
                        t_start += late
                    proc.stdin.write(b)
                    PLAYBACK.put_at("tts", b, t_start + written / bps, st.sr or self._stream_sr, self.rate)
                    written += len(b)
                proc.stdin.close()
            except OSError:
                pass                                       # ffplay 被打断杀掉了
            proc.wait()
            finished = True
            rec[1] = time.time()
            PLAYBACK.cut("tts", rec[1])                    # 被打断：后面排好的那截不算在放
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            log.info("流式播放：首块 %.2fs，开始写 %.2fs（含等你说完），%.1f 秒音频，断流 %d 次共 %.2fs",
                     (st.first or t_play) - st.t0, t_play - st.t0, st.seconds(), gaps, dry)
        finally:
            if not finished or gen != self._gen:
                self._kill(proc)

    def _est_seconds(self, text: str) -> float:
        # 这句大概念多久（原速，宁长勿短）：中文约 0.24 秒/字（实测 0.2-0.28），英文词按 1.5 字、数字按 0.7 字
        try:
            from .tts_gsv import HAN, LATIN_WORD, DIGIT
            t = tts_text(text)
            u = len(HAN.findall(t)) + 1.5 * len(LATIN_WORD.findall(t)) + 0.7 * len(DIGIT.findall(t))
        except Exception:
            u = len(text)
        return 0.24 * max(u, 1.0) + 0.3

    def _ready(self, st: _Stream, need: float) -> bool:
        # 可以开始放了吗：整句收完；或者攒够 prebuf，且按现在的合成速度，剩下的能赶在播放前面
        # （合成比播放慢时，开头要多攒：need × (1 − 合成速度 / 播放倍速)，显卡被识别抢着用时会这样）
        if st.done:
            return True
        have = st.seconds()
        if have < self.prebuf:
            return False
        v = st.speed()
        if v is None:
            return False
        rate = max(self.rate, 0.5)
        if v >= rate:
            return True
        return have >= need * (1.0 - v / rate) + 0.15

    GATE_WAIT_S = 8.0           # 门一般最多等这么久（你一直在说：8 秒后照旧开口）
    GATE_HOLD_S = 30.0          # gate_hold 说"访客还在出声"：接着等，最多等这么久，还在说就这一轮不说了

    def _stale(self) -> bool:
        st = getattr(self, "stale", None)
        try:
            return bool(st()) if st is not None else False
        except Exception:
            return False

    def _drop_stale(self) -> bool:
        """这一轮过时了（访客在被答的那一轮之后又说了一阵）：交给 on_stale（app 把这一轮并进他正在说的那一轮），这一句不放。"""
        cb = getattr(self, "on_stale", None)
        try:
            if cb is not None:
                cb()
            else:
                self.stop()
        except Exception:
            log.exception("on_stale 出错")
            self.stop()
        return False

    def _wait_gate(self, gen: int, first: bool = False) -> bool:
        """一轮话的第一句：你或者访客还在说就先等着，不抢话。→ 没被打断就 True。
        等到 GATE_WAIT_S 秒：gate_hold() 说访客还在出声就接着等（最多 GATE_HOLD_S 秒，还在说就这一轮不说了），
        否则照旧开口。first（有 on_start 的正经一句，不是"嗯"这种接话）：开口前问一次 stale()，过时了就不放。"""
        gate = getattr(self, "gate", None)
        if gate is not None and not self.audible:
            t_wait = time.time()
            while gen == self._gen:
                try:
                    ok = bool(gate())
                except Exception:
                    ok = True
                if ok:
                    break
                if first and self._stale():
                    return self._drop_stale()
                waited = time.time() - t_wait
                if waited >= self.GATE_WAIT_S:
                    hold = getattr(self, "gate_hold", None)
                    try:
                        held = bool(hold()) if hold is not None else False
                    except Exception:
                        held = False
                    if not held:
                        break
                    if waited >= self.GATE_HOLD_S:
                        log.info("出声那道门：访客一直在说（%.0f 秒），这一轮不说了", waited)
                        self.stop()
                        return False
                time.sleep(0.05)
        if first and gen == self._gen and not self.audible and self._stale():
            return self._drop_stale()
        return gen == self._gen

    # ------------------------------------------------------------ 线程

    def _synth_loop(self) -> None:
        while True:
            gen, text, cb = self._synth_q.get()
            if gen != self._gen:
                continue
            hit = self._clone_cached(text)
            if hit is not None:                        # 固定的话：直接放缓存，不用等合成
                self._play_q.put((gen, hit, False, cb, text))
                continue
            port = self._server_port() if self._can_stream() else None
            if port:                                   # 克隆音色边合成边播：先排进播放队列，再去收
                st = _Stream(text)
                self._play_q.put((gen, st, False, cb, text))
                ok = self._stream_synth(text, st, port, gen)
                if not ok and gen == self._gen:
                    try:
                        st.fallback = self._synthesize(text)
                    except Exception:
                        log.exception("合成异常: %r", text)
                st.finish()
                if ok and gen == self._gen and self._too_short(text, st):
                    try:
                        path = self._synthesize(text)  # 整句接口会换种子重来
                    except Exception:
                        path = None
                    if path is not None and gen == self._gen:
                        with self._lock:
                            if gen == self._gen:
                                self._pending += 1
                        self._play_q.put((gen, path, True, None, text))
                    else:
                        _unlink(path)
                continue
            try:
                path = self._synthesize(text)
            except Exception:
                log.exception("合成异常: %r", text)
                path = None
            if gen != self._gen:
                _unlink(path)
                continue
            if path is None:
                with self._lock:
                    if gen == self._gen:
                        self._pending -= 1
                continue
            self._play_q.put((gen, path, True, cb, text))

    def _play_loop(self) -> None:
        while True:
            gen, path, delete, cb, said = self._play_q.get()
            if gen != self._gen:
                if delete:
                    _unlink(path)
                continue
            try:
                if isinstance(path, _Stream):
                    self._play_stream(path, gen, cb, said)
                else:
                    self._play(path, gen, cb, said)
            except Exception:
                log.exception("播放异常: %s", path)
            finally:
                if delete:
                    _unlink(path)
                with self._lock:
                    if gen == self._gen:
                        self._pending -= 1

    def _play(self, path: Path, gen: int, cb=None, said=None) -> None:
        path = Path(path)
        if self.ffplay:
            cmd = [self.ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet"]
            if abs(self.rate - 1.0) > 0.01:
                cmd += ["-af", f"atempo={min(2.0, max(0.5, self.rate)):.2f}"]
            cmd.append(str(path))
        else:
            wav = path
            if path.suffix.lower() != ".wav":
                if not self.ffmpeg:
                    log.warning("没有 ffplay/ffmpeg，放不了 %s", path)
                    return
                wav = path.with_suffix(".play.wav")
                subprocess.run([self.ffmpeg, "-y", "-loglevel", "quiet", "-i", str(path), str(wav)],
                               capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   f"(New-Object Media.SoundPlayer '{wav}').PlaySync()"]
        if gen != self._gen:
            return
        if not self._wait_gate(gen, first=cb is not None):
            return
        if cb is not None:                     # 这一句开始出声了：逐字稿这时才显示它
            try:
                cb(said)
            except Exception:
                log.exception("on_start 回调出错")
        rec = [time.time(), None]
        self.played.append(rec)
        del self.played[:-100]
        with self._lock:
            if gen != self._gen:
                rec[1] = time.time()
                return
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
            self._proc = proc
        _ref_file(path, rec[0], self.rate)     # 这一句记进回声参考（ffplay 起来要一两百毫秒，正好趁这会儿读文件）
        hook = getattr(self, "on_play", None)  # 会议语音桥：同一句也推给通过链接进来的人（见 bridge.py）
        if hook is not None:
            try:
                hook(path, self.rate, proc)
            except Exception:
                log.exception("on_play 回调出错")
        proc.wait()
        rec[1] = time.time()
        PLAYBACK.cut("tts", rec[1])            # 被打断：后面那截不算在放
        with self._lock:
            if self._proc is proc:
                self._proc = None


def _main():
    import sys
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="[mouth %(asctime)s] %(message)s", datefmt="%H:%M:%S")
    from .config import load
    text = " ".join(sys.argv[1:]) or "老师好，嘴这边通了。这是第二句。"
    m = Mouth(load())
    print("server:", m.ensure_server())
    m.warm()
    m.say(text)
    t0 = time.time()
    while m.speaking and time.time() - t0 < 120:
        time.sleep(0.2)
    print("backend:", m.backend, "speaking:", m.speaking)


if __name__ == "__main__":
    _main()
