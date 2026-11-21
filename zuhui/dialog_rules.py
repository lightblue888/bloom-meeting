# -*- coding: utf-8 -*-
"""对话层的几条确定性规矩（app.handle / _extras / _check_missed / act 用）。都是纯函数，单测直接调。

2026-11-14 那场真会（手机访客 x，身份老师）暴露的几类毛病，这里各有一条兜底，不靠提示词：
- 人说的一句话里有几件事（软件意见 + 改论文 + 问题）：split_requests 按句切开、按人说的话分类，没办的那件软件替它补上。
- 软件的事和论文的事分不清：seg_kind 看人说的话（不只看 AI 回的话）；软件的事只进软件反馈，不进论文、不成标注。
- AI 嘴上答应改（"我会一起记着改""排队去改"）却没打 #edit：promised_change 认出来，软件按人的原话记一条改稿。
- AI 请对方接着说（"你接着说""你说"）：invites_continuation；同一个人接着说的下一段算冲它说的，并起来理解。
- "你仔细想一想""再想想"：wants_deeper，直接交给后台深想，前台只说一句交接。
- 对方纠正 AI 的理解："我不是说……我的意思是……"：corrects，按错的理解记的那条撤掉。
- 念出口前去掉内部上下文的名字（【软件现状】这种）：scrub_labels。
"""
from __future__ import annotations

import re

_PUNCT = r"[\s，。！？、,.!?：:；;…“”\"'（）()《》<>【】\-—]"


def bare(s: str | None) -> str:
    return re.sub(_PUNCT, "", s or "")


def short(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 软件还是论文：看人说的话
# 软件本身（界面、显示、声音、转写速度、按钮、打勾、逐字稿……）。"Bloom采用实时转写"这种是论文正文里的话（这场的论文写的就是
# Bloom），所以单个"Bloom""转写""识别""模型"不算；"Bloom 软件""Bloom 里面"才算
# "滚动""滑动""窗口"单说是论文里的常用词（"按月滚动更新""滑动窗口""事件窗口"：2026-11-21 组会把改论文的"这些方法按月滚动更新"
# 记成了一条软件意见），只认说界面的那几种说法
APP_CUE = re.compile(
    r"软件|(?i:bug)|界面|按钮|弹出|弹窗|悬停|鼠标|点开|点不开|折叠|打勾|勾选|勾上|"
    r"滚动条|滚动栏|滚不动|(?:不|没有?|不会|不能|自动|往下|往上|跟着|一直|老是|老)滚动|滚动(?:到|不了|不到|起来|太快|太慢|得)|"
    r"滚轮|滑动(?!窗口|平均)|拉到底|往下翻|自动翻|"
    r"逐字稿|竹子稿|字幕|麦克风|静音|音量|扬声器|外放|收音|回声|卡顿|卡住|卡了|加载|来回切|切换(?=.{0,12}(?:慢|卡))|"
    r"显示(?:在|的是|不出|不了|出来|成|区|位置|对比)|(?:不能|没有?|不会)显示|渲染|状态栏|手机端|手机上|网页|浏览器|"
    r"(?<!滚动|滑动|事件|估计|时间|观察|样本|回看|预测|训练|测试)窗口|布局|撑开|"
    r"对比页|对比生成|生成失败|监控|断开|续上|重连|思考强度|改稿模型|对话模型|用户体验|(?i:monkey)|"
    r"Bloom\s*(?:软件|里面|里|这个软件|的界面)|会议名|会名|改标题|黄条|蓝条|灰条|下划线|填空|悬浮|闪退|报错|打过勾|勾过|"
    r"(?:后台|前台)的?\s*(?:Claude|克劳德)?\s*(?:听不到|没听到|没接|不接)|没接话|不接话|互相说|"
    r"(?i:\bthe\s+app\b|\bsoftware\b|\binterface\b|\bbutton\b|\bscroll(?:bar|ing)?\b|\btranscript\s+panel\b)")
# 论文里的位置、写法（小节名、第几段、这句、措辞、说法……）。单说"论文""申报书"不算（"在两篇论文之间切换很慢"是软件的事）
_PLACE = (r"(?:项目)?简介|摘要|引言|绪论|结论|立项依据|研究意义|研究内容|研究目标|技术路线|预期成果|创新点|前期基础|进度安排|"
          r"参考文献|相关工作|"
          r"第\s*[一二三四五六七八九十两\d]+\s*(?:段|节|章|条|句|部分)|[一二三四五六七八九十\d]+\s*点\s*[一二三四五六七八九十\d]+\s*(?:节|那段|这段|里)|"
          r"[这那上下首末前后]一?(?:段|句)(?:话)?|首段|末句|段落|原文|正文|论文里|文中|申报书里|稿子|"
          r"(?<![A-Za-z])(?<![A-Za-z]\s)\d+\.\d+(?:\.\d+)?(?!\s*%)(?!\d)|[表图]\s*\d+"
          r"|(?i:\b(?:abstract|introduction|conclusion|paragraph|section\s+\d|sentence|related\s+work|methods?|results|discussion|table\s+\d+|fig(?:ure)?\.?\s*\d+)\b)")
PLACE = re.compile(_PLACE)
# 说哪一处要带上的那一截："项目简介的第一段""第三段""三点一节"
PLACE_FULL = re.compile(r"(?:(?:项目)?简介|摘要|引言|结论|立项依据|研究意义|研究内容|技术路线|预期成果|相关工作)(?:的)?"
                        r"(?:第\s*[一二三四五六七八九十两\d]+\s*(?:段|节|章|条|句|部分))?|" + _PLACE)
PAPER_LOC = re.compile(_PLACE + r"|措辞|说法|表述|用词|这个词|转折|(?i:\b(?:wording|phrasing)\b)")
# 改论文的说法（要改成什么样）："改成""写成""删掉""精简一点""加个转折""这样会更好"
EDIT_CUE = re.compile(
    r"改成|改为|换成|写成|写作|改一下|改改|改掉|删掉|删了|删去|删除|去掉|拿掉|加(?:一个|个|一句|一段|上|进去|进)|"
    r"补(?:上|进|一句|充)|精简|压缩|压成|缩短|展开|扩写|带过|合并|拆成|拆开|挪到|移到|调整|重写|换个说法|统一一下|"
    r"应该(?:写|改成|改为|加|删|放到)|不要写|别写|不用写|改的不行|这样(?:写)?(?:会)?(?:更好|比较好|好一些|好点)|这样就行|"
    r"(?i:\b(?:change|replace|rewrite|rephrase|delete|remove|cut|shorten|trim|add|insert|move|merge|split)\b)")
# 只说"这样更好"的：改成什么在前一句（"针对上述问题，Bloom采用……的方式。我觉得这样会更好"）
_EVAL = re.compile(r"这样(?:写)?(?:会)?(?:更好|比较好|好一些|好点)|这样就行")
_TO = re.compile(r"改成|改为|换成|写成|写作|替换成")
# 问进度、问改没改（"第二段你改好了吗"）：不是新的改稿要求
STATUS_ASK = re.compile(r"改好了[吗没么]|改完了[吗没么]|改了[吗没么]|好了没|改好没|改到哪|还没改好|还没有改好")
ASK_CUE = re.compile(r"[？?]|[吗呢么][。！!]?\s*$|什么|怎么|为什么|哪|是不是|对不对|有没有|能不能|要不要|多少|几[个处次条]")
# 软件意见要是"要求 / 抱怨"才记（"这还是软件的问题"这种只是在分类，不单独成一条）
APP_ASK = re.compile(r"要|得|应该|不要|别|不能|不该|改|修|去掉|加|弄出来|为什么|怎么|毛病|问题|不行|很慢|太慢|慢|卡|看不到|"
                     r"没有办法|没办法|乱|难看|不美观|奇怪|失败|不对|没了|不能显示|拉不到|找不到|没找到|没有找到|"
                     r"(?i:\bbug\b|\bfix\b|\bshould\b|\bwhy\b)")
# 软件意见里明着的要求、毛病（带问号的也算要记）："这个 bug 也得修""把打勾的位置弄出来""太慢了"
APP_DEMAND = re.compile(r"(?i:bug)|毛病|得修|要修|修一下|修好|修掉|改一下|改掉|去掉|不要|别再|不该|不能再|应该|得改|要改|记下来|"
                        r"记一下|弄出来|太慢|很慢|非常慢|难看|不美观|乱七八糟|生成失败|拉不到底|又没了|不能显示")
_FILLER = re.compile(r"^(?:(?:嗯|呃|啊|哦|诶|哎|唉|那个|就是|然后|而且|所以说?|其实|另外|还有|对|好|好吧|你看|我觉得|我发现|你哪怕"
                     r"|(?:那个)?(?:后台的?)?(?:Claude|克劳德)(?:你)?(?:看一下|听一下|你)?|我刚刚说了呀|我刚刚说了|我刚刚跟你说了呀)[，,。\s]*)+")
_TAIL = re.compile(r"[，,]?\s*(?:你懂我意思吗|懂我意思吗|你知道吗|知道吗|对吧|对不对|好吧|是不是|你懂吗|能懂我意思吗)[？?。！!]*$")


# 看是不是在说软件之前去掉的：引号里念的论文原话（6 个字以上；"逐字稿""散会"这种界面上的短词照算）、
# "改成“X”"里要改成的字（"把“系统”改成“软件”"：两个引号连同改法一起去掉；"把“逐字稿”改成自动往下滚"不动），
# 和论文里常写的"软件著作权"（2026-11-15 联调：老师说"那个“登记软件著作权”也放到前面去"，被记成了一条软件意见）
_QUOTED = re.compile(r"“[^”]{6,}”|「[^」]{6,}」|『[^』]{6,}』|\"[^\"]{6,}\"")
_REPL = re.compile(r"(?:“[^”]*”\s*)?(?:改成|改为|换成|写成|替换成|改作)\s*“[^”]*”")
_NOT_APP = re.compile(r"软件著作权|软著")


def _app_words(s: str) -> str:
    return _NOT_APP.sub("", _QUOTED.sub("", _REPL.sub("", s or "")))


def app_cue(s: str):
    """APP_CUE 在这句里的命中（不算引号里念的论文原话、"软件著作权"）。"""
    return APP_CUE.search(_app_words(s))


def seg_kind(seg: str) -> str | None:
    """一小段人话说的是什么："app"（软件本身）/ "paper"（论文的哪一处、怎么写）/ None（看不出）。
    论文的位置先看（这场的论文写的就是这个软件，正文里也有"转写""逐字稿"这些词）；再看软件的说法；都没有、只说了怎么改，算论文。"""
    s = seg or ""
    edit = has_edit(s)
    if PAPER_LOC.search(s):
        if app_cue(s) and not edit and len(APP_CUE.findall(_app_words(s))) >= 2:
            return "app"                  # "论文里的标注为什么显示在状态栏"：说的是显示，不是正文
        return "paper"
    if app_cue(s):
        return "app"
    if edit:
        return "paper"
    return None


_WHETHER = re.compile(r"要不要|该不该|需不需要|能不能|可不可以|行不行|好不好|会不会|是不是(?:要|该|得)|(?i:\bshould\s+(?:we|i)\b|\bdo\s+we\s+need\b)")


def edit_request(sentence: str) -> bool:
    """这一句是在要求改（不是在问要不要改、改好了没）："你哪怕精简一点"算，"这段要不要精简一下？"不算。"""
    s = sentence or ""
    if not has_edit(s) or STATUS_ASK.search(s) or _WHETHER.search(s):
        return False
    if (re.search(r"[吗呢么][？?。！!]*\s*$|吧\s*[？?]\s*$", s.strip())
            and not re.search(r"你(?:去|来|帮我|给我)?改|改呀|改吧|删吧", s)):
        return False
    return True


_SEEK_AGREE = re.compile(r"不就行了|对不对|是不是(?!要|该|得)|行不行|可以吗|好不好|你觉得呢|你看呢|怎么样|好吗|行吗|对吧|如何[？?]"
                         r"|(?i:\bright\?|\bwhat\s+do\s+you\s+think\b|\bdon'?t\s+you\s+think\b)")
_DONE = re.compile(r"(?:已经|之前|上次)(?:把)?.{0,12}(?:改|删|压缩|精简|加|补)(?:了|过)")


def directive(text: str) -> bool:
    """这一段里有没有明着要改的那句：是改法（edit_request），不是在说已经改过的，而且这句和后面两句里没在讨对方同意。"""
    sents = [x for x in re.split(r"(?<=[。！？!?；;])", text or "") if x.strip()]
    for i, x in enumerate(sents):
        if edit_request(x) and not _DONE.search(x) and not _SEEK_AGREE.search("".join(sents[i:i + 3])):
            return True
    return False


def has_edit(s: str) -> bool:
    """有没有改论文的说法（识别常在词中间打逗号："这样会，更好"：去掉逗号再看）。
    英文的 add / cut / move 这些词太常见：要同时说了论文里的位置（abstract、section 3、this sentence）才算。"""
    s = s or ""
    m = EDIT_CUE.search(s) or EDIT_CUE.search(re.sub(r"[，,、]", "", s))
    if not m:
        return False
    if re.fullmatch(r"[A-Za-z]+", m.group(0)) and not PAPER_LOC.search(s):
        return False
    return True


def _sentences(text: str) -> list[str]:
    parts = [p for p in re.split(r"(?<=[。！？!?；;])", text or "") if p.strip()]
    out = []
    for p in parts:                       # 一句里"然后""另外""还有"换了话题：再切一刀（"……也得修。然后，针对上述问题"）
        out += [q for q in re.split(r"(?<=[，,])\s*(?=(?:然后|另外|还有|而且|再就是|接下来)[，,])", p) if q.strip()]
    return out


def split_requests(text: str) -> list[dict]:
    """一句话里的几件事：按句切开，每句按人说的话分类，相邻同类的并成一段。看不出类的句子（"你懂我意思吗"）并进前一段。
    → [{"kind": "app"|"paper"|None, "text", "edit": 要改论文, "ask": 是问句, "req": 软件意见里有要求/抱怨}]"""
    sents = [(s, seg_kind(s)) for s in _sentences(text)]
    segs: list[dict] = []
    for i, (s, k) in enumerate(sents):
        short_tail = len(bare(s)) <= 8 and not PAPER_LOC.search(s)
        if segs and (short_tail or k == segs[-1]["kind"]):
            segs[-1]["text"] += s             # 短短一截（"不要写进去""你懂我意思吗""我觉得这样会更好"）：接着上一段说的
            if segs[-1]["kind"] is None:
                segs[-1]["kind"] = seg_kind(segs[-1]["text"])
            continue
        if k is None and segs and segs[-1]["kind"] is not None:
            nxt = next((kk for _, kk in sents[i + 1:] if kk is not None), None)
            if nxt is None or nxt == segs[-1]["kind"]:
                segs[-1]["text"] += s         # 看不出类、后面也没换话题：还是上一段的事
                continue
        if segs and segs[-1]["kind"] is None:
            segs[-1]["text"] += s             # 前面那截看不出类（"针对上述问题，……的方式"）：是这一段的铺垫
            segs[-1]["kind"] = seg_kind(segs[-1]["text"]) if k is None else k
            continue
        segs.append({"kind": k, "text": s})
    for g in segs:
        t = g["text"]
        g["status"] = bool(STATUS_ASK.search(t))
        g["ask"] = bool(ASK_CUE.search(t))
        g["edit"] = (g["kind"] == "paper" and not g["status"]
                     and any(edit_request(s) for s in re.split(r"(?<=[。！？!?；;])", t) if s.strip()))
        g["req"] = g["kind"] == "app" and bool(APP_ASK.search(t))
        g["directive"] = g["edit"] and directive(t)
    return segs


def utterance_kind(text: str) -> str | None:
    """整句话：只有软件的事 → "app"；只有论文的事 → "paper"；两样都有 → "mixed"；看不出 → None。"""
    kinds = {g["kind"] for g in split_requests(text) if g["kind"]}
    if kinds == {"app"}:
        return "app"
    if kinds == {"paper"}:
        return "paper"
    return "mixed" if kinds else None


def is_app_item(title: str, words: str) -> bool:
    """要记成改稿 / 标注的这一条，其实是软件的事吗：先看这条的标题（模型写的，具体到这一件）；标题看不出就看人说的那句话，
    那句话里只有软件的事才算。"""
    k = seg_kind(title or "")
    if k:
        return k == "app"
    return utterance_kind(words or "") == "app"


def clean_clause(s: str) -> str:
    s = _FILLER.sub("", (s or "").strip())
    s = _TAIL.sub("", s)
    return s.strip(" ，,。.！!？?")


def _edit_bit(sent: str, prev: str = "") -> str:
    """一句里说怎么改的那一截：从改法前面那个逗号到改法后面第一个逗号；"改成 X"连着 X 一起；只说"这样更好"的取前一句。"""
    flat = re.sub(r"[，,、\s]", "", sent) if _EVAL.search(re.sub(r"[，,、\s]", "", sent)) and not EDIT_CUE.search(sent) else sent
    m = EDIT_CUE.search(flat)
    if not m:
        return sent
    if _EVAL.fullmatch(m.group(0)) and not EDIT_CUE.search(re.sub(_EVAL, "", flat)):
        before = re.sub(r"(?:我觉得|我认为|感觉|我看)?(?:会)?$", "", flat[:m.start()]).strip(" ，,。")
        if len(bare(before)) >= 6:
            return before                 # "……的方式。我觉得这样会更好"并成了一句：取前面那截新写法
        if prev:
            return prev
    a = max(flat.rfind(c, 0, m.start()) for c in "，,")
    b = min([i for i in (flat.find(c, m.end()) for c in "，,") if i >= 0] or [len(flat)])
    if _TO.fullmatch(m.group(0)) or len(bare(flat[a + 1:b])) < 6:   # "改成，Bloom采用……"：新写法跟在后面，多带一截
        b = min([i for i in (flat.find(c, b + 1) for c in "，,") if i >= 0] or [len(flat)])
    return flat[a + 1:b]


def title_from_words(segs: list[dict] | str, n: int = 40, kind: str = "paper") -> str:
    """按人的原话起一条任务的标题：改论文的取说怎么改的那一两截，前面带上位置（"项目简介 要加一个转折进去"）；
    软件的取头一两句。去掉口头禅、"你懂我意思吗"。"""
    if isinstance(segs, str):
        segs = [g for g in split_requests(segs) if g["kind"] == kind]
    picks = []
    for g in segs:
        sents = [c for c in re.split(r"(?<=[。！？!?；;])", g["text"]) if c.strip()]
        if kind == "paper":
            bits = [_edit_bit(c, sents[i - 1] if i else "") for i, c in enumerate(sents) if has_edit(c)]                 or [c for c in sents if PAPER_LOC.search(c)] or sents
            body = "，".join(x for x in (clean_clause(c) for c in bits[:2]) if x)
            loc = PLACE_FULL.search(g["text"])
            if body and loc and not PLACE.search(body):
                body = f"{loc.group(0).strip()} {body}"
        else:
            keep = [c for c in sents if app_cue(c)] or sents
            body = "，".join(x for x in (clean_clause(c) for c in keep[:2]) if x)
            body = re.sub(r"^软件[：:]\s*", "", body)
        if body:
            picks.append(body)
        if len(picks) >= 2:
            break
    return short("，".join(picks), n)


def join_words(*parts: str) -> str:
    """几段人话接成一段（各自成句：中间补句号，切句、分类时不会粘成一句）。"""
    out = ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        out = (out.rstrip(" ，,") + ("" if out.rstrip()[-1:] in "。！？!?；;…" else "。") + p) if out else p
    return out


def new_part(prev: str | None, cur: str | None) -> str | None:
    """这句是不是上一句再接着说的（访客的话过时重排、并进接着说的那一轮："A" → "A，B"）：是就返回接上的那截 B，不是返回 None。
    按去掉标点比（并的时候会补逗号）。"""
    p, c = prev or "", cur or ""
    bp, bc = bare(p), bare(c)
    if len(bp) < 4 or not bc.startswith(bp) or len(bc) <= len(bp):
        return None
    k, i = 0, 0                            # 在原文里数过 len(bp) 个"字"：后面的就是接上的那截
    while i < len(c) and k < len(bp):
        if not re.match(_PUNCT, c[i]):
            k += 1
        i += 1
    return c[i:].strip(" ，,。.、") or None


# ---------------------------------------------------------------- AI 的回话里说了什么
# 嘴上答应改论文（这一轮却没打 #edit / #note）："我会一起记着改""排队去改""我改一下""交给后台改""两处一起改"
_CHANGE = re.compile(
    r"记着改|记下(?:来)?(?:再|去)?改|记上(?:了)?[，,]?\s*(?:排队|回头|待会|等会|稍后)?(?:去)?改|排队(?:去)?改|"
    r"记成一?条(?:修改|改稿|任务)|记成(?:改稿)?任务|"
    r"(?:交给|让)后台(?:去)?改|(?:一起|都|会|这就|马上|现在|先|来|去)(?:去)?改(?:一下|掉|过来|掉它)?(?:的)?(?=[，,。！!；;]|$)|"
    r"我(?:这就|马上|现在|先|来|去|会|再)?(?:去)?(?:改|调整|修改|精简|压缩|删|加)(?:一下|掉|上)(?!好|完|了吗|过)|"
    r"我(?:这就|马上|现在|先|来|去|会)(?:去)?(?:改|调整|修改|精简|压缩|删|加)(?!好|完|了吗|过)|"
    r"(?i:\bI(?:'ll|\s+will)\s+(?:go\s+ahead\s+and\s+)?(?:change|fix|edit|update|revise|rewrite|tweak|adjust|reword)\b)")
_CHANGE_NOT = re.compile(r"不|没|别|要不要|如果|的话|你说一声|散会后你|改好了|已经改|改完了|(?i:\bif\b|\bwhether\b|\bnot\b|n't)")


def promised_change(spoken: str):
    """AI 这一轮嘴上答应了改论文：返回那处 match（取第一处），没有就 None。
    "要改的话你说一声，我记上""先不改""改好了"这种不算（有条件 / 否定 / 说的是已经做完的）。"""
    s = spoken or ""
    for m in _CHANGE.finditer(s):
        a = max(s.rfind(c, 0, m.start()) for c in "。！？!?；;\n")
        b = max(s.rfind(c, 0, m.start()) for c in "，,")
        after = re.split(r"[，,。！？!?；;\n]", s[m.end():], maxsplit=1)[0][:3]
        clause = s[max(a, b) + 1:m.start()] + m.group(0) + after
        whole = s[a + 1:m.start()]                 # 这一整句前面："要改的话你说一声，我记上改"
        if _CHANGE_NOT.search(clause) or re.search(r"的话|如果|要是|你说一声|(?i:\bif\b)", whole):
            continue
        return m
    return None


def promise_object(spoken: str, m) -> str:
    """"第一段的转折和这句的措辞，我会一起记着改" → "第一段的转折和这句的措辞"（答应改的是什么）。"""
    s = spoken or ""
    a = max(s.rfind(c, 0, m.start()) for c in "。！？!?；;\n")
    body = s[a + 1:m.start()]
    body = re.sub(r"[，,]?\s*(?:我|这条|这个|这处|这两处|都|也|会|先|就|一起)*$", "", body)
    return clean_clause(body)


# 请对方接着说："你接着说""你说""后面还没说完"
_INVITE = re.compile(r"你接着说|接着说吧|你继续说|继续说吧|你先说完|你说完|你慢慢说|还没说完|你说吧|你先说|我听着|我等你|"
                     r"你说[。！!]?\s*$|(?i:\bgo\s+ahead\b|\bkeep\s+going\b|\bfinish\s+your\s+thought\b|\bi'?m\s+listening\b)")


def invites_continuation(spoken: str) -> bool:
    return bool(_INVITE.search((spoken or "").strip()))


# "你仔细想一想""再想想呢""好好想想""让后台想一下"：交给后台深想（"我再想想"是自己在想，不算；"深层模型在改"是要改稿，不算）
_DEEPER = re.compile(
    r"(?:你|您)(?:再|好好|仔细|认真|深入|重新)+(?:地)?(?:想|思考|考虑)(?:一)?(?:想|下|一下)?"
    r"|(?<![我他她咱])(?:再|好好|仔细|认真)+(?:地)?想(?:一)?想(?:呢|吧|看)"
    r"|(?:让|交给|转给)(?:后台|深层(?:思考)?(?:的)?(?:模型|Claude)?|Opus)(?:的)?(?:Claude|模型)?(?:去|来)?(?:想|思考|好好想|仔细想)"
    r"|(?i:\bthink\s+(?:it\s+|this\s+|that\s+)?(?:over|through|harder|again)\b|\bthink\s+(?:more\s+)?carefully\b|\bthink\s+deeper\b)")


def wants_deeper(text: str) -> bool:
    return bool(_DEEPER.search(text or ""))


# 对方纠正 AI 对他要求的理解；AI 回话里认了理解错
_CORRECT = re.compile(r"我不是说(?![了过])|我不是要|我不是让|不是让你|不是要你|不是说要|不是这个意思|我的意思是|你理解错|你理解的不对|"
                      r"你理解反|你搞错了|你听错了|你弄错了|说的不是|不是那样|"
                      r"(?i:\bthat'?s\s+not\s+what\s+i\s+(?:meant|said|asked)\b|\bi\s+didn'?t\s+mean\b|\bwhat\s+i\s+meant\b"
                      r"|\bnot\s+what\s+i\s+asked\b|\byou\s+misunderstood\b)")
_ADMIT = re.compile(r"理解错|理解偏|理解反|搞错了你的意思|听错了|(?i:\bI\s+misunderstood\b|\bgot\s+(?:it|that)\s+wrong\b)")


def corrects(text: str, reply: str = "") -> bool:
    """对方在纠正 AI 对他要求的理解（只看人说的话）。reply 不再算数：快嘴说"我重新记一下"不等于对方纠正了它。"""
    return bool(_CORRECT.search(text or ""))


_CORRECT_STRONG = re.compile(r"我不是说(?![了过])|我不是要|我不是让|不是让你|不是要你|不是说要|不是这个意思|你理解错|你理解的不对|"
                             r"你理解反|你搞错了|你听错了|你弄错了|说的不是|不是那样|"
                             r"(?i:\bthat'?s\s+not\s+what\s+i\s+(?:meant|said|asked)\b|\bi\s+didn'?t\s+mean\b"
                             r"|\bnot\s+what\s+i\s+asked\b|\byou\s+misunderstood\b)")


def strongly_corrects(text: str) -> bool:
    """明着说"我不是说……""你理解错了"（单说"我的意思是"不算：那也常用来补充一件新的事）。"""
    return bool(_CORRECT_STRONG.search(text or ""))


def admits(reply: str) -> bool:
    """AI 回话里认了是自己理解错了。"""
    return bool(_ADMIT.search(reply or ""))


# ---------------------------------------------------------------- 念出口前：内部上下文的名字不念
_LABEL = re.compile(r"【[^】]{0,40}】|软件现状|这一轮的上下文|对方这句话|对话情况|<<<[^>]*>>>")


def scrub_labels(sentence: str) -> str:
    """去掉说漏出来的内部名字（"【软件现状】里没写"）：带着它的那一小截（逗号之间）整截不念，剩下的照念。
    整句都是它就返回空串（这句不念）。"""
    s = sentence or ""
    if not _LABEL.search(s):
        return s
    end = re.search(r"[。！？!?；;…，,]+\s*$", s)       # 句末标点原样留着（流式先吐出去的半句以逗号结尾）
    tail = end.group(0) if end else ""
    body = s[:end.start()] if end else s
    parts = re.split(r"([，,；;])", body)
    keep = []
    for i in range(0, len(parts), 2):
        c = parts[i]
        if _LABEL.search(c):
            continue
        keep.append(c + (parts[i + 1] if i + 1 < len(parts) else ""))
    out = "".join(keep).strip().rstrip("，,；; ")
    out = re.sub(r"[【】]", "", out)
    return (out + (tail or "。")) if bare(out) else ""


def scrub_text(text: str) -> str:
    """一整段要念的话：每句过一遍 scrub_labels（没说漏就原样返回，不动标点）。"""
    t = text or ""
    if not _LABEL.search(t):
        return t
    parts = [p for p in re.split(r"(?<=[。！？!?；;…])", t) if p]
    return "".join(scrub_labels(p) for p in parts).strip()


# AI 回话里反问对方要改哪一处 / 不同意这个改法：这一轮不替它记改稿（说清了再记）
_ASK_BACK = re.compile(r"(?:是不是|是指|你是说|你指的|指的是|哪一|哪个|哪句|哪段|哪一处|对吧|对吗|是吗|行吗|可以吗|要不要|还是)[^。！？!?]*[？?]\s*$"
                       r"|(?i:\b(?:do\s+you\s+mean|which\s+(?:one|sentence|part)|should\s+I)\b[^?]*\?\s*$)")
_DISAGREE = re.compile(r"不妥|不建议|不太建议|站不住|不能这么写|不能这样写|不该这么改|会有问题|对不上|倾向不|没必要|不用改|不需要改|别改|别写|不太对|不是这样|(?i:\bI\s+wouldn'?t\b|\bnot\s+a\s+good\s+idea\b)")


def asked_back(spoken: str) -> bool:
    return bool(_ASK_BACK.search((spoken or "").strip()))


def disagrees(spoken: str) -> bool:
    return bool(_DISAGREE.search(spoken or ""))


def similar(a: str, b: str) -> float:
    """两段话的相像程度（汉字两两一组的重合，0~1）：同一条软件意见别记两遍。"""
    def grams(s):
        s = bare(s).lower()
        return {s[i:i + 2] for i in range(len(s) - 1)}
    x, y = grams(a), grams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / min(len(x), len(y))


# ---------------------------------------------------------------- 2026-11-21 组会的几类毛病
# 会议总纲："咱们这次会议还是改这个第三章跟第五章……一次性把第三章跟第五章全部改好"：说的是这场改哪些，不是哪句怎么改。
# 不记任务（T1 被记成改稿，工人说"没有具体修改点"，进了修改清单），记进会议 meta 的 agenda
_AGENDA_WHEN = re.compile(r"这次|今天|这场|本次|这回|这一次|今儿")
_AGENDA_SCOPE = re.compile(r"第\s*[一二三四五六七八九十两\d]+\s*(?:章|部分)|前\s*[一二三四五六七八九十两\d]+\s*章|"
                           r"[一二三四五六七八九十两\d]+\s*(?:到|至)\s*[一二三四五六七八九十两\d]+\s*章|整篇|全篇|通篇|"
                           r"(?:引言|摘要|结论|方法|实验|相关工作|讨论)(?:部分|那一?章|这一?章)")
_AGENDA_VERB = re.compile(r"(?:改|修改|过一遍|过一下|看一下|看看|讨论|处理)(?![得的过了])")
# 说到了具体一处（某段、某句、原文、某个词）：不是总纲
_AGENDA_FINE = re.compile(r"[这那上下首末前后]一?(?:段|句)|第\s*[一二三四五六七八九十两\d]+\s*(?:段|句)|原文|这个词|这句话|"
                          r"(?<![A-Za-z\d.])\d+\.\d+")


def _plain_sents(text: str) -> list[str]:
    return [p for p in re.split(r"(?<=[。！？!?；;])", text or "") if p.strip()]


def agenda_text(text: str) -> str:
    """这段话里说"这场改哪些"的那几句（这次 / 今天 / 这场 + 改 / 过一遍 + 第几章 / 整篇，没说具体怎么改）；没有返回空串。
    紧跟着的、又说了这几章的一句（"一次性把第三章跟第五章全部改好"）也算。"""
    out, on = [], False
    for s in _plain_sents(text):
        plain = (_AGENDA_SCOPE.search(s) and _AGENDA_VERB.search(s) and not has_edit(s) and not ASK_CUE.search(s)
                 and not _AGENDA_FINE.search(s))
        if plain and (_AGENDA_WHEN.search(s) or on):
            out.append(clean_clause(s))
            on = True
        else:
            on = False
    return "。".join(x for x in out if x)


def agenda_only(text: str) -> bool:
    """整段话只是在说这场的总纲：有总纲那几句，别的句子里没有改法、没点具体一处。"""
    if not agenda_text(text):
        return False
    for s in _plain_sents(text):
        if has_edit(s) or _AGENDA_FINE.search(s):
            return False
    return True


def agenda_scope(text: str) -> str:
    """总纲里说的范围，按出现的顺序、去重："第三章和第五章"。"""
    seen = []
    for m in _AGENDA_SCOPE.finditer(text or ""):
        x = re.sub(r"\s+", "", m.group(0))
        if x not in seen:
            seen.append(x)
    return "和".join(seen[:4])


# 刚派出去就叫停："现在不要派任务，现在不要派任务。别急"（T1 已经在跑，照样跑完进了清单）。整句只是叫停，别的话几乎没有才算
_STOP_DISPATCH = re.compile(r"(?:现在|先|暂时|还)?(?:不要|别|甭)(?:派|发|记|改|动)(?:任务|了|它|这条|那条|出去)?|先不(?:派|改|动|记)|"
                            r"等一下|等一等|等等|别急|先停|停一下|停停")
_STOP_REST = re.compile(r"[呃嗯啊哦诶哎唉不对好吧呀哈，,。.！!？?、\s]|现在|那个|就是|我|你|先|还|再|任务|这条|那条|刚才|一下")


def retracts(text: str) -> bool:
    """这句只是在叫停刚派的改稿："先别派""不要派任务""别改了""等一下""别急"。叫停之外还说了别的（"不要改这里，改成……"）不算。"""
    s = text or ""
    if not _STOP_DISPATCH.search(s):
        return False
    return len(_STOP_REST.sub("", _STOP_DISPATCH.sub("", s))) <= 4


# 让后台看看到底怎么改、想办法："你把我说的这些提交给后台，让他看看这个地方到底应该怎么改"。改法还没定：只深想，不记改稿
# （T10 在对方认可深想给的改法之前就把论文改了）
_ADVICE = re.compile(r"(?:提交|交|转|发)给(?:后台|Claude|克劳德)|让(?:后台|它|他|Claude|克劳德)(?:去|来|再)?(?:看看|看一下|想想|想一想|想个?办法|琢磨)|"
                     r"想(?:个|一个)?办法|看看(?:到底|应该|该)?(?:怎么|如何)改|(?:到底|应该|该)怎么改|怎么改(?:比较|更)?好", re.I)


def wants_advice(text: str) -> bool:
    return bool(_ADVICE.search(text or ""))


def advice_on_paper(text: str, segs: list[dict] | None = None) -> bool:
    """要后台看看论文这处怎么改（wants_advice，而且这段话里有改论文的那截）：整段是在讨论论文的写法。
    里面冒出来的"有些bug"（[19:20] 识别出来的，说的是论文里的方法）不算软件意见。"""
    if not wants_advice(text):
        return False
    segs = split_requests(text) if segs is None else segs
    return any(g["kind"] == "paper" and g["edit"] for g in segs)


# 单说一句"改好了""好了吗""改完了"：有任务在排队、在改的时候多半是在问（语音识别吃了问号）。"我改好了""好了"不算
_BARE_STATUS = re.compile(r"^(?:嗯|呃|那|那个|现在|都)?[，,\s]*(?:改好了|改完了|弄好了|好了没有?|改好没有?|改完没有?|改好了没有?|"
                          r"改完了没有?|好了吗|改好了吗|改完了吗|改了吗|改到哪了?)[吗没么呢]?[。？?！!，,\s]*$")


def bare_status_ask(text: str) -> bool:
    return bool(_BARE_STATUS.match((text or "").strip()))


# 对上一条改稿的补充、加限制："结论是要整张都跳过的……这些冒号得保留。只换标点"（T21 补 T20）、
# "要不还是换个方法拆吧，我给你打回来"（T4 补 T3）
_SUPPLEMENT = re.compile(r"打回来|重来|重做|换个(?:方法|思路|拆法|改法)|换种(?:方法|拆法|改法)|还是(?:要|得)|再加(?:上|一条|一句)?|补充一下|"
                         r"另外(?:还)?(?:要|得)|范围|除了|跳过|保留|只(?:扫|换|改|动|拆|删|留)|别的(?:地方)?(?:都)?不(?:用|要)|"
                         r"(?:都)?不用(?:动|管|改)|照这个|按这个|前面那条|刚才那条|上一条")
# 快嘴的复述里说了是在改前面那条："前面那条扫描的范围我照这个改"
_REFERS_BACK = re.compile(r"前面那条|上一条|刚才那条|刚记的那条|前一条|那条(?:扫描|任务)?的范围|照这个改|并进(?:去|那条)|一起(?:改进去|并进去)")


def supplements(text: str) -> bool:
    return bool(_SUPPLEMENT.search(text or ""))


def refers_back(spoken: str) -> bool:
    return bool(_REFERS_BACK.search(spoken or ""))


_SEC_NO = re.compile(r"(?<![\d.])(\d+(?:\.\d+)+)(?![\d.])|([一二三四五六七八九十\d]+)\s*点\s*([一二三四五六七八九十\d]+)")


def section_no(text: str) -> str | None:
    """话里、标题里第一个小节号："3.1 拆分重做" → "3.1"；"三点一" → "3.1"。没有 None。"""
    m = _SEC_NO.search(text or "")
    if not m:
        return None
    if m.group(1):
        return m.group(1)
    from .normalize import cn_number
    a, b = cn_number(m.group(2)), cn_number(m.group(3))
    return f"{a}.{b}" if str(a).isdigit() and str(b).isdigit() else None
