# -*- coding: utf-8 -*-
"""模型来源（桌面端 设置 → 通用 → 模型来源）。三种：
- api（默认要选的）：DeepSeek、Kimi、智谱 GLM、通义千问、Anthropic API，或任何"Anthropic 兼容接口"，填 API key；
  也可以是 OpenAI 格式的接口：自己填地址的"OpenAI 兼容接口"、本机的 Ollama 和 LM Studio（不要 key）。
  Bloom 直接调接口（llm_api.py），不需要装 Claude Code。
- claude：用这台电脑上 Claude Code 登录的账号（Claude 套餐等）。Bloom 起 Claude Code 干活（改稿时能用它的工具、能联网查）。
- unset：新装、还没选。界面上默认选 DeepSeek，主页面顶上提示去填；这时用不了模型。

存在 ~/.zuhui/provider.json：只在这台电脑上，不进 config.json、不写日志、不推给中继。界面上只显示密钥的最后四位。"""
import json
import re

from .config import STATE

PROVIDER_FILE = STATE / "provider.json"

# 各家官方 Anthropic 兼容接口的地址和型号（2026-11 查的；型号常换，界面上可以改）。型号写接口认的原名，
# 不带 Claude Code 自己的 "[1m]" 写法（设置页上直接显示这些）
PRESETS = {
    # ai：没起名字时会上那位 AI 默认叫什么（也是叫它的名字）
    "deepseek": {"label": "DeepSeek", "ai": "DeepSeek", "base_url": "https://api.deepseek.com/anthropic",
                 "fast": "deepseek-flash", "main": "deepseek-flash"},
    "kimi": {"label": "Kimi", "ai": "Kimi", "base_url": "https://api.moonshot.cn/anthropic",
             "fast": "kimi-k3", "main": "kimi-k3"},
    "glm": {"label": "智谱 GLM", "ai": "GLM", "base_url": "https://open.bigmodel.cn/api/anthropic",
            "fast": "glm-5.3-flash", "main": "glm-5.3"},
    "qwen": {"label": "通义千问", "ai": "千问", "base_url": "https://dashscope.aliyuncs.com/apps/anthropic",
             "fast": "qwen3.6-flash", "main": "qwen3.7-max"},
    "anthropic": {"label": "Anthropic API", "ai": "Claude", "base_url": "https://api.anthropic.com",
                  "fast": "claude-haiku-5-5", "main": "claude-opus-5-5"},
    "custom": {"label": "", "ai": "", "base_url": "", "fast": "", "main": ""},
    # OpenAI 格式（POST <地址>/chat/completions，地址一般以 /v1 结尾）：自己填地址的、本机的 Ollama 和 LM Studio。
    # 型号不预填：各人装的模型不一样，设置页从 <地址>/models 拉列表来选。key：本机两个不要，自己填的可要可不要
    "openai": {"label": "", "ai": "", "base_url": "", "fast": "", "main": "", "format": "openai", "key": "optional"},
    "ollama": {"label": "Ollama", "ai": "", "base_url": "http://127.0.0.1:11434/v1", "fast": "", "main": "",
               "format": "openai", "key": "none", "local": True},
    "lmstudio": {"label": "LM Studio", "ai": "", "base_url": "http://127.0.0.1:1234/v1", "fast": "", "main": "",
                 "format": "openai", "key": "none", "local": True},
}
KINDS = ("claude",) + tuple(PRESETS)
_CACHE = {"mtime": None, "data": {"kind": "unset"}}
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\-\[\]]{1,120}$")
MODEL_MAX = 120
_URL_RE = re.compile(r"^https?://(?:[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?(?:/[A-Za-z0-9._~\-/]*)?$")
# 自己填地址时按域名认是哪家（界面上的名字、估算费用时认价格表用的是 usage / prices.py 自己的 hosts）
_HOST_NAMES = {"api.openai.com": "OpenAI", "api.deepseek.com": "DeepSeek", "api.moonshot.cn": "Kimi", "api.moonshot.ai": "Kimi",
               "open.bigmodel.cn": "智谱 GLM", "dashscope.aliyuncs.com": "通义千问", "openrouter.ai": "OpenRouter",
               "api.siliconflow.cn": "SiliconFlow", "api.anthropic.com": "Anthropic"}


def api_format(d: dict | None) -> str:
    """这份来源的接口格式：anthropic（POST /v1/messages）或 openai（POST /chat/completions）。"""
    return PRESETS.get(str((d or {}).get("kind") or ""), {}).get("format", "anthropic")


def key_rule(kind: str) -> str:
    """required（要填 key）/ optional（可以不填）/ none（不用 key，本机的）。"""
    return PRESETS.get(kind, {}).get("key", "required")


def is_local(url: str) -> bool:
    """接口地址是不是这台电脑自己（localhost、127.x、::1）：本机的不走代理，也不算钱。"""
    m = re.match(r"^\w+://(\[[^\]]+\]|[^/:?#]+)", str(url or ""))
    host = (m.group(1) if m else "").strip("[]").lower()
    return host in ("localhost", "::1", "0.0.0.0") or host.startswith("127.") or host.endswith(".localhost")


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
    kind = d.get("kind")
    if kind in ("claude", "unset") or kind not in PRESETS:
        return None
    if key_rule(kind) == "required" and not str(d.get("key") or "").strip():
        return None
    if kind != "anthropic" and not (d.get("base_url") and d.get("fast") and d.get("main")):
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
        m = re.match(r"https?://(\[[^\]]+\]|[^/:]+)", str(d.get("base_url") or ""))
        host = (m.group(1) if m else "").lower()
        name = _HOST_NAMES.get(host) or next((v for h, v in _HOST_NAMES.items() if host.endswith("." + h)), "") or host or "API"
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
    from . import littools
    local = is_local(str(d.get("base_url") or ""))
    src = (f"本机跑的 {label(d)}（不花钱，不是 Claude 套餐）" if local
           else f" {label(d)} 的 API（按用量计费，不是 Claude 套餐）")
    net = ("查论文时能查文献（在 OpenAlex、arXiv 查论文，在 Crossref 核对引用，查到的是标题、作者、年份、出处、链接和摘要），"
           "打不开网页和论文全文，别的不联网" if littools.enabled() else "改稿和查论文不联网")
    return (f"\n\n【模型来源】这台电脑用的是{src}：会上对话用 {model('fast', d)}，"
            f"改稿、查论文用 {model('main', d)}。{net}。上面写到的 Claude Haiku / Opus / Sonnet、Claude 套餐、"
            f"联网查资料，一律以这句为准。")


def public(d: dict | None = None) -> dict:
    """给设置页看的：密钥只给最后四位。"""
    d = load() if d is None else d
    key = str(d.get("key") or "")
    from . import littools
    return {"kind": d.get("kind", "unset"), "base_url": d.get("base_url") or "", "fast": d.get("fast") or "",
            "main": d.get("main") or "", "key_set": bool(key), "key_tail": key[-4:] if len(key) >= 8 else "",
            "active": active(d) is not None, "label": label(d), "lit": littools.enabled(),
            "presets": {k: {"label": v["label"], "base_url": v["base_url"], "fast": v["fast"], "main": v["main"],
                            "format": v.get("format", "anthropic"), "key": key_rule(k), "local": bool(v.get("local"))}
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
    rule = key_rule(kind)
    key = "" if rule == "none" else (
        str(body.get("key") or "").strip() or (str(old.get("key") or "") if old.get("kind") == kind else ""))
    if (rule == "required" and not key) or len(key) > 400 or any(c.isspace() for c in key):
        raise ValueError("key")
    if kind == "anthropic":
        return {"kind": kind, "key": key}
    pre = PRESETS[kind]
    base = clean_base(kind, body.get("base_url") or pre["base_url"])
    fast = str(body.get("fast") or pre["fast"]).strip()
    main = str(body.get("main") or pre["main"]).strip()
    if not (_MODEL_RE.match(fast) and _MODEL_RE.match(main)):
        raise ValueError("model")
    out = {"kind": kind, "base_url": base, "key": key, "fast": fast, "main": main}
    if rule != "required" and not key:
        out.pop("key")
    return out


def clean_base(kind: str, url) -> str:
    """接口地址：去掉首尾空格和末尾的 /，不合格抛 ValueError("base_url")。
    Anthropic 格式的各家都说别带 /v1（会自己加 /v1/messages）；OpenAI 格式的照填（一般带 /v1），
    粘了整个 …/chat/completions 的去掉这一截。"""
    base = str(url or "").strip().rstrip("/")
    if PRESETS.get(kind, {}).get("format") == "openai":
        base = re.sub(r"/chat/completions$", "", base).rstrip("/")
        if not _URL_RE.match(base):
            raise ValueError("base_url")
        return base
    if not _URL_RE.match(base) or base.endswith("/v1"):
        raise ValueError("base_url")
    return base


def save(d: dict) -> None:
    tmp = PROVIDER_FILE.with_name(PROVIDER_FILE.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROVIDER_FILE)
