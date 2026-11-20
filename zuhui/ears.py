# -*- coding: utf-8 -*-
"""耳朵：盯着口喷鸡（Blurt）正在进行的录屏，把实时转写按停顿合并成一条条"老师的话"，每条配一张截图。

数据来源：
  ~/.blurt/jobs/j-*.json 里 type=="record" 且 state=="running" 的任务 → 它的 session 目录
  <session>/live/transcript.jsonl 每行 {"start":4.43,"end":5.72,"text":"..."}（Blurt 边录边追加）
  <session>/meta.json 的 region:[x,y,w,h] = 录制区域，截图只截这块
  <session>/meta.json 的 window:{"hwnd":N,...} = 只录这个窗口（record.py --window-hwnd），截图也只截这个窗口

事件（on_event(kind, payload)，在独立分发线程里调用，耗时也不会卡住 tail 循环）：
  "bridge_ready"     None                  耳朵就绪，开始等录屏
  "meeting_started"  session 路径 str      发现一场录屏开始
  "utterance"        Utterance             老师说完一条（停顿 idle_flush 秒）
  "meeting_ended"    session 路径 str      录屏结束；之后继续等下一场
  "log"              {"kind", ...}         要记进会议日志的一行（回声丢掉了哪段、静音开关、主持人在别的设备上；见 PlaybackRef / _MicState）

手动测试：
  python -m zuhui.ears --replay <session目录> [--speed 2] [--fast]
"""
from __future__ import annotations

import glob
import json
import logging
import math
import os
import queue
from collections import deque
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("zuhui.ears")

BLURT_HOME = Path(os.environ.get("BLURT_HOME") or (Path.home() / ".blurt"))
JOBS = BLURT_HOME / "jobs"
# 整条只有这些字 = 语气词/口头禅，丢掉
FILLER = re.compile(r"^(?:[\s呃嗯恩啊哦噢嘛呢吧哈额唉诶欸哎，。、,.!?！？…~～-]|(?i:um+|uh+|hm+|mm+|erm?|ah+|oh+)\b)*$")   # 英文的 "Um." "Hmm." 也只是语气（2026-11-13）
MAX_CHARS = 400          # 一条意见最多攒这么多字，再长就先发出去
POLL_S = 0.1             # tail 转写文件和音频的间隔
JOB_POLL_S = 1.0         # 等录屏开始的轮询间隔
DRAFT_QUIET = 0.3        # 你安静这么久（判停也说多半说完了），就把初稿交出去让模型先想着
DRAFT_DECODE = 0.12      # 安静这么久就先在后台解初稿（只占 CPU 0.2 秒左右），和上面那段等待重叠；你又开口就丢掉


@dataclass
class Utterance:
    n: int                  # 本场第几条（从 1 起）
    text: str               # 合并后的整条话
    t: str                  # 录屏内时间 "MM:SS"（这条第一段的 start）
    shot: str | None        # 截图路径（screenshots=False 或截图失败时为 None）
    session: str            # 录屏 session 目录
    heard: float | None = None    # 你说完这句的时刻（墙上时间，按音频时钟估的）
    landed: float | None = None   # 最后一段文字落地的时刻
    sent: float | None = None     # 耳朵把这条发出来的时刻
    started: float | None = None  # 你开口说这句的时刻（墙上时间，按音频时钟估的）：逐字稿按它排顺序


# ---------------------------------------------------------------- 工具函数

def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def running_record_job() -> dict | None:
    """正在录的 record 任务（多个就取最晚开始的那个）。"""
    best = None
    for f in glob.glob(str(JOBS / "j-*.json")):
        j = _load_json(Path(f))
        if not j:
            continue
        if j.get("type") == "record" and j.get("state") == "running" and j.get("session"):
            j["_file"] = f
            if best is None or (j.get("started") or "") > (best.get("started") or ""):
                best = j
    return best


def job_running(job_file: str) -> bool:
    j = _load_json(Path(job_file))
    return bool(j) and j.get("state") == "running"


def _iso_to_epoch(s: str) -> float:
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def region_of(session: Path):
    m = _load_json(session / "meta.json") or {}
    r = m.get("region")
    if r and len(r) == 4:
        x, y, w, h = [int(v) for v in r]
        if w > 50 and h > 50:
            return (x, y, x + w, y + h)
    return None


def window_of(session: Path) -> int | None:
    """只录一个窗口的录屏（record.py --window-hwnd）：meta.json 的 window.hwnd；不是就返回 None。"""
    m = _load_json(session / "meta.json") or {}
    w = m.get("window")
    try:
        h = int(w.get("hwnd")) if isinstance(w, dict) and w.get("hwnd") else 0
    except (TypeError, ValueError):
        h = 0
    return h or None


_W32 = None


def _win32():
    """自己的 user32 / gdi32 实例（不改 ctypes.windll 那份共享函数的原型），句柄都按指针宽度传。"""
    global _W32
    if _W32 is None:
        import ctypes
        from ctypes import wintypes as wt
        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        for f in ("IsWindow", "IsIconic", "IsWindowVisible"):
            getattr(u, f).argtypes = [wt.HWND]
        u.GetWindowRect.argtypes = u.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
        u.GetWindowDC.argtypes = [wt.HWND]
        u.GetWindowDC.restype = wt.HDC
        u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        u.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
        u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        g.CreateCompatibleDC.argtypes = [wt.HDC]
        g.CreateCompatibleDC.restype = wt.HDC
        g.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wt.HBITMAP
        g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        g.SelectObject.restype = wt.HGDIOBJ
        g.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
        g.DeleteObject.argtypes = [wt.HGDIOBJ]
        g.DeleteDC.argtypes = [wt.HDC]
        _W32 = (u, g)
    return _W32


def _print_window(hwnd: int):
    """PrintWindow(PW_RENDERFULLCONTENT)：窗口自己画到内存里，被别的窗口挡住、WebView2/GPU 内容都截得到，
    只要窗口的客户区（不含标题栏边框）。最小化/隐藏/已关 → None。按物理像素截（本线程临时 per-monitor DPI aware）。"""
    import ctypes
    from ctypes import wintypes as wt
    from PIL import Image
    u, g = _win32()
    if not u.IsWindow(hwnd) or u.IsIconic(hwnd) or not u.IsWindowVisible(hwnd):
        return None
    old = None
    try:
        old = u.SetThreadDpiAwarenessContext(-4)        # PER_MONITOR_AWARE_V2，只管这个线程
    except Exception:
        pass
    try:
        wr, cr, pt = wt.RECT(), wt.RECT(), wt.POINT(0, 0)
        if not (u.GetWindowRect(hwnd, ctypes.byref(wr)) and u.GetClientRect(hwnd, ctypes.byref(cr))
                and u.ClientToScreen(hwnd, ctypes.byref(pt))):
            return None
        W, H = wr.right - wr.left, wr.bottom - wr.top
        if W < 2 or H < 2 or cr.right < 2 or cr.bottom < 2:
            return None
        hdc = u.GetWindowDC(hwnd)
        if not hdc:
            return None
        mdc = bmp = None
        try:
            mdc = g.CreateCompatibleDC(hdc)
            bmp = g.CreateCompatibleBitmap(hdc, W, H)
            if not mdc or not bmp:
                return None
            g.SelectObject(mdc, bmp)
            if not u.PrintWindow(hwnd, mdc, 2):         # PW_RENDERFULLCONTENT
                return None

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]
            bi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=W, biHeight=-H, biPlanes=1,
                                  biBitCount=32)
            buf = ctypes.create_string_buffer(W * H * 4)
            if g.GetDIBits(mdc, bmp, 0, H, buf, ctypes.byref(bi), 0) != H:
                return None
        finally:
            if bmp:
                g.DeleteObject(bmp)
            if mdc:
                g.DeleteDC(mdc)
            u.ReleaseDC(hwnd, hdc)
        img = Image.frombuffer("RGB", (W, H), buf, "raw", "BGRX", 0, 1)
        x, y = pt.x - wr.left, pt.y - wr.top
        return img.crop((x, y, min(W, x + cr.right), min(H, y + cr.bottom)))
    finally:
        if old:
            try:
                u.SetThreadDpiAwarenessContext(old)
            except Exception:
                pass


def _gfx_window(hwnd: int, p: Path) -> bool:
    """备用：ffmpeg gfxcapture（Windows.Graphics.Capture）抓这个窗口一帧，同样只有窗口本身。约 0.5 秒。"""
    import subprocess
    from .components import which
    ff = which("ffmpeg")                  # PATH，再 ~/.blurt/bin（install.ps1 放的）
    if not ff:
        return False
    try:
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            f"gfxcapture=hwnd={hwnd}:capture_cursor=0", "-vf", "hwdownload,format=bgra",
                            "-frames:v", "1", "-update", "1", str(p)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=5, creationflags=0x08000000)
        return r.returncode == 0 and p.exists() and p.stat().st_size > 0
    except Exception:
        return False


def _shot_window(hwnd: int, p: Path) -> str | None:
    """只截录屏的那个窗口，绝不退回截全屏：PrintWindow（几到几十毫秒）；拿不到或整张全黑 → gfxcapture；都不行 → None。"""
    _u, _g = _win32()
    if not _u.IsWindow(hwnd) or _u.IsIconic(hwnd) or not _u.IsWindowVisible(hwnd):
        return None                                     # 最小化/隐藏/已关：这一刻没有窗口画面
    try:
        img = _print_window(hwnd)
        if img is not None and any(hi > 8 for _lo, hi in img.getextrema()):
            try:
                img = _with_island(hwnd, img, p)
            except Exception as e:
                log.warning("悬浮岛没拼进截图: %r", e)
            img.save(p)
            return str(p)
    except Exception as e:
        log.warning("PrintWindow 截窗口失败: %r", e)
    p.unlink(missing_ok=True)
    return str(p) if _gfx_window(hwnd, p) else None


def _client_origin(hwnd: int) -> tuple[int, int] | None:
    """窗口客户区左上角在桌面上的物理像素位置（和 PrintWindow 截出来的那张图对齐）。"""
    import ctypes
    from ctypes import wintypes as wt
    u, _g = _win32()
    old = None
    try:
        old = u.SetThreadDpiAwarenessContext(-4)
    except Exception:
        pass
    try:
        pt = wt.POINT(0, 0)
        return (pt.x, pt.y) if u.ClientToScreen(hwnd, ctypes.byref(pt)) else None
    finally:
        if old:
            try:
                u.SetThreadDpiAwarenessContext(old)
            except Exception:
                pass


def _island_shot(out: Path, timeout: float = 1.5) -> dict | None:
    """向 Bloom 要悬浮岛此刻的样子（它平时不进任何截屏）：写 ~/.zuhui/run/island-shot.req，Bloom 把岛截成
    out（PNG，圆角外透明）再写 out.json {shown, x, y, w, h}（桌面物理像素）。没开 / 没显示 / 超时 → None。"""
    run = Path(os.environ.get("ZUHUI_HOME") or Path.home() / ".zuhui") / "run"
    run.mkdir(parents=True, exist_ok=True)
    done = out.with_name(out.name + ".json")
    done.unlink(missing_ok=True)
    tmp = run / f"island-shot.req.{os.getpid()}"
    tmp.write_text(json.dumps({"out": str(out)}), encoding="utf-8")
    os.replace(tmp, run / "island-shot.req")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if done.exists():
            try:
                d = json.loads(done.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.03)
                continue
            done.unlink(missing_ok=True)
            return d if d.get("shown") and out.exists() else None
        time.sleep(0.03)
    (run / "island-shot.req").unlink(missing_ok=True)   # 没人接（旧版 Bloom）：别留着
    return None


def _with_island(hwnd: int, img, p: Path):
    """会议窗口的截图里加上悬浮岛：岛在屏幕上什么样、在哪，就按它和窗口的相对位置贴上去（岛在窗口外面就把
    画布往外扩，扩出来的地方填深灰，不带桌面上的别的东西）。拿不到岛就原样返回。"""
    from PIL import Image
    org = _client_origin(hwnd)
    if not org:
        return img
    ip = p.with_name(p.stem + ".island.png")
    d = _island_shot(ip)
    if not d:
        return img
    try:
        isl = Image.open(ip).convert("RGBA")
        isl.load()
    finally:
        ip.unlink(missing_ok=True)
    wx, wy = org
    ix, iy = int(d["x"]), int(d["y"])
    left, top = min(wx, ix), min(wy, iy)
    right, bottom = max(wx + img.width, ix + isl.width), max(wy + img.height, iy + isl.height)
    canvas = Image.new("RGB", (right - left, bottom - top), (32, 33, 36))
    canvas.paste(img.convert("RGB"), (wx - left, wy - top))
    canvas.paste(isl, (ix - left, iy - top), isl)
    return canvas


def screenshot(session: Path, n: int) -> str | None:
    """截录制区域存到 <session>/meeting/shot-NNNN.png；失败返回 None。
    只录一个窗口的录屏（meta.json 有 window.hwnd）：只截那个窗口（被挡住也行），截不到就 None，绝不截全屏。"""
    hwnd = window_of(session)
    if hwnd:
        out = session / "meeting"
        out.mkdir(parents=True, exist_ok=True)
        try:
            return _shot_window(hwnd, out / f"shot-{n:04d}.png")
        except Exception as e:
            log.warning("截窗口失败: %r", e)
            return None
    try:
        from PIL import ImageGrab
    except Exception as e:
        log.warning("PIL 不可用，不截图: %r", e)
        return None
    out = session / "meeting"
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"shot-{n:04d}.png"
    bbox = region_of(session)
    try:
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True) if bbox else ImageGrab.grab(all_screens=True)
        except Exception as e:
            log.warning("按区域截图失败 (%r)，改截全屏", e)
            img = ImageGrab.grab()
        img.save(p)
        return str(p)
    except Exception as e:
        log.warning("截图失败: %r", e)
        return None


def fmt_t(s) -> str:
    """录音里的秒数 → "MM:SS"。转写里时间戳坏了（不是数字、负数）就当 0，别让耳朵线程因为一个字段崩掉。"""
    try:
        s = max(0, int(float(s or 0)))
    except (TypeError, ValueError):
        s = 0
    return f"{s // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------- 合并器

# 认声音：这句是不是你本人的声音。旁边有人聊天时，别人的话会被当成你的（转写进你的话里、判停一直等"你还在说"）。
# 用口喷鸡声纹同一个模型（3D-Speaker CAM++ 中英，~/.blurt/models），和你登记过的声音（~/.zuhui/voiceprint.json，
# 从以往开会你自己的话里建，见 docs）比余弦。模型或声纹没有 → 什么都不管，和以前一样。
VOICE_MODEL = BLURT_HOME / "models" / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
VOICE_PROFILE = Path(os.environ.get("ZUHUI_HOME") or (Path.home() / ".zuhui")) / "voiceprint.json"
VOICE_MIN_S = 1.25       # 有声的部分够这么长才判（再短判不准，照旧当成你）
VOICE_MAX_S = 4.0        # 只取最近这么长的有声部分来判（够准，也快：一次约 30-60 毫秒）
VOICE_LINE_TH = 0.15     # 一段转写的声音和你的余弦低于这个 = 不是你（旁人说话、电视），不收。你自己的话实测 ≥0.46（噪声大时偶尔 0.19），旁人聊天 ≤0.18
VOICE_AFTER_TH = 0.25    # 你这段话之后又听到的声音低于这个 = 不是你，不算"你还在说"。你接着说实测 ≥0.45，旁人 ≤0.18


class _VoiceCheck:
    """CAM++ 说话人向量（onnxruntime 直接跑，特征和 sherpa-onnx 一样：kaldi fbank 80 维、25/10 毫秒、povey 窗、
    预加重 0.97、减均值），和登记的声音比余弦。后台线程加载，加载完之前 sim() 返回 None（= 不管）。"""

    def __init__(self):
        self._sess = self._prof = self._np = None
        self._mtime = None
        self.ok = False
        threading.Thread(target=self._load, name="ears-voice", daemon=True).start()

    def _load(self) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            d = json.loads(VOICE_PROFILE.read_text(encoding="utf-8"))
            if d.get("model") != VOICE_MODEL.name or not VOICE_MODEL.exists():
                return
            prof = np.asarray(d["profile"], np.float32)
            so = ort.SessionOptions()
            so.intra_op_num_threads, so.inter_op_num_threads = 2, 1
            sess = ort.InferenceSession(str(VOICE_MODEL), so, providers=["CPUExecutionProvider"])
            self._name = sess.get_inputs()[0].name
            mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)
            c = np.linspace(mel(20.0), mel(7600.0), 82)
            m = mel(np.arange(256) * 16000.0 / 512)
            w = np.zeros((80, 257), np.float32)
            for b in range(80):
                l, ce, r = c[b], c[b + 1], c[b + 2]
                w[b, :256] = np.where((m > l) & (m <= ce), (m - l) / (ce - l), np.where((m > ce) & (m < r), (r - m) / (r - ce), 0.0))
            self._mel = w.T
            self._win = ((0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399)) ** 0.85).astype(np.float32)
            self._np, self._prof, self._sess = np, prof / (np.linalg.norm(prof) + 1e-9), sess
            self._mtime = VOICE_PROFILE.stat().st_mtime
            self.ok = True
            log.info("认声音：已加载（%s，登记 %d 段）", VOICE_MODEL.name, len(d.get("built_from") or []))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("认声音起不来（%r），照旧不管", e)

    def stale(self) -> bool:
        try:
            return self.ok and VOICE_PROFILE.stat().st_mtime != self._mtime
        except OSError:
            return False

    def _fbank(self, x):
        np = self._np
        n = len(x)
        nf = (n + 80) // 160
        idx = (np.arange(nf) * 160 - 120)[:, None] + np.arange(400)[None, :]      # snip_edges=false：边上镜像
        idx = np.where(idx < 0, -idx - 1, idx)
        idx = np.clip(np.where(idx >= n, 2 * n - 1 - idx, idx), 0, n - 1)
        fr = x[idx].astype(np.float64)
        fr -= fr.mean(axis=1, keepdims=True)
        fr[:, 1:] -= 0.97 * fr[:, :-1].copy()
        fr[:, 0] *= 0.03
        spec = np.abs(np.fft.rfft(fr * self._win, n=512, axis=1)) ** 2
        return np.log(np.maximum(spec.astype(np.float32) @ self._mel, 1.1920929e-07))

    def sim(self, x) -> float | None:
        """x：16 kHz float32 音频 → 和你的声音的余弦；没加载好或太短 → None。"""
        if not self.ok or x is None or len(x) < 16000 * 0.5:
            return None
        try:
            np = self._np
            f = self._fbank(np.asarray(x, np.float32))
            f = (f - f.mean(axis=0, keepdims=True)).astype(np.float32)
            e = np.asarray(self._sess.run(None, {self._name: f[None]})[0]).reshape(-1)
            return float(e @ self._prof / (np.linalg.norm(e) + 1e-9))
        except Exception as e:
            log.warning("认声音出错（%r）", e)
            return None


_VOICE: _VoiceCheck | None = None


def _voice_check() -> _VoiceCheck:
    """进程里只加载一份；声纹文件换过了就重新加载。"""
    global _VOICE
    if _VOICE is None or _VOICE.stale():
        _VOICE = _VoiceCheck()
    return _VOICE


# ---------------------------------------------------------------- 放出去的声音（回声的参考）
# 2026-11-14 那场（20261114-225356）：主持人出门，用手机以访客 x 进会，家里没人。家里电脑的扬声器放着 x 的声音、
# 学生的配音、x 那边的环境声，麦克风又把它们录了进来——口喷鸡的回声消除只消掉约 19 dB，剩下的照样被转写，
# 成了"主持人"说的话（谢谢点赞、你什么时候成为超人了……），还带着主持人改稿的权限。录音里 78 段转写全在放音时；
# 29 分钟时扬声器一关，后面 18 分钟（访客还说了 85 段、学生 23 句）麦克风一个字也没录到。
# 所以耳朵得知道 Bloom 自己此刻在放什么：语音桥把每位访客收到的声音（16 kHz，就是放到扬声器的那份）、
# 嘴把学生每一句的 PCM（按播放时刻）记进 PLAYBACK。耳朵收到一段转写，拿麦克风这段的"频谱随时间的花纹"
# 和放出去的那份比：几乎全程都在放音、花纹又对得上（回声就是那份声音，只是小了、染了房间的色）= 回声，丢掉；
# 你在放音时说话，花纹是你自己的，对不上，照收。
REF_SR = 16000
REF_HOP = 160                 # 参考音量 10 毫秒一格
REF_ON_DB = -55.0             # 放出去的声音这一帧过这条线（dBFS）才算"在放"
ECHO_SIM = 0.6                # 麦克风这段和放出去那份的花纹相关（按响度加权）到这么高 = 回声。离线回放（tests/echo_replay.py）：
                              # 那场 78 段回声中位 0.66；你在访客放音时说话（真声混进去，130 段）中位 0.25、95% 0.45、最高 0.58。
                              # 0.5 能多认出约 15% 的回声，但会丢 2/130 句你的真话；宁可漏认回声（后面还有 app 的文字比对），不丢你的话
ECHO_SIM_WIDE = 0.65          # 还没学到延迟、要在 LAG_MIN～LAG_MAX 里大范围找时，门槛高一点（找的范围大，碰巧对上的也多）
ECHO_COVER = 0.6              # 有声帧里至少这么多是在放音时录到的才可能是回声（你打断学生时，后半段已经不在放了）
BARGE_ECHO_SIM = 0.6          # 判"你打断学生"之前那半秒：花纹相关到这么高才当回声不打断（离线：误打断的回声 0.67–0.80，
                              # 你真开口时最高 0.54——比转写那道门更保守，宁可多打断一次也不晚打断）
ECHO_MIN_FRAMES = 6           # 能比的帧（有声、又在放音）不到这么多（约 0.2 秒）就不判
LAG_MIN, LAG_MAX = -0.1, 1.5  # 放出去 → 录音里出现（按耳朵估的墙上时间）的延迟找这个范围：播放器缓冲 + 口喷鸡回声消除的等待
HOST_AWAY_BACK_TH = 0.55      # 主持人在别的设备上时，家里麦克风这段的声纹得这么像你、而且几乎没在放音，才算你回到电脑前了


class PlaybackRef:
    """Bloom 自己往扬声器放的声音（访客的、学生的），按墙上时间存最近 90 秒（16 kHz 单声道），给耳朵当回声的参考。
    put()：访客那一路一小块一小块来（到达时刻会抖，按样本数接着排，偏太多才按时钟重对）；
    put_at()：学生这一句的 PCM 和它开始出声的时刻（采样率不同就换成 16 kHz，语速不是 1 就按 10 毫秒一块抽掉/补上）；
    cut()：学生被打断，后面排好的那截作废（不然你打断以后接着说的话会被当成回声）。
    host_elsewhere：主持人正用另一台设备以访客身份参会时那位访客的名字（guest_ears 按声纹认出来）；这时家里这台的麦克风不算主持人。"""

    KEEP_S = 90.0

    def __init__(self):
        self._lock = threading.Lock()
        self._tracks: dict = {}
        self.host_elsewhere: str | None = None
        self.host_elsewhere_sim: float | None = None
        self.host_here_at = 0.0       # 家里这台麦克风最近一次证实你本人就在电脑前（之后十分钟 guest_ears 不再认"在别的设备上"）

    def _np(self):
        import numpy as np
        return np

    def _track(self, src: str, kind: str) -> dict:
        tr = self._tracks.get(src)
        if tr is None:
            np = self._np()
            n = int(self.KEEP_S * REF_SR)
            tr = self._tracks[src] = {"kind": kind, "ring": np.zeros(n, np.int16),
                                      "env": np.full(n // REF_HOP, -120.0, np.float32), "t0": None, "end": 0}
        return tr

    def _write(self, tr: dict, i: int, x) -> None:
        np = self._np()
        ring, env = tr["ring"], tr["env"]
        n = len(ring)
        if len(x) > n:
            i, x = i + len(x) - n, x[-n:]
        if not len(x):
            return
        ring[np.arange(i, i + len(x)) % n] = x
        b0, b1 = i // REF_HOP, (i + len(x) - 1) // REF_HOP + 1
        blk = ring[np.arange(b0 * REF_HOP, b1 * REF_HOP) % n].astype(np.float32).reshape(-1, REF_HOP) / 32768.0
        env[np.arange(b0, b1) % len(env)] = 10.0 * np.log10((blk * blk).mean(axis=1) + 1e-12)

    def _zero(self, tr: dict, i0: int, i1: int) -> None:
        if i1 > i0:
            np = self._np()
            self._write(tr, max(i0, i1 - len(tr["ring"])), np.zeros(min(i1 - i0, len(tr["ring"])), np.int16))

    def put(self, src: str, pcm, t_end: float, kind: str = "guest") -> None:
        """一小块正在放的声音（16 kHz 单声道 int16），t_end = 它最后一个样本放出去的时刻（到达时刻）。"""
        try:
            np = self._np()
            x = np.frombuffer(pcm, dtype=np.int16) if isinstance(pcm, (bytes, bytearray, memoryview)) else np.asarray(pcm, np.int16)
            with self._lock:
                tr = self._track(src, kind)
                if tr["t0"] is None:
                    tr["t0"], tr["end"] = t_end - len(x) / REF_SR, 0
                start = int(round((t_end - tr["t0"]) * REF_SR)) - len(x)
                if abs(start - tr["end"]) <= REF_SR // 5:      # 一块接一块：按样本数排，很慢地跟着时钟走（到达时刻一抖一抖的）
                    start = tr["end"] + int(round(0.002 * (start - tr["end"])))
                if start > tr["end"]:
                    self._zero(tr, tr["end"], start)           # 断了一阵（访客关麦、网络卡）：中间是静音
                self._write(tr, start, x)
                tr["end"] = start + len(x)
        except Exception:
            log.debug("参考声音记不上", exc_info=True)

    def put_at(self, src: str, pcm, t_start: float, sr: int = REF_SR, rate: float = 1.0, kind: str = "tts") -> None:
        """一整块要放的声音（int16 单声道，采样率 sr，按 rate 倍速放），从 t_start 开始出声。"""
        try:
            x = _to_ref(self._np(), pcm, sr, rate)
            if x is None or not len(x):
                return
            with self._lock:
                tr = self._track(src, kind)
                if tr["t0"] is None:
                    tr["t0"], tr["end"] = t_start, 0
                start = int(round((t_start - tr["t0"]) * REF_SR))
                if start > tr["end"]:
                    self._zero(tr, tr["end"], start)
                self._write(tr, start, x)
                tr["end"] = max(tr["end"], start + len(x))
        except Exception:
            log.debug("参考声音记不上", exc_info=True)

    def cut(self, src: str, t: float) -> None:
        """src 在 t 时刻停了（被打断 / 放完）：t 以后排着的作废。"""
        with self._lock:
            tr = self._tracks.get(src)
            if tr is None or tr["t0"] is None:
                return
            i = int(round((t - tr["t0"]) * REF_SR))
            if i < tr["end"]:
                self._zero(tr, i, tr["end"])
                tr["end"] = max(i, tr["end"] - len(tr["ring"]))

    def read(self, w0: float, w1: float, kinds=None):
        """[w0, w1) 墙上时间里放出去的声音（几路加起来，float32，16 kHz）。没放的地方是 0。"""
        np = self._np()
        n = max(0, int(round((w1 - w0) * REF_SR)))
        out = np.zeros(n, np.float32)
        with self._lock:
            for tr in self._tracks.values():
                if tr["t0"] is None or (kinds and tr["kind"] not in kinds):
                    continue
                N = len(tr["ring"])
                i = int(round((w0 - tr["t0"]) * REF_SR))
                lo, hi = max(i, tr["end"] - N, 0), min(i + n, tr["end"])
                if hi > lo:
                    out[lo - i:hi - i] += tr["ring"][np.arange(lo, hi) % N].astype(np.float32) / 32768.0
        return out

    def level(self, w0: float, w1: float, kinds=None) -> float:
        """[w0, w1) 里放出去的声音最响的那 10 毫秒（dBFS）；什么都没放 = -120。"""
        np = self._np()
        best = -120.0
        with self._lock:
            for tr in self._tracks.values():
                if tr["t0"] is None or (kinds and tr["kind"] not in kinds):
                    continue
                E = len(tr["env"])
                b0 = max(int((w0 - tr["t0"]) * REF_SR) // REF_HOP, (tr["end"] - len(tr["ring"])) // REF_HOP + 1, 0)
                b1 = min(int((w1 - tr["t0"]) * REF_SR) // REF_HOP + 1, tr["end"] // REF_HOP)
                if b1 > b0:
                    best = max(best, float(tr["env"][np.arange(b0, b1) % E].max()))
        return best

    def clear(self) -> None:
        with self._lock:
            self._tracks.clear()
        self.host_elsewhere = self.host_elsewhere_sim = None
        self.host_here_at = 0.0


def _to_ref(np, pcm, sr: int, rate: float):
    """任意采样率的 int16 → 16 kHz int16；rate ≠ 1（ffplay atempo 变速不变调）就按 10 毫秒一块抽掉/重复，花纹对得上就行。"""
    x = np.frombuffer(pcm, dtype=np.int16) if isinstance(pcm, (bytes, bytearray, memoryview)) else np.asarray(pcm, np.int16)
    if not len(x):
        return x
    sr = int(sr or REF_SR)
    if sr != REF_SR:
        y = x.astype(np.float32)
        k = max(1, int(round(sr / REF_SR)))
        if k > 1:                                   # 粗的抗混叠：几个样本取平均
            y = np.convolve(y, np.ones(k, np.float32) / k, mode="same")
        n = int(len(y) * REF_SR / sr)
        y = np.interp(np.arange(n) * (sr / REF_SR), np.arange(len(y)), y)
        x = np.clip(y, -32768, 32767).astype(np.int16)
    if rate and abs(float(rate) - 1.0) > 0.01:
        blk = REF_HOP
        nb = len(x) // blk
        if nb:
            idx = np.minimum((np.arange(int(nb / float(rate))) * float(rate)).astype(np.int64), nb - 1)
            x = x[:nb * blk].reshape(nb, blk)[idx].reshape(-1)
    return x


PLAYBACK = PlaybackRef()


class _MicState:
    """主持人麦克风静音（会议页上点的，POST /api/mic/mute）：静音时你那一路不转写、不算你在说话、不打断学生、
    不推给通过链接进来的人。静了哪几段记下来（墙上时间）：转写比声音晚一两秒，静音时说的话静音解除后才落地也照样不收。
    状态存在 ~/.zuhui/mic_mute.json：后端中途重启也还是静音（不会悄悄把麦克风重新打开）；这场会散了才清掉，
    下一场会从不静音开始（开会前在大厅里点了静音，就带进下一场会）。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (Path(os.environ.get("ZUHUI_HOME") or (Path.home() / ".zuhui")) / "mic_mute.json")
        self._lock = threading.Lock()
        self.spans: deque = deque(maxlen=200)     # [开始, 结束或 None]
        self.muted = False
        self.session: str | None = None           # 正在开的那场（录屏目录）；静音时一起存下，换了一场就不算数
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if bool(d.get("muted")):
                self.muted = True
                self.session = d.get("session") or None
                self.spans.append([time.time(), None])
        except (OSError, ValueError, AttributeError):
            pass

    def set(self, muted: bool) -> bool:
        muted = bool(muted)
        with self._lock:
            if muted != self.muted:
                now = time.time()
                if muted:
                    self.spans.append([now, None])
                elif self.spans and self.spans[-1][1] is None:
                    self.spans[-1][1] = now
                self.muted = muted
            try:
                if muted:
                    self.path.write_text(json.dumps({"muted": True, "session": self.session, "at": round(time.time(), 1)},
                                                    ensure_ascii=False), encoding="utf-8")
                elif self.path.exists():
                    self.path.unlink()
            except OSError:
                pass
            return self.muted

    def overlap(self, ws: float, we: float) -> float:
        """[ws, we]（墙上时间）里静音的比例。"""
        if we <= ws:
            we = ws + 0.05
        now, cover = time.time(), 0.0
        with self._lock:
            for s, e in self.spans:
                cover += max(0.0, min(we, e if e is not None else now) - max(ws, s))
        return min(1.0, cover / (we - ws))


MIC = _MicState()

_MEL_W = None


def _logmel(np, x):
    """32 毫秒一帧（512 点，不重叠）、100–7000 Hz 32 个 mel 带的对数能量。"""
    global _MEL_W
    if _MEL_W is None:
        mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)   # noqa: E731
        c = np.linspace(mel(100.0), mel(7000.0), 34)
        m = mel(np.arange(257) * 16000.0 / 512)
        w = np.zeros((257, 32), np.float32)
        for b in range(32):
            lo, ce, hi = c[b], c[b + 1], c[b + 2]
            w[:, b] = np.where((m > lo) & (m <= ce), (m - lo) / (ce - lo), np.where((m > ce) & (m < hi), (hi - m) / (hi - ce), 0.0))
        _MEL_W = (w, np.hanning(512).astype(np.float32))
    w, win = _MEL_W
    n = len(x) // 512
    if n <= 0:
        return np.zeros((0, 32), np.float32), np.zeros(0, np.float32)
    fr = np.asarray(x[:n * 512], np.float32).reshape(n, 512)
    db = 10.0 * np.log10((fr * fr).mean(axis=1) + 1e-12)
    sp = np.abs(np.fft.rfft(fr * win, axis=1)) ** 2
    return np.log(sp @ w + 1e-9).astype(np.float32), db.astype(np.float32)


def echo_match(np, mic, voiced, ref, offsets) -> dict:
    """mic：麦克风这段（float32，16 kHz）；voiced：mic 每 32 毫秒一帧是不是人声；ref：同一段放出去的声音，往前多读了
    max(offsets) 帧（ref 的第 j+o 帧对 mic 的第 j 帧 = 延迟 o 帧）。在 offsets 里找花纹最对得上的延迟：
    → {"sim": 有声且在放音的帧上，去掉各频带均值后逐帧余弦、按麦克风响度加权的平均, "cover": 有声帧里在放音的比例,
       "off": 延迟帧数, "n": 比了几帧}"""
    M, mdb = _logmel(np, mic)
    R, rdb = _logmel(np, ref)
    n = min(len(M), len(voiced))
    M, mdb, v = M[:n], mdb[:n], np.asarray(voiced[:n], bool)
    nv = int(v.sum())
    widest = {"sim": None, "cover": 0.0, "off": None, "n": 0, "voiced": nv}
    best = None
    if nv == 0:
        return widest
    for o in offsets:
        if o < 0 or o + n > len(R):
            continue
        act = rdb[o:o + n] > REF_ON_DB
        rows = np.where(v & act)[0]
        cover = len(rows) / nv
        sim = None
        if len(rows) >= ECHO_MIN_FRAMES:
            a = M[rows] - M[rows].mean(axis=0, keepdims=True)
            b = R[rows + o] - R[rows + o].mean(axis=0, keepdims=True)
            s = (a * b).sum(axis=1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1) + 1e-9)
            # 按麦克风这一帧的响度加权：转写转出来的是这段里最响的那个声音——你比回声响，你的帧说了算
            # （只取中位数的话，你说话的停顿里全是回声帧，整段会被当成回声）。权重封顶在 90 分位，免得一两帧说了算
            w = 10.0 ** (mdb[rows] / 10.0)
            w = np.minimum(w, np.percentile(w, 90))
            sim = float((w * s).sum() / (w.sum() + 1e-12))
        if cover > widest["cover"]:
            widest = {"sim": sim, "cover": cover, "off": o, "n": len(rows), "voiced": nv}
        if sim is not None and cover >= ECHO_COVER and (best is None or sim > best["sim"]):
            best = {"sim": sim, "cover": cover, "off": o, "n": len(rows), "voiced": nv}
    return best or widest


class _AudioVAD:
    """跟读录屏的 live/audio.pcm（16 kHz 单声道 s16le），用 Silero 判断"此刻有没有人在说话"。

    耳朵靠它决定什么时候算你说完：文字落地时你已经停了，立刻发出去；你又开口了，就等下一块一起发。
    学生自己出声的时候（muted() 为真）听到的不算你在说话。没有 onnxruntime 或模型就退回能量门限。"""
    FRAME = 512          # 32 毫秒一帧

    def __init__(self, session: Path, muted=None, threshold: float = 0.5):
        self.pcm = session / "live" / "audio.pcm"
        self.muted = muted or (lambda: False)
        self.threshold = float(threshold)
        self.pos = 0                 # 读到的字节
        self.last_voice = -1.0       # 音频时钟（秒）：最后一个有声帧的结束
        self.audio_now = 0.0         # 音频时钟：读到哪了
        self.knows = False           # 读到过音频没有
        self.pump_wall = 0.0         # 最近一次读音频的墙上时间（audio_now 对应这一刻）
        self.on_barge = None         # 你打断学生时调它（参数：一句说明）
        self.pending = None          # 学生有话排着还没出声吗
        self.barge_ratio, self.barge_floor = 2.5, 0.02
        self._echo = deque(maxlen=150)   # 放音时麦克风每帧的音量（约 5 秒）
        self._loud = 0
        self._run_on = 0
        self._barged = False
        self._rest = b""
        self.frames: deque = deque(maxlen=1250)  # 最近约 40 秒每帧（结束秒, 是不是你在出声）：认声音时只取有声的帧
        self.probs: deque = deque(maxlen=1250)   # 同样这些帧（结束秒, Silero 概率）：学生出声时也照记，比回声用
        self._offs: deque = deque(maxlen=300)    # 最近 30 秒每次读音频时的（墙上时刻 − 读到的录音秒）：取最小的当录音时钟的零点
        self.lags: deque = deque(maxlen=9)       # 认出回声时对上的延迟（秒）：学到 3 个以后只在它附近找
        self.mic_off = lambda: bool(MIC.muted or PLAYBACK.host_elsewhere)   # 静音 / 主持人在别的设备上：这一路不算你
        self._sess = self._np = None
        try:
            import numpy as np
            self._np = np
            import onnxruntime as ort
            model = BLURT_HOME / "models" / "silero_vad.onnx"
            if model.exists():
                so = ort.SessionOptions()
                so.intra_op_num_threads = so.inter_op_num_threads = 1
                self._sess = ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
                self._names = {i.name for i in self._sess.get_inputs()}
                self._reset_state()
        except Exception as e:
            log.warning("Silero 判停起不来（%r），退回能量门限", e)
            self._sess = None

    BARGE_FRAMES = 8             # 放音时，连续约 0.26 秒明显比回声响 = 你在打断
    echo_gate = True             # 打断之前先和放出去的声音比一下（config ears.echo_gate = false 关掉）
    PENDING_FRAMES = 12          # 回话还没出声时，你连续说了约 0.38 秒 = 你还没说完

    def _fire_barge(self, why: str) -> None:
        if self.on_barge is not None:
            try:
                self.on_barge(why)
            except Exception:
                log.exception("on_barge 出错")

    def read(self, t0: float, t1: float):
        """录音里 t0..t1 秒那一段（float32，-1..1）。读不到返回 None。"""
        np = self._np
        if np is None or t1 <= t0:
            return None
        a, b = int(max(0.0, t0) * 16000) * 2, int(t1 * 16000) * 2
        try:
            with open(self.pcm, "rb") as f:
                f.seek(a)
                raw = f.read(b - a)
        except OSError:
            return None
        return np.frombuffer(raw[:len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0

    def voiced(self, t0: float, t1: float) -> list[float]:
        """t0..t1 秒里判成你在出声的帧（帧的结束秒，按时间先后）。只记得最近约 40 秒。"""
        return [t for t, v in self.frames if v and t0 < t - self.FRAME / 16000.0 and t <= t1]

    def voiced_audio(self, t0: float, t1: float, max_s: float = 4.0):
        """t0..t1 秒里有声的那些帧连起来（最多最后 max_s 秒）；读不到返回 None。"""
        np = self._np
        ends = self.voiced(t0, t1)[-int(max_s * 16000 / self.FRAME):]
        if np is None or not ends:
            return None
        d = self.FRAME / 16000.0
        x = self.read(ends[0] - d, ends[-1])
        if x is None:
            return None
        base = ends[0] - d
        parts = [x[int(round((t - d - base) * 16000)):int(round((t - base) * 16000))] for t in ends]
        return np.concatenate(parts) if parts else None

    def voice_before(self, t: float) -> float:
        """t 秒（含）之前最后一个有声帧的结束；记得的范围里没有 → -1。"""
        for te, v in reversed(self.frames):
            if v and te <= t:
                return te
        return -1.0

    def wall(self, t: float) -> float:
        """录音里的 t 秒 → 墙上时间。音频是一批一批写进文件的，直接拿最近一次读到的时刻推会晚 0–0.2 秒、还忽早忽晚：
        取最近 30 秒里"读到的时刻 − 读到的位置"最小的那次当零点（剩下固定的那点延迟由回声比对时找的延迟吸收）。"""
        if self._offs:
            return t + min(self._offs)
        return self.pump_wall - (self.audio_now - t)

    def echo_at(self, t0: float, t1: float, kinds=None) -> dict | None:
        """录音 t0..t1 秒这段和 Bloom 同一时间放出去的声音比花纹（见 echo_match）：
        → {"echo": 是不是回声, "sim", "cover", "lag", "n", "voiced"}；读不到、那会儿什么都没放 → None。"""
        np = self._np
        if np is None or t1 - t0 < 0.15:
            return None
        F = self.FRAME / 16000.0
        t0 = math.floor(max(0.0, t0) / F) * F                     # 和判停的帧对齐
        x = self.read(t0, t1)
        if x is None or len(x) < self.FRAME * 3:
            return None
        n = len(x) // self.FRAME
        w0 = self.wall(t0)
        if len(self.lags) >= 3:                                   # 学到延迟了：只在它附近找
            lag = sorted(self.lags)[len(self.lags) // 2]
            lo, hi, th = max(LAG_MIN, lag - 0.13), min(LAG_MAX, lag + 0.13), ECHO_SIM
        else:
            lo, hi, th = LAG_MIN, LAG_MAX, ECHO_SIM_WIDE
        if PLAYBACK.level(w0 - LAG_MAX, w0 + n * F - LAG_MIN, kinds) <= REF_ON_DB:
            return None                                           # 那会儿什么都没放
        ref = PLAYBACK.read(w0 - LAG_MAX, w0 + n * F - LAG_MIN, kinds)
        offs = range(int(round((LAG_MAX - hi) / F)), int(round((LAG_MAX - lo) / F)) + 1)
        te = np.fromiter((a for a, _ in self.probs), np.float64, len(self.probs))
        pp = np.fromiter((b for _, b in self.probs), np.float32, len(self.probs))
        ends = t0 + (np.arange(n) + 1) * F
        voiced = np.zeros(n, bool)
        if len(te):
            k = np.clip(np.searchsorted(te, ends - F / 2), 0, len(te) - 1)
            near = np.abs(te[k] - ends) < F / 2
            voiced = (pp[k] >= self.threshold) & near
            if int(voiced.sum()) < ECHO_MIN_FRAMES:          # 判停几乎没判出人声（短促的"哦"、小声的回声）：放宽到像人声的帧
                voiced = (pp[k] >= 0.15) & near
        r = echo_match(np, x[:n * self.FRAME], voiced, ref, offs)
        r["lag"] = None if r.get("off") is None else round(LAG_MAX - r["off"] * F, 3)
        r["echo"] = bool(r.get("sim") is not None and r["cover"] >= ECHO_COVER and r["sim"] >= th)
        r["th"] = th
        return r

    def _reset_state(self):
        np = self._np
        if self._sess is None or np is None:
            return
        if "state" in self._names:          # Silero v5：一个 state
            self._state = np.zeros((2, 1, 128), np.float32)
        else:                               # v4：h、c 两个
            self._h = np.zeros((2, 1, 64), np.float32)
            self._c = np.zeros((2, 1, 64), np.float32)

    def _prob(self, frame) -> float:
        np = self._np
        if self._sess is None:
            return 1.0 if float(np.sqrt((frame * frame).mean())) > 0.02 else 0.0
        x = frame.reshape(1, -1)
        if "state" in self._names:
            feeds = {"input": x, "state": self._state, "sr": np.array(16000, np.int64)}
            out, self._state = self._sess.run(None, feeds)[:2]
        else:
            feeds = {"x": x, "h": self._h, "c": self._c}
            if "sr" in self._names:
                feeds["sr"] = np.array(16000, np.int64)
            out, self._h, self._c = self._sess.run(None, feeds)[:3]
        return float(np.asarray(out).reshape(-1)[0])

    def pump(self) -> None:
        """读掉新追加的音频。出错就当这一轮没听到。"""
        np = self._np
        if np is None:
            return
        try:
            size = self.pcm.stat().st_size
        except OSError:
            return
        if size < self.pos:                       # 新的一段录音
            self.pos, self._rest, self.last_voice = 0, b"", -1.0
            self._reset_state()
            self.frames.clear()
        n = size - self.pos
        if n <= 0:
            return
        if n > 32000 * 20:                        # 落后太多：只听最近三秒
            self.pos, self._rest = size - 32000 * 3, b""
            self._reset_state()
            self.frames.clear()
            n = size - self.pos
        try:
            with open(self.pcm, "rb") as f:
                f.seek(self.pos)
                raw = f.read(n)
        except OSError:
            return
        self.pos += len(raw)
        buf = self._rest + raw
        step = self.FRAME * 2
        k = len(buf) // step
        t0 = (self.pos - len(buf)) / 32000.0
        muted = bool(self.muted())
        off = bool(self.mic_off())
        pending = bool(self.pending()) if self.pending else False
        try:
            for i in range(k):
                frame = np.frombuffer(buf[i * step:(i + 1) * step], dtype=np.int16).astype(np.float32) / 32768.0
                p = self._prob(frame)
                t_end = t0 + (i + 1) * self.FRAME / 16000.0
                self.frames.append((t_end, not muted and not off and p >= self.threshold))
                self.probs.append((t_end, p))
                if off:                    # 静音 / 主持人在别的设备上：这一路的声音不算你在说话，也不打断学生
                    self._loud = self._run_on = 0
                    continue
                if not muted:
                    if p >= self.threshold:
                        self.last_voice = t_end
                        self._run_on = self._run_on + 1 if pending else 0
                        # 学生的回话还在合成、没出声，你又接着说了好一会儿：这一轮不要了，等你说完。
                        # 访客的声音正从音箱放、录到的就是它（花纹对得上）：那是访客在说，不是你（他说话另有一路判）
                        if pending and self._run_on >= self.PENDING_FRAMES and not self._barged:
                            if self._guest_echo(t_end):
                                self._run_on = 0
                                continue
                            self._barged = True
                            self._fire_barge("你接着在说，学生那句还没出声就撤了")
                    else:
                        self._run_on = 0
                    if not pending:
                        self._barged = False
                    self._loud = 0
                    continue
                # 学生的声音正从音箱出来：麦克风里有回声。回声的音量比较稳，取它的中位数当底；
                # 你一开口，音量会明显高出一截，持续约四分之一秒就算你在打断
                rms = float(np.sqrt((frame * frame).mean()))
                self._echo.append(rms)
                base = sorted(self._echo)[len(self._echo) // 2] if len(self._echo) >= 15 else 0.0
                if p >= self.threshold and rms > max(self.barge_ratio * base, self.barge_floor):
                    self._loud += 1
                else:
                    self._loud = max(0, self._loud - 1)
                if self._loud >= self.BARGE_FRAMES and not self._barged:
                    # 这一下响的花纹就是音箱正放的（学生自己声音的一阵大、访客的声音也在放）：不是你开口（访客插话由语音桥那一路判，
                    # 照样会停）。2026-11-14 家里没人时就这样误打断过一次（"音量是回声的 19.9 倍"）
                    if self._guest_echo(t_end, kinds=None):
                        self._loud = 0
                        continue
                    self._barged = True
                    self.last_voice = t_end
                    self._fire_barge(f"放音时你开口了（音量是回声的 {rms / max(base, 1e-4):.1f} 倍）")
        except Exception as e:
            log.warning("判停推理出错（%r），退回能量门限", e)
            self._sess = None
        self._rest = buf[k * step:]
        self.audio_now = self.pos / 32000.0
        self.pump_wall = time.time()
        self._offs.append(self.pump_wall - self.audio_now)
        self.knows = True

    def _guest_echo(self, t_end: float, kinds=("guest",)) -> bool:
        """要算你开口 / 打断之前问一句：最近这半秒录到的，是不是音箱正在放的声音（默认只看访客的；kinds=None 学生的也算）
        又被录进来（花纹对得上）。没在放、判不了 → False（照旧算你）。"""
        if not self.echo_gate:
            return False
        try:
            r = self.echo_at(t_end - 0.5, t_end, kinds=kinds)
        except Exception:
            log.exception("插话前比回声出错")
            return False
        if r is not None and r.get("echo") and r["sim"] >= BARGE_ECHO_SIM:
            log.info("录到的是音箱的回声（花纹相关 %.2f，延迟 %s 秒），不算你开口", r.get("sim") or 0.0, r.get("lag"))
            return True
        return False


_TERMS = [
    (re.compile(r"(?i)(?<![A-Za-z])cloud\s*code(?![A-Za-z])"), "Claude Code"),
    (re.compile(r"(?i)(?<![A-Za-z])cloud(?![A-Za-z])"), "Claude"),
    (re.compile(r"(?i)(?<![A-Za-z])deep\s*(?:seek|sea|see|sick|sik|sec)(?![A-Za-z])"), "DeepSeek"),
    (re.compile(r"(?i)(?<![A-Za-z])Claude\s*docs?(?![A-Za-z])"), "Claude Docs"),
]


def fix_terms(text: str) -> str:
    """识别结果的写法整形（每段都过，快嘴和逐字稿拿到的都是这一版）：脏话删掉；软件名写对（cloud→Claude、
    DIPSICK→DeepSeek、听错的主持人名字→主持人名字，含散会后学到的规则）；口述的命令和型号写对（杠 model→/model、
    Claude 杠 Opus 杠四杠六→claude-opus-4-6、V 四 PRO→V4 Pro）；再按当前论文的词库改回论文里的写法。
    见 zuhui/normalize.py（python -m zuhui.normalize --test）。原始识别只留在录屏目录的 live/transcript.jsonl。"""
    try:
        from .normalize import live
        return live(text)
    except Exception:
        log.exception("normalize 出错，退回老规则")
    for rx, to in _TERMS:
        text = rx.sub(to, text)
    try:
        from .lexicon import correct
        text = correct(text)
    except Exception:
        pass
    return text


_MARKS = re.compile(r"(?i)(?<![A-Za-z])/?sil(?![A-Za-z])|<\|[^|>]*\|>|\[(?:noise|music|laughter|silence)\]")
_SENT = re.compile(r"[^。！？!?；;]+[。！？!?；;]*")


def _core(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;…“”\"'（）()]", "", s or "")


_KANA = [(re.compile(r"[うウ]ん[うウ]ん|ううん|ウウン"), "嗯嗯"), (re.compile(r"[うウ]ん|ン"), "嗯"),
         (re.compile(r"[あア]+"), "啊"), (re.compile(r"[えエ]+"), "诶"), (re.compile(r"[はハ]い"), "嗯")]
_KANA_REST = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]+")


def clean_asr(text: str) -> str:
    """识别结果的几种毛病先收拾掉：
    · 静音标记（"/sil"、<|...|>）被当成了字；
    · 中文语气词被认成日文假名（"うん""ううん"）：改成"嗯"，别的假名删掉；
    · 长段语音解码时打转：同一句连说好几遍，末尾还拖半句。只留一遍。"""
    t = _MARKS.sub("", text or "")
    for rx, to in _KANA:
        t = rx.sub(to, t)
    t = _KANA_REST.sub("", t)
    t = re.sub(r"(.{2,12}?)\1{3,}", r"\1", t)           # 一小截连着重复四遍以上（"然后这个然后这个…"）
    out, prev = [], ""
    for m in _SENT.finditer(t):
        sent = m.group(0)
        c = _core(sent)
        if not c:
            if out:
                out[-1] += sent.strip()
            continue
        if len(c) >= 4 and c == prev:
            continue                                     # 和上一句一字不差：打转出来的
        out.append(sent)
        prev = c
    if len(out) >= 2:                                    # 拖在最后的半句，是上一句的开头：也是打转
        last = out[-1]
        lc = _core(last)
        if last.rstrip()[-1:] not in "。！？!?；;" and len(lc) >= 4 and _core(out[-2]).startswith(lc):
            out.pop()
    t = "".join(out).strip()
    return t if _core(t) else ""


_GREET = re.compile(r"(?i)^(?:喂|你好|您好|哈喽|嗨|hello|hi|hey|在吗|在不在|你在吗|在不|能听到吗|听得到吗|听得见吗|能听见吗|"
                    r"听到了吗|能听到我说话吗|听到没|测试|试一下|试试)+$")
_BYE = re.compile(r"(?i)^(?:好的|好|行|ok|okay|嗯|那|那就|那么|今天|我们|大家)*(?:散会|今天(?:就|先)?到这(?:里|儿)?|"
                  r"今天(?:就|先)?这样|就到这(?:里|儿)?|结束吧|会议结束|先这样吧?)(?:吧|了|啊|哈)*$")
_CLOSE = re.compile(r"(?i)^(?:好的|好|行|嗯|嗯嗯|恩|恩恩|ok|okay|对|对对|对的|是的|没问题|可以|没事|收到|了解|明白|知道了|"
                    r"谢谢|辛苦了|辛苦|好吧|行吧|那好|就这样|不错|挺好|很好|可以的|不用了)+$")
_THINK = re.compile(r"^(?:呃|嗯|恩|啊|那个|这个|就是|然后|怎么说呢|怎么说|我想想|我再想想|让我想想|我想一下|等一下|等等|稍等|"
                    r"我看看|我看一下|嗯哼)+$")


def quick_kind(text: str) -> str | None:
    """几类不用模型想的话：greet 打招呼试麦 / bye 散会 / hold 让学生等 / close 收尾确认、附和（好的、没问题、嗯嗯）/
    think 你在想（那个、我想想）。先查公开数据整理的词表（zuhui/turn.py），没命中再用下面几条正则。"""
    try:
        from .turn import lexicon
        k = lexicon().whole_kind(text)
        if k:
            return "close" if k == "backchannel" else k
    except Exception:
        pass
    c = _core(text).lower()
    if not c or len(c) > 16:
        return None
    for kind, rx in (("think", _THINK), ("greet", _GREET), ("bye", _BYE), ("close", _CLOSE)):
        if rx.match(c):
            return kind
    return None


PUNCT_END = "。！？!?；;…"
PUNCT_ANY = PUNCT_END + "，,、：:"


class _Collector:
    """把实时转写的小段按停顿合并成一条意见，攒够了就 emit(Utterance)。

    停顿阈值看情况：刚说完一句（句号/问号）只等 0.8 秒；才几个字、没标点，多半话没说完，多等一会；
    停在逗号上也多等一点。段与段之间停过半秒、又没标点的，补个逗号，逐字稿好读，快嘴也好断句。
    每来一段就 partial({"text","t","n"}) 一次，前端拿它当"正在说的那行"。
    """

    def __init__(self, session: Path, idle_flush: float, min_chars: int, screenshots: bool, emit, partial=None, drop=None):
        self.session = session
        self.idle_flush = float(idle_flush)
        self.min_chars = int(min_chars)
        self.screenshots = bool(screenshots)
        self.emit = emit
        self.partial = partial
        self.vad = None           # _AudioVAD：听得到音频时，靠它判你说完没有
        self.drop = drop          # drop(墙上开始, 墙上结束, 文字) → True 就丢掉这段（学生自己的声音漏进麦克风）
        self.buf: list[str] = []
        self.t0 = None
        self.last_wall = None
        self.last_end = None      # 上一段转写在录屏里的结束秒
        self.heard = None         # 最后一段话说完的墙上时间（估）
        self.covered = -1.0       # 录音里到这一秒为止的话都已经有准确文字了（预览只显示这之后的）
        self.tentative = None     # tentative(文字)：多半说完了，可以先让模型想着；tentative(None)：你又开口了，作废
        self._spec = None         # 已经报过的那一次（文字, 当时最后一个有声帧[, "draft"]）
        self.draft = None         # 快识别（DraftASR）：None = 不出初稿
        self._draft_job = None    # 最近一次初稿：{"key": (最后有声帧, 起点), "res": (文字, 毫秒) 或 None, "used"}
        self._p = None            # smart-turn 对这次停顿打的分（说完了的概率）；你一开口或来了新字就作废
        self._p_voice = None      # 打分时最后一个有声帧的位置
        self.voice = _voice_check()   # 认声音（_VoiceCheck）：None 或没加载好 = 不管，和以前一样
        self._after = None        # 你这段话之后听到的声音判过没有：(last_end, 判时有声秒数, 是不是你)
        self.on_log = None        # on_log(kind, **字段)：记进会议日志（回声丢掉了哪段、主持人在别的设备上……）
        self.en = False           # 日志里的说明用英文（会议语言是 en）
        self.echo_gate = True     # 和放出去的声音比花纹、认回声（config ears.echo_gate = false 关掉）
        self._away_seen = None    # 已经记过"主持人在别的设备上"的那位访客
        self.n = 0

    def _log(self, kind: str, **kw) -> None:
        if self.on_log is not None:
            try:
                self.on_log(kind, **kw)
            except Exception:
                log.exception("on_log 出错")

    def _L(self, zh: str, en: str) -> str:
        return en if self.en else zh

    def _raw_voiced(self, v, s: float, e: float, max_s: float = VOICE_MAX_S):
        """s..e 秒里 Silero 判成人声的帧连起来（不管当时是不是静音 / 学生在出声）；读不到 → None。"""
        np = v._np
        F = v.FRAME / 16000.0
        ends = [t for t, p in v.probs if p >= v.threshold and s < t - F and t <= e][-int(max_s / F):]
        if np is None or not ends:
            return None
        x = v.read(ends[0] - F, ends[-1])
        if x is None:
            return None
        base = ends[0] - F
        return np.concatenate([x[int(round((t - F - base) * 16000)):int(round((t - base) * 16000))] for t in ends])

    def _gate(self, seg: dict, text: str, v) -> bool:
        """每段转写在 drop（学生 / 访客的字对一遍）和认声音之前先过三道 → True = 丢掉：
        ① 静音时说的：不收，日志里也不记字；
        ② 主持人正用另一台设备以访客身份参会（PLAYBACK.host_elsewhere）：家里这台麦克风录到的不算主持人的话；
           除非这段声纹很像你、当时又几乎没在放音——那是你回到电脑前了，恢复；
        ③ 和 Bloom 同一时间放出去的声音（访客的、学生的）花纹对得上：扬声器漏进麦克风的回声。"""
        try:
            s = float(seg.get("start") or 0.0)
            e = float(seg.get("end") or s)
        except (TypeError, ValueError):
            return False
        if MIC.overlap(v.wall(s), v.wall(e)) >= 0.5:
            log.info("静音时说的，不收（%d 字）", len(text))
            return True
        r = None
        away = PLAYBACK.host_elsewhere
        if self.echo_gate or away:
            try:
                r = v.echo_at(s - 0.1, e + 0.1)
            except Exception:
                log.exception("回声比对出错")
        if away:
            sim = None
            if r is None or r.get("cover", 0.0) < 0.3:
                chk = self.voice
                x = self._raw_voiced(v, s, e)
                if chk is not None and chk.ok and x is not None and len(x) >= 16000 * VOICE_MIN_S:
                    sim = chk.sim(x)
            if sim is not None and sim >= HOST_AWAY_BACK_TH:
                PLAYBACK.host_elsewhere = None
                PLAYBACK.host_here_at = time.time()
                self._away_seen = None
                self._log("mic", text=self._L(f"这台电脑的麦克风又听到你本人了（声纹 {sim:.2f}，当时没在放音）：恢复成主持人的话",
                                              f"the mic here hears you again (voiceprint {sim:.2f}, nothing playing): your words count again"))
            else:
                if self._away_seen != away:
                    self._away_seen = away
                    s0 = PLAYBACK.host_elsewhere_sim
                    self._log("mic", text=self._L(
                        f"{away} 的声音和主持人的声纹对上了（{s0 or 0:.2f}）：主持人在用另一台设备参会，这台电脑麦克风录到的不再当成主持人的话",
                        f"{away}'s voice matches the host's voiceprint ({s0 or 0:.2f}): the host joined from another device, so this computer's mic no longer speaks for the host"))
                self._log("echo", text=text, guest=away, why=self._L(
                    f"主持人正用 {away} 的设备参会，这台电脑麦克风录到的不算主持人的话",
                    f"the host is in the meeting as {away} on another device; this computer's mic does not speak for the host"),
                    sim=None if r is None else r.get("sim"), cover=None if r is None else round(r.get("cover", 0.0), 2))
                return True
        if self.echo_gate and r is not None and r.get("echo"):
            if r["sim"] >= 0.6 and r.get("lag") is not None:
                v.lags.append(r["lag"])                  # 很有把握的回声：记下延迟，以后只在它附近找
            self._log("echo", text=text, why=self._L(
                f"和扬声器正在放的声音对上了（花纹相关 {r['sim']:.2f}，{r['cover']:.0%} 的时间在放音），是回声，丢掉",
                f"matches what the speakers were playing (pattern correlation {r['sim']:.2f}, playing {r['cover']:.0%} of the time): echo, dropped"),
                sim=round(r["sim"], 3), cover=round(r["cover"], 2), lag=r.get("lag"))
            return True
        return False

    def add(self, seg: dict):
        try:
            self.covered = max(self.covered, float(seg.get("end") or seg.get("start") or 0.0))
        except (TypeError, ValueError):
            pass
        t = clean_asr(fix_terms((seg.get("text") or "").strip()))
        if not t:
            return
        if self.buf and len(_core(t)) >= 6 and _core(t) == _core(self.buf[-1]):
            return                                       # 和上一段一字不差：解码打转
        v = self.vad
        if v is not None and v.knows:
            try:
                if self._gate(seg, t, v):
                    return
            except Exception:
                log.exception("回声 / 静音判断出错")
        if self.drop is not None and v is not None and v.knows:
            try:   # 录音时钟 → 墙上时间（v.wall：取最近 30 秒最稳的那个零点）
                ws = v.wall(float(seg.get("start") or 0.0))
                we = v.wall(float(seg.get("end") or seg.get("start") or 0.0))
                r = self.drop(ws, we, t)
                if r is True:
                    return
                if isinstance(r, str):                   # 回声和你的话混在一段里：只留你的那部分
                    if not _core(r):
                        return
                    t = r
            except Exception:
                log.exception("回声判断出错")
        if not self._is_you(seg, t):
            return                                       # 不是你的声音（旁边有人说话）：不收
        if self.t0 is None:
            try:
                self.t0 = float(seg.get("start") or 0.0)
            except (TypeError, ValueError):
                self.t0 = 0.0
        try:
            gap = float(seg.get("start") or 0.0) - float(self.last_end if self.last_end is not None else seg.get("start") or 0.0)
        except (TypeError, ValueError):
            gap = 0.0
        if self.buf and gap >= 0.5 and self.buf[-1][-1:] not in PUNCT_ANY and t[:1] not in PUNCT_ANY:
            self.buf.append("，")
        self.buf.append(t)
        self._p = None
        try:
            self.last_end = float(seg.get("end") or seg.get("start") or 0.0)
        except (TypeError, ValueError):
            pass
        self.last_wall = time.time()
        v = self.vad
        if v is not None and v.knows and self.last_end is not None:
            self.heard = self.last_wall - max(0.0, v.audio_now - self.last_end)
        if self.partial:
            try:
                self.partial({"text": "".join(self.buf), "t": fmt_t(self.t0), "n": self.n + 1})
            except Exception:
                log.exception("partial 回调抛异常")
        if sum(len(x) for x in self.buf) >= MAX_CHARS:
            self.flush()

    def preview(self, p: dict) -> None:
        """宿主每两三秒把"还在说、没断句"的那一截先解码一遍（live/preview.json）：逐字稿先显示它，
        你一停顿，准确的文字落地，就把预览换掉。学生出声时不显示（多半是音箱漏进来的）。"""
        if not self.partial or not p:
            return
        v = self.vad
        if v is not None and (v.muted() or v.mic_off()):
            return                                       # 学生在出声 / 你静音了 / 主持人在别的设备上：不显示
        try:
            s, e = float(p.get("start")), float(p.get("end"))
        except (TypeError, ValueError):
            return
        if e <= self.covered + 0.3 or s < self.covered - 1.0 or time.time() - float(p.get("wall") or 0) > 6:
            return                                       # 已经有准确文字了，或者是旧的
        t = clean_asr(fix_terms((p.get("text") or "").strip()))
        if not _core(t):
            return
        base = "".join(self.buf)
        sep = "，" if base and base[-1:] not in PUNCT_ANY else ""
        try:
            self.partial({"text": base + sep + t, "t": fmt_t(self.t0 if self.t0 is not None else s),
                          "n": self.n + 1, "preview": True})
        except Exception:
            log.exception("partial 回调抛异常")

    def _threshold(self) -> float:
        text = "".join(self.buf).strip()
        base = self.idle_flush
        # 转写落到这里时，宿主已经等过两秒多的静音才关块、又解码了一两秒：再多等基本是白等
        if text[-1:] in PUNCT_END:
            return min(base, 0.25)         # 这句说完了
        if len(text) < 8:
            return base + 0.7              # 几个字、没句号：多半还没说完
        if text[-1:] in "，,、":
            return base + 0.3              # 停在逗号上
        return base

    def _quiet_needed(self) -> float:
        """你要安静多久才算说完：问句短一点，句号中等，停在逗号上或没有标点（多半话没说完）要久一点。
        这段时间从你最后一个字的声音算起；文字落地时一般已经过去一秒左右了。"""
        text = "".join(self.buf).strip()
        core = re.sub(r"[\s，。！？、,.!?：:；;…]", "", text)
        last = text[-1:]
        try:   # 公开词表 + smart-turn 的打分（zuhui/turn.py）：打招呼/散会 0.5 秒，收尾 1.0，半截话 4.0，模型说没完 2.5，否则 1.2
            from . import turn
            return turn.need(text, self._p)
        except Exception:
            pass
        quick = quick_kind(text)
        if quick in ("greet", "bye"):
            return 0.5                                   # 打招呼、散会：说完就接
        if quick == "close":
            return 1.4                                   # "好的""没问题"：学生本来就不接话，多等等看你还说不说
        if quick == "think":
            return 2.5                                   # "那个""我想想"：你在想，后面还有话
        if last in "？?" or core[-1:] in "吗呢吧么":
            need = 0.6
        elif last in "。！!；;…":
            need = 1.3
        else:
            need = 1.8
        if len(core) < 6:
            need += 0.5
        return need

    def _is_you(self, seg: dict, text: str) -> bool:
        """这段转写的声音是不是你：有声的部分够 VOICE_MIN_S 秒、和你登记的声音余弦低于 VOICE_LINE_TH 才算不是你。
        判不了（没登记、模型没有、太短、读不到）一律算你。访客和学生的回声在这之前已经由 drop 分走了。"""
        chk, v = self.voice, self.vad
        if chk is None or not chk.ok or v is None or not v.knows:
            return True
        try:
            s, e = float(seg.get("start") or 0.0), float(seg.get("end") or seg.get("start") or 0.0)
        except (TypeError, ValueError):
            return True
        if len(v.voiced(s, e)) * v.FRAME / 16000.0 < VOICE_MIN_S:
            return True
        sim = chk.sim(v.voiced_audio(s, e, VOICE_MAX_S))
        if sim is None or sim >= VOICE_LINE_TH:
            return True
        log.info("不是你的声音（%.2f），不收：%r", sim, text)
        return False

    def _own_voice(self, v) -> float | None:
        """你这段话的字之后麦克风里又有人声（判停会以为你还在说）：这声音是不是你。
        是你、判不了 → None（照旧）；不是你 → 你自己最后出声的位置（这段话最后一个字附近），判停从那里算安静。"""
        chk = self.voice
        if chk is None or not chk.ok or self.last_end is None:
            return None
        a = self.last_end + 0.2
        secs = len(v.voiced(a, v.audio_now)) * v.FRAME / 16000.0
        if secs < VOICE_MIN_S:
            return None
        got = self._after
        if got is None or got[0] != self.last_end or (not got[2] and secs >= got[1] + 1.0):
            sim = chk.sim(v.voiced_audio(a, v.audio_now, VOICE_MAX_S))   # 新的一段，或不是你、又多听了一秒：再判
            got = self._after = (self.last_end, secs, sim is None or sim >= VOICE_AFTER_TH)
        if got[2]:
            return None                                   # 是你（判过一次就一直算你，直到下一段字落地）
        return v.voice_before(self.last_end + 0.4)

    def idle(self) -> bool:
        if not self.buf or self.last_wall is None:
            return False
        waited = time.time() - self.last_wall
        v = self.vad
        if v is not None and v.knows and self.last_end is not None:
            last = v.last_voice                                   # 你最后出声的位置
            if last > self.last_end + 0.4:
                own = self._own_voice(v)                          # 字后面那段人声不是你（旁边有人说话）：不算
                if own is not None:
                    last = own
            quiet = v.audio_now - max(last, 0.0)                  # 你已经安静了多久
            if last > self.last_end + 0.4:                        # 这块之后你又说话了：下一块还在路上
                if quiet < 1.0:
                    return waited >= 90.0                         # 你还在说：一直等，不设上限（90 秒只防杂音一直响）
                more = max(0.0, last - self.last_end)             # 路上那块大概多长
                return quiet >= min(4.0, 1.6 + 0.4 * more)        # 你停了：等它落地；文字一直不来（多半是杂音）就不等了
            if quiet >= 0.2 and (self._p is None or self._p_voice != last):
                self._score_turn(v, last)                 # 停顿 0.2 秒：让 smart-turn 听这一轮的音频打一次分
            need = self._quiet_needed()
            self._maybe_tentative(v, quiet, need, last)
            return quiet >= need and waited >= 0.05       # 你停够了，文字也到了：这才算说完
        return waited >= self._threshold()

    def _maybe_tentative(self, v, quiet: float, need: float, last: float | None = None) -> None:
        """抢跑：判停打分说多半完了（要等的不超过 1.2 秒）、你也已经安静了 0.35 秒，就先把这句交出去让模型想着；
        你又开口、或者来了新字，就作废。"""
        if not self.tentative:
            return
        last = v.last_voice if last is None else last
        text = "".join(self.buf).strip()
        draft = self._spec is not None and len(self._spec) > 2
        if self._spec and (last != self._spec[1] or (text != self._spec[0] and not draft)):
            self._spec = None                     # 初稿抢跑的：准确文字落地不作废（真说完时后端判对不对得上），你又开口才作废
            self._call_tentative(None)
        if self._spec is None and text and 0.35 <= quiet < need <= 1.2 and (self._p is None or self._p >= 0.5):
            self._spec = (text, last)
            self._call_tentative(text)

    def draft_tick(self) -> None:
        """初稿抢跑（每轮 tail 调一次）：你停了 DRAFT_QUIET 秒、录音里还有没转写的人声 → 快识别出初稿 → tentative。"""
        d, v = self.draft, self.vad
        if d is None or not self.tentative or v is None or not v.knows:
            return
        spec = self._spec
        if spec is not None and len(spec) > 2 and v.last_voice != spec[1]:
            self._spec = None                              # 出初稿以后你又开口了：作废
            self._call_tentative(None)
        job = self._draft_job
        last = v.last_voice
        quiet = v.audio_now - last
        if (job is not None and job.get("res") is not None and not job.get("used")
                and job.get("last") == last and quiet >= DRAFT_QUIET):
            job["used"] = True
            p = self._turn_p(v, job["s"])
            if p is None or p >= 0.5:                      # 判停打分说多半没说完：不交
                self._draft_done(job)
        if not d.ready:
            return
        if last < 0 or v.muted() or v.mic_off():
            return
        start = max(self.covered, last - 15.0, 0.0)        # 这之前的话已经有准确文字了
        if last - start < 0.3 or quiet < DRAFT_DECODE or quiet > 4.0:
            return
        key = (round(last, 3), round(start, 3))
        if job is not None and job["key"] == key:
            return
        if self._spec is not None and self._spec[1] == last:
            return                                         # 准确文字已经落地、已经抢跑过了
        job = {"key": key, "last": last, "res": None, "used": False, "s": start, "e": last, "t": time.time()}
        self._draft_job = job

        def work(job=job, pcm=self.session / "live" / "audio.pcm"):
            r = d.asr(pcm, max(0.0, job["s"] - 0.05), job["e"] + 0.25, timeout=2.0)
            job["res"] = r if r is not None else ("", 0)
        threading.Thread(target=work, name="ears-draft", daemon=True).start()

    def _draft_done(self, job: dict) -> None:
        text, ms = job["res"]
        v = self.vad
        if not text or v is None or v.last_voice != job["last"]:
            return                                         # 没出字，或者你又开口了
        if self.covered > job["s"] + 0.3:
            return                                         # 准确文字已经落了一部分：初稿和它重叠，不用了
        t = clean_asr(fix_terms(text))
        if not _core(t):
            return
        base = "".join(self.buf)
        sep = "，" if base and base[-1:] not in PUNCT_ANY else ""
        full = base + sep + t
        self._spec = (full, job["last"], "draft")
        log.info("初稿 %d ms（停顿后 %.2f 秒）：%s", ms, time.time() - job["t"] + DRAFT_DECODE, t)
        self._call_tentative({"text": full, "draft": True, "ms": ms})

    def _turn_p(self, v, start: float) -> float | None:
        """判停打分（smart-turn）：从这句开口前 0.5 秒到现在（最多 8 秒）。模型没有就 None。"""
        try:
            from .turn import model
            m = model()
            if m is None:
                return None
            a = min(start, self.t0) if self.t0 is not None else start
            pcm = v.read(max(a - 0.5, v.audio_now - 8.0), v.audio_now)
            if pcm is not None and len(pcm) > 1600:
                return m.p_done(pcm)
        except Exception:
            log.exception("smart-turn 打分出错")
        return None

    def _call_tentative(self, text):
        try:
            self.tentative(text)
        except Exception:
            log.exception("tentative 回调出错")

    def _score_turn(self, v, last: float | None = None) -> None:
        """smart-turn 听这一轮（从这条话第一段开口前 0.5 秒到现在，最多 8 秒）的音频，给"说完了"打分。约 20-40 毫秒。"""
        self._p_voice = v.last_voice if last is None else last
        try:
            from .turn import model
            m = model()
            if m is None or self.t0 is None:
                return
            pcm = v.read(max(float(self.t0) - 0.5, v.audio_now - 8.0), v.audio_now)
            if pcm is not None and len(pcm) > 1600:
                self._p = m.p_done(pcm)
        except Exception:
            log.exception("smart-turn 打分出错")

    def flush(self):
        text, t0 = "".join(self.buf).strip(), self.t0
        self._p = None
        self._spec = None
        self._after = None
        heard, landed = self.heard, self.last_wall
        started = None
        v = self.vad
        try:                                     # 这句第一段在录音里的起点 → 墙上时间
            if t0 is not None and v is not None and v.knows:
                started = v.pump_wall - (v.audio_now - float(t0))
            elif t0 is not None and heard and self.last_end is not None:
                started = heard - (float(self.last_end) - float(t0))
        except (TypeError, ValueError):
            started = None
        self.buf, self.t0, self.last_wall, self.last_end, self.heard = [], None, None, None, None
        if not text:
            return
        if len(text) < self.min_chars or FILLER.match(text):
            log.info("skip filler: %r", text)
            if self.partial:
                try:
                    self.partial(None)
                except Exception:
                    pass
            return
        text = text.strip("，,、 ")
        if text and text[-1:] not in PUNCT_END:
            text += "。"
        self.n += 1
        shot = None
        if self.screenshots:      # 先把话发出去，截图在旁边截（屏幕位置现在主要靠对比页上报，截图只是备查）
            shot = str(self.session / "meeting" / f"shot-{self.n:04d}.png")
            threading.Thread(target=screenshot, args=(self.session, self.n), name="ears-shot", daemon=True).start()
        self.emit(Utterance(n=self.n, text=text, t=fmt_t(t0), shot=shot, session=str(self.session),
                            heard=heard, landed=landed, sent=time.time(), started=started))


def _parse_lines(lines: list[bytes]):
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw.decode("utf-8"))
        except Exception:
            continue


def _transcript_end(session: Path) -> int:
    """实时转写文件现在多长（从末尾接着听时从这里读起）。"""
    try:
        return (session / "live" / "transcript.jsonl").stat().st_size
    except OSError:
        return 0


def _tail_session(session: Path, from_start: bool, still_running, c: _Collector, vad=None, start_pos: int | None = None) -> int:
    """tail <session>/live/transcript.jsonl 直到 still_running() 为假；返回本场发出的条数。
    start_pos：从末尾接着听时从哪儿读起（调用方在报"开会"之前量好：报完到这里开始读之间说的话不丢）。"""
    tp = session / "live" / "transcript.jsonl"
    pv = session / "live" / "preview.json"
    pos = 0 if from_start or not tp.exists() else (start_pos if start_pos is not None else tp.stat().st_size)
    partial = b""
    pv_seen = None
    c.vad = vad
    while True:
        if vad is not None:
            vad.pump()
        try:
            st = pv.stat()
            if (st.st_mtime_ns, st.st_size) != pv_seen:
                pv_seen = (st.st_mtime_ns, st.st_size)
                c.preview(json.loads(pv.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        if tp.exists():
            with open(tp, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            if chunk:
                pos += len(chunk)
                lines = (partial + chunk).split(b"\n")
                partial = lines.pop()  # 最后一截可能是半行
                for seg in _parse_lines(lines):
                    c.add(seg)
        if c.idle():
            c.flush()
        c.draft_tick()
        if not still_running():
            time.sleep(1.0)  # 让最后一段转写落盘
            if tp.exists():
                with open(tp, "rb") as f:
                    f.seek(pos)
                    rest = (partial + f.read()).split(b"\n")
                for seg in _parse_lines(rest):
                    c.add(seg)
            if c.buf:
                c.flush()
            return c.n
        time.sleep(POLL_S)


def _ears_cfg(cfg: dict | None) -> dict:
    if cfg is None:
        from .config import load
        cfg = load()
    e = dict(cfg.get("ears") or {})
    e.setdefault("idle_flush", 2.0)
    e.setdefault("min_chars", 2)
    e.setdefault("screenshots", True)
    return e


# ---------------------------------------------------------------- 耳朵

_STOP = object()


class Ears:
    """后台守护线程：等录屏开始 → 逐条分发 → 录屏结束后继续等下一场。

    on_event(kind, payload) 在单独的分发线程里调用（queue 解耦），再慢也不会卡住 tail 循环。
    """

    def __init__(self, cfg: dict, on_event, muted=None, drop=None, on_barge=None, pending=None):
        e = _ears_cfg(cfg)
        self.muted = muted            # 学生正在出声吗（出声时麦克风里的声音不算你在说话）
        self.drop = drop              # 回声过滤（见 _Collector.drop）
        self.on_barge = on_barge      # 你打断学生时调
        self.pending = pending        # 学生有话排着还没出声吗
        self.vad = None               # 当前这场录音的 _AudioVAD
        self.use_vad = e.get("vad", True) is not False
        self.idle_flush = float(e["idle_flush"])
        self.min_chars = int(e["min_chars"])
        self.screenshots = bool(e["screenshots"])
        self.on_event = on_event
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self.session: Path | None = None      # 当前正在听的录屏（没有则 None）
        self.en = str((cfg.get("meeting") or {}).get("lang") or "zh") == "en"
        # 回声比对（见 PlaybackRef）：config ears.echo_gate = false 整个关掉（退回只靠 app 的文字比对）；ears.echo_sim 改门槛
        self.echo_gate = e.get("echo_gate", True) is not False
        if e.get("echo_sim") is not None:
            global ECHO_SIM
            ECHO_SIM = float(e["echo_sim"])
        from .draft_asr import DraftASR
        self.draft = DraftASR(cfg)            # 快识别：出初稿让模型先想着（见 _Collector.draft_tick）

    # -- 对外 --
    def start(self) -> None:
        if self._tail_thread and self._tail_thread.is_alive():
            return
        self._stop.clear()
        self.draft.start()
        self._dispatch_thread = threading.Thread(target=self._dispatch, name="ears-dispatch", daemon=True)
        self._tail_thread = threading.Thread(target=self._run, name="ears-tail", daemon=True)
        self._dispatch_thread.start()
        self._tail_thread.start()

    def set_muted(self, muted=None) -> bool:
        """主持人麦克风静音（会议页 POST /api/mic/mute）：muted=None 只问现在的状态。→ 现在是否静音。
        静音时：这一路不转写、不显示正在说的半句、不算你在说话（学生不等你）、不打断学生；语音桥推给访客的这一路推静音。
        一直静到你再点一下；这场会散了自动解除（存在 ~/.zuhui/mic_mute.json，
        后端中途重启也还是静音）。"""
        if muted is None:
            return MIC.muted
        was = MIC.muted
        now = MIC.set(bool(muted))
        if now != was:
            self._emit("log", {"kind": "mic", "text": ("Host mic muted" if now else "Host mic unmuted") if self.en
                               else ("主持人麦克风已静音" if now else "主持人麦克风已打开")})
        return now

    def stop(self) -> None:
        self._stop.set()
        if self._tail_thread and self._tail_thread.is_alive():
            self._tail_thread.join(timeout=5.0)
        self._q.put(_STOP)
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout=5.0)
        self.draft.stop()

    def user_talking(self, window: float = 0.5) -> bool:
        """你此刻是不是正在说话（最后一个有声帧离现在不到 window 秒）。学生开口前先问这个。"""
        v = self.vad
        if v is None or not v.knows or v.last_voice < 0 or v.mic_off():
            return False
        age = (v.audio_now - v.last_voice) + (time.time() - v.pump_wall)
        return age < window

    # -- 内部 --
    def _emit(self, kind: str, payload):
        self._q.put((kind, payload))

    def _dispatch(self):
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            kind, payload = item
            try:
                self.on_event(kind, payload)
            except Exception:
                log.exception("on_event(%s) 抛异常", kind)

    def _run(self):
        bridge_start = time.time()
        self._emit("bridge_ready", None)
        seen: set[str] = set()
        while not self._stop.is_set():
            try:
                j = running_record_job()
            except Exception:
                log.exception("扫 jobs 失败")
                j = None
            if j and j["id"] not in seen:
                seen.add(j["id"])
                session = Path(j["session"])
                # 录屏在耳朵之前就开始了 → 从文件末尾接着听，不回放旧话
                from_start = _iso_to_epoch(j.get("started") or "") >= bridge_start - 5
                log.info("recording %s -> %s (from_start=%s)", j["id"], session, from_start)
                self.session = session
                # 从末尾接着听（后端重启后接上同一场）：先量好从哪儿读，再报"开会"。以前等载入完静音检测才量，
                # 这一两秒里说的话（界面上已经显示"接着开"了）被跳过
                start_pos = None if from_start else _transcript_end(session)
                self._emit("meeting_started", str(session))
                c = _Collector(session, self.idle_flush, self.min_chars, self.screenshots,
                               emit=lambda u: self._emit("utterance", u),
                               partial=lambda p: self._emit("partial", p), drop=self.drop)
                c.tentative = lambda t: self._emit("tentative", t)
                c.on_log = lambda kind, **kw: self._emit("log", dict(kw, kind=kind))
                c.en, c.echo_gate = self.en, self.echo_gate
                self.draft.start()                # 进程掉了就重新拉起（在跑就什么都不做）
                c.draft = self.draft
                job_file = j["_file"]
                if MIC.muted and MIC.session not in (None, str(session)):
                    self.set_muted(False)         # 上一场留下的静音（散会前后端崩了没清掉）：新的一场从不静音开始
                MIC.session = str(session)        # 开会前在大厅点的静音带进这一场
                try:
                    vad = _AudioVAD(session, muted=self.muted) if self.use_vad else None
                    if vad is not None:
                        vad.on_barge, vad.pending = self.on_barge, self.pending
                        vad.echo_gate = self.echo_gate
                    self.vad = vad
                    n = _tail_session(session, from_start,
                                      lambda: job_running(job_file) and not self._stop.is_set(), c, vad, start_pos=start_pos)
                except Exception:
                    log.exception("tail %s 失败", session)
                    n = c.n
                self.session = None
                log.info("meeting ended: %s said=%d", session, n)
                if MIC.muted:                     # 散会：静音解除，下一场从不静音开始
                    self.set_muted(False)
                MIC.session = None
                PLAYBACK.clear()                  # "主持人在别的设备上"也只管这一场
                self._emit("meeting_ended", str(session))
            self._stop.wait(JOB_POLL_S)


# ---------------------------------------------------------------- 回放

def _load_segments(session: Path) -> list[dict]:
    tp = session / "live" / "transcript.jsonl"
    segs: list[dict] = []
    if tp.exists():
        for raw in tp.read_text(encoding="utf-8-sig").splitlines():
            if raw.strip():
                try:
                    segs.append(json.loads(raw))
                except Exception:
                    continue
    else:
        d = json.loads((session / "transcript.json").read_text(encoding="utf-8-sig"))
        segs = d.get("segments", d if isinstance(d, list) else [])
    return segs


def replay(session, on_event, speed: float = 1.0, realtime: bool = True) -> None:
    """回放一段旧录屏的转写（阻塞；截图照常截当前屏幕）。

    realtime=True：按转写时间戳节奏推进（除以 speed），第一段立刻发，不等录屏开头的静默；
    realtime=False：尽快发完。合并/过滤参数取 config 的 ears 段。
    """
    session = Path(session)
    e = _ears_cfg(None)
    idle_flush = float(e["idle_flush"])
    speed = max(float(speed or 1.0), 0.01)
    segs = _load_segments(session)

    on_event("meeting_started", str(session))
    c = _Collector(session, idle_flush, int(e["min_chars"]), bool(e["screenshots"]),
                   emit=lambda u: on_event("utterance", u),
                   partial=lambda p: on_event("partial", p))
    base_wall = time.time()
    base_t = None
    prev_end = None

    def wait_until(t: float):
        if not realtime or base_t is None:
            return
        delay = base_wall + (t - base_t) / speed - time.time()
        if delay > 0:
            time.sleep(delay)

    for s in segs:
        start = float(s.get("start") or 0.0)
        end = float(s.get("end") or start)
        if base_t is None:
            base_t = end  # 时间轴以第一段落地为零点
        if prev_end is not None and c.buf and start - prev_end >= idle_flush:
            wait_until(prev_end + idle_flush)   # 停顿够了 → 这条说完了
            c.flush()
        wait_until(end)                         # 这一段转写落地的时刻
        c.add(s)
        prev_end = end if prev_end is None else max(prev_end, end)
    if c.buf:
        c.flush()
    on_event("meeting_ended", str(session))


def _main():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="[ears %(asctime)s] %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="耳朵：回放一段旧录屏，或常驻监听")
    ap.add_argument("--replay", metavar="SESSION")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--fast", action="store_true", help="回放时不按时间戳等")
    a = ap.parse_args()

    def show(kind, p):
        if kind == "utterance":
            print(f"[{p.t}] #{p.n} {p.text}  shot={p.shot}")
        elif kind == "partial":
            print(f"   … {(p or {}).get('text', '')}")
        else:
            print(f"<{kind}> {p}")

    if a.replay:
        replay(a.replay, show, speed=a.speed, realtime=not a.fast)
        return
    from .config import load
    ears = Ears(load(), show)
    ears.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ears.stop()


if __name__ == "__main__":
    _main()
