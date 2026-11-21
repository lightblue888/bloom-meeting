# -*- coding: utf-8 -*-
"""配置：仓库里的 config.json 覆盖默认值；密钥只从环境变量读，永不落盘到仓库。"""
import copy
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("ZUHUI_HOME") or (Path.home() / ".zuhui"))
STATE.mkdir(parents=True, exist_ok=True)
PROMPTS = ROOT / "prompts"


def local_path(key: str) -> str:
    """这台电脑上另装在别处的东西（GPT-SoVITS 整合包、IndexTTS2、克隆音色的参考录音）：~/.zuhui/paths.json，
    {"gsv_dir": ..., "indextts_dir": ..., "indextts_venv": ..., "voice_ref": ...}。没写就是空字符串：对应功能不开，
    代码里不写任何人电脑上的路径（开源版）。"""
    try:
        v = json.loads((STATE / "paths.json").read_text(encoding="utf-8")).get(key)
        return str(v).strip() if v else ""
    except (OSError, ValueError, AttributeError):
        return ""

DEFAULTS = {
    "paper": {
        "file": "paper/paper.md",          # 论文的本地 Markdown 镜像（相对仓库根）
        "title": "文档",
    },
    "fast": {                               # 快嘴：秒回的对话模型（套餐里的常驻 Claude Code 进程，见 cli_brain.py）
        "model": "claude-haiku-5-5",        # 快嘴只用套餐里的 Haiku（2026-11-12 起 Bloom 里去掉了 DeepSeek，不能换）
        "history_turns": 40,                # 软件记着的最近几来回：换进程时整段补给新进程（以前走接口每轮全发，才压到 14）
        # 下面三个 Bloom 自己不读了：只有 scratch_ab/sim 的扮演者（DeepSeek）还读
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "disable_thinking": True,           # DeepSeek Flash 默认先思考再回答，关掉才能秒回
    },
    "ears": {                               # 耳朵：口喷鸡录屏的实时转写
        "idle_flush": 0.5,                  # 老师停顿这么多秒 = 一条说完（句号后更短，几个字没说完更长，见 ears._Collector）
        "min_chars": 2,
        "screenshots": True,
    },
    "mouth": {                              # 嘴：克隆音色 + 接话缓存
        "tts_port": 47321,
        "indextts_venv": local_path("indextts_venv"),   # IndexTTS2 的 venv python（~/.zuhui/paths.json；空 = 不用）
        "edge_voice": "zh-CN-YunxiNeural",
        "rate": 1.15,                       # 播放语速（1.0 = 原速）
        "ack_delay": 0,                     # 0 = 不放随机接话（和问题对不上，也不进逐字稿）
        # 每回都一样的话：预先合成一次，之后直接放缓存（只放克隆音色的版本）
        "phrases": ["记下了，马上排队改。", "我在，你说。", "好，这一处我撤回来。", "好的，今天到这。",
                    "在的在的，你说。", "我看一下屏幕。", "好，我等你。", "我看一下。",
                    # 难的问题转给 Claude 查之前先说的那句（换着说；问论文的才提论文）
                    "我去论文里核对一下。", "稍等，我在论文里查一下。", "稍等，我查一下。", "这个我核实一下再说。",
                    "我想一想，马上回你。",
                    # 对方说"你仔细想一想"：交给后台深想前的那一句（app._think_deeper）
                    "好，这个我交给后台仔细想一想。"],
        # 回话的开头一句：模型可以从这几句里挑一句开头（也可以不用），预先合成好，模型一出字就能出声。
        # 和上一句同一个开头时不念（fast.py），免得"好的好的，"连着说
        "openers": ["好的好的，", "明白了，", "嗯，是的，", "不是的，", "这个嘛，", "我想想，", "在的在的，",
                    "了解，", "收到，", "好的，", "懂了，", "可以，", "没错，", "这样啊，", "行，", "对，",
                    "明白，", "没有，", "不是，", "嗯嗯，", "是的，"],
        # 英文组会的同一套（2026-11-13）：开头语给快嘴挑（提示词里列着），常用句是代码里写死的英文固定话；
        # 都预先合成一次放缓存，英文会第一声不用等合成
        "openers_en": ["Okay,", "Sure,", "Will do,", "Got it,", "I see,", "Understood,", "Yes,",
                       "Right,", "No,", "Not quite,", "Hmm,", "Let me think,", "Well,", "I'm here,"],
        "phrases_en": ["Got it, it's queued.", "I'm here, go ahead.", "Okay, that's it for today.", "Sure, I'll wait.",
                       "Let me look at your screen.", "Let me check the paper.", "One moment, I'll look in the paper.",
                       "One moment, let me check.", "Let me verify that first.", "Let me think, I'll be right back.",
                       "Okay, I'll stay quiet until you call me.", "Okay.",
                       "Okay, I'll have the background think this through."],
        # （"有道理，""确实，"去掉了：助手不附和。"好，""嗯，""哦，"一个字的合成出来听不清，不预先合成，留在第一句里一起念）
        # 四个字以上、不带"记下了"这类有含义的话：太短的合成不稳，有含义的会和快嘴接下来说的打架
        "backchannels": ["恩，好的。", "好的好的。", "恩，我听着。", "稍等，我看一下。",
                         "恩恩，明白。", "好，我看看。", "行，知道了。", "恩，收到了。"],
    },
    "share": {                              # 邀请链接：会议中继（自己服务器上的 LiveKit + relay/token_server.py，见 zuhui/share.py）
        "enabled": True,
        "base_url": "",                     # 中继的网址（如 https://example.com/）；空 = 不开邀请链接，只能一个人开会
    },
    "look": {                               # 要看屏幕才答得上的问题：交给 Claude 看截图（对话模型看不了图）
        "enabled": True,
        "timeout_s": 150,                   # model / effort 不写就和改稿的一样（Opus 5.5 · Extra）
    },
    "deep": {                               # 快嘴拿不准论文里有没有、或者它的话核对不上论文：交给 Claude 去论文里查了再答
        "enabled": True,                    # False = 不核对、不转（快嘴说什么就念什么）
        "timeout_s": 180,                   # 能联网以后查别的论文要打开原文：放宽到 3 分钟
        # model / effort 不写：和改稿的一样，就是界面上显示的那个（Opus 5.5 · Extra）。以前这里写死 medium，界面写 Extra、
        # 查资料和深想实际跑 Medium（2026-11-14 组会被发现）。Dev 模拟会把改稿的强度设成 medium（ZUHUI_HANDS_EFFORT），这里跟着走
        "web": True,                        # 问到别的论文、外部事实时联网查原文（WebSearch / WebFetch）；False = 只读论文
    },
    "addr": {                               # 多人会里接不接：本机训的分类模型（zuhui/addr.py；scratch_ab/sim/addr_train.py 训的）
        "enabled": False,
        "mode": "veto",                     # veto：照旧判断，只在模型很确定不该接（p < lo）时拦下（固定考卷测试集 92.1%→96.1%）；model：模型说了算（调参集上更差）
        "threshold": 0.5,
        "lo": 0.05,
        "hi": 0.98,                         # veto 下老规则判了不接、模型很确定该接（p > hi）就放行；调参集 +1 句、测试集不变（scratch_ab/sim/addr_promote.py）
        "device": "auto",                   # auto / cuda / cpu
    },
    "hands": {                              # 慢手：Claude Code 工人
        "workers": 1,
        "claude": "claude",
        "model": "claude-opus-5-5",
        "effort": "xhigh",                  # 思考强度：low / medium / high / xhigh / max（界面上显示为 Extra）
        "timeout_s": 420,
    },
}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return out


def load(path: Path | None = None) -> dict:
    """DEFAULTS ← config.json ← 换过的模型（~/.zuhui/models.json）← 测试用的环境变量（ZUHUI_FAST_MODEL 等）
    ← 设置里改的昵称（~/.zuhui/profile.json，盖过 meeting.me）。"""
    p = path or (ROOT / "config.json")
    cfg = copy.deepcopy(DEFAULTS)
    if p.exists():
        with open(p, encoding="utf-8-sig") as f:
            cfg = _merge(cfg, json.load(f))
    apply_saved_models(cfg)
    from .host_profile import apply as apply_profile
    apply_profile(cfg)
    from .voice_prefs import apply as apply_speech      # 引导 / 设置 → 语音 里选的声音、专有名词
    apply_speech(cfg)
    from .relay_cfg import apply as apply_relay         # 设置 → 通用 → 邀请 里粘的连接码：中继地址
    apply_relay(cfg)
    return cfg


# ---------------- 会上那位 AI 叫什么 ----------------
# 对话模型的 id → 没起名字时的叫法（认不出的叫 AI）
_MODEL_FAMILY = (("claude", "Claude"), ("gpt", "GPT"), ("o1", "GPT"), ("o3", "GPT"), ("o4", "GPT"), ("gemini", "Gemini"),
                 ("deepseek", "DeepSeek"), ("qwen", "Qwen"), ("kimi", "Kimi"), ("glm", "GLM"), ("llama", "Llama"),
                 ("meta-llama", "Llama"), ("gemma", "Gemma"), ("mistral", "Mistral"), ("phi", "Phi"))


def default_assistant_name(cfg: dict) -> str:
    """没在设置里起名字时：在用别家接口就按那家叫（DeepSeek、Kimi……，见 provider.py），否则按对话模型叫
    （claude-* 叫 Claude……），认不出就叫 AI。"""
    from . import provider
    if provider.load().get("kind") == "unset":     # 新装、还没选模型来源：不默认叫 Claude
        return "AI"
    p = provider.active()
    m = str((p or {}).get("fast") or (cfg.get("fast") or {}).get("model") or "").strip().lower().rsplit("/", 1)[-1]
    return provider.ai_name(p) or next((v for k, v in _MODEL_FAMILY if m.startswith(k)), "AI")


def assistant_name(cfg: dict | None = None) -> str:
    """会上那位 AI 的名字：设置里起的（~/.zuhui/profile.json 的 assistant），没起就按对话模型。
    参会人列表、逐字稿、访客页、纪要、提示词里的 @AI、叫它的名字都从这里取，别处不要写死。"""
    cfg = load() if cfg is None else cfg
    n = str((cfg.get("assistant") or {}).get("name") or "").strip()
    return n or default_assistant_name(cfg)


# ---------------- 模型切换（会议页卡片上点、开会时说一句都行） ----------------
# 每一类的可选项：(id, 界面上的名字)。默认是 Claude Haiku 5.5 + Claude Opus 5.5 + Extra。
MODEL_OPTIONS = {
    # 对话模型（快嘴）：走用户 Claude 套餐的常驻进程（zuhui/cli_brain.py）。2026-11-12 去掉了 DeepSeek Pro、Flash，只剩 Haiku；
    # 2026-11-20 用户："对其他用户来说 也是可以改的"——三档都给，Haiku 最快，Opus 最慢（换之前 app.set_model 先试一次）
    "dialog": [("claude-haiku-5-5", "Claude Haiku 5.5"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
               ("claude-opus-5-5", "Claude Opus 5.5")],
    "edit": [("claude-opus-5-5", "Claude Opus 5.5"), ("claude-opus-4-6[1m]", "Claude Opus 4.6")],
    "effort": [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("xhigh", "Extra"), ("max", "Max")],
}
MODEL_PARTS = {"dialog": ("fast", "model"), "edit": ("hands", "model"), "effort": ("hands", "effort")}
MODELS_FILE = STATE / "models.json"          # 换过的模型：{"dialog","edit","effort"}，后端重启后照旧
_ENV_OVERRIDE = {"dialog": "ZUHUI_FAST_MODEL", "edit": "ZUHUI_HANDS_MODEL", "effort": "ZUHUI_HANDS_EFFORT"}   # 测试用


# 型号 id 里的厂牌 → 界面上的写法（认不出的厂牌原样显示）
_BRANDS = {"claude": "Claude", "deepseek": "DeepSeek", "kimi": "Kimi", "moonshot": "Moonshot", "glm": "GLM", "qwen": "Qwen",
           "gpt": "GPT", "gemini": "Gemini", "doubao": "Doubao", "ernie": "ERNIE", "minimax": "MiniMax", "abab": "MiniMax",
           "yi": "Yi", "hunyuan": "Hunyuan", "step": "Step", "baichuan": "Baichuan", "mistral": "Mistral", "llama": "Llama",
           "gemma": "Gemma", "phi": "Phi", "mixtral": "Mixtral", "internlm": "InternLM"}


def pretty_model(m) -> str:
    """型号 id → 界面上好读的名字：claude-opus-5-5 → Claude Opus 5.5，deepseek-flash → DeepSeek Flash，
    glm-5.3-flash → GLM 5.3 Flash，qwen3.7-max → Qwen3.7 Max，kimi-k3 → Kimi K3。
    本机模型：Ollama 的 qwen3:8b → Qwen3 8B（:latest 不写），LM Studio 带组织名的 qwen/qwen3-8b → Qwen3 8B。
    认不出的（自定义接口的型号）原样。"""
    import re as _re
    raw = str(m or "").strip()
    low = _re.sub(r"\[1m\]$", "", raw.lower())
    low = low.rsplit("/", 1)[-1]                         # LM Studio、OpenRouter：组织名/型号
    tag = ""
    if ":" in low:                                       # Ollama：型号:标签（大小、量化）
        low, tag = low.split(":", 1)
        tag = "" if tag == "latest" else tag
    parts = [x for x in _re.split(r"[-_\s]+", low) if x]
    if len(parts) > 1 and parts[0] == "meta" and parts[1].startswith("llama"):
        parts = parts[1:]
    if not parts:
        return raw
    brand = next((b for b in _BRANDS if parts[0].startswith(b) and (parts[0] == b or _re.match(r"^[\d.]+$", parts[0][len(b):]))), None)
    if brand is None:
        return raw
    head = _BRANDS[brand] + parts[0][len(brand):]
    tparts = [x for x in _re.split(r"[-_\s]+", tag) if x]
    q = next((i for i, x in enumerate(tparts) if _re.match(r"^(?:i?q\d|fp\d|bf16|f16)", x)), None)
    rest = parts[1:] + (tparts[:q] if q is not None else tparts)    # 量化写法（q4_K_M、fp16）不显示
    if brand == "claude" and len(rest) >= 3 and rest[1].isdigit() and rest[2].isdigit():   # claude-opus-5-5
        return f"Claude {rest[0].capitalize()} {rest[1]}.{rest[2]}" + "".join(" " + x.capitalize() for x in rest[3:])

    def word(x: str) -> str:
        if _re.match(r"^\d+(?:\.\d+)?[bmk]$", x):         # 参数量：8b → 8B，0.6b → 0.6B
            return x.upper()
        if _re.match(r"^[a-z]\d[\w.]*$", x) and len(x) <= 4:
            return x.upper()
        return x if _re.match(r"^[\d.]+[a-z]?$", x) else x.capitalize()
    return " ".join([head] + [word(x) for x in rest])


def model_label(part: str, mid: str | None) -> str:
    """界面上的型号名。在用别家接口时，对话、改稿两档显示那家的型号（设置 → 模型来源），写成好读的样子。"""
    if part in ("dialog", "edit"):
        from . import provider
        if provider.mode() == "api":
            from .llm_api import model
            return pretty_model(model("fast" if part == "dialog" else "main"))
    return next((label for i, label in MODEL_OPTIONS.get(part, []) if i == mid), str(mid or ""))


def model_current(cfg: dict) -> dict:
    return {part: (cfg.get(sec) or {}).get(key) for part, (sec, key) in MODEL_PARTS.items()}


def normalize_model(part: str, value: str) -> str:
    """界面上点的 id、名字，或者口述的大小写不一的写法 → 选项里的 id；认不出就 ValueError。"""
    if part not in MODEL_OPTIONS:
        raise ValueError(f"不认识的模型类别：{part}")
    v = " ".join(str(value or "").split()).lower()
    squash = v.replace(" ", "")
    for i, label in MODEL_OPTIONS[part]:
        if v in (i.lower(), label.lower()) or squash in (i.lower().replace(" ", ""), label.lower().replace(" ", "")):
            return i
    raise ValueError(f"{part} 没有这个选项：{value}")


def apply_saved_models(cfg: dict) -> None:
    try:
        saved = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    for part, (sec, key) in MODEL_PARTS.items():
        if saved.get(part):
            try:
                cfg.setdefault(sec, {})[key] = normalize_model(part, saved[part])
            except ValueError:
                pass
        env = os.environ.get(_ENV_OVERRIDE[part])
        if env:          # 测试：直接用环境变量里的 id（可以是选项以外的，比如便宜的 claude-haiku-4-5）
            cfg.setdefault(sec, {})[key] = env
    fast = cfg.setdefault("fast", {})
    if not str(fast.get("model") or "").startswith("claude-"):   # config.json 里还写着 deepseek 的：对话模型只有 Haiku
        fast["model"] = MODEL_OPTIONS["dialog"][0][0]
    sync_efforts(cfg)


def sync_efforts(cfg: dict) -> None:
    """界面上显示的思考强度（改稿模型那个）就是后台真用的：查资料 / 深想（deep）、看截图（look）、写标注（annotate）
    都跟着改稿的走，不再各写各的（以前 deep 写死 medium、标注写死 medium，界面却显示 Extra）。
    Dev 模拟会用环境变量 ZUHUI_HANDS_EFFORT=medium 把改稿的强度设成 medium，这几样跟着变 medium（只为省时间）。"""
    hands = cfg.setdefault("hands", {})
    eff = hands.get("effort") or "xhigh"
    hands["annotate_effort"] = eff
    for sec in ("deep", "look"):
        cfg.setdefault(sec, {}).pop("effort", None)       # 不写：hands._ask 用改稿的（hcfg["effort"]，换强度时是同一个字典，马上跟着变）


def set_model(cfg: dict, part: str, value: str) -> str:
    """换一类模型：改 cfg（对话下一句、改稿下一条任务就用上）并记进 ~/.zuhui/models.json。返回规范后的 id。"""
    mid = normalize_model(part, value)
    sec, key = MODEL_PARTS[part]
    cfg.setdefault(sec, {})[key] = mid
    if part == "effort":
        sync_efforts(cfg)                           # 标注、查资料跟着换（界面上显示的就是真用的）
    try:
        saved = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        saved = saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        saved = {}
    saved[part] = mid
    saved["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = MODELS_FILE.with_name(MODELS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, MODELS_FILE)
    return mid


def user_env(name: str) -> str | None:
    """环境变量：当前进程里没有就去读用户级注册表（刚设的变量旧终端看不到）。"""
    v = os.environ.get(name)
    if v:
        return v
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None
    return None


def api_key(cfg: dict) -> str:
    """DeepSeek 的密钥：Bloom 自己不用了，只有 scratch_ab/sim 的扮演者还在用。"""
    env = cfg["fast"]["api_key_env"]
    v = user_env(env)
    if not v:
        raise SystemExit(f"缺少快嘴模型的密钥：请设置环境变量 {env}")
    return v


CURRENT_PAPER = STATE / "current_paper.json"     # 换过的论文记在这里：后端重启后还是这一篇
# 没选论文时 paper_path 给的路径：文件不存在，所在的文件夹是个空目录（后台进程的工作目录要有个真实的地方）。
# 没选论文就开会 = 没有论文的会（meta.mode="chat"）：只记逐字稿，AI 不接话
NO_PAPER = STATE / "no_paper" / "no-paper.md"


def current_paper() -> dict | None:
    try:
        d = json.loads(CURRENT_PAPER.read_text(encoding="utf-8"))
        return d if d.get("path") and Path(d["path"]).is_file() else None
    except (OSError, ValueError, AttributeError):
        return None


def paper_cleared() -> bool:
    """大厅里点过「移除」：current_paper.json 里记着 {"none": true}，不再退回 config 里那篇。"""
    try:
        return json.loads(CURRENT_PAPER.read_text(encoding="utf-8")).get("none") is True
    except (OSError, ValueError, AttributeError):
        return False


def clear_paper() -> None:
    """记下"现在没有论文"（POST /api/paper/clear）。重新选一篇时 current_paper.json 被换成那一篇。"""
    tmp = CURRENT_PAPER.with_name(CURRENT_PAPER.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"none": True, "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, CURRENT_PAPER)


def paper_path(cfg: dict) -> Path:
    """屏幕上这篇论文的文件。没选论文（移除了、新装的机器上 config 里那篇也不在）：NO_PAPER（不存在的文件）。"""
    cur = current_paper()
    if cur:
        return Path(cur["path"])
    if not paper_cleared():
        f = (cfg.get("paper") or {}).get("file")
        if f:
            p = Path(f)
            p = p if p.is_absolute() else ROOT / p
            if p.is_file():
                return p
    try:
        NO_PAPER.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return NO_PAPER


def is_no_paper(path) -> bool:
    """这个路径算不算"没有论文"：NO_PAPER 本身，或者文件不在。"""
    if not path:
        return True
    p = Path(str(path))
    try:
        if os.path.normcase(str(p.resolve())) == os.path.normcase(str(NO_PAPER.resolve())):
            return True
    except OSError:
        pass
    return not p.is_file()


def no_paper(cfg: dict) -> bool:
    """现在没选论文：现在开会就是没有论文的会。"""
    return is_no_paper(paper_path(cfg))


def paper_title(cfg: dict) -> str:
    cur = current_paper()
    if cur:
        return str(cur.get("name") or Path(cur["path"]).stem)
    return "" if no_paper(cfg) else str(cfg["paper"].get("title") or "文档")
