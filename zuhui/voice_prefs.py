# -*- coding: utf-8 -*-
"""AI 的声音和转写的专有名词（首次引导和 设置 → 语音 里改）。

存在 ~/.zuhui/speech.json：
  {"voice": "auto" | "clone" | "edge" | "sapi" | "off",
   "edge_voice": "zh-CN-YunxiNeural", "sapi_voice": "", "hotwords": ["人名", "术语", ...]}

- voice：auto = 有克隆音色用克隆音色，没有用微软在线语音，再不行用 Windows 自带的；edge / sapi 只用那一种；
  off = 不出声，只在逐字稿里写字。mouth.py 每句都按这里的来，改了马上生效。
- hotwords：转写容易听错的专有名词（人名、术语），一行一个。写进来就替换 config.json 里 meeting.hotwords 那份
  （lexicon.py 读它）。

config.load() 调 apply() 叠上去，所以重装、升级都不丢，也不用改 config.json。出错抛 ValueError，参数是界面翻译用的代码。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time

from .config import STATE

SPEECH_FILE = STATE / "speech.json"
VOICES = ("auto", "clone", "edge", "sapi", "off")
# 微软在线语音（edge-tts）里挑几个常用的：中文男女声、英文男女声
EDGE_VOICES = [
    ("zh-CN-YunxiNeural", "zh", "m"),
    ("zh-CN-XiaoxiaoNeural", "zh", "f"),
    ("zh-CN-YunjianNeural", "zh", "m"),
    ("zh-CN-XiaoyiNeural", "zh", "f"),
    ("en-US-GuyNeural", "en", "m"),
    ("en-US-JennyNeural", "en", "f"),
    ("en-US-AriaNeural", "en", "f"),
]
HOT_MAX = 12            # lexicon 只取前 12 个（转写的热词表有长度上限）
HOT_LEN = 20
_CACHE = {"mtime": None, "data": {}}
_SAPI = {"at": 0.0, "list": None, "busy": False}
_SAPI_LOCK = threading.Lock()


def read() -> dict:
    try:
        mt = SPEECH_FILE.stat().st_mtime_ns
    except OSError:
        return {}
    if _CACHE["mtime"] != mt:
        try:
            d = json.loads(SPEECH_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        _CACHE["mtime"], _CACHE["data"] = mt, (d if isinstance(d, dict) else {})
    return dict(_CACHE["data"])


def voice() -> str:
    v = str(read().get("voice") or "auto")
    return v if v in VOICES else "auto"


def edge_voice() -> str | None:
    v = str(read().get("edge_voice") or "")
    return v if any(v == e[0] for e in EDGE_VOICES) else None


def sapi_voice() -> str | None:
    v = read().get("sapi_voice")
    return str(v) if isinstance(v, str) else None


def hotwords() -> list[str] | None:
    """引导或设置里填过的专有名词；没填过 None（用 config.json 里的）。"""
    v = read().get("hotwords")
    return [str(x) for x in v][:HOT_MAX] if isinstance(v, list) else None


def apply(cfg: dict) -> None:
    """叠到 config 上：mouth.edge_voice / sapi_voice / voice，meeting.hotwords。"""
    m = cfg.setdefault("mouth", {})
    m["voice"] = voice()
    if edge_voice():
        m["edge_voice"] = edge_voice()
    if sapi_voice() is not None:
        m["sapi_voice"] = sapi_voice()
    h = hotwords()
    if h is not None:
        cfg.setdefault("meeting", {})["hotwords"] = h


def _clean_hot(raw) -> list[str]:
    items = raw.splitlines() if isinstance(raw, str) else (raw if isinstance(raw, list) else None)
    if items is None:
        raise ValueError("hotwords")
    out: list[str] = []
    for x in items:
        w = re.sub(r"\s+", " ", str(x)).strip()
        if not w:
            continue
        if len(w) > HOT_LEN:
            raise ValueError("hot_too_long")
        if w not in out:
            out.append(w)
    if len(out) > HOT_MAX:
        raise ValueError("hot_too_many")
    return out


def save(patch: dict) -> dict:
    """改其中几项（voice / edge_voice / sapi_voice / hotwords）→ 存好的全部。"""
    d = read()
    if "voice" in patch:
        v = str(patch.get("voice") or "")
        if v not in VOICES:
            raise ValueError("voice")
        d["voice"] = v
    if "edge_voice" in patch:
        v = str(patch.get("edge_voice") or "")
        if not any(v == e[0] for e in EDGE_VOICES):
            raise ValueError("edge_voice")
        d["edge_voice"] = v
    if "sapi_voice" in patch:
        v = str(patch.get("sapi_voice") or "").strip()
        if v and v not in [x["name"] for x in (sapi_voices() or [])]:
            raise ValueError("sapi_voice")
        d["sapi_voice"] = v
    if "hotwords" in patch:
        d["hotwords"] = _clean_hot(patch.get("hotwords"))
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = SPEECH_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SPEECH_FILE)
    return read()


_PROBE = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo.Name + '|' + "
          "$_.VoiceInfo.Culture.Name + '|' + $_.VoiceInfo.Gender }; $s.Dispose()")


def _probe_sapi() -> None:
    out = None
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PROBE], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=20,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        out = []
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 2 and parts[0]:
                out.append({"name": parts[0], "lang": parts[1], "gender": (parts[2] if len(parts) > 2 else "").lower()})
    except Exception:
        out = None
    with _SAPI_LOCK:
        _SAPI.update(at=time.time(), list=out if out is not None else (_SAPI["list"] or []), busy=False)


def sapi_voices(wait: float = 8.0) -> list[dict] | None:
    """这台电脑装了的 Windows 声音 [{"name","lang","gender"}]；第一次要问系统（一两秒），之后缓存 10 分钟。
    wait 秒内没问完返回 None（界面先显示"读取中"，下次再问）。不是 Windows：[]。"""
    if sys.platform != "win32":
        return []
    with _SAPI_LOCK:
        fresh = _SAPI["list"] is not None and time.time() - _SAPI["at"] < 600
        if not fresh and not _SAPI["busy"]:
            _SAPI["busy"] = True
            th = threading.Thread(target=_probe_sapi, name="sapi-voices", daemon=True)
            th.start()
        else:
            th = None
    if th is not None and wait > 0:
        th.join(wait)
    return _SAPI["list"]


def has_clone() -> bool:
    """配过克隆音色（~/.zuhui/voice.json）。"""
    try:
        d = json.loads((STATE / "voice.json").read_text(encoding="utf-8"))
        return isinstance(d, dict) and bool(d)
    except (OSError, ValueError):
        return False


def has_edge() -> bool:
    import shutil
    from pathlib import Path
    return bool(shutil.which("edge-tts") or any(
        p.exists() for p in (Path(sys.executable).parent / "edge-tts.exe", Path(sys.executable).parent / "edge-tts")))


def public(cfg: dict | None = None) -> dict:
    """设置页和引导要的全部：现在选的、能选的。"""
    cfg = cfg or {}
    m = cfg.get("mouth") or {}
    h = hotwords()
    if h is None:
        hv = (cfg.get("meeting") or {}).get("hotwords") or []
        h = [str(x) for x in hv][:HOT_MAX] if isinstance(hv, list) else []
    return {
        "voice": voice(),
        "edge_voice": edge_voice() or str(m.get("edge_voice") or "zh-CN-YunxiNeural"),
        "sapi_voice": sapi_voice() if sapi_voice() is not None else str(m.get("sapi_voice") or ""),
        "hotwords": h,
        "hot_max": HOT_MAX,
        "hot_len": HOT_LEN,
        "edge_voices": [{"id": i, "lang": lang, "gender": g} for i, lang, g in EDGE_VOICES],
        "sapi_voices": sapi_voices(wait=0),
        "has_clone": has_clone(),
        "has_edge": has_edge(),
    }
