# -*- coding: utf-8 -*-
"""走用户 Claude 套餐（claude -p，不走 API、不另花钱）问一次、拿回文字或 JSON。给不急的活用：会后纪要、整理、
词表释义、挑讨论里相关的句子、模拟会的扮演者。（2026-11-12：DeepSeek 账户没钱了，
用户也说别走 API，用套餐。）不给任何工具、不读用户的设置和 CLAUDE.md、不存会话。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def ask(prompt: str, system: str = "", model: str = HAIKU, effort: str = "low", timeout: float = 300) -> str:
    """问一次，返回回答的文字。出错抛 RuntimeError（调用方自己兜底）。"""
    from .config import load
    from .hands import _resolve_claude, _strip_claude_env
    cfg = load()
    exe = _resolve_claude(str((cfg.get("hands") or {}).get("claude", "claude")))
    d = Path(tempfile.mkdtemp(prefix="bloom-ask-"))
    args = [exe, "-p", "--model", model, "--effort", effort, "--tools", "", "--output-format", "text",
            "--no-session-persistence", "--setting-sources", ""]
    if system:
        sp = d / "system.md"
        sp.write_text(system, encoding="utf-8")
        args += ["--system-prompt-file", str(sp)]
    flags = 0x08000000 if os.name == "nt" else 0          # 不弹黑窗口
    from . import provider
    args = provider.map_args(args)                        # 设置里填了别家的 API key：换成那家的型号
    try:
        r = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=_strip_claude_env(), cwd=str(d), timeout=timeout, creationflags=flags)
    finally:                                              # 用完就删（以前不删，攒了几千个空目录）
        shutil.rmtree(d, ignore_errors=True)
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(f"claude -p 没答出来（rc={r.returncode}）：{(r.stderr or out)[-200:]}")
    return out


def ask_json(prompt: str, system: str = "", model: str = HAIKU, effort: str = "low", timeout: float = 300):
    """问一次，从回答里取出 JSON（对象或数组）。"""
    txt = ask(prompt + "\n\n只输出 JSON，不要别的文字、不要代码块。", system=system, model=model, effort=effort, timeout=timeout)
    a = min([i for i in (txt.find("{"), txt.find("[")) if i >= 0] or [-1])
    if a < 0:
        raise ValueError(f"回答里没有 JSON：{txt[:120]}")
    b = max(txt.rfind("}"), txt.rfind("]"))
    return json.loads(txt[a:b + 1])
