# -*- coding: utf-8 -*-
"""论文相关的设置（设置 → 通用 → 论文）：~/.zuhui/paper_prefs.json {"docx_mode": "track" | "direct"}。

- docx_mode：从 Word 转来的论文，会上改完怎么交回（zuhui/docx_writeback.py）
    track   修订模式（默认）：另存的「（会上修订）.docx」里每处改动都是 Word 修订，能逐条接受、拒绝
    direct  直接改：改好的字直接写进去，不留修订痕迹
出错抛 ValueError("docx_mode")（界面按代码翻译）。"""
from __future__ import annotations

import json
import os
import time

from .config import STATE

PREFS_FILE = STATE / "paper_prefs.json"
DOCX_MODES = ("track", "direct")


def load() -> dict:
    try:
        d = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    m = str(d.get("docx_mode") or "")
    return {"docx_mode": m if m in DOCX_MODES else "track"}


def save(body: dict) -> dict:
    cur = load()
    if "docx_mode" in body:
        m = str(body.get("docx_mode") or "")
        if m not in DOCX_MODES:
            raise ValueError("docx_mode")
        cur["docx_mode"] = m
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = PREFS_FILE.with_name(PREFS_FILE.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dict(cur, at=time.strftime("%Y-%m-%dT%H:%M:%S")), ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, PREFS_FILE)
    return load()
