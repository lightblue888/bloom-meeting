# -*- coding: utf-8 -*-
"""GPT-SoVITS 适配服务 —— 组会助手的嗓子（快）。接口和 tts_server.py（IndexTTS2）一模一样，后面转给 GPT-SoVITS 的 api_v2.py。

启动（voice.json 里 engine=gptsovits 时 Mouth.ensure_server() 会 DETACHED 自动这样拉起）：
    python -u <项目目录>\\zuhui\\tts_gsv.py
本文件只 import 标准库，用哪个 python 跑都行。它自己用整合包的 runtime\\python.exe 拉起 api_v2.py
（cwd = 整合包目录，日志 ~/.zuhui/gsv_api.log），等它加载完、预热一句，才开自己的端口，
并写 ~/.zuhui/tts_server.json（{"pid","port","engine":"gptsovits"}；view.py 的"嗓子就绪"看这个文件找端口）。

接口（127.0.0.1，端口 voice.json gsv.port，默认 47322）：
    GET  /health  → {"ok": true, "loaded": true, "engine": "gptsovits", "voice": 参考音频, "idle_s": 12, ...}
    POST /say {"text": "...", "play": true, "wav": 可选}
                  → {"ok": true, "synth_ms": 420, "dur": 2.1, "wav": "..."}；play=false + wav=<路径>：只合成不播
    POST /stream {"text": "..."} → 流式：边合成边吐裸 PCM（s16le 单声道，采样率在响应头 X-Sample-Rate），
                  第一块约 0.25-0.35 秒就到、和句子长短无关；句首空白剪掉，句中/句末一段空白最长 stream_gap_cap 秒。
                  没有换种子重来（已经放出去了），太短的那种由调用方自己判断要不要整句重念。
    POST /quit    → 退出（连带关掉自己拉起的 api_v2）

配置 ~/.zuhui/voice.json 的 "gsv" 段（见 DEFAULTS；合成参数每句现读，改了立刻生效；
dir/python/config/api_port 改了要重启本服务）。
空闲 gsv.idle_s 秒（默认 6 小时；0 = 不退）自动退出，并关掉自己拉起的 api_v2，释放显存。
合成出来的音频太短或长得离谱（相对字数）就换个随机种子重来一次，免得把坏句子放出去。
"""
import array
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import struct
import time
import urllib.error
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = os.environ.get("ZUHUI_HOME") or os.path.join(os.path.expanduser("~"), ".zuhui")
CFG = os.path.join(STATE, "voice.json")
RUN = os.path.join(STATE, "tts_server.json")
API_RUN = os.path.join(STATE, "gsv_api.json")
API_LOG = os.path.join(STATE, "gsv_api.log")
CREATE_NO_WINDOW = 0x08000000

def _local(key):
    """这台电脑上的路径：~/.zuhui/paths.json（本文件只用标准库，自己读）。"""
    try:
        with open(os.path.join(STATE, "paths.json"), encoding="utf-8") as f:
            return str(json.load(f).get(key) or "")
    except (OSError, ValueError, AttributeError):
        return ""


DEFAULTS = {
    "dir": _local("gsv_dir"),                           # 整合包根目录（有 api_v2.py 和 runtime\）；~/.zuhui/paths.json
    "python": "",                                       # 空 = <dir>\runtime\python.exe
    "config": r"GPT_SoVITS\configs\tts_infer_zuhui.yaml",   # 相对 dir
    "api_port": 9880,
    "port": 47322,
    "ref": "",
    "prompt_text": "",
    "prompt_lang": "zh",
    "text_lang": "zh",              # zh = 中英混合（论文里常夹英文词）；all_zh = 纯中文
    "top_k": 15,
    "top_p": 1.0,
    "temperature": 1.0,
    "repetition_penalty": 1.35,
    "speed": 1.0,                   # 合成语速；播放端 mouth.rate 另外再加速
    "text_split_method": "cut5",    # 按标点切；嘴那边已经按句切过了
    "batch_size": 8,
    "parallel_infer": True,
    "sample_steps": None,           # 只对 v3/v4/v5 有用；None = 模型默认（v5turbo 4 步）
    "fragment_interval": 0.25,
    "stream_mode": 3,               # /stream 用：3 = 固定长度切块（首包 0.25-0.35 s，与句长无关；2026-11-12 实测）；
                                    # 2 = 在停顿处切（首包 1-3 s）。整句（/say）不受影响
    "stream_chunk": 8,              # 每块最少几个语义 token；8 比 12 断流余量大（实测 12 在短句上会断 0.17 s）
    "stream_lead_keep": 0.04,       # 流式句首的空白只留这么长（剪掉 = 早出声）
    "stream_gap_cap": 0.6,          # 流式里一段空白最长这么久，多的丢掉（GPT-SoVITS 偶尔句中/句末卡出两三秒空白）
    "seed": -1,
    "idle_s": 21600,
}

sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".claude", "progress"))
try:
    from claude_progress import Progress   # 只依赖标准库；不在也不影响服务
except Exception:
    Progress = None

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机服务不走系统代理


def log(msg):
    print(f"[gsv {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def gsv_cfg() -> dict:
    g = dict(DEFAULTS)
    g.update((read_json(CFG) or {}).get("gsv") or {})
    if not g.get("python"):
        g["python"] = os.path.join(g["dir"], "runtime", "python.exe")
    return g


def api_call(port, path, body=None, timeout=120.0):
    """→ (http 状态码, content-type, bytes)。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with _NO_PROXY.open(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type") or "", r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type") or "", e.read()


def api_up(port) -> bool:
    """端口上是 GPT-SoVITS api_v2（openapi 里有 /tts）。"""
    try:
        code, _, body = api_call(port, "/openapi.json", timeout=2)
        return code == 200 and b'"/tts"' in body
    except Exception:
        return False


def pid_alive(pid) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"], capture_output=True, text=True,
                             timeout=10, creationflags=CREATE_NO_WINDOW).stdout
        return str(int(pid)) in out
    except Exception:
        return False


def kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True, timeout=20,
                       creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def wav_duration(path) -> float:
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def dead_air(path, floor: float = 0.01) -> float:
    """一句话中间最长的一段静音（秒，不算开头结尾）。GPT-SoVITS 偶尔在句中卡出两三秒空白
    （2026-11-11 试听样例 "文件就在 Desktop 文件夹里。" 一遍 5.2 秒，中间空了 3.1 秒；正常的逗号停顿不到 0.8 秒）。"""
    try:
        import array
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                return 0.0
            sr, ch = w.getframerate(), w.getnchannels()
            a = array.array("h", w.readframes(w.getnframes()))
        if ch > 1:
            a = a[::ch]
        hop = max(1, sr // 50)                       # 20 ms
        lim = floor * 32768.0
        quiet = []
        n = len(a[0:hop:4]) or 1
        for i in range(0, len(a) - hop, hop):
            quiet.append((sum(x * x for x in a[i:i + hop:4]) / n) ** 0.5 < lim)
        # 去掉开头结尾的静音，只看中间
        while quiet and quiet[0]:
            quiet.pop(0)
        while quiet and quiet[-1]:
            quiet.pop()
        best = run = 0
        for q in quiet:
            run = run + 1 if q else 0
            best = max(best, run)
        return best * hop / float(sr)
    except Exception:
        return 0.0


DEAD_AIR_S = 1.6     # 句中空白超过这么久 = 卡住了，换种子重来
EDGE_KEEP_S = 0.25   # 开头结尾的静音最多留这么长


def trim_edges(path, floor: float = 0.01, keep: float = EDGE_KEEP_S) -> float:
    """开头结尾的空白剪到 keep 秒（GPT-SoVITS 偶尔在句末拖两三秒空白：2026-11-11 试听样例
    "文件就在 Desktop 文件夹里。"一遍 5.2 秒，后面 3 秒没声音，播的时候就是干等）。返回剪掉的秒数。"""
    try:
        import array
        with wave.open(path, "rb") as w:
            params = w.getparams()
            if params.sampwidth != 2 or params.nchannels != 1:
                return 0.0
            a = array.array("h", w.readframes(params.nframes))
        sr = params.framerate
        hop = max(1, sr // 50)
        lim = floor * 32768.0
        n = len(a[0:hop:4]) or 1      # 每 4 个采样取 1 个算响度，够用也快
        loud = [i for i in range(0, len(a) - hop, hop)
                if (sum(x * x for x in a[i:i + hop:4]) / n) ** 0.5 >= lim]
        if not loud:
            return 0.0
        k = int(keep * sr)
        s, e = max(0, loud[0] - k), min(len(a), loud[-1] + hop + k)
        cut = (len(a) - (e - s)) / float(sr)
        if cut < 0.1:
            return 0.0
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(a[s:e].tobytes())
        return cut
    except Exception:
        return 0.0


HAN = re.compile(r"[\u4e00-\u9fff]")
LATIN_WORD = re.compile(r"[A-Za-z]+")
DIGIT = re.compile(r"\d")


def expected_range(text: str):
    """按字数估一句话正常该念多久（秒，speed=1，开头结尾的空白已经剪掉）。中文约 4-6 字/秒，英文词约 2-3 个/秒。
    下限 0.17 秒/字：2026-11-11 试听 525 遍里正常最快 0.19；整半句被吞掉的 4 遍在 0.13-0.167（"平时用 Flash，回得比较快。"
    只念出半句），会被拦下换种子重来。"""
    units = len(HAN.findall(text)) + 1.5 * len(LATIN_WORD.findall(text)) + 0.7 * len(DIGIT.findall(text))
    units = max(units, 1.0)
    return 0.17 * units, 0.55 * units + 2.0


class State:
    lock = threading.Lock()
    last = time.time()
    server = None
    proc = None          # 自己拉起的 api_v2 进程（Popen）
    api_pid = None       # 要负责关掉的 api_v2 pid（自己拉起的或上次遗留下来接管的）
    api_port = 9880
    ready = False
    restarting = False
    stats = {"n": 0, "retries": 0, "fails": 0}


# ---------------------------------------------------------------- api_v2 进程

def launch_api(g, beat=None) -> None:
    """拉起 api_v2.py 并等它能用（最多 5 分钟）。端口上已经有一个就直接用。"""
    port = int(g["api_port"])
    State.api_port = port
    if api_up(port):
        old = (read_json(API_RUN) or {}).get("pid")
        if old and pid_alive(old):
            State.api_pid = old                  # 上次遗留的，接管，退出时一并关掉
        log(f"api_v2 already up on :{port} (pid {old or '?'}) → reuse")
        return
    py, root = g["python"], g["dir"]
    if not os.path.exists(py) or not os.path.exists(os.path.join(root, "api_v2.py")):
        raise RuntimeError(f"GPT-SoVITS not found: {py} / {root}")
    logf = open(API_LOG, "a", encoding="utf-8", errors="replace")
    logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} launch api_v2 :{port} =====\n")
    logf.flush()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PATH"] = os.path.join(root, "runtime") + os.pathsep + env.get("PATH", "")   # 同 go-webui.bat
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)                         # 全离线跑，别让它走代理
    # -I 会忽略 PYTHON* 环境变量，所以 utf8 / 不缓冲用命令行开关（不然日志是 GBK 还攒着不写）
    cmd = [py, "-I", "-X", "utf8", "-u", "api_v2.py", "-a", "127.0.0.1", "-p", str(port), "-c", g["config"]]
    State.proc = subprocess.Popen(cmd, cwd=root, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  creationflags=CREATE_NO_WINDOW, env=env)
    State.api_pid = State.proc.pid
    with open(API_RUN, "w", encoding="utf-8") as f:
        json.dump({"pid": State.proc.pid, "port": port, "started": time.time()}, f)
    t0 = time.time()
    while time.time() - t0 < 300:
        if State.proc.poll() is not None:
            raise RuntimeError(f"api_v2 exited rc={State.proc.returncode}, see {API_LOG}")
        if api_up(port):
            log(f"api_v2 up on :{port} after {time.time() - t0:.1f}s (pid {State.proc.pid})")
            return
        if beat:
            beat(f"GPT-SoVITS 加载中 {time.time() - t0:.0f}s")
        time.sleep(1.0)
    raise RuntimeError("api_v2 not up after 300s")


def stop_api():
    if State.api_pid:
        log(f"stop api_v2 pid {State.api_pid}")
        kill_tree(State.api_pid)
        try:
            if (read_json(API_RUN) or {}).get("pid") == State.api_pid:
                os.remove(API_RUN)
        except OSError:
            pass
    State.proc, State.api_pid = None, None


def restart_api_async():
    if State.restarting:
        return
    State.restarting = True

    def run():
        try:
            State.ready = False
            stop_api()
            launch_api(gsv_cfg())
            synth("预热一下。", None)
            State.ready = True
            log("api_v2 restarted")
        except Exception as e:
            log(f"restart failed: {e!r}")
        finally:
            State.restarting = False

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- 合成

def tts_body(text: str, g: dict, seed=None) -> dict:
    body = {
        "text": text,
        "text_lang": g["text_lang"],
        "ref_audio_path": g["ref"],
        "prompt_text": g["prompt_text"],
        "prompt_lang": g["prompt_lang"],
        "top_k": int(g["top_k"]),
        "top_p": float(g["top_p"]),
        "temperature": float(g["temperature"]),
        "text_split_method": g["text_split_method"],
        "batch_size": int(g["batch_size"]),
        "speed_factor": float(g["speed"]),
        "fragment_interval": float(g["fragment_interval"]),
        "seed": int(g["seed"] if seed is None else seed),
        "parallel_infer": bool(g["parallel_infer"]),
        "repetition_penalty": float(g["repetition_penalty"]),
        "media_type": "wav",
        "streaming_mode": False,
    }
    for k in ("sample_steps", "aux_ref_audio_paths", "super_sampling", "split_bucket", "batch_threshold"):
        if g.get(k) is not None:
            body[k] = g[k]
    return body


def synth(text: str, out, g=None) -> dict:
    """合成一句到 out（None = 只预热不留文件）。时长不对劲就换种子重来一次。"""
    g = g or gsv_cfg()
    if not g.get("ref") or not os.path.exists(g["ref"]):
        raise RuntimeError(f"reference voice not found: {g.get('ref')}")
    lo, hi = expected_range(text)
    tries = []
    for attempt in range(2):
        seed = None if attempt == 0 else int(time.time() * 1000) % 2_000_000_000
        t0 = time.time()
        code, ctype, data = api_call(State.api_port, "/tts", tts_body(text, g, seed), timeout=150)
        ms = int((time.time() - t0) * 1000)
        if code != 200 or "json" in ctype or len(data) < 1000:
            raise RuntimeError(f"api_v2 /tts {code}: {data[:300].decode('utf-8', 'replace')}")
        tmp = (out or os.path.join(STATE, "tmp", "gsv-warm.wav")) + ".part"
        os.makedirs(os.path.dirname(os.path.abspath(tmp)), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        trimmed = trim_edges(tmp)
        if trimmed > 1.0:
            log(f"trimmed {trimmed:.2f}s of silence at the edges: {text!r}")
        dur = wav_duration(tmp)
        gap = dead_air(tmp)
        tries.append({"ms": ms, "dur": round(dur, 2), "gap": round(gap, 2)})
        ok = lo <= dur * float(g["speed"]) <= hi and gap * float(g["speed"]) <= DEAD_AIR_S
        if ok or attempt == 1:
            if out:
                os.replace(tmp, out)
            else:
                os.remove(tmp)
            if not ok:
                log(f"still odd after retry ({dur:.2f}s, gap {gap:.2f}s, want {lo:.1f}-{hi:.1f}): {text!r}")
            return {"synth_ms": sum(t["ms"] for t in tries), "dur": round(dur, 2), "tries": tries, "odd": not ok}
        State.stats["retries"] += 1
        log(f"odd take {dur:.2f}s, gap {gap:.2f}s (want {lo:.1f}-{hi:.1f}, gap <= {DEAD_AIR_S}) → retry: {text!r}")
    raise RuntimeError("unreachable")


class Shaper:
    """流式 PCM（s16le 单声道）边过边剪：句首空白只留 lead 秒，之后任何一段空白最长 cap 秒、多的丢掉。
    按 10 ms 一帧看响不响（和 trim_edges 同一个门槛）。不回头改已经吐出去的。"""

    def __init__(self, sr: int, lead: float, cap: float, floor: float = 0.01):
        self.sr = sr
        self.hop = max(1, sr // 100) * 2               # 一帧 10 ms 的字节数
        self.lead = int(lead * sr) * 2
        self.cap = int(cap * sr) * 2
        self.lim = floor * 32768.0
        self.rest = b""
        self.started = False
        self.held = b""                                 # 开口前的静音（只留尾巴）
        self.quiet = 0                                  # 开口后当前这段静音已经吐了多少字节
        self.cut = 0                                    # 丢掉的字节
        self.out = 0                                    # 吐出去的字节

    def _loud(self, fr: bytes) -> bool:
        a = array.array("h")
        a.frombytes(fr)
        s = a[::4]
        return bool(s) and (sum(x * x for x in s) / len(s)) ** 0.5 >= self.lim

    def feed(self, b: bytes) -> bytes:
        buf = self.rest + b
        n = len(buf) // self.hop * self.hop
        self.rest = buf[n:]
        out = []
        for i in range(0, n, self.hop):
            fr = buf[i:i + self.hop]
            loud = self._loud(fr)
            if not self.started:
                if loud:
                    self.started = True
                    keep = self.held[-self.lead:] if self.lead else b""
                    self.cut += len(self.held) - len(keep)
                    out += [keep, fr]
                    self.held = b""
                else:
                    self.held += fr
                    if len(self.held) > self.lead + 64 * self.hop:   # 别攒太多：老的直接算剪掉
                        drop = len(self.held) - self.lead
                        self.cut += drop
                        self.held = self.held[drop:]
                continue
            if loud:
                self.quiet = 0
                out.append(fr)
            elif self.quiet + len(fr) <= self.cap:
                self.quiet += len(fr)
                out.append(fr)
            else:
                self.cut += len(fr)
        o = b"".join(out)
        self.out += len(o)
        return o

    def flush(self) -> bytes:
        o = self.rest if (self.started and self.quiet + len(self.rest) <= self.cap) else b""
        self.cut += len(self.rest) - len(o) + (0 if self.started else len(self.held))
        self.rest = self.held = b""
        self.out += len(o)
        return o


def play(wav: str):
    ffplay = shutil.which("ffplay")
    if ffplay:
        subprocess.run([ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", wav], creationflags=CREATE_NO_WINDOW)
    else:
        ps = f"(New-Object Media.SoundPlayer '{wav}').PlaySync()"
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], creationflags=CREATE_NO_WINDOW)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 安静
        pass

    def do_GET(self):
        if self.path.startswith("/health"):
            g = gsv_cfg()
            self._json(200, {"ok": bool(State.ready), "loaded": bool(State.ready), "engine": "gptsovits",
                             "voice": g.get("ref"), "prompt_text": g.get("prompt_text"), "version": g.get("version"),
                             "idle_s": round(time.time() - State.last, 1), "pid": os.getpid(),
                             "port": int(g["port"]), "api_port": State.api_port, "api_pid": State.api_pid,
                             "restarting": State.restarting, "stats": State.stats})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})
        if self.path.startswith("/quit"):
            self._json(200, {"ok": True, "bye": True})
            threading.Thread(target=State.server.shutdown, daemon=True).start()
            return
        if self.path.startswith("/stream"):
            return self._stream(req)
        if not self.path.startswith("/say"):
            return self._json(404, {"ok": False, "error": "not found"})
        text = (req.get("text") or "").strip()
        if not text:
            return self._json(400, {"ok": False, "error": "empty text"})
        if not State.ready:
            return self._json(503, {"ok": False, "error": "restarting"})
        keep = req.get("wav")
        wav = keep or os.path.join(STATE, "tmp", f"gsv-say-{os.getpid()}-{threading.get_ident()}.wav")
        os.makedirs(os.path.dirname(os.path.abspath(wav)), exist_ok=True)
        g = gsv_cfg()
        for k in ("ref", "prompt_text", "prompt_lang", "text_lang", "speed", "top_k", "temperature", "seed"):
            if req.get(k) is not None:
                g[k] = req[k]
        State.last = time.time()
        try:
            with State.lock:
                r = synth(text, wav, g)
            State.stats["n"] += 1
            if req.get("play", True):
                play(wav)
            State.last = time.time()
            self._json(200, {"ok": True, "synth_ms": r["synth_ms"], "dur": r["dur"], "wav": wav if keep else None,
                             "tries": r["tries"], "odd": r["odd"]})
        except Exception as e:
            State.stats["fails"] += 1
            log(f"say failed: {e!r}")
            if State.proc is not None and State.proc.poll() is not None:
                log("api_v2 died → restarting in background")
                restart_api_async()
            self._json(500, {"ok": False, "error": repr(e)})
        finally:
            if not keep:
                try:
                    os.remove(wav)
                except OSError:
                    pass


    def _stream(self, req):
        """边合成边吐：先读到 api_v2 的 wav 头（和第一块音频一起来），知道采样率再回响应头，之后来一块吐一块。
        调用方断开（被打断）就停，把连接让给下一句。"""
        text = (req.get("text") or "").strip()
        if not text:
            return self._json(400, {"ok": False, "error": "empty text"})
        if not State.ready:
            return self._json(503, {"ok": False, "error": "restarting"})
        g = gsv_cfg()
        for k in ("ref", "prompt_text", "prompt_lang", "text_lang", "speed", "top_k", "temperature", "seed"):
            if req.get(k) is not None:
                g[k] = req[k]
        if not g.get("ref") or not os.path.exists(g["ref"]):
            return self._json(500, {"ok": False, "error": f"reference voice not found: {g.get('ref')}"})
        body = tts_body(text, g)
        body.update(streaming_mode=int(g.get("stream_mode") or 3), min_chunk_length=int(g.get("stream_chunk") or 8),
                    batch_size=1, parallel_infer=False, media_type="wav")   # 流式只能一条一条（batch>1 会报张量尺寸不符）
        State.last = t0 = time.time()
        first = None
        sent = False
        shaper = None
        why = ""
        with State.lock:
            conn = http.client.HTTPConnection("127.0.0.1", State.api_port, timeout=60)
            try:
                conn.request("POST", "/tts", json.dumps(body, ensure_ascii=False).encode("utf-8"),
                             {"Content-Type": "application/json; charset=utf-8"})
                r = conn.getresponse()
                if r.status != 200:
                    raise RuntimeError(f"api_v2 /tts {r.status}: {r.read()[:300].decode('utf-8', 'replace')}")
                head = b""
                while len(head) < 44:
                    b = r.read1(65536)
                    if not b:
                        raise RuntimeError("api_v2 stream ended before any audio")
                    head += b
                if head[:4] != b"RIFF":
                    raise RuntimeError(f"api_v2 stream is not wav: {head[:16]!r}")
                sr = struct.unpack("<I", head[24:28])[0]
                shaper = Shaper(sr, float(g.get("stream_lead_keep") or 0.0), float(g.get("stream_gap_cap") or 0.6))
                self.send_response(200)
                self.send_header("Content-Type", "audio/L16")
                self.send_header("X-Sample-Rate", str(sr))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                sent = True
                pending = head[44:]
                while True:
                    out = shaper.feed(pending) if pending else b""
                    if out:
                        self.wfile.write(out)
                        self.wfile.flush()
                        if first is None:
                            first = time.time() - t0
                    b = r.read1(65536)
                    if not b:
                        break
                    pending = b
                out = shaper.flush()
                if out:
                    self.wfile.write(out)
                    self.wfile.flush()
                State.stats["streams"] = State.stats.get("streams", 0) + 1
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                why = "client gone"
            except Exception as e:
                why = repr(e)[:200]
                State.stats["fails"] += 1
                log(f"stream failed: {why}")
                if not sent:
                    try:
                        self._json(500, {"ok": False, "error": why})
                    except Exception:
                        pass
                if State.proc is not None and State.proc.poll() is not None:
                    log("api_v2 died → restarting in background")
                    restart_api_async()
            finally:
                conn.close()
        State.last = time.time()
        if shaper is not None:
            bps = shaper.sr * 2.0
            log(f"stream {len(text)} chars: first {first if first is None else round(first, 2)}s, "
                f"total {time.time() - t0:.2f}s, audio {shaper.out / bps:.2f}s, cut {shaper.cut / bps:.2f}s"
                + (f" ({why})" if why else ""))
        self.close_connection = True


def idle_watch(idle_s):
    while True:
        time.sleep(15)
        if idle_s and time.time() - State.last > idle_s:
            log(f"idle {idle_s}s → exit")
            threading.Thread(target=State.server.shutdown, daemon=True).start()
            return


def main():
    os.makedirs(STATE, exist_ok=True)
    g = gsv_cfg()
    port = int(g["port"])
    prog = Progress("gptsovits_tts_server", total=0, note="启动中") if Progress else None

    def beat(note):
        if prog:
            prog.beat(note=note)

    alive = {"on": True}

    def heart():
        while alive["on"]:
            beat(None)
            time.sleep(5)

    threading.Thread(target=heart, daemon=True).start()
    try:
        State.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)   # 先占端口，占不到说明已有一个在跑
    except OSError as e:
        log(f"port {port} busy ({e!r}) → another adapter is running, exit")
        alive["on"] = False
        if prog:
            prog.done(note=f"端口 {port} 已有服务")
        return
    serving = threading.Thread(target=State.server.serve_forever, name="http", daemon=True)
    serving.start()                     # 加载期间 /health 就能答（ok=false），/say 回 503
    try:
        beat("拉起 GPT-SoVITS api_v2…")
        launch_api(g, beat)
        beat("预热一句…")
        t0 = time.time()
        try:
            synth("预热一下，老师好。", None, g)
            log(f"warm-up {time.time() - t0:.2f}s")
        except Exception as e:
            log(f"warm-up failed (ignored): {e!r}")
        State.ready = True
        State.last = time.time()
        with open(RUN, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": port, "engine": "gptsovits", "api_port": State.api_port,
                       "started": time.time()}, f)
        alive["on"] = False
        if prog:
            prog.done(note=f"就绪 127.0.0.1:{port}")
        log(f"ready on 127.0.0.1:{port} (api_v2 :{State.api_port})")
        threading.Thread(target=idle_watch, args=(int(g.get("idle_s") or 0),), daemon=True).start()
        while serving.is_alive():
            serving.join(1.0)
    except Exception as e:
        log(f"fatal: {e!r}")
        if prog:
            prog.error(repr(e))
        State.server.shutdown()
        raise
    finally:
        alive["on"] = False
        if (read_json(RUN) or {}).get("pid") == os.getpid():
            try:
                os.remove(RUN)
            except OSError:
                pass
        stop_api()
        log("bye")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass
    main()
