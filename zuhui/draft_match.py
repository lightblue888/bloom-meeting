# -*- coding: utf-8 -*-
"""快识别的初稿和 Fun-ASR 的准确结果算不算"同一句话"：算的话，按初稿提前想好的回话可以直接用。

只放过不改意思的差别：标点、语气词、虚字和代词（了/的/吧/你/他…）、口吃重复、同音字（拼音一样）；
长句（16 字以上）再允许每 20 个字有一处单字出入（"下次去改 / 下去去改"这种近音听错，回话不会因此变）；
换掉一整个词（"普通"→"主动"，两个字以上）不管句子多长都不放。
否定字（不/没/别…）、英文或数字有出入、短句里改了实词（"普通"→"主动"）、整句太短，都不算，按准确结果重新想。
只用标准库；拼音由调用方传进来（后端没有 pypinyin，快识别进程里有）。"""
from __future__ import annotations

import difflib
import re

PUNCT = re.compile(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()…~～\-—]+")
FILL = re.compile(r"[嗯呃啊哦噢额唉诶欸哎]")
LIGHT = set("了的地得吧啊呢吗哦嘛呀么哈呵就也还是在着过个这那你我他她它们")
NEG = set("不没别非无未莫勿甭")           # 否定字：差一个就可能意思反过来
LONG = 16                                  # 归一化后这么长才允许零星的实词出入
PER_DIFF = 20                              # 每这么多字允许 1 个字不同
TERM = re.compile(r"[A-Za-z0-9]")
MIN_CHARS = 6          # 归一化后少于这么多字：不认（短句快识别错得多）
MIN_SIM = 0.85


def norm(s: str) -> str:
    return FILL.sub("", PUNCT.sub("", s or "")).lower()


def _stutter(seg: str, other: str, at: int) -> bool:
    """seg 是不是旁边字的重复（"你你""转转"）：多出来或少掉的只是口吃。"""
    if not seg:
        return True
    for ch in seg:
        if ch not in other[max(0, at - 2):at + len(seg) + 2]:
            return False
    return True


def same_meaning(draft: str, final: str, pinyin=None) -> tuple[bool, str]:
    """→ (算不算同一句, 理由)。pinyin(str) -> list[str]（不带声调）；没有就不认同音字。"""
    a, b = norm(draft), norm(final)
    if a == b:
        return True, "一字不差"
    if len(b) < MIN_CHARS:
        return False, "太短"
    sim = difflib.SequenceMatcher(None, a, b).ratio()
    if sim < MIN_SIM:
        return False, f"相似度 {sim:.2f}"
    budget = len(b) // PER_DIFF if len(b) >= LONG else 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op == "equal":
            continue
        da, db = a[i1:i2], b[j1:j2]
        if TERM.search(da + db):
            return False, f"英文/数字不同：{da!r}→{db!r}"
        if NEG & set(da + db):
            return False, f"否定字不同：{da!r}→{db!r}"
        if set(da + db) <= LIGHT:
            continue
        if op in ("insert", "delete") and (_stutter(db, a, i1) if op == "insert" else _stutter(da, b, j1)):
            continue
        if op == "replace" and pinyin is not None and len(da) == len(db):
            try:
                if pinyin(da) == pinyin(db):
                    continue
            except Exception:
                pass
        if max(len(da), len(db)) == 1 and budget > 0:     # 单字出入：用额度
            budget -= 1
            continue
        return False, f"实词不同：{da!r}→{db!r}"
    return True, f"只差虚字/重复/同音或零星一两个字（{sim:.2f}）"
