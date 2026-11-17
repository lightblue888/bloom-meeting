# -*- coding: utf-8 -*-
"""IndexTTS2 常驻语音服务 —— 组会助手的嗓子。权重只加载一次，之后每句只合成不加载。

启动命令（必须用 IndexTTS2 的 venv python，不能用系统 python；Mouth.ensure_server() 会自动这样拉起）：
    <IndexTTS2 目录>\\.venv\\Scripts\\python.exe -u <项目目录>\\zuhui\\tts_server.py

本文件只 import 标准库（venv 里没有项目依赖），状态目录自己算：ZUHUI_HOME 环境变量，默认 ~/.zuhui。

接口（127.0.0.1，端口默认 47321，实际写在 ~/.zuhui/tts_server.json）：
    GET  /health                       → {"ok": true, "loaded": true, "voice": "...", "idle_s": 12}
    POST /say   {"text": "...", "play": true, "wav": 可选, "ref": 可选, "emo": 可选, "emo_alpha": 可选}
                                       → 合成（play=true 则播放并阻塞到播完）{"ok": true, "synth_ms": 1800, "dur": 4.2, "wav": "..."}
                                         play=false + wav=<路径>：只合成不播，wav 保留在该路径
    POST /quit                         → 退出

音色配置 ~/.zuhui/voice.json：{"ref": "参考音频.wav", "emo": null 或 "语气描述", "emo_alpha": 0.6}
（首次启动时没有这个文件而 ~/.claude/meeting/voice.json 存在，就复制过来。）
空闲 ZUHUI_TTS_IDLE 秒（默认 40 分钟）自动退出，释放显存和 <IndexTTS2 目录>\\.gpu.lock。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = os.environ.get("ZUHUI_HOME") or os.path.join(os.path.expanduser("~"), ".zuhui")


def _local(key):
    """这台电脑上的路径：~/.zuhui/paths.json（本文件只用标准库，自己读）。"""
    try:
        with open(os.path.join(STATE, "paths.json"), encoding="utf-8") as f:
            return str(json.load(f).get(key) or "")
    except (OSError, ValueError, AttributeError):
        return ""


INDEXTTS_DIR = os.environ.get("INDEXTTS_DIR") or _local("indextts_dir")
CFG = os.path.join(STATE, "voice.json")
RUN = os.path.join(STATE, "tts_server.json")
OLD_CFG = os.path.join(os.path.expanduser("~"), ".claude", "meeting", "voice.json")
LOCK = os.path.join(INDEXTTS_DIR, ".gpu.lock")
PORT = int(os.environ.get("ZUHUI_TTS_PORT") or os.environ.get("MEETING_TTS_PORT") or "47321")
IDLE_EXIT = int(os.environ.get("ZUHUI_TTS_IDLE") or os.environ.get("MEETING_TTS_IDLE") or "2400")
DEFAULT_REF = _local("voice_ref")          # 克隆音色的参考录音（~/.zuhui/paths.json）

sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".claude", "progress"))
try:
    from claude_progress import Progress   # 只依赖标准库；不在也不影响服务
except Exception:
    Progress = None


def log(msg):
    print(f"[tts {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def migrate_voice_cfg():
    """~/.zuhui/voice.json 不存在而旧版 ~/.claude/meeting/voice.json 存在 → 复制过来。"""
    if not os.path.exists(CFG) and os.path.exists(OLD_CFG):
        try:
            shutil.copyfile(OLD_CFG, CFG)
            log(f"voice.json copied from {OLD_CFG}")
        except OSError as e:
            log(f"copy voice.json failed (ignored): {e!r}")


def voice_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        c = {}
    c.setdefault("ref", DEFAULT_REF)
    c.setdefault("emo", None)
    c.setdefault("emo_alpha", 0.6)
    return c


def pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=10).stdout
        return str(pid) in out
    except Exception:
        return False


def acquire_gpu_lock(beat):
    """和 explain-video 流水线共用的 GPU 锁：别人在用就排队，持锁进程死了就接管。"""
    while True:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            try:
                pid = int(open(LOCK).read().strip() or 0)
            except (ValueError, OSError):
                pid = 0
            if not pid or not pid_alive(pid):
                try:
                    os.remove(LOCK)
                except OSError:
                    pass
                continue
            beat("GPU 被别的任务占用，排队中…")
            time.sleep(10)


def release_gpu_lock():
    try:
        if open(LOCK).read().strip() == str(os.getpid()):
            os.remove(LOCK)
    except OSError:
        pass


class State:
    tts = None
    lock = threading.Lock()
    last = time.time()
    server = None
    steps = None          # 扩散步数：None = 模型自带的 25 步
    timing = {}           # 上一次合成各段用时（秒）：gpt / s2mel / bigvgan / rtf


def patch_steps():
    """模型把扩散步数写死成 25。这里把声学模型的 inference 包一层，让步数可以按请求调（越少越快，音质略降）。"""
    cfm = State.tts.s2mel.models["cfm"]
    orig = cfm.inference

    def inference(mu, x_lens, prompt, style, f0, n_timesteps, *a, **k):
        return orig(mu, x_lens, prompt, style, f0, int(State.steps or n_timesteps), *a, **k)

    cfm.inference = inference


def synth(text: str, ref: str, emo, emo_alpha: float, out: str, beams=None, steps=None):
    import contextlib
    import io as _io
    import re as _re
    kw = {}
    if emo:
        kw = dict(emo_alpha=float(emo_alpha), use_emo_text=True, emo_text=emo, use_random=False)
    if beams:
        kw["num_beams"] = int(beams)       # 模型默认 3 束搜索；1 束快得多
    State.steps = int(steps) if steps else None
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            State.tts.infer(spk_audio_prompt=ref, text=text, output_path=out, verbose=False, **kw)
    finally:
        State.steps = None
    t = {}
    for key, name in (("gpt_gen_time", "gpt"), ("s2mel_time", "s2mel"), ("bigvgan_time", "bigvgan"), ("RTF", "rtf")):
        m = _re.search(r">> %s: ([0-9.]+)" % key, buf.getvalue())
        if m:
            t[name] = round(float(m.group(1)), 2)
    State.timing = t


def play(wav: str):
    ffplay = shutil.which("ffplay")
    if ffplay:
        subprocess.run([ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", wav])
    else:
        ps = f"(New-Object Media.SoundPlayer '{wav}').PlaySync()"
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])


def duration(wav: str) -> float:
    try:
        import wave
        with wave.open(wav, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


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
            c = voice_cfg()
            self._json(200, {"ok": True, "loaded": State.tts is not None, "voice": c["ref"], "emo": c["emo"],
                             "idle_s": round(time.time() - State.last, 1), "pid": os.getpid(), "port": PORT})
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
        if not self.path.startswith("/say"):
            return self._json(404, {"ok": False, "error": "not found"})
        text = (req.get("text") or "").strip()
        if not text:
            return self._json(400, {"ok": False, "error": "empty text"})
        c = voice_cfg()
        ref = req.get("ref") or c["ref"]
        emo = req.get("emo", c["emo"])
        emo_alpha = req.get("emo_alpha", c["emo_alpha"])
        beams = req.get("num_beams", c.get("num_beams"))
        steps = req.get("steps", c.get("steps"))
        if not os.path.exists(ref):
            return self._json(500, {"ok": False, "error": f"reference voice not found: {ref}"})
        keep = req.get("wav")
        tmpdir = None if keep else tempfile.mkdtemp(prefix="tts-")
        wav = keep or os.path.join(tmpdir, "say.wav")
        if keep:
            os.makedirs(os.path.dirname(os.path.abspath(keep)), exist_ok=True)
        State.last = time.time()
        try:
            with State.lock:
                t0 = time.time()
                synth(text, ref, emo, emo_alpha, wav, beams=beams, steps=steps)
                synth_ms = int((time.time() - t0) * 1000)
                timing = dict(State.timing)
            dur = duration(wav)
            if req.get("play", True):
                play(wav)
            State.last = time.time()
            self._json(200, {"ok": True, "synth_ms": synth_ms, "dur": round(dur, 2), "wav": wav if keep else None,
                             "timing": timing, "num_beams": beams, "steps": steps})
        except Exception as e:
            log(f"say failed: {e!r}")
            self._json(500, {"ok": False, "error": repr(e)})
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)


def idle_watch():
    while True:
        time.sleep(15)
        if time.time() - State.last > IDLE_EXIT:
            log(f"idle {IDLE_EXIT}s → exit")
            threading.Thread(target=State.server.shutdown, daemon=True).start()
            return


def main():
    os.makedirs(STATE, exist_ok=True)
    migrate_voice_cfg()
    prog = Progress("indextts2_tts_server", total=0, note="启动中") if Progress else None

    def beat(note):
        log(note)
        if prog:
            prog.beat(note=note)

    alive = {"on": True}

    def heart():
        while alive["on"]:
            if prog:
                prog.beat()
            time.sleep(5)

    threading.Thread(target=heart, daemon=True).start()
    acquire_gpu_lock(beat)
    try:
        os.chdir(INDEXTTS_DIR)
        beat("加载 IndexTTS2 权重（首次约 1-3 分钟）…")
        from indextts.infer_v2 import IndexTTS2
        State.tts = IndexTTS2(cfg_path=os.path.join(INDEXTTS_DIR, "checkpoints", "config.yaml"),
                              model_dir=os.path.join(INDEXTTS_DIR, "checkpoints"),
                              use_fp16=True, use_cuda_kernel=False, use_deepspeed=False)
        patch_steps()
        beat("权重已加载，预热一句…")
        try:
            warm = os.path.join(tempfile.gettempdir(), "tts-warm.wav")
            synth("预热。", voice_cfg()["ref"], None, 0.6, warm)
        except Exception as e:
            log(f"warm-up failed (ignored): {e!r}")
        State.server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        with open(RUN, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": PORT, "started": time.time()}, f)
        alive["on"] = False
        if prog:
            prog.done(note=f"就绪 127.0.0.1:{PORT}")
        log(f"ready on 127.0.0.1:{PORT}")
        threading.Thread(target=idle_watch, daemon=True).start()
        State.server.serve_forever()
    finally:
        alive["on"] = False
        try:
            with open(RUN, encoding="utf-8") as f:
                mine = json.load(f).get("pid") == os.getpid()
        except Exception:
            mine = False
        if mine:                       # 只删自己写的（GPT-SoVITS 适配服务也写这个文件）
            try:
                os.remove(RUN)
            except OSError:
                pass
        release_gpu_lock()
        log("bye")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass
    main()
