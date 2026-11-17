# -*- coding: utf-8 -*-
"""实时转写的文字整形：口述的命令、型号、软件名写对，脏话删掉。

ears.fix_terms 每来一段识别结果就过一遍（预览那一截也过），快嘴拿到的、逐字稿显示的都是这一版；
原始识别结果只留在录屏目录的 live/transcript.jsonl 里。散会后重转写出来的文字也过同一遍（zuhui/retranscribe.py）。

顺序：脏话 → 拆开念的字母（"R M 杠 R F"、"I B P"）→ 软件词表（lexicon.software_correct：cloud→Claude、
DIPSICK→DeepSeek、OPPOS→Opus、听错的主持人名字→主持人名字……，含散会后学到的规则）→ 命令和型号
（杠 model → /model，Claude 杠 Opus 杠四杠六 → claude-opus-4-6，Opus 四点六 → Opus 4.6，V 四 PRO → V4 Pro，
一 million → 1M，中括号一 M → [1m]）→ 论文词库（lexicon.correct）。

只改这几类确定的写法：杠 / 斜杠 后面紧跟英文才算命令或短横，中文数字只在型号名后面才换成阿拉伯数字，
"四点要开会""抬杠""他妈妈"这类日常说法不动（tests：python -m zuhui.normalize --test）。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 脏话：直接删（实时显示就删，原话只在录屏目录的原始识别里）
_PROFANE = [
    r"他妈的", r"他娘的", r"特么的?", r"tmd", r"TMD", r"妈了个[巴八]子", r"妈的(?![妈])", r"尼玛(?=[的，。！？!?,\s]|$)",
    r"卧槽", r"我[操艹肏](?![作心场控练办持纵守劳盘])", r"[草操艹肏]你妈的?", r"草泥马", r"日了狗了?", r"狗日的",
    r"傻[逼屄比B]", r"煞笔", r"沙比", r"(?<![A-Za-z])S\s?B(?![A-Za-z])", r"[妈麻]痹的?", r"妈逼",
    r"(?i:(?<![a-z])(?:mother\s*)?fuck(?:ing|er|ed|s)?(?![a-z]))", r"(?i:(?<![a-z])shit(?:ty)?(?![a-z]))",
    r"(?i:(?<![a-z])bitch(?:es)?(?![a-z]))", r"(?i:(?<![a-z])wtf(?![a-z]))",
    # "他妈"：前面是我/你/真/又/这个……（"我他妈又忘了""这个他妈也得消掉"）才算脏话；"他妈妈""他妈说"不动
    r"(?<=[我你他她它真太又就都也还可不没好啥么干为的了个是这那])他妈(?![妈们说讲叫问让给在来去家])",
]
_PROFANE_RX = re.compile("|".join(f"(?:{p})" for p in _PROFANE))


def find_profanity(text: str) -> list[tuple[int, int, str]]:
    """脏话出现的位置 [(起, 止, 原文)]（散会整理时一条一条列进删改清单）。"""
    return [(m.start(), m.end(), m.group(0)) for m in _PROFANE_RX.finditer(text or "")]


def tidy(t: str) -> str:
    """删掉词以后留下的空白和重复标点收拾一下。"""
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\s+([，。！？、；：])", r"\1", t)
    t = re.sub(r"([，、；：,])\s*([，。！？、；：,.!?])", r"\2", t)     # "又，。" → "又。"
    t = re.sub(r"^[\s，、；：,]+", "", t)
    return t.strip()


def strip_profanity(text: str) -> str:
    if not text or not _PROFANE_RX.search(text):
        return text
    return tidy(_PROFANE_RX.sub("", text))


# ---------------------------------------------------------------- 数字
_CN = {"零": 0, "〇": 0, "一": 1, "幺": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM = r"[零〇一二两三四五六七八九十]"
_NUM = rf"(?:\d+(?:\.\d+)?|{_CN_NUM}{{1,3}}(?:点{_CN_NUM}{{1,2}})?)"
_UNIT_AFTER = r"(?![个次条处句遍种位天年月号分秒倍块张页节章人场轮点])"


def cn_int(s: str) -> str:
    """一 → 1，十二 → 12，二十 → 20，四六 → 46（逐位念的）。不认识就原样返回。"""
    s = s.strip()
    if not s:
        return s
    if s.isdigit():
        return s
    if "十" in s:
        a, _, b = s.partition("十")
        if (a and a not in _CN) or (b and b not in _CN):
            return s
        return str((_CN[a] if a else 1) * 10 + (_CN[b] if b else 0))
    if all(c in _CN for c in s):
        return "".join(str(_CN[c]) for c in s)
    return s


def cn_number(s: str) -> str:
    """四点六 → 4.6，五点五 → 5.5，4.6 → 4.6。"""
    s = s.strip()
    if "点" in s:
        a, _, b = s.partition("点")
        return f"{cn_int(a)}.{cn_int(b)}"
    return cn_int(s)


# ---------------------------------------------------------------- 拆开念的字母
SHELL = {"rm", "ls", "cp", "mv", "cd", "ps", "du", "df", "git", "pip", "npm", "pnpm", "uv", "cat", "ssh", "scp", "tar",
         "kill", "chmod", "mkdir", "curl", "wget", "top", "conda", "python", "node", "cargo"}
_LETTERS = r"[A-Za-z](?:\s+[A-Za-z]){1,5}"


def spelled(text: str) -> str:
    """"R M 杠 R F" → "rm -rf"（拆开念的命令带参数）；"I B P" → "IBP"（拆开念的大写缩写）。"""
    text = re.sub(rf"(?<![A-Za-z])({_LETTERS}|[A-Za-z]{{2,6}})\s*(?:(杠杠|--)|杠|-)\s*([A-Za-z](?:\s?[A-Za-z]){{0,3}})(?![A-Za-z])",
                  lambda m: _shell_flags_fix(m), text)
    text = re.sub(r"(?<![A-Za-z])([A-Z](?: [A-Z]){1,5})(?![A-Za-z])", lambda m: m.group(1).replace(" ", ""), text)
    return text


def _shell_flags_fix(m: re.Match) -> str:
    # group(1)=命令（可能拆开念），group(2)=双杠，group(3)=参数
    cmd = re.sub(r"\s+", "", m.group(1)).lower()
    if cmd not in SHELL:
        return m.group(0)
    flags = re.sub(r"\s+", "", m.group(3)).lower()
    dash = "--" if m.group(2) else "-"
    pre = " " if m.start() > 0 and re.match(r"[^\s(（\"“]", m.string[m.start() - 1]) else ""
    nxt = m.string[m.end():m.end() + 1]
    post = " " if nxt and re.match(r"[^\s，。！？、,.!?)）\"”]", nxt) else ""
    return f"{pre}{cmd} {dash}{flags}{post}"


# ---------------------------------------------------------------- 命令、型号
_SEG = rf"(?:[A-Za-z][A-Za-z0-9.]*|\d+(?:\.\d+)?|{_CN_NUM}{{1,3}}(?:点{_CN_NUM}{{1,2}})?)"
_CHAIN = re.compile(rf"(?<![A-Za-z0-9_/.-])({_SEG})((?:\s*杠\s*{_SEG})+)(?![A-Za-z0-9_])")


def _chain(m: re.Match) -> str:
    head = m.group(1)
    if re.fullmatch(rf"{_CN_NUM}+(?:点{_CN_NUM}+)?", head):
        return m.group(0)                        # "三杠四"：中文数字开头不是型号，不动
    parts = [head] + re.split(r"\s*杠\s*", m.group(2).strip())[1:]
    out = [p if re.match(r"[A-Za-z0-9]", p) else cn_number(p) for p in parts]
    s = "-".join(p for p in out if p)
    if s.split("-")[0].lower() == "claude" and re.search(r"\d", s):
        s = s.lower()                            # Claude 杠 Opus 杠四杠六 → claude-opus-4-6（命令行里的型号 id 全小写）
    return s


def _slash(m: re.Match) -> str:
    pre = m.string[m.start() - 1] if m.start() > 0 else ""
    sp = " " if re.match(r"[一-鿿A-Za-z0-9]", pre) else ""      # 跟在中文后面空一格；标点、空格后面不空
    return f"{sp}/{m.group(1).lower()}"


def commands(text: str) -> str:
    t = text
    # 型号 id 里的短横：Claude杠Opus杠四杠六 → claude-opus-4-6；GPT杠SoVITS → GPT-SoVITS
    t = _CHAIN.sub(_chain, t)
    # 斜杠命令：杠 model / 斜杠 model → /model（杠后面紧跟英文才算；"抬杠""杠杆""杠是斜杠吧"不动）
    t = re.sub(r"(?:斜杠|杠|/)\s*([A-Za-z][A-Za-z0-9_-]*)(?<!-)", lambda m: _slash(m) if _slash_ok(m) else m.group(0), t)
    # 1M 上下文：一 million / 一 M → 1M；型号 id 后面跟着"中括号一 M" → [1m]
    t = re.sub(r"(?i)(claude-[a-z0-9.-]*\d)\s*[，,、]?\s*(?:然后)?\s*(?:中括号|方括号|括号)?\s*(?:里面?)?\s*(?:框|写|加|是|带)?\s*"
               r"(?:一个)?\s*(?:一|1)\s*(?:m|million|兆)(?![a-z])\s*(?:中括号|方括号|括号)?", r"\1[1m]", t)
    t = re.sub(r"(?i)(?<![A-Za-z0-9.])(?:一|1)\s*(?:million|m)(?![a-z])(?=\s*(?:的|上下文|context|版|模型|token|$|[，。！？、,.!?）)]))", "1M", t)
    # 型号后面的版本号：Opus 四点六 → Opus 4.6，Claude Opus 5.5 不动，Opus4.6 → Opus 4.6
    t = re.sub(rf"(?<![A-Za-z-])(Opus|Sonnet|Haiku|Claude|Fable|Gemini)\s*({_NUM}){_UNIT_AFTER}",
               lambda m: f"{m.group(1)} {cn_number(m.group(2))}" if (re.search(r"[\d点]", m.group(2)) or m.group(1) != "Claude")
               else m.group(0), t)
    t = re.sub(rf"(?<![A-Za-z])(Qwen)\s*({_NUM}){_UNIT_AFTER}", lambda m: f"{m.group(1)}{cn_number(m.group(2))}", t)
    t = re.sub(r"(?<![A-Za-z])GPT\s*(?:杠|-)?\s*([三四五]|\d)(\s*[oO](?![A-Za-z]))?",
               lambda m: f"GPT-{cn_int(m.group(1))}{'o' if m.group(2) else ''}", t)
    # DeepSeek V4 Pro / Flash：V 四 PRO → V4 Pro
    t = re.sub(rf"(?i)(?<![A-Za-z])(DeepSeek)\s*[Vv]\s*({_NUM})", lambda m: f"DeepSeek V{cn_number(m.group(2))}", t)
    t = re.sub(rf"(?<![A-Za-z])[Vv]\s*({_CN_NUM}(?:点{_CN_NUM})?)\s*(?=(?:pro|PRO|Pro|flash|FLASH|Flash)(?![A-Za-z]))",
               lambda m: f"V{cn_number(m.group(1))} ", t)
    t = re.sub(r"(?<![A-Za-z])V(\d+(?:\.\d+)?)\s*(pro|PRO|Pro)(?![A-Za-z])", r"V\1 Pro", t)
    t = re.sub(r"(?<![A-Za-z])V(\d+(?:\.\d+)?)\s*(flash|FLASH|Flash)(?![A-Za-z])", r"V\1 Flash", t)
    t = re.sub(r"DeepSeek(?=V\d)", "DeepSeek ", t)
    return t


def _slash_ok(m: re.Match) -> bool:
    """杠 / 斜杠 + 英文：前面不是英文字母或数字（那是短横，_CHAIN 已经处理）。'/' 本身只把"/ model"并成"/model"。"""
    pre = m.string[m.start() - 1] if m.start() > 0 else ""
    if re.match(r"[A-Za-z0-9_.-]", pre):
        return False
    if m.group(0).startswith("/"):
        return m.group(0) != "/" + m.group(1)    # 已经是 /model：不用动
    return True


# ---------------------------------------------------------------- 总入口
def live(text: str, profanity: bool = True) -> str:
    """实时那条路（ears.fix_terms）：每段识别结果过一遍。profanity=False：脏话留着（散会后重转写的"原始版"用，
    整理时再一条条列进删改清单删掉）。"""
    if not text:
        return text
    t = strip_profanity(text) if profanity else text
    if not t:
        return t
    t = spelled(t)
    try:
        from .lexicon import software_correct
        t = software_correct(t)
    except Exception:
        pass
    t = commands(t)
    try:
        from .lexicon import correct
        t = correct(t)
    except Exception:
        pass
    t = re.sub(r"(?<=[一-鿿])(?=/[a-z])", " ", t)            # "应该是/model" → "应该是 /model"
    return t


# ---------------------------------------------------------------- 测试：从两场会里真听错的说法来的
CASES = [
    # (识别结果, 应该写成)
    ("切换的指令应该是杠model。", "切换的指令应该是 /model。"),
    ("Cloud杠Opus杠四杠六，然后中括号里面框一个EM，好像是这个指令。", "claude-opus-4-6，然后中括号里面框一个EM，好像是这个指令。"),
    ("呃，改稿模型我想在Cloud Opus 5.5和Cloud Opus 4.6，然后一million的模型里面去做。",
     "呃，改稿模型我想在Claude Opus 5.5和Claude Opus 4.6，然后1M的模型里面去做。"),
    ("然后你的那个兑换模型，DIPSICK V四PRO，我想点一个能够切换的功能，包括改稿模型。",
     "然后你的那个对话模型，DeepSeek V4 Pro，我想点一个能够切换的功能，包括改稿模型。"),
    ("我就想跟这个迪普斯克说，我说把你的模型切换成什么什么。", "我就想跟这个DeepSeek说，我说把你的模型切换成什么什么。"),
    ("杠MODEL那个指令，你首先杠是斜杠吧，你总得知道吧？然后OPPOS呃，CLOUD杠OPPOS杠四杠六这个东西。",
     "/model那个指令，你首先杠是斜杠吧，你总得知道吧？然后Opus呃，claude-opus-4-6这个东西。"),
    ("比如我让你R M杠R F呢？", "比如我让你 rm -rf 呢？"),
    ("比如说我说R M杠R F，这是删除指令。", "比如说我说 rm -rf，这是删除指令。"),
    ("我现在想用一个skill，比如说杠blue嘛。", "我现在想用一个skill，比如说 /bloom嘛。"),
    ("我现在要新开一个对话，杠BLOOM，然后他就能完成", "我现在要新开一个对话，/bloom，然后他就能完成"),
    ("对，我知道他说录Broom窗口本身", "对，我知道他说录Bloom窗口本身"),
    ("改论文是一方面，但是cold cold desktop它可以帮助我。", "改论文是一方面，但是Claude Code Desktop它可以帮助我。"),
    ("我要把这个F O O BENCH跟老师讨论。我现在不能用CTRL加滚轮", "我要把这个FooBench跟老师讨论。我现在不能用Ctrl加滚轮"),
    ("或者拿来发GITHUB开源", "或者拿来发GitHub开源"),
    ("比如说乱码或者LATEX不显示啊", "比如说乱码或者LaTeX不显示啊"),
    ("只能传Latex和Markdown嘛", "只能传LaTeX和Markdown嘛"),
    ("然后允许你开SUBAGENT去改。", "然后允许你开subagent去改。"),
    ("这到底是什么BUG？", "这到底是什么bug？"),
    ("一打岔，我他妈又。忘记了哦，对，这个他妈也得帮我消掉", "一打岔，我又。忘记了哦，对，这个也得帮我消掉"),
    ("可以交由更深层的 cloud 那个模型去找", "可以交由更深层的 Claude 那个模型去找"),
    ("改稿模型用 /model claude-opus-4-6 中括号一M", "改稿模型用 /model claude-opus-4-6[1m]"),
    ("斜杠model claude杠opus杠四杠六 一M", "/model claude-opus-4-6[1m]"),
    ("换成DeepSeek V四 flash", "换成DeepSeek V4 Flash"),
    ("我想换成opus四点六", "我想换成Opus 4.6"),
    ("GPT杠SoVITS的声音", "GPT-SoVITS的声音"),
    # 日常说法：不动
    ("我四点要开会，开到五点半。", "我四点要开会，开到五点半。"),
    ("你别跟我抬杠，这个杠杆率太高了。", "你别跟我抬杠，这个杠杆率太高了。"),
    ("他妈妈今天来了，他妈说要早点走。", "他妈妈今天来了，他妈说要早点走。"),
    ("这个操作系统不太好用，我操作一下。", "这个操作系统不太好用，我操作一下。"),
    ("第三杠四节写得不对。", "第三杠四节写得不对。"),
    ("一百万个样本，四六级考试。", "一百万个样本，四六级考试。"),
    ("Claude 四个任务都改完了。", "Claude 四个任务都改完了。"),
    ("这个草图画得不错，草地很绿。", "这个草图画得不错，草地很绿。"),
]


def _test() -> int:
    bad = 0
    for src, want in CASES:
        got = live(src)
        ok = got == want
        bad += not ok
        print(("ok  " if ok else "FAIL") + f" {src}\n     → {got}" + ("" if ok else f"\n  want {want}"))
    print(f"{len(CASES) - bad}/{len(CASES)} passed")
    return bad


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        raise SystemExit(1 if _test() else 0)
    for line in sys.stdin:
        print(live(line.rstrip("\n")))
