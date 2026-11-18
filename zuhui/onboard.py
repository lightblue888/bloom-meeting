# -*- coding: utf-8 -*-
"""首次引导走没走过（~/.zuhui/setup.json {"done", "skipped", "at"}），和开会前要看的准备情况。

桌面端第一次打开时显示引导（头像昵称 → 模型 → 转写 → 声音）；走完或跳过都记在这里，之后不再自己弹出，
设置里可以再走一遍。能不能开会不看这里：看模型来源设了没有、转写模型在不在（大厅里的"准备情况"）。"""
from __future__ import annotations

import json
import os
import time

from .config import STATE

SETUP_FILE = STATE / "setup.json"


def state() -> dict:
    try:
        d = json.loads(SETUP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    return {"done": bool(d.get("done")), "skipped": bool(d.get("skipped")), "at": d.get("at")}


def mark(done: bool | None = None, skipped: bool | None = None, reset: bool = False) -> dict:
    d = {} if reset else {k: v for k, v in state().items() if k in ("done", "skipped")}
    if done is not None:
        d["done"] = bool(done)
    if skipped is not None:
        d["skipped"] = bool(skipped)
    d["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = SETUP_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SETUP_FILE)
    return state()


def components() -> dict | None:
    """基础组件在不在（components.py：转写模型、pandoc、ffmpeg……）；没有这个模块的老版本 None。"""
    try:
        from . import components as _c
        return _c.status()
    except Exception:
        return None


def voice_engine(pref: str, has_clone: bool, has_edge: bool) -> str | None:
    """按选的声音和这台电脑有的，会上实际先用哪个：clone / edge / sapi；不出声 None。"""
    if pref == "off":
        return None
    if pref in ("auto", "clone") and has_clone:
        return "clone"
    if pref in ("auto", "clone", "edge") and has_edge:
        return "edge"
    return "sapi"


def tts_engine() -> str | None:
    """会上实际先用哪种声音（按设置 → 语音 选的 + 这台电脑有的）：clone / edge / sapi；不出声 None。"""
    try:
        from . import voice_prefs
        return voice_engine(voice_prefs.voice(), voice_prefs.has_clone(), voice_prefs.has_edge())
    except Exception:
        return "sapi"


def tts_label() -> str:
    """状态面板上语音合成那一行：克隆音色 = 引擎 · 声音名（~/.zuhui/voice.json，没起名字用参考音频的文件名）；
    微软在线语音 = Microsoft · 音色；Windows 自带 = Windows · 音色；不出声 = "—"。"""
    import re
    from pathlib import Path
    from . import voice_prefs
    eng = tts_engine()
    if eng is None:
        return "—"
    if eng == "edge":
        ev = voice_prefs.edge_voice() or "zh-CN-YunxiNeural"
        return "Microsoft · " + re.sub(r"Neural$", "", re.sub(r"^[a-z]{2}-[A-Z]{2}-", "", ev))
    if eng == "sapi":
        sv = (voice_prefs.sapi_voice() or "").replace("Microsoft ", "").replace(" Desktop", "").strip()
        return "Windows" + (f" · {sv}" if sv else "")
    try:
        v = json.loads((STATE / "voice.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        v = {}
    if str(v.get("engine") or "").lower() == "gptsovits":
        g = v.get("gsv") or {}
        name = g.get("name") or g.get("label") or (Path(str(g.get("ref"))).stem if g.get("ref") else "")
        return "GPT-SoVITS" + (f" · {name}" if name else "")
    name = v.get("name") or (Path(str(v.get("ref"))).stem if v.get("ref") else "")
    return "IndexTTS2" + (f" · {name}" if name else "")
