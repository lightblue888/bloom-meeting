# -*- coding: utf-8 -*-
"""软件现状（prompts/app_facts.md）：AI 被问到这个软件时照它答。

里面跟这台电脑有关的几句（用的什么模型、能不能换、AI 的声音、转写、能不能邀请别人）按现在的设置现填：
别人装的 Bloom 用的是他自己选的服务商和声音，不能照着开发者那台电脑说。模板里的 {{MODELS}} 这类占位符在这里换掉，
@AI 换成 AI 现在的名字。signature() 变了（设置里换了模型来源、声音、名字）就该重建提示词。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import PROMPTS, STATE

_FILES = ("provider.json", "speech.json", "voice.json", "profile.json", "livekit.env")


def signature() -> tuple:
    """会影响这几句的文件的修改时间：变了就重建。"""
    out = []
    for n in _FILES:
        try:
            out.append((STATE / n).stat().st_mtime_ns)
        except OSError:
            out.append(0)
    return tuple(out)


def _models() -> tuple[str, str, str]:
    """(用的什么模型, 能不能换、在哪换, 查资料联不联网)"""
    from . import provider
    mode = provider.mode()
    if mode == "api":
        from .llm_api import model
        name = provider.label()
        return (f"对话、改稿、查论文都走 {name} 的 API：会上对话用 {model('fast')}，改稿、查论文、散会后的纪要用 {model('main')}。",
                "模型在 设置 → 通用 → 模型来源 里换（服务商、API key、两个型号），开会时换不了。",
                "查资料不联网：问到别的论文、外部事实，只能凭论文和已知的说，拿不准就直说。")
    if mode == "claude":
        return ("对话、改稿、查论文都走主持人自己的 Claude Code 账号：对话用快的那个型号，改稿、查论文用强的那个。",
                "改稿模型和思考强度可以在 @AI 的参会人卡片上换，只有主持人和共同主持人能换，从下一条改稿任务开始用。",
                "查资料的那一路能联网：问到别的论文、外部事实时会上网找原文、带出处回答，原文打不开会直说。")
    return ("还没设置模型来源（设置 → 通用 → 模型来源）。", "模型在 设置 → 通用 → 模型来源 里设。", "")


def _voice() -> str:
    try:
        from . import voice_prefs
        from .onboard import voice_engine
        v = voice_prefs.voice()
        eng = voice_engine(v, voice_prefs.has_clone(), voice_prefs.has_edge())
    except Exception:
        eng = "sapi"
    return {
        None: "你不出声：你的回答只写进逐字稿，大家在屏幕上看。",
        "clone": "你的声音是克隆音色，在本机显卡上合成，只在你开口时用显卡。",
        "edge": "你的声音是微软的在线语音（要联网；连不上时换成 Windows 自带的语音）。",
        "sapi": "你的声音是 Windows 自带的语音，在本机合成。",
    }[eng]


def _asr() -> str:
    name = "Fun-ASR"
    try:
        home = Path(os.environ.get("BLURT_HOME") or Path.home() / ".blurt")
        bc = json.loads((home / "config.json").read_text(encoding="utf-8-sig"))
        n = str((((bc.get("record") or {}).get("dictation") or {}).get("asr_model")) or "")
        if n:
            name = {"fun-asr-nano-onnx": "Fun-ASR Nano"}.get(n, n)
    except (OSError, ValueError, AttributeError):
        pass
    return f"语音识别是 {name}，在本机 CPU 上跑；判断对方说没说完的小模型也在本机。"


def _invite() -> str:
    try:
        from .share import _env
        ok = bool(_env().get("LIVEKIT_API_KEY") and _env().get("LIVEKIT_API_SECRET"))
    except Exception:
        ok = False
    if ok:
        return "只有一条邀请链接，别人用浏览器打开就能进会，能听到主持人和你，你也能听到他们。"
    return "这台电脑还没配会议中继，邀请链接现在用不了，别人进不来；配好之后别人用浏览器打开邀请链接就能进会。"


def render(name: str | None = None) -> str:
    """填好的软件现状。name：AI 现在的名字（不给就按设置读）。"""
    try:
        text = (PROMPTS / "app_facts.md").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "（软件现状读不到）"
    models, switch, search = _models()
    for k, v in (("MODELS", models), ("SWITCH", switch), ("SEARCH", search), ("VOICE", _voice()), ("ASR", _asr()),
                 ("INVITE", _invite())):
        text = text.replace("{{" + k + "}}", v)
    if name is None:
        from .config import assistant_name
        name = assistant_name()
    return text.replace("@AI", name)
