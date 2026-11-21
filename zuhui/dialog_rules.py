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
    """这一段里有没有明着要改的那句：是改法（edit_request），不是在说已经改过的，而且这句和后面两句里没在讨对方同意。
    "我把这个，会用到。的前面那个逗号删掉了"（识别断在半截上，接回去看）：说的是他自己已经改了，不算（2026-11-21 下午 T6）。"""
    sents = _split_sents(text)
    for i, x in enumerate(sents):
        if (edit_request(x) and not _DONE.search(x) and not own_done(x)
                and not _SEEK_AGREE.search("".join(sents[i:i + 3]))):
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
    parts = _glue([p for p in re.split(r"(?<=[。！？!?；;])", text or "") if p.strip()])   # "会用到。的前面……"接回一句
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
                     and any(edit_request(s) for s in _split_sents(t)))
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
        sents = _split_sents(g["text"])
        if kind == "paper":
            bits = [_edit_bit(c, sents[i - 1] if i else "") for i, c in enumerate(sents) if has_edit(c)]                 or [c for c in sents if PAPER_LOC.search(c)] or sents
            # 半截话（"的前面那个逗号删掉了"）不当标题：去掉开头的虚字，太短的不要（2026-11-21 下午 T6）
            body = "，".join(x for x in (tidy_title(c) for c in bits[:2]) if x)
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


# AI 刚被打断，对方叫它把话说完："继续，继续。""你你你继续说，我没有打断你。""接着说吧""说完""然后呢"
# （2026-11-21 [18:10]：这两句一句被当成收尾确认，一句让它回了"刚才那句我说完了"）。"继续改""继续看"是别的事，不算
_NOT_CUT_YOU = re.compile(r"我?(?:没有?|不是)(?:在)?(?:打断你|打断|跟你说|说你)|(?:刚才)?不是(?:在)?(?:跟你说|说你)(?:的)?")
_RESUME_FILL = re.compile(r"可以|没事|[嗯呃啊哦诶哎好的行对那就你吧呀啦嘛呗，,。.！!？?、~～\s]")
_RESUME = re.compile(r"^(?:(?:继续|接着)(?:说|讲)?(?:下去)?|往下(?:说|讲)|(?:先|把话)?说完|说下去|讲下去|然后呢|接着来)+$")
_RESUME_EN = re.compile(r"^(?:please\s+)?(?:go\s+on|continue|keep\s+going|carry\s+on|finish(?:\s+what\s+you\s+were\s+saying)?)"
                        r"(?:\s*,?\s*please)?[.!]?$", re.I)


def wants_resume(text: str) -> bool:
    s = (text or "").strip()
    if _RESUME_EN.match(s):
        return True
    core = _NOT_CUT_YOU.sub("", s)
    said_not_cut = core != s
    core = _RESUME_FILL.sub("", core)
    if not core:
        return said_not_cut                    # 只说了"我没有打断你"：也是叫它接着说
    return len(core) <= 16 and bool(_RESUME.match(core))


# 只说了"做吧"、没说做什么："删了吧，删了吧。""删吧。""我没有打断你，你去删啊。""那你改吧""加上吧"
_GO_FILL = re.compile(r"可以|没问题|[嗯呃啊哦诶哎好的行对那就你去快赶紧直接吧呀了啦嘛呗哈，,。.！!？?、~～\s]")
_GO_VERB = re.compile(r"^(?:删(?:掉|除)?|改(?:掉)?|加(?:上)?|补(?:上)?|换(?:掉)?|写(?:上)?|去掉|拿掉|弄|做|办)+$")


def bare_go_ahead(text: str) -> bool:
    core = _GO_FILL.sub("", _NOT_CUT_YOU.sub("", text or ""))
    return 0 < len(core) <= 8 and bool(_GO_VERB.match(core))


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
    """AI 回话里反问了对方：问句在最后，或者在中间（"你是说……你已经自己删了，对吧？那我这边……"，2026-11-21 下午 T6
    就是这种，兜底照样记了一条）。"""
    s = (spoken or "").strip()
    if _ASK_BACK.search(s):
        return True
    return any(_ASK_BACK.search(x.strip()) for x in re.split(r"(?<=[？?])", s)[:-1] if x.strip())


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
    """这句只是在叫停刚派的改稿："先别派""不要派任务""别改了""等一下""别急"。叫停之外还说了别的（"不要改这里，改成……"）不算。
    点名不要某条任务（"ok，这个标点任务不用改了，不用改了，就这样吧"）也算（named_cancel；2026-11-21 下午：叫停之外的字
    超过四个，没认出来，快嘴又把它记成了一条"撤销……的任务"排在后面，被点名的那条照样改完）。"""
    s = text or ""
    if named_cancel(s) is not None:
        return True
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
_REFERS_BACK = re.compile(r"前面那条|上一条|刚才那条|刚记的那条|前一条|那条(?:扫描|任务)?的范围|照这个改|并进(?:去|那条)|一起(?:改进去|并进去)"
                          # 2026-11-21 组会 [18:03:23]"「7 结论 第1段 改成先问后答」那条还在改，我去改它的写法"
                          r"|那条(?:还|正)在(?:改|排队)|(?:去|再|一起)?改它的(?:写法|改法)|并到那条|在那条里(?:一起)?改")


def supplements(text: str) -> bool:
    return bool(_SUPPLEMENT.search(text or ""))


def refers_back(spoken: str) -> bool:
    return bool(_REFERS_BACK.search(spoken or ""))


# 对方手改过一段，快嘴却说看不到（2026-11-21 组会 [18:29:11]"我这边看不到你改成什么样，论文全文里是你改之前的版本，你念一下"）
_DENY_HAND = re.compile(r"看不到(?:你|您)?(?:刚才?|刚刚)?(?:改|修改)|(?:看不到|不知道|不清楚)(?:你|您)?(?:改|写)成(?:了)?什么样|"
                        r"(?:全文|论文|稿子)(?:里|全文里)?(?:还)?是(?:你|您)?改之前|(?:你|您)?念一下(?:你|您)?改(?:后|过|好)|"
                        r"(?i:\bcan'?t\s+see\s+(?:your|the)\s+(?:edits?|changes?|revision)\b)")


def denies_hand_edit(sentence: str) -> bool:
    return bool(_DENY_HAND.search(sentence or ""))


def names_task(spoken: str, title: str) -> bool:
    """AI 的话里点了这条任务的标题（"「7 结论 第1段 改成先问后答」那条"，或者标题原样出现在话里）。"""
    t, s = bare(title), bare(spoken)
    if len(t) < 6:
        return False
    return t in s or any(bare(q) == t for q in re.findall(r"「([^」]{2,80})」", spoken or ""))


# ---------------------------------------------------------------- 软件替 AI 补记的改稿：标题不照抄口语
# 2026-11-21 组会：T6"不用点结论，不用点结论"、T7"你你你这个总起距为什么要扯到图三删掉啊"、T12"第一节 我说了要扩写的"。
# 标题按 AI 的复述写；复述里没有改法，就按人话里的改法写，去掉结巴、重复、口头禅，前面带上在哪一节
_STUTTER = re.compile(r"([你我他她它这那就呃嗯啊哦或])\1+")
_ORAL_LEAD = re.compile(r"^(?:(?:我(?:说了|说过|是说|刚才说了?|的意思是)|你(?:应该|得|要|就)|你|我|那么|就是|其实|然后|而且|所以|"
                        r"这个|那个|呃|嗯)[，,\s]*)*(?:要|得|应该)?")
_WHY_EDIT = re.compile(r"为什么(?:要|会|还|又)?|干嘛(?:要)?|怎么(?:还|又)")
_ORAL_END = re.compile(r"(扩写|展开|删掉|删了|删除|删去|去掉|改掉|拿掉|补上|加上|精简|压缩|合并|拆开)(?:的|了)+$")
_ACK_LEAD = re.compile(r"^(?:(?:好的?|行|嗯|对|明白(?:了)?|了解|收到|可以|没问题|OK|ok)[，,\s]*)+")
_ME_DO = re.compile(r"^(?:你是说|你的意思是|你是想|就是说)?\s*(?:(?:那|就)?我(?:这就|马上|现在|先|来|去|会|就|再)?(?:把|将)?)?")
_RESTATE_ASK = re.compile(r"[？?]|[吗呢么][。！!]?\s*$|要不要|是不是|对不对|行不行|可以吗|好不好")
_RESTATE_NOT = re.compile(r"建议|倾向|如果|的话|还是|或者|你定|你看|已经|改好了|改完了|不用改|不改|先不")
_ACK_TAIL = re.compile(r"记下|记上|排队|交给后台|改完(?:了)?(?:再)?跟你说|这就去改|马上改|去改|我改一下")
_TASK_QUOTE = re.compile(r"「[^」]{1,80}」(?:那条|这条|那一条|这一条)?")


def oral_free(s: str) -> str:
    """口语原话 → 能当标题的说法："你你你" → "你"，重复的半句只留一次，去掉开头的口头禅、人称和句末语气词；
    "X 为什么要扯到 Y 删掉" → "X 把 Y 删掉"。"""
    s = _STUTTER.sub(r"\1", clean_clause(s or ""))
    out = []
    for p in re.split(r"[，,]", s):
        p = p.strip()
        if p and not (out and bare(p) == bare(out[-1])):
            out.append(p)
    s = _ORAL_LEAD.sub("", "，".join(out)).strip(" ，,")
    s = re.sub(r"[啊呀吧呢嘛哈]+$", "", s).strip(" ，,")
    s = _ORAL_END.sub(r"\1", s)                    # "我说了要扩写的" → "扩写"
    m = _WHY_EDIT.search(s)
    e = EDIT_CUE.search(s, m.end()) if m else None
    if m and e:
        obj = re.split(r"扯到|提到|放到|写到|说到|放进|写进|到|把|将", s[m.end():e.start()])[-1].strip()
        s = s[:m.start()].rstrip(" ，,") + (f"把{obj}" if obj else "") + s[e.start():]
    return s if len(bare(s)) >= 2 else ""


_NTH = r"第\s*[一二三四五六七八九十两\d]+\s*(?:段|节|章|条|句|部分)|[（(]\s*\d{1,2}\s*[）)]\s*(?:段|条)?"


def where_of(text: str) -> str:
    """话里说的是哪一处："五点二的第一节" → "5.2 第一节"，"5.2 第（1）条" → "5.2 第（1）条"；只说了小节号就是它；
    都没有用 PLACE_FULL（"引言""第三段"）；没有返回空串。"""
    t = text or ""
    m = _SEC_NO.search(t)
    sec = section_no(t)
    if m and sec:
        k = re.match(r"\s*节?\s*(?:的|里的?|里面的?)?\s*(" + _NTH + r"|第\s*[（(]\s*\d{1,2}\s*[）)]\s*(?:段|条|节))",
                     t[m.end():m.end() + 12])
        return (sec + " " + re.sub(r"\s+", "", k.group(1))) if k else sec
    loc = PLACE_FULL.search(t)
    return loc.group(0).strip() if loc else ""


def restated_edit(spoken: str) -> str:
    """AI 回话里复述的改法（"好，我把 5.2 开头那句删掉。我这就排队去改。" → "5.2 开头那句删掉"）：不是在问、不是在提议、
    不是在说已经改过的；点了任务标题的那截（"「…」那条还在改"）不算。没有返回空串。"""
    s = _TASK_QUOTE.sub("", spoken or "")
    for x in _split_sents(s):
        x = x.strip()
        plain = re.sub(r"“[^”]*”", "", x)            # 引号里念的原文不算
        if not has_edit(x) or _RESTATE_ASK.search(plain) or _RESTATE_NOT.search(plain):
            continue
        parts = [p.strip() for p in re.split(r"[，,]", x.rstrip("。！!；;"))]
        while parts and _ACK_TAIL.search(parts[-1]) and not has_edit(_ACK_TAIL.sub("", parts[-1])):
            parts.pop()                            # "我记下了""排队去改"：不是改法
        body = _ME_DO.sub("", _ACK_LEAD.sub("", "，".join(p for p in parts if p))).strip(" ，,：:")
        body = re.split(r"[：:]", body, maxsplit=1)[0].strip() if len(body) > 30 else body
        if len(bare(body)) >= 4 and has_edit(body):
            return body
    return ""


def edit_title(words: str, spoken: str = "", n: int = 40) -> str:
    """软件替 AI 补记的改稿（漏记、嘴上答应了没打标签）的标题：AI 复述了改法就用复述；没有就按人话里说怎么改的那一两截，
    去掉口语（oral_free），前面带上在哪一节（人话里没说就用 AI 回话里说的）。都取不出来返回空串。"""
    r = restated_edit(spoken)
    if r:
        return short(r, n)
    bits = []
    for g in split_requests(words):
        if g["kind"] != "paper":
            continue
        sents = _split_sents(g["text"])
        for i, c in enumerate(sents):
            if has_edit(c):
                b = oral_free(_edit_bit(c, sents[i - 1] if i else ""))
                if b and all(bare(b) != bare(x) for x in bits):
                    bits.append(b)
        if len(bits) >= 2:
            break
    body = "，".join(bits[:2]) or oral_free(title_from_words(words, n=80))
    if not body:
        return ""
    place = where_of(words) or where_of(_TASK_QUOTE.sub("", spoken or ""))
    if place and not PLACE.search(body):
        body = f"{place} {body}"
    return short(body, n)


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


# ---------------------------------------------------------------- 2026-11-21 下午那场会的几类毛病
# 1. 交给深想的题目：真正要想的那个问题，不是"你让后台想一个答案"这句原话
_Q_CONTENT = re.compile(r"什么|是啥|为什么|怎么|如何|哪|多少|几[个处次条种类节段]|有没有|是不是|要不要|该不该|需不需要|能不能|可不可以|"
                        r"会不会|意义|区别|含义|定义|(?i:\b(?:what|why|how|which|whether|should)\b)")
_Q_EMPTY = re.compile(r"你(?:觉得|看)?(?:怎么看|怎么样|如何)|你觉得呢|你看呢|你说呢|怎么说呢|对吧|是吧|对不对|好不好|行不行|可以吗|"
                      r"我想想|我再想想")
_Q_HANDOFF = re.compile(r"后台|深层|告诉我|想一?个答案|想个办法|想一想|想想|仔细想|好好想|认真想|(?i:\bthink\b)")


def _glue(parts: list[str]) -> list[str]:
    """断句断在半截上的接回去：识别常把"会用到的前面那个逗号"断成"会用到。的前面那个逗号"，"的……"开头的那截并回上一句。"""
    out: list[str] = []
    for p in parts:
        if out and re.match(r"\s*[的地得了着过吗呢吧啊呀嘛]", p):
            out[-1] = out[-1].rstrip(" 。.！!；;") + p.strip()
        else:
            out.append(p)
    return out


def _split_sents(text: str) -> list[str]:
    return _glue([p for p in re.split(r"(?<=[。！？!?；;])", text or "") if p.strip()])


# 识别切出来的半截话（2026-11-21 组会 [18:13:36]"这个是不是写的太，就是你后面。"）：不当问题
_FRAG = re.compile(r"(?:太|很|比较|有点|就是|然后|那个|这个|是不是|也|还)[，,]|(?:就是|然后|那个|这个|就是你|你后面)$")


def _q_clause(s: str) -> str:
    """一句话里有内容的问题（去掉口头禅、空问、句末语气词）；不是问题、没内容、是半截话返回空串。"""
    if not ASK_CUE.search(s):
        return ""
    c = clean_clause(_Q_EMPTY.sub("", s))
    c = re.sub(r"^(?:那么|那|你|您)+(?=\S)", "", c)
    c = re.sub(r"[啊呀呢吧嘛哈]+$", "", c).strip(" ，,。.")
    if len(bare(c)) < 6 or not (_Q_CONTENT.search(c) or PAPER_LOC.search(c) or has_edit(c)) or _FRAG.search(c):
        return ""
    return c


def question_of(text: str, n: int = 60) -> str:
    """一段人话里真在问的那个问题（交给深想时显示、交过去用）：带问号或问话词、有实际内容的头一句，去掉口头禅、
    "你怎么看"这种空问；紧跟着的下一句也在问、问的不是同一件事，接上（"后面是不是写的太少了？是不是应该稍微展开一点？"）；
    这句没点位置、整段话点了（"五点二的第一节"），把位置带在前面。没有返回空串。
    "我先叫个啥，我先啥？"这种没内容的、"这个是不是写的太，就是你后面"这种半截话不算。"""
    sents = _split_sents(text)
    for i, s in enumerate(sents):
        c = _q_clause(s)
        if not c:
            continue
        c2 = _q_clause(sents[i + 1]) if i + 1 < len(sents) else ""
        if c2 and not same_question(c, c2) and len(c) + len(c2) <= 56:
            c = f"{c}，{c2}"
        loc = where_of(text or "")
        if loc and not PLACE.search(c):
            c = f"{loc} {c}"
        return short(c, n)
    return ""


_PROPOSE = re.compile(r"我(?:可以|建议|倾向于?|打算|准备)(?:在|把|给)?|(?:可以|不如)(?=补|加|改|删|换|写|拆|挪|展开)")


def proposal_of(text: str) -> str:
    """AI 一句话里提的改法、倾向（"我可以补一句：五种……" → "提议补一句：五种……"；"我倾向不全部讲" → "倾向不全部讲"）；没有返回空串。"""
    for x in _split_sents(text):
        m = _PROPOSE.search(x)
        if not m:
            continue
        body = x[m.end():].strip(" ，,。！!")
        body = re.sub(r"[，,]?\s*(?:你(?:要|看|觉得)|要不要|行不行|可以吗|你定)[^，,]*$", "", body).strip(" ，,")
        if len(bare(body)) >= 4:
            return ("倾向" if "倾向" in m.group(0) else "提议") + short(body, 40)
    return ""


def think_question(user_q: str, ai_reply: str = "", n: int = 90) -> str:
    """对方只说"你再想想""想想"：要想的问题 = 他前面问的（user_q）加上 AI 上一句的提议（"（我前面提议补一句……）"）；
    他前面没问：用 AI 上一句的问话（"你要我这样展开吗" → "要不要这样展开"），前面带上在哪一节。都没有返回空串。"""
    q = (user_q or "").strip()
    prop = proposal_of(ai_reply)
    if not q:
        q = question_of(ai_reply)
        q = re.sub(r"(?:你)?要(?:我|不要我)(.+?)[吗么]?$", r"要不要\1", q)
    if prop and bare(prop[2:]) not in bare(q):
        q = f"{q}（我前面{prop}）" if q else (where_of(ai_reply) + f" 我前面{prop}，要不要这样改").strip()
    return short(q, n)


def deep_question(text: str) -> str:
    """"你仔细想想，要不要删掉 3.2 第二段？"：这句里除了叫后台想的那几截，还问了有内容的问题，就是它；没有返回空串（从上文找）。"""
    keep = [c for c in re.split(r"(?<=[，,。！？!?；;])", text or "") if c.strip()
            and not _Q_HANDOFF.search(c) and not wants_deeper(c)]
    return question_of("".join(keep))


def same_question(a: str, b: str) -> bool:
    """两句是不是在问同一件事（"评测单元统计里的 SEG 是什么意思" 和 "就是 SEG 是什么东西？"）：
    有同一个英文词、缩写，或者去掉问话虚词后汉字两两一组重合一半以上。"""
    def lat(s):
        return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", s or "")} - {"the", "and", "what", "is", "ok", "okay"}
    if lat(a) & lat(b):
        return True
    junk = re.compile(r"什么|东西|意思|是不是|怎么|为什么|这个|那个|你的|我的|一下|就是|然后|你这|论文|里面|有没有")

    def grams(s):
        s = bare(junk.sub("", s or ""))
        return {s[i:i + 2] for i in range(len(s) - 1)}
    x, y = grams(a), grams(b)
    if not x or not y:
        return False
    k = len(x & y)
    return k >= 3 and k / min(len(x), len(y)) >= 0.5


# 2. 嘴上说"先不动""等你确认"：兜底不能再替它派任务，它自己打了 #edit 也不记（说的和做的要一致）。
# "别的地方先不动"（说的是范围）不算：只认"我先不动""等你确认""确认了再记"这几种
_HOLD_OFF = re.compile(r"等你(?:确认|定|拍板|想好|说清|再说)|(?:确认|定|想好|说清)(?:了|好)?(?:之后|以后)?(?:我)?再(?:记|改|动)|"
                       r"我(?:这边)?(?:就|先|暂时)?(?:先)?不(?:动|改|记)(?![成为])|(?:^|[，,。！!？?；;])\s*(?:好|行|嗯|那)?[，,\s]*(?:那)?"
                       r"(?:就)?先不(?:动|改|记)(?![成为])|不(?:会)?再动这")


_HOLD_SCOPE = re.compile(r"其余|别的|其他|其它|剩下|另外|以外|之外|别处")


def holds_off(spoken: str) -> bool:
    """"好，那我先不动，等你确认要补的写法再记"。说的是范围的（"其余我先不动""别的地方先不动"）不算。"""
    s = spoken or ""
    for m in _HOLD_OFF.finditer(s):
        a = max(s.rfind(c, 0, m.start()) for c in "，,。！!？?；;")
        if not _HOLD_SCOPE.search(s[a + 1:m.end()]):
            return True
    return False


def join_tail(prev: str, cur: str) -> str:
    """上一句没回就被这句接上了：这句只是半截（"会用到。"，几个字、不是回答、不是问话），按一句话接（逗号），
    不然照 join_words 各自成句。"""
    b = bare(cur)
    tail = (2 <= len(b) <= 8 and not ASK_CUE.search(cur or "") and not PAPER_LOC.search(cur or "") and not has_edit(cur)
            and not re.match(r"(?:好|对|是|嗯|行|可以|没问题|不|没|算了|等|统一|改|删|就这样|你|我|那|这|他|然后|还有|另外|而且|但是|(?i:ok))", b))
    if tail and prev:
        return (prev or "").strip().rstrip(" 。.！!，,") + "，" + (cur or "").strip()
    return join_words(prev, cur)


# 3. 点名不要某条任务："这个标点任务不用改了，就这样吧""标点那条撤掉""T5 不要了"
_CANCEL_VERB = re.compile(r"(?:你)?不用(?:再)?(?:改|做|弄|动)(?:了)?|不要(?:再)?(?:改|做|弄|动)?了|别(?:再)?(?:改|做|弄|动)了|"
                          r"不(?:改|做)了|撤(?:掉|了|回|销)|取消|作废|停掉|停下|拿掉|不用了")
_TASK_NOUN = re.compile(r"任务|那一?条|这一?条|[这那][个处]改动")
_TID = re.compile(r"(?<![A-Za-z0-9\-])[Tt]\s?(\d{1,3})(?![\d.])")
_CANCEL_HEAD = re.compile(r"^\s*(?:撤销|撤回|撤掉|取消|作废|别改|不要改|不用改)\s*(?:掉)?")


def named_cancel(text: str) -> str | None:
    """点名不要某条任务：返回指代那条的那几个词（"这个标点"，可能是空串），不是返回 None。
    说了要改成什么（"那条不用改成 X，改成 Y"）是在改要求，不算；只说"不用改了"、没说哪条任务的也不算（交给模型）。"""
    s = text or ""
    if _TO.search(s):
        return None
    v = _CANCEL_VERB.search(s)
    ref = _TASK_NOUN.search(s) or _TID.search(s)
    if not v or not ref or abs(ref.start() - v.start()) > 24:
        return None
    w = _CANCEL_VERB.sub(" ", s)
    w = re.sub(r"(?i)\bok(?:ay)?\b|就这样吧?|好吧|这样吧|算了", " ", _TASK_NOUN.sub(" ", w))
    w = re.sub(r"[这那](?:个|条|一条)|刚才|刚刚|刚记的|刚派的|[吧了呀啊]", " ", w)
    w = clean_clause(" ".join(w.split()))
    if len(bare(w)) > 16 or has_edit(w) or re.search(r"[？?]", w):
        return None                      # 撤之外还交代了别的事、还在问（"那条撤掉，2.3 第二段改成……"）：交给模型一起办
    return w


def cancel_head(title: str) -> str | None:
    """标题以"撤销""取消""别改"开头：返回后面那截（说的是哪条），不是返回 None。"""
    m = _CANCEL_HEAD.match(title or "")
    return (title or "")[m.end():].strip() if m else None


def cancel_title(title: str) -> bool:
    """快嘴把"这条不要了"写成了一条改稿："撤销全文标点按前后句改的任务""取消顿号那条"。
    "取消第二段的分号"这种是改论文（没说任务），不算；要和任务板上的标题对的交给调用方（Meeting._cancel_edit）。"""
    rest = cancel_head(title)
    return rest is not None and bool(_TASK_NOUN.search(rest))


def task_short_name(title: str) -> str:
    """念任务时的简称：标题第一个逗号前那截，最多十六个字（"全文标点按前后句改，别再拆成句号逗号" → "全文标点按前后句改"）。"""
    t = str(title or "").strip()
    head = re.split(r"[，,；;：:]", t, maxsplit=1)[0].strip()
    if len(bare(head)) < 4:
        head = t
    return head if len(head) <= 16 else head[:16]


def scrub_task_ids(text: str, names: dict, en: bool = False) -> str:
    """念出口的话里的任务编号（"T7 还在排队收尾"）换成那条的简称："「X」那条"。任务板上没有这个编号的不动
    （论文里的"T2 加权"之类）。"""
    if not text or not names:
        return text

    def rep(m):
        tid = f"T{int(m.group(1))}"
        if tid not in names:
            return m.group(0)
        n = names[tid]
        return (f'the "{n}" edit ' if n else "that edit ") if en else (f"「{n}」那条" if n else "那条")
    out = re.sub(_TID.pattern + r"\s*(?:这一?条|那一?条|任务|号)?", rep, text)
    return re.sub(r"\s+([，,。！!？?；;])", r"\1", out).replace("那条那条", "那条")


# 4. 兜底按原话起的标题：不能以"的"开头、不能是半截话；"我把……删掉了"是在说他自己已经改了，不是要改
_LEAD_JUNK = re.compile(r"^(?:[的地得了着过吗呢吧啊呀嘛和跟与及而并]|然后|就是|那个|这个)+")
_OWN_DONE = re.compile(r"^(?:我|我们)(?:已经|刚才?|刚刚|自己|都)*(?:把|给).{0,30}?(?:改|删|去|加|补|换|挪|移|调)[^，,。]{0,4}了[。！!]?$")
_VAGUE_VERB = re.compile(r"改一下|改改|改一改|调一下|调整一下|弄一下|处理一下|修一下|动一下")


def tidy_title(s: str) -> str:
    s = _LEAD_JUNK.sub("", clean_clause(s)).strip(" ，,。.、")
    s = re.sub(r"(删掉|删除|删去|去掉|改掉|拿掉|加上|补上|换掉)了$", r"\1", s)
    return s if len(bare(s)) >= 2 else ""


def own_done(sentence: str) -> bool:
    return bool(_OWN_DONE.search(clean_clause(sentence)))


def vague_edit(text: str) -> str | None:
    """改法、位置都没说的改稿要求（"把标点符号改一下"）：返回要改的东西（"标点符号"）；不是返回 None。
    说了哪一处（3.2、第二段、这句）、改成什么、删什么加什么、引了原话的都不算；"把这个改一下""那你改一下"指的是上文说过的那处，
    也不算。"""
    s = text or ""
    if not _VAGUE_VERB.search(s) or len(bare(s)) > 20:
        return None
    if PAPER_LOC.search(s) or _TO.search(s) or re.search(r"[“”「」\"]", s) or EDIT_CUE.search(_VAGUE_VERB.sub("", s)):
        return None
    m = re.search(r"把(.{1,10}?)(?:" + _VAGUE_VERB.pattern + ")", s)
    obj = clean_clause(m.group(1)) if m else ""
    if len(bare(obj)) < 2 or re.fullmatch(r"(?:这|那|它|他|这个|那个|这些|那些|这里|那里|这儿|那儿|刚才那个|上面那个|你说的那个)", bare(obj)):
        return None
    return obj


# 5. AI 刚问过（"要统一的话你点个头""要不要也改，你定"），他回一两个字就是在回答
_INVITE_ANSWER = re.compile(r"[？?]\s*$|点个?头|你(?:来)?定|你说一声|说一声|告诉我|你看(?:呢|行不行|行吗)|要不要|要的话|需要的话|"
                            r"行不行|可以吗|好不好|对吧|是吧|是不是|(?i:\blet\s+me\s+know\b|\byour\s+call\b|\bshould\s+I\b)")
_YES_NO = re.compile(r"(?:好|好的|好吧|行|可以|对|对的|是|是的|嗯|要|要的|统一|改|改吧|删|删吧|加|加吧|补|补吧|就这样|就这么改|"
                     r"按你说的|照你说的|没问题|同意|不|不用|不要|不改|别改|算了|先不|先不用|ok|okay|yes|no)+", re.I)


def invites_answer(spoken: str) -> bool:
    """AI 这句话最后在问对方、等他拍板（问号结尾，或者"要统一的话你点个头""你定"）。"""
    sents = [x for x in re.split(r"(?<=[。！？!?；;])", (spoken or "").strip()) if x.strip()]
    return any(_INVITE_ANSWER.search(x.strip()) for x in sents[-2:])


def answers_question(text: str, question: str = "") -> bool:
    """对方这句是不是在回答 AI 刚问的话：几个字的"好""可以""改吧""统一，统一"，或者拿问话里的词来答。"""
    b = bare(text).lower()
    if not b or len(b) > 12:
        return False
    if _YES_NO.fullmatch(b):
        return True
    core = re.sub(r"[吧啊呀了的嘛哈]+$", "", re.sub(r"(.{2,6}?)\1+", r"\1", b))
    return 2 <= len(core) <= 8 and core in bare(question).lower()
