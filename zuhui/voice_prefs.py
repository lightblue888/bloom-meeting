# -*- coding: utf-8 -*-
"""AI 的声音和转写的专有名词（首次引导和 设置 → 语音 里改）。

存在 ~/.zuhui/speech.json：
  {"voice": "auto" | "clone" | "edge" | "sapi" | "off",
   "edge_voice": "zh-CN-YunxiNeural", "sapi_voice": "", "hotwords": ["人名", "术语", ...]}

- voice：auto = 有克隆音色用克隆音色，没有用微软在线语音，再不行用 Windows 自带的；edge / sapi 只用那一种；
  off = 不出声，只在逐字稿里写字。mouth.py 每句都按这里的来，改了马上生效。
- hotwords：转写容易听错的专有名词（人名、术语），一行一个。写进来就替换 config.json 里 meeting.hotwords 那份
  （lexicon.py 读它）。
- pron：AI 念词的读法 [{"w": 写法, "r": 读法}]（第 7 批）。AI 说话前按它换（mouth.speakable，所有声音引擎都过这一步），
  逐字稿上还是原来的写法。

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
HOT_MAX = 1000          # 自己的词表最多这么多；每场会转写一次只能带 24 个提示词，lexicon.merged_hotwords 按论文挑
HOT_LEN = 40
PRON_MAX = 200          # 读法最多这么多条
PRON_LEN = 40           # 每条的写法、读法各最多这么多字
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


def pron() -> list[dict]:
    """设置里填的读法 [{"w": 写法, "r": 读法}]；没填过是空的。"""
    v = read().get("pron")
    if not isinstance(v, list):
        return []
    return [{"w": str(x["w"]), "r": str(x["r"])} for x in v
            if isinstance(x, dict) and str(x.get("w") or "").strip() and str(x.get("r") or "").strip()][:PRON_MAX]


# 一行一条：写法和读法之间用 = 、→ 、-> 、Tab 或逗号隔开（csv 导入的"写法,读法"也认）；取第一个分隔符
_PRON_SEP = re.compile(r"^(.*?)\s*(?:=|→|->|\t|,|，)\s*(.*)$")


def _clean_pron(raw) -> list[dict]:
    """界面送来的读法（[{w, r}] / [[w, r]] / 一行一条的文字）→ 存的样子。同一个写法写了两次：留后写的那个读法。"""
    if isinstance(raw, str):
        items = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            m = _PRON_SEP.match(line.strip())
            if not m:
                raise ValueError("pron_bad")
            items.append((m.group(1), m.group(2)))
    elif isinstance(raw, list):
        items = []
        for x in raw:
            if isinstance(x, dict):
                items.append((x.get("w"), x.get("r")))
            elif isinstance(x, (list, tuple)) and len(x) == 2:
                items.append((x[0], x[1]))
            else:
                raise ValueError("pron_bad")
    else:
        raise ValueError("pron_bad")
    out: dict[str, str] = {}
    for w, r in items:
        w, r = re.sub(r"\s+", " ", str(w or "")).strip(), re.sub(r"\s+", " ", str(r or "")).strip()
        if not w and not r:
            continue
        if not w or not r:
            raise ValueError("pron_bad")
        if len(w) > PRON_LEN or len(r) > PRON_LEN:
            raise ValueError("pron_too_long")
        out[w] = r
    if len(out) > PRON_MAX:
        raise ValueError("pron_too_many")
    return [{"w": w, "r": r} for w, r in out.items()]


_PRON_RX: dict = {"key": None, "rx": None, "to": []}


def _pron_rx(entries: list[dict]):
    """读法表 → 一个正则（长的写法先试，换过的字不再被别的条目换第二遍）。英文、数字开头或结尾的写法按整词找
    （"IT" 不会换掉 "ITEM" 里的）；大小写混着写的（LaTeX、PyTorch）不分大小写，全大写的缩写、中文照原样找。"""
    key = tuple((e["w"], e["r"]) for e in entries)
    if _PRON_RX["key"] == key:
        return _PRON_RX["rx"], _PRON_RX["to"]
    pats, to = [], []
    for w, r in sorted(key, key=lambda e: -len(e[0])):
        p = re.escape(w)
        if re.match(r"[A-Za-z0-9]", w):
            p = r"(?<![A-Za-z0-9])" + p
        if re.search(r"[A-Za-z0-9]$", w):
            p += r"(?![A-Za-z0-9])"
        if w != w.lower() and w != w.upper():
            p = "(?i:" + p + ")"
        pats.append("(" + p + ")")
        to.append(r)
    rx = re.compile("|".join(pats)) if pats else None
    _PRON_RX.update(key=key, rx=rx, to=to)
    return rx, to


def say_as(text: str) -> str:
    """念之前按设置里的读法换（mouth.speakable 调，所有声音引擎都过这里）；没填读法原样返回。"""
    t = text or ""
    entries = pron()
    if not entries or not t:
        return t
    rx, to = _pron_rx(entries)
    return rx.sub(lambda m: to[m.lastindex - 1], t) if rx else t


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
    """改其中几项（voice / edge_voice / sapi_voice / hotwords / pron）→ 存好的全部。"""
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
    if "pron" in patch:
        d["pron"] = _clean_pron(patch.get("pron"))
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
        "pron": pron(),
        "pron_max": PRON_MAX,
        "pron_len": PRON_LEN,
        "edge_voices": [{"id": i, "lang": lang, "gender": g} for i, lang, g in EDGE_VOICES],
        "sapi_voices": sapi_voices(wait=0),
        "has_clone": has_clone(),
        "has_edge": has_edge(),
    }
