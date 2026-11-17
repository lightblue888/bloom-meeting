# -*- coding: utf-8 -*-
"""模型来源（桌面端 设置 → 通用 → 模型来源）。三种：
- api（默认要选的）：DeepSeek、Kimi、智谱 GLM、通义千问、Anthropic API，或任何"Anthropic 兼容接口"，填 API key。
  Bloom 直接调接口（llm_api.py），不需要装 Claude Code。
- claude：用这台电脑上 Claude Code 登录的账号（Claude 套餐等）。Bloom 起 Claude Code 干活（改稿时能用它的工具、能联网查）。
- unset：新装、还没选。界面上默认选 DeepSeek，主页面顶上提示去填；这时用不了模型。

存在 ~/.zuhui/provider.json：只在这台电脑上，不进 config.json、不写日志、不推给中继。界面上只显示密钥的最后四位。"""
import json
import re

from .config import STATE

PROVIDER_FILE = STATE / "provider.json"

# 各家官方 Claude Code 接入页上的地址和型号（2026-11 查的；型号常换，界面上可以改）
PRESETS = {
    # ai：没起名字时会上那位 AI 默认叫什么（也是叫它的名字）
    "deepseek": {"label": "DeepSeek", "ai": "DeepSeek", "base_url": "https://api.deepseek.com/anthropic",
                 "fast": "deepseek-flash", "main": "deepseek-flash[1m]"},
    "kimi": {"label": "Kimi", "ai": "Kimi", "base_url": "https://api.moonshot.cn/anthropic",
             "fast": "kimi-k3", "main": "kimi-k3[1m]"},
    "glm": {"label": "智谱 GLM", "ai": "GLM", "base_url": "https://open.bigmodel.cn/api/anthropic",
            "fast": "glm-5.3-flash", "main": "glm-5.3[1m]"},
    "qwen": {"label": "通义千问", "ai": "千问", "base_url": "https://dashscope.aliyuncs.com/apps/anthropic",
             "fast": "qwen3.6-flash", "main": "qwen3.7-max"},
    "anthropic": {"label": "Anthropic API", "ai": "Claude", "base_url": "https://api.anthropic.com",
                  "fast": "claude-haiku-5-5", "main": "claude-opus-5-5"},
    "custom": {"label": "", "ai": "", "base_url": "", "fast": "", "main": ""},
}
KINDS = ("claude",) + tuple(PRESETS)
_CACHE = {"mtime": None, "data": {"kind": "unset"}}
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\-\[\]]{1,80}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~\-/]*)?$")


def load() -> dict:
    """存下来的来源；没设过（或文件坏了）是 {"kind": "unset"}。按修改时间缓存（参会人列表每次刷新都要问 AI 叫什么）。"""
    try:
        mt = PROVIDER_FILE.stat().st_mtime_ns
    except OSError:
        return {"kind": "unset"}
    if _CACHE["mtime"] != mt:
        try:
            d = json.loads(PROVIDER_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = None
        _CACHE["mtime"], _CACHE["data"] = mt, (d if isinstance(d, dict) and d.get("kind") in KINDS else {"kind": "unset"})
    return dict(_CACHE["data"])


def active(d: dict | None = None) -> dict | None:
    """在用别家 / Anthropic 的 API key 时返回那份设置，否则 None（用 Claude Code 自己登录的账号）。"""
    d = load() if d is None else d
    if d.get("kind") in ("claude", "unset") or not str(d.get("key") or "").strip():
        return None
    if d.get("kind") != "anthropic" and not (d.get("base_url") and d.get("fast") and d.get("main")):
        return None
    return d


def mode(d: dict | None = None) -> str:
    """api（直接调接口）/ claude（起 Claude Code，用它登录的账号）/ none（还没设，或 API 的设置不全）。"""
    d = load() if d is None else d
    if d.get("kind") == "claude":
        return "claude"
    return "api" if active(d) else "none"


def label(d: dict | None = None) -> str:
    """这个来源给人看的名字（DeepSeek、Kimi……；自定义的按接口地址的域名）。"""
    d = active(d)
    if not d:
        return ""
    name = PRESETS.get(d["kind"], {}).get("label") or ""
    if not name:
        m = re.match(r"https?://([^/:]+)", str(d.get("base_url") or ""))
        name = m.group(1) if m else "API"
    return name


def ai_name(d: dict | None = None) -> str:
    """用 API key 时 AI 默认叫什么（DeepSeek、Kimi……）；自定义接口返回空串（按快速模型的名字认）。"""
    d = active(d)
    return PRESETS.get(d["kind"], {}).get("ai", "") if d else ""


def facts(d: dict | None = None) -> str:
    """给提示词补一句：这台电脑用的是哪家的 API（软件现状里写的 Claude 型号、套餐以这句为准）。不是 API 就是空串。"""
    d = active(d)
    if not d:
        return ""
    from .llm_api import model
    return (f"\n\n【模型来源】这台电脑用的是 {label(d)} 的 API（按用量计费，不是 Claude 套餐）：会上对话用 {model('fast', d)}，"
            f"改稿、查论文用 {model('main', d)}。改稿和查论文不联网。上面写到的 Claude Haiku / Opus / Sonnet、Claude 套餐、"
            f"联网查资料，一律以这句为准。")


def public(d: dict | None = None) -> dict:
    """给设置页看的：密钥只给最后四位。"""
    d = load() if d is None else d
    key = str(d.get("key") or "")
    return {"kind": d.get("kind", "unset"), "base_url": d.get("base_url") or "", "fast": d.get("fast") or "",
            "main": d.get("main") or "", "key_set": bool(key), "key_tail": key[-4:] if len(key) >= 8 else "",
            "active": active(d) is not None, "label": label(d),
            "presets": {k: {"label": v["label"], "base_url": v["base_url"], "fast": v["fast"], "main": v["main"]}
                        for k, v in PRESETS.items()}}


def clean(body: dict) -> dict:
    """设置页交上来的 → 要存的那份。不合格抛 ValueError（给界面翻译的代码：kind / base_url / model / key）。
    key 不传或传空 = 沿用已存的（界面上不回显密钥）。"""
    kind = str(body.get("kind") or "claude")
    if kind not in KINDS:
        raise ValueError("kind")
    if kind == "claude":
        return {"kind": "claude"}
    old = load()
    key = str(body.get("key") or "").strip() or (str(old.get("key") or "") if old.get("kind") == kind else "")
    if not key or len(key) > 400 or any(c.isspace() for c in key):
        raise ValueError("key")
    if kind == "anthropic":
        return {"kind": kind, "key": key}
    pre = PRESETS[kind]
    base = str(body.get("base_url") or pre["base_url"]).strip().rstrip("/")
    if not _URL_RE.match(base) or base.endswith("/v1"):           # 各家都说别带 /v1（claude 自己会加 /v1/messages）
        raise ValueError("base_url")
    fast = str(body.get("fast") or pre["fast"]).strip()
    main = str(body.get("main") or pre["main"]).strip()
    if not (_MODEL_RE.match(fast) and _MODEL_RE.match(main)):
        raise ValueError("model")
    return {"kind": kind, "base_url": base, "key": key, "fast": fast, "main": main}


def save(d: dict) -> None:
    tmp = PROVIDER_FILE.with_name(PROVIDER_FILE.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROVIDER_FILE)
