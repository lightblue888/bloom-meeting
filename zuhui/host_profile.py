# -*- coding: utf-8 -*-
"""个人资料（桌面端 设置 → 通用 → 个人资料）：你的昵称、头像，和会上那位 AI 的名字。

- 都存在 ~/.zuhui/profile.json {"name": 你的昵称, "assistant": AI 的名字}。config.load() 把它们叠在 config.json 上
  （meeting.me、assistant.name）：装在哪、重装、升级都不丢，也不用去改 config.json。AI 的名字没起就按对话模型叫
  （config.assistant_name）。
- 头像存成 ~/.zuhui/avatars/me.png（裁成正方形，最大 256×256，重新编码过）。会议页、访客页、侧栏都先找它；
  没有再找手放进去的 <名字>.jpg 这类，再没有就用 Bloom 的花。

出错抛 ValueError，参数是给界面翻译用的代码：empty / too_long / taken（和会上另一位重名）/ too_big / bad_image。"""
import base64
import binascii
import io
import json
import re
from pathlib import Path

from .config import STATE

PROFILE_FILE = STATE / "profile.json"
AVATARS = STATE / "avatars"
AVATAR_TYPES = (".jpg", ".jpeg", ".png", ".webp", ".svg")
NAME_MAX = 24
COLOR = "#60a5fa"                    # 没选过时：你蓝
AI_COLOR = "#e3906f"                 # AI 橙
# 设置里给的可选颜色（深浅两种主题上都看得清；访客的配色会避开你和 AI 选中的那两个）
PALETTE = ["#60a5fa", "#e3906f", "#4ade80", "#facc15", "#f472b6", "#22d3ee", "#c084fc", "#f87171", "#2dd4bf", "#a3e635"]
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_CACHE = {"mtime": None, "data": {}}
AVATAR_MAX_BYTES = 8_000_000
AVATAR_PX = 256


def _read() -> dict:
    """profile.json 的内容；按修改时间缓存（批注卡片每次刷新都要问颜色）。"""
    try:
        mt = PROFILE_FILE.stat().st_mtime_ns
    except OSError:
        return {}
    if _CACHE["mtime"] != mt:
        try:
            d = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        _CACHE["mtime"], _CACHE["data"] = mt, (d if isinstance(d, dict) else {})
    return dict(_CACHE["data"])


def colors() -> tuple[str, str]:
    """(你的颜色, AI 的颜色)：设置里选的，没选过是蓝、橙。"""
    d = _read()
    c, a = str(d.get("color") or ""), str(d.get("ai_color") or "")
    return (c if _COLOR_RE.match(c) else COLOR), (a if _COLOR_RE.match(a) else AI_COLOR)


def save_color(which: str, value) -> str:
    """which 是 color（你）或 ai_color（AI）；value 是 #rrggbb。不对抛 ValueError("color")。"""
    v = str(value or "").strip().lower()
    if which not in ("color", "ai_color") or not _COLOR_RE.match(v):
        raise ValueError("color")
    d = _read()
    d[which] = v
    _write(d)
    return v


def _saved(key: str) -> str | None:
    n = _read().get(key)
    return n.strip() if isinstance(n, str) and n.strip() else None


def saved_name() -> str | None:
    return _saved("name")


def saved_assistant() -> str | None:
    return _saved("assistant")


def apply(cfg: dict) -> None:
    """config.load() 用：设置里改过的昵称、AI 的名字盖过 config.json。"""
    n, a = saved_name(), saved_assistant()
    if n:
        cfg.setdefault("meeting", {})["me"] = n
    if a:
        cfg.setdefault("assistant", {})["name"] = a


def clean_name(raw, taken=()) -> str:
    """一行、去掉控制字符、空白并成一个；不能空、不能超过 NAME_MAX 个字、不能和会上另一位（taken）重名。"""
    s = " ".join("".join(ch if ch.isprintable() else " " for ch in str(raw or "")).split())
    if not s:
        raise ValueError("empty")
    if len(s) > NAME_MAX:
        raise ValueError("too_long")
    if s.lower() in {str(t).strip().lower() for t in taken if t}:
        raise ValueError("taken")
    return s


def _write(d: dict) -> None:
    tmp = PROFILE_FILE.with_name(PROFILE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROFILE_FILE)


def save_name(raw, taken=()) -> str:
    name = clean_name(raw, taken)
    d = _read()
    d["name"] = name
    _write(d)
    return name


def save_assistant(raw, taken=()) -> str | None:
    """AI 的名字；空的 = 不起名字，按对话模型叫（返回 None）。"""
    d = _read()
    if not str(raw or "").strip():
        d.pop("assistant", None)
        _write(d)
        return None
    name = clean_name(raw, taken)
    d["assistant"] = name
    _write(d)
    return name


def avatar_file() -> Path | None:
    """你在设置里选的头像（me.*）；没选过是 None。"""
    for ext in AVATAR_TYPES:
        f = AVATARS / f"me{ext}"
        if f.is_file():
            return f
    return None


def decode_image(data) -> bytes:
    """界面传来的图：data: 地址或者纯 base64。"""
    s = str(data or "")
    if s.startswith("data:"):
        s = s.split(",", 1)[-1]
    if not s:
        raise ValueError("bad_image")
    if len(s) > AVATAR_MAX_BYTES * 4 // 3 + 8:
        raise ValueError("too_big")
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("bad_image") from None


def save_avatar(data: bytes) -> Path:
    """存成 me.png：照片转正、居中裁成正方形、缩到 256 以内。原来的 me.* 换掉。"""
    if not data:
        raise ValueError("bad_image")
    if len(data) > AVATAR_MAX_BYTES:
        raise ValueError("too_big")
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        im = ImageOps.exif_transpose(im)
    except Exception:
        raise ValueError("bad_image") from None
    w, h = im.size
    s = min(w, h)
    if s < 1:
        raise ValueError("bad_image")
    im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    if s > AVATAR_PX:
        im = im.resize((AVATAR_PX, AVATAR_PX), Image.LANCZOS)
    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P", "PA") else "RGB")
    AVATARS.mkdir(parents=True, exist_ok=True)
    out = AVATARS / "me.png"
    tmp = AVATARS / "me.png.tmp"
    im.save(tmp, "PNG", optimize=True)
    for ext in AVATAR_TYPES:
        f = AVATARS / f"me{ext}"
        if f != out and f.is_file():
            f.unlink()
    tmp.replace(out)
    return out


def clear_avatar() -> None:
    """不用自己选的头像了（回到手放进去的那张，或者 Bloom 的花）。"""
    for ext in AVATAR_TYPES:
        (AVATARS / f"me{ext}").unlink(missing_ok=True)
