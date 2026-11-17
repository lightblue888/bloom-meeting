# -*- coding: utf-8 -*-
"""论文词库：从当前论文里自动整理中英混杂的术语，换论文时重建。用在三处：

1. 语音识别热词（hotwords）：最容易被听错的专有名词、缩写，给 Fun-ASR 当提示（live.py 读 ~/.zuhui/lexicon/hotwords.json）；
2. 转写纠错（variants）：识别常写成的样子 → 论文里的写法（"O H L C"→OHLC、"trading view"→TradingView、
   "IBP bench"→FooBench），耳朵每收到一段就过一遍（ears.fix_terms）；
3. 一份给人看的术语表（docs/lexicon/<论文>-术语表.md）。

全是规则，不调模型：英文词按出现次数、是不是缩写 / 专有名词 / 驼峰词排序；中英对照从"中文（English）"里抽；
变体只生成不会误伤日常说法的那几类（拆开的字母、拆开的驼峰、去掉连字符、全小写的专有名词）。
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

from .config import ROOT, STATE

LEX_DIR = STATE / "lexicon"
CURRENT = LEX_DIR / "current.json"
HOTWORDS = LEX_DIR / "hotwords.json"
DOCS = ROOT / "docs" / "lexicon"
MAX_HOTWORDS = 24            # Fun-ASR-Nano：热词占提示的 token，太多会挤掉音频（transcribe.FunAsrOnnx.max_terms）

# 英文里的虚词、网址碎片、LaTeX 命令：不算术语
_STOP = set("""a an the of and or in on at to for from by with as is are was were be been being it its this that these those
which who whom what when where why how not no than then there here we our you your they their he she his her i me my
can could may might will would shall should do does did done has have had about into over under between after before
only also very more most less least such each other some any all both either neither first second two three one new
using use used based via per vs etc et al eg ie fig figure table section appendix
https http www com org net doi github io html pdf py en zh cn latex frac tag content version retrieved
begin end textbf textit mathrm mathbf cite ref label left right cdot ldots sum int log exp max min arg""".split())
_EN = re.compile(r"(?<![A-Za-z0-9_\\])([A-Za-z][A-Za-z0-9]*(?:[-–][A-Za-z0-9]+)*)(?![A-Za-z0-9_])")
_PHRASE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})(?![A-Za-z0-9_])")   # Flow Matching
_PAIR = re.compile(r"([一-鿿]{2,14})[（(]\s*([A-Za-z][A-Za-z0-9 .,\-–/]{0,40}?)\s*[)）]")
_LEAD = re.compile(r"^.*?[的和与及或按用由把在是为对将从向以]")      # "只记录这段时间的开盘价" → "开盘价"


def _kind(t: str) -> str:
    if re.fullmatch(r"[A-Z0-9][A-Z0-9\-–]+", t):
        return "acronym"                   # OHLC、GAN、AR-T、BTCUSDT
    if re.search(r"[a-z][A-Z]|[A-Z]{2,}[a-z]", t):
        return "camel"                     # MetaTrader、FooBench、TradingView
    if " " in t:
        return "phrase"                    # Flow Matching
    if t[:1].isupper():
        return "name"                      # Dukascopy、Binance、Sharpe
    return "word"                          # bar、tick、backtesting


def _variants(t: str) -> set[str]:
    """识别可能写成的样子（不含 t 本身）。只生成不会误伤日常说法的几类。"""
    out: set[str] = set()
    k = _kind(t)
    core = t.replace("–", "-")
    if k == "acronym":
        letters = re.sub(r"[-–]", "", core)
        if 3 <= len(letters) <= 6 and letters.isalpha():        # 两个字母的（OU、SE）拆开太容易误伤，不生成
            out.add(" ".join(letters))                       # O H L C
            out.add(" ".join(letters).lower())
        if "-" in core:
            out.add(core.replace("-", " "))                  # AR T
            out.add(" ".join(core.replace("-", "")))         # A R T
    if k == "camel":
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", t)
        if len(parts) >= 2:
            spaced = " ".join(parts)
            out |= {spaced, spaced.lower(), spaced.title()}  # Trading View / trading view / Foo Bench
        if len(t) >= 6:
            out.add(t.lower())                               # metatrader
    if k == "name" and len(t) >= 6:
        out.add(t.lower())                                   # dukascopy、binance
    if k == "phrase":
        out.add(t.lower())                                   # flow matching
    out.discard(t)
    return {v for v in out if v.strip() and v.lower() not in _STOP}


def build(paper: Path, name: str | None = None) -> dict:
    text = Path(paper).read_text(encoding="utf-8-sig", errors="replace")
    text_nourl = re.sub(r"https?://\S+|www\.\S+|doi:\S+", " ", text)
    counts: Counter = Counter()
    for m in _EN.finditer(text_nourl):
        t = m.group(1)
        if t.lower() in _STOP or len(t) < 2:
            continue
        counts[t] += 1
    for m in _PHRASE.finditer(text_nourl):
        ph = m.group(1)
        if not any(w.lower() in _STOP for w in ph.split()):
            counts[ph] += 1
    # 同一个词的大小写写法合并到最常见的那个（Backtesting / backtesting）
    by_low: dict[str, Counter] = {}
    for t, n in counts.items():
        by_low.setdefault(t.lower(), Counter())[t] += n
    merged = {c.most_common(1)[0][0]: sum(c.values()) for c in by_low.values()}

    pairs: dict[str, str] = {}
    for m in _PAIR.finditer(text):
        zh = _LEAD.sub("", m.group(1)) or m.group(1)
        en = m.group(2).strip(" ,.")
        if 2 <= len(zh) <= 8 and re.search(r"[A-Za-z]{2}", en):
            pairs.setdefault(en, zh)

    lower_seen = Counter(m.group(1) for m in _EN.finditer(text_nourl) if m.group(1).islower())
    # 参考文献里论文标题的大写词（Learning、Journal、Systems）不是论文自己的术语：普通名字只数正文里的
    cut = re.search(r"^#{1,4}\s*(参考文献|References|Bibliography)", text_nourl, re.M | re.I)
    body_names = Counter(m.group(1) for m in _EN.finditer(text_nourl[:cut.start()] if cut else text_nourl))
    phrases = {t: n for t, n in merged.items() if _kind(t) == "phrase"}
    terms = []
    for t, n in merged.items():
        k = _kind(t)
        if k == "word" and n < 3:
            continue                     # 只出现一两次的普通小写词不收
        if k in ("name", "phrase") and n < 2:
            continue
        if k == "name" and body_names.get(t, 0) < 2:
            continue                     # 正文里几乎没出现：多半是参考文献标题里的词
        if k == "name" and lower_seen.get(t.lower()):
            continue                     # 论文里也有小写的 data / backtesting：是普通英文词，只是在标题里大写了
        if k == "name" and any(t in ph.split() and pn >= 0.8 * n for ph, pn in phrases.items()):
            continue                     # Flow、Matching 只是 "Flow Matching" 的一半
        # 听错风险：缩写、驼峰、专有名词最容易被写错，排前面
        risk = {"acronym": 3.0, "camel": 3.0, "name": 2.0, "phrase": 1.5, "word": 1.0}[k]
        if k == "acronym" and len(re.sub(r"[-–]", "", t)) <= 2:
            risk = 1.2                   # OU、SE：太短，热词里往后排
        terms.append({"term": t, "kind": k, "count": n, "zh": pairs.get(t), "score": round(risk * (1 + n ** 0.5), 2)})
    terms.sort(key=lambda x: -x["score"])
    for en, zh in pairs.items():
        if not any(x["term"] == en for x in terms):
            terms.append({"term": en, "kind": _kind(en), "count": 1, "zh": zh, "score": 0.5})

    variants: dict[str, str] = {}
    for x in terms:
        if x["kind"] == "word" and x["count"] < 10:
            continue
        for v in _variants(x["term"]):
            variants.setdefault(v, x["term"])
    words = sorted((x for x in terms if x["kind"] == "word" and x["count"] >= 7), key=lambda x: -x["count"])[:2]
    hot = [x["term"] for x in terms if x["kind"] != "word"][:MAX_HOTWORDS - len(words)]
    hot += [x["term"] for x in words]                                  # bar、tick 这类论文里常说的英文小词
    return {"paper": name or Path(paper).stem, "path": str(paper), "built": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mtime": Path(paper).stat().st_mtime, "terms": terms, "pairs": pairs, "variants": variants,
            "hotwords": hot[:MAX_HOTWORDS]}


def enrich(lex: dict, cfg: dict) -> dict:
    """给前 60 个术语补中文释义：把术语和它在论文里第一次出现的那句话交给对话模型，一次调用。失败就原样返回。"""
    try:
        from openai import OpenAI
        import httpx
        from .config import api_key
        text = Path(lex["path"]).read_text(encoding="utf-8-sig", errors="replace")
        items = []
        for x in lex["terms"][:60]:
            i = text.find(x["term"])
            ctx = re.sub(r"\s+", " ", text[max(0, i - 40): i + 60]) if i >= 0 else ""
            items.append({"term": x["term"], "context": ctx})
        from .claude_text import ask_json                       # 走套餐里的 Haiku（不走 API）
        prompt = ("下面是一篇中文论文里的英文术语，每个附了它在论文里出现的上下文。给每个术语写一个简短的中文释义"
                  "（十五个字以内，按这篇论文里的意思；人名、会议名就写是谁/什么会议）。只输出 JSON 对象，键是术语原文，值是释义。\n"
                  + json.dumps(items, ensure_ascii=False))
        gloss = ask_json(prompt, timeout=180) or {}
        for x in lex["terms"]:
            g = gloss.get(x["term"])
            if isinstance(g, str) and g.strip() and not x.get("zh"):
                x["zh"] = g.strip()[:30]
        lex["glossed"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        lex["gloss_error"] = repr(e)[:200]
    return lex


def save(lex: dict) -> None:
    LEX_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(json.dumps(lex, ensure_ascii=False, indent=1), encoding="utf-8")
    # 实时转写的热词：软件名（Claude、DeepSeek、Opus、Bloom、你的名字……）和论文术语各一半（Fun-ASR-Nano 最多 24 个）
    HOTWORDS.write_text(json.dumps(merged_hotwords(lex["hotwords"]), ensure_ascii=False), encoding="utf-8")


def ensure(paper: Path, name: str | None = None, force: bool = False) -> dict:
    """当前论文的词库：没有、论文换了、论文改过了就重建。"""
    try:
        cur = json.loads(CURRENT.read_text(encoding="utf-8"))
        if not force and cur.get("path") == str(paper) and abs(cur.get("mtime", 0) - Path(paper).stat().st_mtime) < 1:
            return cur
    except (OSError, ValueError):
        pass
    lex = build(paper, name)
    try:
        from .config import load
        lex = enrich(lex, load())
    except Exception:
        pass
    save(lex)
    return lex


_cache = {"mtime": None, "rules": []}


def correct(text: str) -> str:
    """把识别常写成的样子换回论文里的写法（ears.fix_terms 调）。长的先换，按词边界换，不碰中文里的字。"""
    try:
        mt = CURRENT.stat().st_mtime_ns
    except OSError:
        return text
    if _cache["mtime"] != mt:
        try:
            lex = json.loads(CURRENT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return text
        rules = []
        for v, t in sorted((lex.get("variants") or {}).items(), key=lambda kv: -len(kv[0])):
            pat = re.escape(v).replace(r"\ ", r"\s+")
            rules.append((re.compile(rf"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])", re.I if " " in v else 0), t))
        _cache.update(mtime=mt, rules=rules)
    for rx, t in _cache["rules"]:
        text = rx.sub(t, text)
    return text


# ================================================================ 软件词表（不随论文变）
# 开会时常说的软件 / AI 名字：识别常写成的样子 → 正确写法。每条是 (写法, [识别常写成的样子（正则，英文不分大小写，按词边界）])。
# 顺序有讲究：长的在前（"cloud code desktop" 先于 "cloud code" 先于 "cloud"，"light blue" 先于 "blue"）。
# 来源：两场真会（2026-11-11 13:59 / 16:54）里听错的说法，加上常见的几种。中文音译只收不会和日常说法撞的。
ME_DEFAULT = ""                     # 主持人的名字：config.json 的 meeting.me（没写就不加这条）
SOFTWARE: list[tuple[str, list[str]]] = [
    ("Claude Code Desktop", [r"(?:claude|cloud|cold|clod|clawed)\s*(?:code|cold|coat|cod)\s*desktop"]),
    ("Claude Code", [r"(?:cloud|cold|clod|clawed|claud)\s*(?:code|coat|cod)", r"cold\s*cold", r"claude\s*code"]),
    ("Claude Docs", [r"claude\s*docs?"]),
    ("Claude Opus", [r"(?:claude|cloud)\s*(?:ops|openers?|offers?|opops|oppos|opos|opus)"]),
    ("Claude", [r"cloud", r"claud", r"clawd", r"coled", "克劳德", "克劳迪"]),
    ("DeepSeek", [r"deep\s*(?:seek|sea|see|sick|sik|sec|sic|sake)", r"dip\s*(?:sick|seek|sic|sik)", r"dib\s*ck",
                  "迪普斯克", "迪普西克", "迪普希克", "迪普seek", "迪普sick"]),
    ("Opus", [r"oppos", r"opops", r"opos", r"opus", "欧帕斯", "奥帕斯", "欧普斯"]),
    ("Sonnet", [r"sonnet", "索内特"]),
    ("Haiku", [r"haiku"]),
    ("Fable", [r"fable"]),
    ("@ME", []),                     # 你的名字（config.json meeting.me）；听错成什么写在 meeting.me_asr（正则列表）
    ("Bloom", [r"bloom", r"broom", r"blume", r"(?<!light )(?<!like )(?<!lite )blue", "布鲁姆"]),
    ("Blurt", [r"blurt"]),
    ("GitHub", [r"git\s*hub", "吉特哈布"]),
    ("LaTeX", [r"latex", r"la\s*tex"]),
    ("Markdown", [r"mark\s*down", "マップアウト", "マークダウン", "马克当"]),
    ("PyTorch", [r"py\s*torch", "派torch"]),
    ("Python", [r"python"]),
    ("Tauri", [r"tauri"]),
    ("LiveKit", [r"live\s*kit"]),
    ("MinerU", [r"miner\s*u", r"mineru"]),
    ("GPT-SoVITS", [r"gpt\s*[-杠]?\s*so\s*vits", r"gpt\s*sovits", r"GPT搜VITS"]),
    ("Fun-ASR-Nano", [r"fun\s*[-杠]?\s*asr\s*[-杠]?\s*nano"]),
    ("Fun-ASR", [r"fun\s*[-杠]?\s*asr"]),
    ("Paraformer", [r"para\s*former"]),
    ("Whisper", [r"whisper"]),
    ("Qwen", [r"qwen", r"q\s*wen"]),
    ("ChatGPT", [r"chat\s*gpt"]),
    ("OpenAI", [r"open\s*ai"]),
    ("Anthropic", [r"anthropic"]),
    ("Codex", [r"codex"]),
    ("VS Code", [r"vs\s*code", r"vscode"]),
    ("Overleaf", [r"over\s*leaf"]),
    ("Zotero", [r"zotero"]),
    ("NapCat", [r"nap\s*cat"]),
    ("ModelScope", [r"model\s*scope"]),
    ("Hugging Face", [r"hugging\s*face"]),
    ("CUDA", [r"cuda"]),
    ("Windows", [r"windows"]),
    ("macOS", [r"mac\s*os"]),
    ("Ctrl", [r"ctrl"]),
    ("Desktop", [r"(?<=Claude Code )desktop", r"DESKTOP"]),
    ("subagent", [r"sub\s*agent"]),
    ("对话模型", ["兑换模型"]),                                        # 16:54 那场："你的那个兑换模型"
    ("逐字稿", ["竹字稿", "逐色稿", "主字稿"]),
]
# 只在识别写成全大写时改成小写的英文词（"BUG"→bug、"SUBAGENT"→subagent）：大小写写对就不动
LOWER_WORDS = {"model", "bug", "bugs", "debug", "skill", "skills", "polish", "prompt", "token", "tokens", "bar", "star",
               "demo", "app", "link", "commit", "push", "pull", "issue", "release", "server", "client", "agent",
               "subagent", "plugin", "hook", "hooks", "effort", "extra", "max", "high", "low", "medium", "flash", "pro",
               "highlight", "hello", "ok", "bench", "loss", "batch", "epoch", "baseline", "benchmark", "paper", "dataset"}
# 写成全大写才对的缩写：不动
ACRONYMS = {"AI", "UI", "API", "CLI", "GPU", "CPU", "PDF", "TXT", "QQ", "LLM", "ASR", "TTS", "CER", "WER", "URL", "SSH",
            "VPS", "JSON", "HTML", "CSS", "IBP", "OHLC", "RTX", "PR", "MCP", "RAG", "OK", "PPT", "AEC", "VAD", "SDK", "IDE"}

_sw_cache: dict = {"key": None, "rules": []}


def _me_asr() -> list[str]:
    """主持人的名字常被听成什么（config.json 的 meeting.me_asr，正则；比如名字是 Ann Lee 就写 "ann\\s*lee"）。"""
    try:
        from .config import load
        v = (load().get("meeting") or {}).get("me_asr") or []
        return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []
    except Exception:
        return []


def _me() -> str:
    try:
        from .config import load
        return str((load().get("meeting") or {}).get("me") or ME_DEFAULT)
    except Exception:
        return ME_DEFAULT


def _rx(pat: str) -> re.Pattern:
    """英文的样子按词边界、不分大小写；中文的样子原样找。"""
    if re.search(r"[A-Za-z]", pat) and not re.search(r"[一-鿿぀-ヿ]", pat):
        return re.compile(rf"(?i)(?<![A-Za-z0-9_\-/]){pat}(?![A-Za-z0-9_])")
    return re.compile(pat)


def _software_rules() -> list[tuple[re.Pattern, str]]:
    try:
        lm = LEARNED.stat().st_mtime_ns
    except OSError:
        lm = None
    key = (lm, _me())
    if _sw_cache["key"] == key:
        return _sw_cache["rules"]
    me = key[1]
    rules: list[tuple[re.Pattern, str]] = []
    for r in learned_rules():                    # 学到的规则先换（只有证据够的才在里面，见 learn()）
        try:
            rules.append((re.compile(r["pattern"]), r["to"]))
        except (re.error, KeyError, TypeError):
            pass
    for to, pats in SOFTWARE:
        if to == "@ME":
            if not me:
                continue
            to, pats = me, _me_asr()
        for p in pats:
            try:
                rules.append((_rx(p), to))
            except re.error:
                pass
    _sw_cache.update(key=key, rules=rules)
    return rules


def _case(m: re.Match) -> str:
    w = m.group(0)
    if w in ACRONYMS or not w.isupper() or len(w) < 3:
        return w
    low = w.lower()
    return low if low in LOWER_WORDS else w


def software_correct(text: str) -> str:
    """软件 / AI 名字写对（normalize.live 调，在命令和型号整形之前）。"""
    if not text:
        return text
    for rx, to in _software_rules():
        text = rx.sub(to, text)
    return re.sub(r"(?<![A-Za-z])[A-Za-z]{3,}(?![A-Za-z])", _case, text)


# 热词：Fun-ASR-Nano 的提示最多 24 个（每个都占 512 个 token 里的位置），软件名和论文术语各占一半
SOFTWARE_HOT = ["Claude", "Claude Code", "DeepSeek", "Opus", "Bloom", "@ME", "Desktop", "GitHub", "LaTeX", "Markdown",
                "对话模型", "改稿模型", "逐字稿", "subagent", "Flash", "PyTorch", "Tauri", "LiveKit", "MinerU", "GPT-SoVITS"]


LEARNED_HOT_MAX = 6          # 散会后学到的热词最多占这么多个（排在软件词后、论文术语前）


def learned_hot(n: int = LEARNED_HOT_MAX, days: float = 30.0) -> list[str]:
    """散会后学到的热词：最近 days 天里出现过的，按次数排。"""
    try:
        hot = json.loads(LEARNED.read_text(encoding="utf-8")).get("hot") or []
    except (OSError, ValueError):
        return []
    now = time.time()
    hot = [h for h in hot if now - float(h.get("last_ts") or 0) < days * 86400]
    hot.sort(key=lambda h: (-int(h.get("count") or 0), -float(h.get("last_ts") or 0)))
    return [str(h["word"]) for h in hot[:n]]


def merged_hotwords(paper_hot: list[str] | None, n: int = MAX_HOTWORDS, software: int = 10) -> list[str]:
    me = _me()
    sw = [me if w == "@ME" else w for w in SOFTWARE_HOT]
    out: list[str] = []
    for w in sw[:software] + learned_hot() + list(paper_hot or []) + sw[software:]:
        if w and w not in out:
            out.append(w)
    return out[:n]


# ================================================================ 散会后学到的规则（可查、可撤销）
LEARNED = LEX_DIR / "learned.json"          # {"version":1,"rules":[{id,from,to,pattern,count,evidence,source,added,active}]}
LEARN_LOG = LEX_DIR / "learned.log.jsonl"   # 每次加 / 撤销 / 恢复一行，给人看的流水
MIN_EVIDENCE = 2                             # 同一种听错至少两次一致才学；一次的只有发音几乎一样、目标是词表里的词才学


def _load_learned() -> dict:
    try:
        d = json.loads(LEARNED.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("rules"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"version": 1, "rules": []}


def _save_learned(d: dict) -> None:
    LEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEARNED.with_name(LEARNED.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(LEARNED)


def _log(action: str, rule: dict, why: str = "") -> None:
    LEX_DIR.mkdir(parents=True, exist_ok=True)
    row = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "id": rule.get("id"), "from": rule.get("from"),
           "to": rule.get("to"), "count": rule.get("count")}
    if why:
        row["why"] = why
    with open(LEARN_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def learned_rules(include_inactive: bool = False) -> list[dict]:
    return [r for r in _load_learned()["rules"] if include_inactive or r.get("active", True)]


def _pattern_for(frm: str) -> str:
    esc = re.escape(frm).replace(r"\ ", r"\s*")
    if re.search(r"[A-Za-z]", frm) and not re.search(r"[一-鿿]", frm):
        return rf"(?i)(?<![A-Za-z0-9_\-/]){esc}(?![A-Za-z0-9_])"
    if re.search(r"[A-Za-z]", frm):                  # 中英混的（"IBP变bench"）：英文不分大小写
        return rf"(?i){esc}"
    return esc


def add_rule(frm: str, to: str, evidence: list[dict], source: str, why: str = "") -> dict | None:
    """加一条（或给已有的那条加证据）。被撤销过的同一条不会自己再加回来（要恢复用 redo）。"""
    frm, to = frm.strip(), to.strip()
    if not frm or not to or frm == to or len(frm) < 2:
        return None
    d = _load_learned()
    for r in d["rules"]:
        if r.get("from") == frm and r.get("to") == to:
            seen = {(e.get("meeting"), e.get("line")) for e in r.get("evidence") or []}
            new = [e for e in evidence if (e.get("meeting"), e.get("line")) not in seen]
            if new:
                r["evidence"] = (r.get("evidence") or []) + new
                r["count"] = len(r["evidence"])
                _save_learned(d)
            return r
    rid = "r%04d" % (1 + max([int(str(r.get("id", "r0"))[1:] or 0) for r in d["rules"]] or [0]))
    rule = {"id": rid, "from": frm, "to": to, "pattern": _pattern_for(frm), "count": len(evidence), "evidence": evidence,
            "source": source, "added": time.strftime("%Y-%m-%d %H:%M:%S"), "active": True}
    if why:
        rule["why"] = why
    d["rules"].append(rule)
    _save_learned(d)
    _log("add", rule, why)
    return rule


# 开会时常说的中文词（"兑换模型"这种听错，目标得是这里面的词才学）
APP_VOCAB = ["对话模型", "改稿模型", "逐字稿", "纪要", "导航栏", "截图", "散会", "主持人", "参会者", "回顾", "修订对比",
             "修订标记", "语音合成", "语音识别", "思考强度", "邀请链接", "复制链接", "悬浮岛", "整理", "转写"]
_TOK = re.compile(r"[A-Za-z][A-Za-z0-9.+\-]*|\d+(?:\.\d+)?|[一-鿿]")


def vocabulary() -> dict[str, str]:
    """认得的写法（小写 → 正确写法）：软件词表、论文术语、开会常用词、你的名字。"""
    out: dict[str, str] = {}
    me = _me()
    for to, _ in SOFTWARE:
        t = me if to == "@ME" else to
        if t:
            out.setdefault(t.lower(), t)
    try:
        cur = json.loads(CURRENT.read_text(encoding="utf-8"))
        for x in cur.get("terms") or []:
            if isinstance(x, dict) and x.get("term"):
                out.setdefault(str(x["term"]).lower(), str(x["term"]))
    except (OSError, ValueError):
        pass
    for w in APP_VOCAB:
        out.setdefault(w, w)
    return out


# 这些是真英文词：不能当成"听错的写法"去学（"cold cold desktop"里的 cold、"light blue"里的 blue）
COMMON_EN = set("""cold blue light like lite code cloud deep sea see sick open order ultra auto extra max high low bar star bug
app link test pro flash dim one two three four five the and for not you your this that what with have just very more some
time good make take look need want know think show work word play read real call back down over only even also well
""".split())


def _sounds_like(a: str, b: str) -> bool:
    """两个英文写法听着像不像（OPPOS / Opus、DIPSICK / DeepSeek）：首字母一样、字母像、辅音骨架像。"""
    import difflib
    a, b = re.sub(r"[^a-z]", "", a.lower()), re.sub(r"[^a-z]", "", b.lower())
    if len(a) < 3 or len(b) < 3 or a[0] != b[0]:
        return False
    sk = lambda w: re.sub(r"[aeiouyhw]", "", w.replace("ph", "f").replace("ck", "k").replace("c", "k").replace("ee", "i"))
    r1 = difflib.SequenceMatcher(None, a, b).ratio()
    r2 = difflib.SequenceMatcher(None, sk(a), sk(b)).ratio()
    return r1 >= 0.6 and r2 >= 0.8


def _diff_pairs(old: str, new: str) -> list[tuple[str, str]]:
    """实时那一版和重转那一版逐词比：换掉的那几处 (实时写的, 重转写的)，按原文截。"""
    import difflib
    a = [(m.group(0), m.start(), m.end()) for m in _TOK.finditer(old)]
    b = [(m.group(0), m.start(), m.end()) for m in _TOK.finditer(new)]
    sm = difflib.SequenceMatcher(None, [x[0].lower() for x in a], [x[0].lower() for x in b], autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or i2 - i1 > 6 or j2 - j1 > 6:
            continue
        out.append((old[a[i1][1]:a[i2 - 1][2]].strip(), new[b[j1][1]:b[j2 - 1][2]].strip()))
    return out


def _section_terms() -> list[str]:
    """当前论文的小节名（去掉编号）：改稿常说"定义与符号""数据与回测"，也是识别常听错的词。"""
    try:
        from .config import current_paper
        p = Path((current_paper() or {}).get("path") or "")
        out = []
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            m = re.match(r"^#{1,4}\s+(?:[\d.]+\s*)?(.+?)\s*$", line)
            if m and 2 <= len(m.group(1)) <= 20:
                out.append(m.group(1))
        return out
    except Exception:
        return []


def _core_pair(before: str, after: str, terms: list[str]) -> tuple[str, str] | None:
    """整理时的一处识别错字（before → after）缩成能当规则的一对：先找改动落在哪个认得的词上（after 里有这个词，
    before 里对应那截不一样），就是 (那截, 那个词)；没有就去掉两边一样的字、再各带一个字的上下文。改动只有一个字的不要。"""
    for t in sorted(terms, key=len, reverse=True):
        k = after.find(t)
        if k < 0 or t in before:
            continue
        pre, suf = after[:k], after[k + len(t):]
        if before.startswith(pre) and before.endswith(suf) and len(before) > len(pre) + len(suf):
            frm = before[len(pre):len(before) - len(suf)] if suf else before[len(pre):]
            if frm and frm != t:
                if len(frm) < 4:                        # 太短的错写（"主存"）本身可能就是个词：连前后的字一起收
                    r_ext = suf[:4 - len(frm)]
                    l_ext = pre[-max(0, 4 - len(frm) - len(r_ext)):] if 4 - len(frm) - len(r_ext) > 0 else ""
                    frm, t = l_ext + frm + r_ext, l_ext + t + r_ext
                return frm, t
    p = 0
    while p < min(len(before), len(after)) and before[p] == after[p]:
        p += 1
    q = 0
    while q < min(len(before), len(after)) - p and before[-1 - q] == after[-1 - q]:
        q += 1
    core_b, core_a = before[p:len(before) - q], after[p:len(after) - q]
    if max(len(core_b), len(core_a)) < 2:
        return None                                     # 一个字的改动（飞→翻）：得看上下文，不收
    a0, b0 = max(0, p - 1), q - 1 if q > 0 else 0
    frm = before[a0:len(before) - b0] if b0 else before[a0:]
    to = after[a0:len(after) - b0] if b0 else after[a0:]
    return (frm, to) if frm != to else None


def learn_cleanup(d: Path) -> dict:
    """散会整理完：把整理时改过的识别错字收成纠错规则 / 候选，把高频词收成热词。返回 {"rules": [新规则], "hot": [新热词]}。"""
    d = Path(d)
    try:
        cleanup = json.loads((d / "cleanup.json").read_text(encoding="utf-8")).get("items") or []
    except (OSError, ValueError):
        cleanup = []
    try:
        people = json.loads((d / "participants.json").read_text(encoding="utf-8")) or []
    except (OSError, ValueError):
        people = []
    names = [str(p.get("name")) for p in people if p.get("name") and p.get("id") not in ("me", "claude")]
    vocab = vocabulary()
    terms = list(dict.fromkeys(list(vocab.values()) + _section_terms() + names))
    known = {t.lower() for t in terms}
    try:
        raw = [json.loads(x) for x in (d / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        raw = []
    immediate, observed = [], []
    for it in cleanup:
        if it.get("kind") != "asr" or it.get("restored"):
            continue
        pair = _core_pair(str(it.get("before") or ""), str(it.get("after") or ""), terms)
        if not pair:
            continue
        frm, to = pair
        if len(frm) < 4:                                # 太短的错写（"主存"）本身可能就是个词：从原句里带上后面（不够再前面）的字
            text = str((raw[int(it.get("line"))] if 0 <= int(it.get("line") or -1) < len(raw) else {}).get("text") or "")
            k = text.find(frm, max(0, int(it.get("pos") or 0) - 2))
            if k < 0:
                k = text.find(frm)
            if k >= 0:
                need = 4 - len(frm)
                r_ext = text[k + len(frm):k + len(frm) + need]
                l_ext = text[max(0, k - (need - len(r_ext))):k] if need > len(r_ext) else ""
                if re.search(r"[，。！？、,.!?\s]", l_ext + r_ext) is None:
                    frm, to = l_ext + frm + r_ext, l_ext + to + r_ext
            if len(frm) < 4:
                continue                                # 带不上上下文（前后是标点）：不收
        ev = {"meeting": d.name, "line": it.get("line"), "before": it.get("before"), "after": it.get("after"), "via": "cleanup"}
        (immediate if pair[1].lower() in known else observed).append((frm, to, ev))
    data = _load_learned()                              # 先记候选（读一次、写一次），再加规则（add_rule 自己读写）
    cands = data.setdefault("candidates", [])
    promote = []
    for frm, to, ev in observed:
        c = next((x for x in cands if x.get("from") == frm and x.get("to") == to), None)
        if c is None:
            c = {"from": frm, "to": to, "evidence": [], "first": time.strftime("%Y-%m-%d %H:%M:%S")}
            cands.append(c)
        if all(e.get("meeting") != d.name or e.get("line") != ev["line"] for e in c["evidence"]):
            c["evidence"].append(ev)
        if len({e.get("meeting") for e in c["evidence"]}) >= 2 and c not in promote:   # 两场会都这么改过：收
            promote.append(c)
    _save_learned(data)
    added, seen = [], set()
    for frm, to, ev in immediate:
        before = {(r.get("from"), r.get("to")) for r in learned_rules(include_inactive=True)}
        r = add_rule(frm, to, [ev], source="cleanup", why="整理时纠正，改成的是论文 / 软件 / 参会人里的词")
        if r and (frm, to) not in before and (frm, to) not in seen:
            added.append(r)
            seen.add((frm, to))
    for c in promote:
        r = add_rule(c["from"], c["to"], c["evidence"], source="cleanup", why="两场会整理时都这么纠正")
        if r and (c["from"], c["to"]) not in seen:
            added.append(r)
            seen.add((c["from"], c["to"]))
    # 热词：被认错过的词、访客的名字、整场出现 3 次以上的英文词（不是常见英文、不是已经在热词里的）
    lines = []
    try:
        lines = [json.loads(x) for x in (d / "transcript.clean.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        pass
    words = Counter()
    for l in lines:
        if l.get("who") == "student":
            continue
        for w in re.findall(r"(?<![A-Za-z])[A-Za-z][A-Za-z0-9.+\-]{2,}(?![A-Za-z])", str(l.get("text") or "")):
            if w.lower() not in COMMON_EN and w.lower() not in _STOP:
                words[vocab.get(w.lower(), w)] += 1
    fresh = [w for w, c in words.items() if c >= 3]
    fixed = [it for it in cleanup if it.get("kind") == "asr" and not it.get("restored")]
    for it in fixed:
        pair = _core_pair(str(it.get("before") or ""), str(it.get("after") or ""), terms)
        if pair and pair[1].lower() in known and len(pair[1]) >= 2:
            fresh.append(pair[1])
    fresh += [n for n in names if 2 <= len(n) <= 8]
    data = _load_learned()
    hot = data.setdefault("hot", [])
    now, new = time.time(), []
    for w in dict.fromkeys(fresh):
        h = next((x for x in hot if x.get("word") == w), None)
        if h is None:
            h = {"word": w, "count": 0, "first": time.strftime("%Y-%m-%d %H:%M:%S")}
            hot.append(h)
            new.append(w)
        h["count"] = int(h.get("count") or 0) + 1
        h["last_ts"] = now
    _save_learned(data)
    try:                                                # 热词表重写一遍（下一场会、重转写、访客转写都用）
        lex = json.loads(CURRENT.read_text(encoding="utf-8"))
        HOTWORDS.write_text(json.dumps(merged_hotwords(lex.get("hotwords")), ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass
    return {"rules": added, "hot": new}


def learn_meeting(d: Path, vocab: dict[str, str] | None = None) -> list[dict]:
    """散会重转写之后：实时那一版里听错、重转那一版写对了（而且写对的是认得的词）的地方，记成候选；
    同一种听错攒够两次（这场会里或者以往的会里）才变成规则。实时那一版里一个英文写法听着像认得的词、这场会出现两次以上的，
    也一样（"OPPOS"→Opus）。返回这次新加上的规则。"""
    d = Path(d)
    live = [json.loads(x) for x in (d / "transcript.live.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] \
        if (d / "transcript.live.jsonl").exists() else []
    best = [json.loads(x) for x in (d / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    if not live or len(live) != len(best):
        return []
    vocab = vocab or vocabulary()
    from .normalize import live as norm
    found: dict[tuple[str, str], list[dict]] = {}
    for i, (a, b) in enumerate(zip(live, best)):
        if a.get("who") != "teacher" or not b.get("asr"):
            continue
        # 实时那一版先用现在的规则整一遍：现有规则已经能改对的（cold cold desktop、杠 blue）就不会再冒出来
        for old, new in _diff_pairs(norm(str(a.get("text") or "")), str(b.get("text") or "")):
            to = vocab.get(new.lower()) or vocab.get(re.sub(r"\s+", "", new).lower())
            if not to or not old or to.lower() in old.lower() or len(old) > 3 * len(to) + 4:
                continue
            if old.lower() in COMMON_EN or old.lower() in LOWER_WORDS:
                continue
            if to in norm(old):                     # 现有规则已经能改对
                continue
            if re.search(r"[一-鿿]", to) and len(old) != len(to):
                continue                              # 中文词只学同样长短的听错（"兑换模型"→"对话模型"）
            found.setdefault((old, to), []).append({"meeting": d.name, "line": i, "live": a.get("text"), "best": b.get("text")})
    # 实时那一版自己：听着像认得的英文词、出现两次以上
    for i, a in enumerate(live):
        if a.get("who") != "teacher":
            continue
        for w in re.findall(r"(?<![A-Za-z])[A-Za-z]{4,}(?![A-Za-z])", str(a.get("text") or "")):
            if w.lower() in vocab or w.lower() in LOWER_WORDS or w.upper() in ACRONYMS or w.lower() in COMMON_EN:
                continue
            if norm(w) != w:                          # 现有规则已经会改它
                continue
            hits = [t for k, t in vocab.items() if re.fullmatch(r"[a-z]+", k) and _sounds_like(w, k)]
            if len(hits) == 1 and hits[0] not in norm(w):
                found.setdefault((w, hits[0]), []).append({"meeting": d.name, "line": i, "live": a.get("text"), "via": "sound"})
    data = _load_learned()
    cands = data.setdefault("candidates", [])
    added = []
    for (frm, to), ev in found.items():
        c = next((x for x in cands if x.get("from") == frm and x.get("to") == to), None)
        if c is None:
            c = {"from": frm, "to": to, "evidence": [], "first": time.strftime("%Y-%m-%d %H:%M:%S")}
            cands.append(c)
        seen = {(e.get("meeting"), e.get("line")) for e in c["evidence"]}
        c["evidence"] += [e for e in ev if (e.get("meeting"), e.get("line")) not in seen]
    _save_learned(data)
    for c in list(cands):
        frm, to, ev = c["from"], c["to"], c["evidence"]
        strong = (len(ev) == 1 and re.fullmatch(r"[A-Za-z]{4,}", frm) and _sounds_like(frm, to)
                  and frm.lower() not in LOWER_WORDS and any("best" in e for e in ev))
        need = MIN_EVIDENCE + (1 if bool(re.search(r"[A-Za-z]", frm)) != bool(re.search(r"[A-Za-z]", to)) else 0)
        if len(ev) >= need or strong:                # 中文听成英文名 / 英文名听成中文：要三次
            why = f"{len(ev)} 次一致" if len(ev) >= need else "一次，但发音几乎一样、重转写也写成了这个词"
            existed = any(x.get("from") == frm and x.get("to") == to for x in learned_rules(include_inactive=True))
            r = add_rule(frm, to, ev, source="meeting", why=why)
            if r and not existed:
                added.append(r)
            d2 = _load_learned()
            d2["candidates"] = [x for x in d2.get("candidates", []) if not (x.get("from") == frm and x.get("to") == to)]
            _save_learned(d2)
    return added


def undo(rule_id: str, active: bool = False) -> dict | None:
    """撤销一条学到的规则（active=True 就是恢复）。规则不删，只停用，流水里记一笔。"""
    d = _load_learned()
    for r in d["rules"]:
        if r.get("id") == rule_id:
            r["active"] = bool(active)
            _save_learned(d)
            _log("redo" if active else "undo", r)
            return r
    return None


def write_doc(lex: dict) -> Path:
    """给人看的术语表：按类别列出术语、出现次数、中文对照，以及热词和纠错规则。"""
    DOCS.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[\\/:*?\"<>|]+", "-", lex.get("paper") or "论文")
    p = DOCS / f"{name}-术语表.md"
    kinds = [("acronym", "缩写"), ("camel", "驼峰写法的名字（系统、数据集、方法）"), ("name", "专有名词（人名、公司、指标）"),
             ("phrase", "英文短语"), ("word", "常说的英文小词")]
    out = [f"# {lex.get('paper')} · 术语表", "",
           f"从论文自动整理（{lex.get('built')}），换论文或论文改过会自动重建。用途：语音识别热词、转写纠错、给人查。", ""]
    for k, title in kinds:
        rows = [x for x in lex["terms"] if x["kind"] == k]
        if not rows:
            continue
        out += [f"## {title}", "", "| 术语 | 出现次数 | 中文 |", "|---|---:|---|"]
        out += [f"| {x['term']} | {x['count']} | {x.get('zh') or ''} |" for x in rows[:60]]
        out.append("")
    if lex.get("pairs"):
        out += ["## 论文里的中英对照", "", "| English | 中文 |", "|---|---|"]
        out += [f"| {en} | {zh} |" for en, zh in lex["pairs"].items()]
        out.append("")
    out += ["## 语音识别热词（前 %d 个）" % len(lex["hotwords"]), "", "、".join(lex["hotwords"]), "",
            "## 转写纠错规则（识别写成 → 换成）", "", "| 识别写成 | 换成 |", "|---|---|"]
    out += [f"| {v} | {t} |" for v, t in sorted(lex["variants"].items(), key=lambda kv: kv[1].lower())]
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return p


if __name__ == "__main__":
    import sys
    from .config import load, paper_path, paper_title
    if sys.argv[1:2] == ["learned"]:            # python -m zuhui.lexicon learned：学到的规则和候选
        d = _load_learned()
        for r in d["rules"]:
            print(f"{r['id']} {'用着' if r.get('active', True) else '已撤销'}  {r['from']} → {r['to']}  "
                  f"（{r.get('count')} 次，{r.get('why') or r.get('source')}，{r.get('added')}）")
        for c in d.get("candidates", []):
            print(f"候选  {c['from']} → {c['to']}  （{len(c.get('evidence') or [])} 次，还不够）")
        print(f"流水：{LEARN_LOG}")
        raise SystemExit(0)
    if sys.argv[1:2] in (["undo"], ["redo"]):    # python -m zuhui.lexicon undo r0003 / redo r0003
        r = undo(sys.argv[2], active=sys.argv[1] == "redo")
        print(json.dumps(r, ensure_ascii=False, indent=1) if r else "没有这条规则")
        raise SystemExit(0 if r else 1)
    cfg = load()
    lx = ensure(paper_path(cfg), paper_title(cfg), force="--force" in sys.argv)
    print(json.dumps({"paper": lx["paper"], "terms": len(lx["terms"]), "variants": len(lx["variants"]),
                      "hotwords": lx["hotwords"], "doc": str(write_doc(lx))}, ensure_ascii=False, indent=1))
