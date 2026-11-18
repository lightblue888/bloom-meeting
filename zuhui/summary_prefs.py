# -*- coding: utf-8 -*-
"""纪要格式（设置 → 通用 → 纪要）：~/.zuhui/summary_prefs.json {"template": ..., "custom": ...}。

- template：default 默认（按主题分组的"图文总结"）/ progress 进展汇报（每人进展、意见、下周任务）/
  paper 论文讨论 / proposal 开题、预答辩 / custom 自定义（custom 里写本组自己的要求，最多 CUSTOM_MAX 字）。
- 散会后的纪要按这里选的格式写；回顾里也能换一种格式重新生成（meetings.summarize 的 template 参数）。

出错抛 ValueError，参数是界面翻译用的代码：template（不认识的格式）、too_long（自定义要求超长）。"""
from __future__ import annotations

import json
import os
import re
import time

from .config import STATE

PREFS_FILE = STATE / "summary_prefs.json"
TEMPLATES = ("default", "progress", "paper", "proposal", "custom")
CUSTOM_MAX = 2000
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _read() -> dict:
    try:
        d = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def clean_custom(text) -> str:
    """自定义要求：统一换行、去掉控制字符和首尾空白。超过 CUSTOM_MAX 字抛 ValueError("too_long")（不悄悄截断）。"""
    s = _CTRL.sub("", str(text or "").replace("\r\n", "\n").replace("\r", "\n")).strip()
    if len(s) > CUSTOM_MAX:
        raise ValueError("too_long")
    return s


def load() -> dict:
    """{"template", "custom"}；没设过、文件坏了、写的格式不认识：默认格式、没有自定义要求。"""
    d = _read()
    t = str(d.get("template") or "")
    custom = str(d.get("custom") or "")
    return {"template": t if t in TEMPLATES else "default", "custom": custom[:CUSTOM_MAX]}


def save(body: dict) -> dict:
    """{"template"?, "custom"?}：给了哪个改哪个，返回存好的。"""
    cur = load()
    if "template" in body:
        t = str(body.get("template") or "")
        if t not in TEMPLATES:
            raise ValueError("template")
        cur["template"] = t
    if "custom" in body:
        cur["custom"] = clean_custom(body.get("custom"))
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = PREFS_FILE.with_name(PREFS_FILE.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dict(cur, at=time.strftime("%Y-%m-%dT%H:%M:%S")), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PREFS_FILE)
    return cur


def public() -> dict:
    return dict(load(), templates=list(TEMPLATES), custom_max=CUSTOM_MAX)
