# -*- coding: utf-8 -*-
"""模型来源（桌面端 设置 → 通用 → 模型来源）。

Bloom 所有用模型的地方（会上对话、改稿、查论文、散会后的整理和纪要）都是起本机的 Claude Code。
- 默认：Claude Code 自己登录的账号（Claude 套餐，或者 Anthropic Console 的 API key）。
- 也可以填别家"Anthropic 兼容接口"的地址和 API key：DeepSeek、Kimi、智谱 GLM、通义千问，或者任何兼容的接口。
  做法和各家官方的 Claude Code 接入说明一样：起 claude 时带上 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN，
  并把 Claude 的型号换成那家的（Haiku 那档 → 快速模型，Opus / Sonnet → 主力模型）。
- 只填 Anthropic 的 API key 也行（ANTHROPIC_API_KEY，型号照旧是 Claude 的），不用在 Claude Code 里登录。

存在 ~/.zuhui/provider.json：只在这台电脑上，不进 config.json、不写日志、不推给中继。界面上只显示密钥的最后四位。"""
import json
import re
from pathlib import Path

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
    "anthropic": {"label": "Anthropic API", "ai": "Claude", "base_url": "", "fast": "", "main": ""},
    "custom": {"label": "", "ai": "", "base_url": "", "fast": "", "main": ""},
}
KINDS = ("claude",) + tuple(PRESETS)
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\-\[\]]{1,80}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~\-/]*)?$")


def load() -> dict:
    """存下来的来源；没设过就是 {"kind": "claude"}。"""
    try:
        d = json.loads(PROVIDER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"kind": "claude"}
    if not isinstance(d, dict) or d.get("kind") not in KINDS:
        return {"kind": "claude"}
    return d


def active(d: dict | None = None) -> dict | None:
    """在用别家 / Anthropic 的 API key 时返回那份设置，否则 None（用 Claude Code 自己登录的账号）。"""
    d = load() if d is None else d
    if d.get("kind") == "claude" or not str(d.get("key") or "").strip():
        return None
    if d.get("kind") != "anthropic" and not (d.get("base_url") and d.get("fast") and d.get("main")):
        return None
    return d


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
    """给提示词补一句：这台电脑用的不是 Claude 套餐（软件现状里写的 Claude 型号、套餐以这句为准）。没在用别家接口就是空串。"""
    d = active(d)
    if not d or d["kind"] == "anthropic":
        return ""
    return (f"\n\n【模型来源】这台电脑没用 Claude 套餐，用的是 {label(d)} 的接口（按用量计费）：会上对话用 {d['fast']}，"
            f"改稿、查论文、看截图用 {d['main']}。上面写到的 Claude Haiku / Opus / Sonnet、Claude 套餐，一律以这句为准。")


def model(claude_id: str, d: dict | None = None) -> str:
    """起 claude 时 --model 的值：用别家接口时把 Claude 的型号换成那家的（Haiku 那档 → 快速模型，其余 → 主力模型）。"""
    d = active(d)
    if not d or d["kind"] == "anthropic":
        return claude_id
    return d["fast"] if "haiku" in str(claude_id).lower() else d["main"]


def map_args(args: list[str], d: dict | None = None) -> list[str]:
    """命令行参数里 --model 后面那个换成这个来源的型号（别的不动）。"""
    d = active(d)
    if not d:
        return args
    out = list(args)
    for i, a in enumerate(out[:-1]):
        if a == "--model":
            out[i + 1] = model(out[i + 1], d)
    return out


def apply_env(env: dict, d: dict | None = None) -> dict:
    """起 claude 的环境变量：用 API key 时带上地址、密钥和各档型号；用 Claude Code 自己的账号时不动。"""
    d = active(d)
    if not d:
        return env
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        env.pop(k, None)
    key = str(d["key"]).strip()
    if d["kind"] == "anthropic":
        env["ANTHROPIC_API_KEY"] = key
        return env
    env["ANTHROPIC_BASE_URL"] = str(d["base_url"]).rstrip("/")
    env["ANTHROPIC_AUTH_TOKEN"] = key
    env["ANTHROPIC_MODEL"] = d["main"]
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = d["main"]
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = d["main"]
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = d["fast"]
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = d["fast"]
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"      # 不往 Anthropic 发统计（这台机器可能根本连不上它）
    env.setdefault("API_TIMEOUT_MS", "600000")
    return env


def public(d: dict | None = None) -> dict:
    """给设置页看的：密钥只给最后四位。"""
    d = load() if d is None else d
    key = str(d.get("key") or "")
    return {"kind": d.get("kind", "claude"), "base_url": d.get("base_url") or "", "fast": d.get("fast") or "",
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
