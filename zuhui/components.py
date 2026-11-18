# -*- coding: utf-8 -*-
"""基础部件在不在：一行安装命令（install.ps1）装好的那些东西，开会离不了。给大厅 / 引导页显示"准备好了没有"。

    status() -> {名字: {"ok": bool, "path": str | None, "detail": 一句话}}

只看文件、缓存过的 which，不联网。唯一的子进程是问 ffmpeg 一次有没有 gfxcapture（录窗口用的，没有它录出来是黑的），
按 ffmpeg 的路径和修改时间缓存。BASIC 是开会必需的，OPTIONAL 缺了只少一样功能（访客加入、联网的声音）。

另外给后端各处找工具用：
- which(name)：PATH 里找，找不到再找 Bloom / Blurt 自己放工具的地方（~/.blurt/bin，uv 安装器的 ~/.local/bin ……）。
  桌面端拉起后端时带的 PATH 常常是装 uv 之前的，光靠 PATH 找不到。extend_path() 把这些目录接到本进程 PATH 后面，
  后端起的子进程也就找得到。
- pandoc()：随后端装的 pypandoc-binary 带的那份优先（版本固定），没有再找系统的；PYPANDOC_PANDOC 指定的最优先。
- uv_run()：转写干活进程的 `uv run` 命令头（Blurt 那套 sherpa-onnx 环境）。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# 转写干活进程（guest_ears、retranscribe）用的包：和 Blurt 的 transcribe.py 同一版，装的时候已经缓存进 uv
SHERPA = "1.13.8"
UV_WITH = ["--with", f"sherpa-onnx=={SHERPA}", "--with", "numpy==2.3.4"]
# Blurt 的 asr_models.json 里 fun-asr-nano-onnx 的 install.dir / keep
ASR_DIR = "fun-asr-nano-int8"
ASR_KEEP = ("encoder_adaptor.int8.onnx", "llm.int8.onnx", "embedding.int8.onnx", "Qwen3-0.6B")
SMART_TURN = "smart-turn-v3.2-cpu.onnx"

BASIC = ("asr_model", "vad", "asr_runtime", "smart_turn", "uv", "ffmpeg", "ffplay", "pandoc")
OPTIONAL = ("relay", "voice", "sapi")

WHICH_TTL = 30.0                # 找不到的也记着，这么久以后再找（装好了不用重启后端）
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_which: dict[str, tuple[float, str | None]] = {}
_gfx: dict[tuple[str, float], bool | None] = {}
_gfx_lock = threading.Lock()
_sapi: list[str] | None = None


def blurt_home() -> Path:
    return Path(os.environ.get("BLURT_HOME") or Path.home() / ".blurt")


def bin_dirs() -> list[Path]:
    """PATH 之外还要找的目录，按顺序：~/.blurt/bin（桌面端放 uv / ffmpeg、install.ps1 放 ffmpeg 的地方）、
    ~/.local/bin（uv 官方安装器）、~/.cargo/bin、winget 的 Links。"""
    home = Path.home()
    out = [blurt_home() / "bin", home / ".local" / "bin", home / ".cargo" / "bin"]
    la = os.environ.get("LOCALAPPDATA")
    if la:
        out.append(Path(la) / "Microsoft" / "WinGet" / "Links")
    return out


def which(name: str, refresh: bool = False) -> str | None:
    """shutil.which，PATH 里没有再找 bin_dirs()。结果缓存 WHICH_TTL 秒（缓存的路径没了就重找）。"""
    now = time.monotonic()
    hit = _which.get(name)
    if hit and not refresh and now - hit[0] < WHICH_TTL and (hit[1] is None or os.path.exists(hit[1])):
        return hit[1]
    found = shutil.which(name)
    if not found:
        extra = os.pathsep.join(str(d) for d in bin_dirs() if d.is_dir())
        found = shutil.which(name, path=extra) if extra else None
    _which[name] = (now, found)
    return found


def extend_path() -> list[str]:
    """把 bin_dirs() 里存在、PATH 里还没有的目录接到本进程 PATH 后面（排在原有的后面，不抢）。返回加了哪些。"""
    cur = os.environ.get("PATH", "")
    have = {os.path.normcase(os.path.normpath(p)) for p in cur.split(os.pathsep) if p}
    add = [str(d) for d in bin_dirs() if d.is_dir() and os.path.normcase(os.path.normpath(str(d))) not in have]
    if add:
        os.environ["PATH"] = os.pathsep.join([p for p in (cur, *add) if p])
        _which.clear()
    return add


def bundled_pandoc() -> Path | None:
    """pypandoc-binary 带的 pandoc（site-packages/pypandoc/files/pandoc[.exe]）。只找文件，不 import pypandoc。"""
    try:
        spec = importlib.util.find_spec("pypandoc")
    except (ImportError, ValueError):
        return None
    if not spec or not spec.origin:
        return None
    exe = Path(spec.origin).parent / "files" / ("pandoc.exe" if os.name == "nt" else "pandoc")
    return exe if exe.is_file() else None


def pandoc() -> str | None:
    """转 Word 用的 pandoc：PYPANDOC_PANDOC 指定的 → 随后端装的 → 系统的（PATH、bin_dirs）。"""
    env = os.environ.get("PYPANDOC_PANDOC")
    if env and Path(env).is_file():
        return env
    b = bundled_pandoc()
    return str(b) if b else which("pandoc")


def uv_run(offline: bool = True) -> list[str]:
    """`uv run … python` 之前那截：在 uv 的缓存里现搭 sherpa-onnx 环境。offline：只用缓存（快，不碰网；
    缓存里没有就起不来，调用方再不带它重试一次）。"""
    return [which("uv") or "uv", "run", *(["--offline"] if offline else []), "--no-project", "--quiet", *UV_WITH]


# ---------------------------------------------------------------- 各部件
def _item(ok: bool, path, detail: str) -> dict:
    return {"ok": bool(ok), "path": str(path) if path else None, "detail": detail}


def _asr_model() -> dict:
    d = blurt_home() / "models" / ASR_DIR
    miss = [k for k in ASR_KEEP if not (d / k).exists()]
    if not miss:
        return _item(True, d, "Fun-ASR-Nano")
    return _item(False, None, "Fun-ASR-Nano 没下载" if len(miss) == len(ASR_KEEP) else "Fun-ASR-Nano 不完整")


def _file(path: Path, name: str) -> dict:
    ok = path.is_file() and path.stat().st_size > 0
    return _item(ok, path if ok else None, name if ok else f"{name} 没下载")


def uv_cache_dir() -> Path | None:
    d = os.environ.get("UV_CACHE_DIR")
    if d:
        return Path(d)
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA")
        return Path(la) / "uv" / "cache" if la else None
    x = os.environ.get("XDG_CACHE_HOME")
    return (Path(x) if x else Path.home() / ".cache") / "uv"


def _asr_runtime() -> dict:
    """实时转写的 Python 包（sherpa-onnx）在不在 uv 的缓存里：不在的话第一次开会要现下。
    uv 的 wheels-v*/pypi/<包>/<版本>-<标签>：Linux / macOS 上是目录（指向解开的包），Windows 上是个指针小文件；
    旁边的 .http / .msgpack 是下载元数据，.lock 是锁，都不算。"""
    c = uv_cache_dir()
    hits = []
    if c and c.is_dir():
        for w in c.glob("wheels-v*"):
            hits += [p for p in (w / "pypi" / "sherpa-onnx").glob(f"{SHERPA}-*")
                     if not p.name.endswith((".http", ".msgpack", ".lock"))]
    if hits:
        return _item(True, hits[0], f"sherpa-onnx {SHERPA}")
    return _item(False, None, f"sherpa-onnx {SHERPA} 没缓存，第一次开会要下载")


def _gfxcapture(ff: str) -> bool | None:
    """这个 ffmpeg 有没有 gfxcapture（Windows 录窗口，FFmpeg 8 起）。问一次（约 0.1 秒），按路径和修改时间缓存。"""
    try:
        key = (ff, os.path.getmtime(ff))
    except OSError:
        return None
    with _gfx_lock:
        if key in _gfx:
            return _gfx[key]
        try:
            r = subprocess.run([ff, "-hide_banner", "-h", "filter=gfxcapture"], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=10, creationflags=_NO_WINDOW)
            ok = b"Filter gfxcapture" in (r.stdout or b"") + (r.stderr or b"")
        except (OSError, subprocess.SubprocessError):
            ok = None                     # 起不来 / 卡住：也记着，别每次都等
        _gfx[key] = ok
        return ok


def _ffmpeg() -> dict:
    ff = which("ffmpeg")
    if not ff:
        return _item(False, None, "没有 ffmpeg，录不了会议窗口")
    if os.name != "nt":
        return _item(True, ff, "ffmpeg")
    g = _gfxcapture(ff)
    if g is False:
        return _item(False, ff, "ffmpeg 太旧（没有 gfxcapture），录出来是黑的")
    return _item(True, ff, "ffmpeg" if g else "ffmpeg（没查到 gfxcapture）")


def _ffplay() -> dict:
    p = which("ffplay")
    return _item(bool(p), p, "ffplay" if p else "没有 ffplay，回话只能用系统播放器放")


def _pandoc() -> dict:
    p = pandoc()
    if not p:
        return _item(False, None, "没有 pandoc，导入不了 Word")
    b = bundled_pandoc()
    return _item(True, p, "pandoc（随后端安装）" if b and str(b) == p else "pandoc")


def _spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _relay() -> dict:
    miss = [n for n, mod in (("livekit", "livekit.rtc"), ("sounddevice", "sounddevice")) if not _spec(mod)]
    if miss:
        return _item(False, None, "没装 " + "、".join(miss) + "，访客加入不了")
    return _item(True, None, "livekit")


def edge_tts() -> str | None:
    """edge-tts 命令：PATH 里的，或者和后端同一个 Python 环境里的（uv sync --extra voice 装在那儿）。"""
    p = which("edge-tts")
    if p:
        return p
    for c in (Path(sys.executable).parent / "edge-tts.exe", Path(sys.executable).parent / "edge-tts"):
        if c.is_file():
            return str(c)
    return None


def _voice() -> dict:
    p = edge_tts()
    return _item(bool(p), p, "edge-tts" if p else "没装 edge-tts，只能用系统声音")


def sapi_voices() -> list[str]:
    """Windows 自带的离线声音（SAPI，System.Speech 用的那些）。读注册表，结果缓存。"""
    global _sapi
    if _sapi is not None:
        return _sapi
    names: list[str] = []
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Speech\Voices\Tokens") as k:
                i = 0
                while True:
                    try:
                        names.append(winreg.EnumKey(k, i))
                    except OSError:
                        break
                    i += 1
        except OSError:
            pass
    _sapi = names
    return names


def _sapi_item() -> dict:
    if os.name != "nt":
        return _item(False, None, "只有 Windows 有")
    v = sapi_voices()
    return _item(bool(v), None, f"{len(v)} 个系统声音" if v else "系统里没有声音")


def status(refresh: bool = False) -> dict:
    """每个基础部件：{"ok", "path", "detail"}。refresh：which 不用缓存重新找（ffmpeg 的检查仍按文件缓存）。"""
    if refresh:
        _which.clear()
    models = blurt_home() / "models"
    uv = which("uv")
    return {
        "asr_model": _asr_model(),
        "vad": _file(models / "silero_vad.onnx", "Silero VAD"),
        "asr_runtime": _asr_runtime(),
        "smart_turn": _file(models / SMART_TURN, "smart-turn"),
        "uv": _item(bool(uv), uv, "uv" if uv else "没有 uv，转写起不来"),
        "ffmpeg": _ffmpeg(),
        "ffplay": _ffplay(),
        "pandoc": _pandoc(),
        "relay": _relay(),
        "voice": _voice(),
        "sapi": _sapi_item(),
    }


def missing(st: dict | None = None) -> list[str]:
    """BASIC 里没就绪的那些（空 = 开会需要的都在）。"""
    st = st if st is not None else status()
    return [k for k in BASIC if not (st.get(k) or {}).get("ok")]
