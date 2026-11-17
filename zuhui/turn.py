# -*- coding: utf-8 -*-
"""判停：你说完了没有、该不该接话。两样现成的东西（调研和实测见 docs/research/turn-taking-lexicon.md）：

1. Pipecat smart-turn v3.2（BSD-2，8 MB int8 ONNX，原生支持中文）：听这一轮最后不超过 8 秒的音频，给出"说完了"的概率。
   本机 4 线程一次约 40 ms。特征提取用 Pipecat 自带的 numpy 版（turn_features.py，不用装 transformers）。
2. 词表 prompts/lexicon/turn_taking.json（Easy Turn / TEN / CrossWOZ / Pipecat 等公开数据整理，来源和许可证写在文件里）：
   think 你在想、backchannel 附和、close 收尾确认、greet 打招呼、bye 散会、hold 让学生等、unfinished_tail 半截话的结尾。

接法（ears._Collector._quiet_needed 调 need()）：
  整句是 hold / bye / greet → 0.5 秒就接；整句是 close / backchannel → 1.0 秒（学生本来就不接话，只是等等看你还说不说）；
  最后一个分句以 think / unfinished_tail 结尾（不是问句）→ 4 秒或等你再开口；
  smart-turn 说没说完（p < 0.5）→ 2.5 秒兜底；否则 1.2 秒。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("zuhui.turn")

ROOT = Path(__file__).resolve().parent.parent
LEXICON = ROOT / "prompts" / "lexicon" / "turn_taking.json"
MODEL = Path.home() / ".blurt" / "models" / "smart-turn-v3.2-cpu.onnx"
SR = 16000

_FINAL_PARTICLES = {"啊", "呀", "吧", "嘛", "呢", "哦", "哈", "嗯", "恩", "啦", "了", "哇", "噢"}
_PUNCT = re.compile(r"[\s，。！？、,.!?：:；;…“”\"'（）()~～]")
_CLAUSE = re.compile(r"[，。！？,.!?；;：:…]")


def _core(s: str) -> str:
    return _PUNCT.sub("", s or "").lower()


LEXICON_EN = ROOT / "prompts" / "lexicon" / "turn_taking_en.json"   # 英文那套，按 AMI / Switchboard 标注语料的高频说法整理（来源写在文件里）
# 英文"话没说完"的结尾词：按整词比（中文那套去了空格比结尾，"more data"会被当成以"a"结尾、多等 4 秒）
_EN_TAILS = {"and", "but", "or", "nor", "because", "cause", "so", "if", "than", "the", "a", "an", "to", "of", "for", "with", "from",
             "about", "into", "onto", "my", "your", "our", "their", "his", "her", "its", "um", "uh", "er", "like", "kindof", "sortof",
             "which", "whether", "plus", "versus", "vs", "between", "among", "through", "toward", "towards", "across", "against",
             "without", "within", "during", "via", "per", "whereas", "although", "unless", "very", "quite", "andthen", "butthen",
             "ormaybe", "suchas", "aswellas", "includingthe"}
# 句末是缩写的"we're""that's""let's"：后面一定还有话（按带撇号的原词比，免得"we're"去了撇号变成"were"）
_EN_CONTRACTION_TAILS = {"i'm", "you're", "we're", "they're", "it's", "that's", "there's", "here's", "what's", "let's", "i'd", "we'd",
                         "you'd", "they'd", "i'll", "we'll", "you'll", "they'll", "i've", "we've", "you've", "they've", "he's", "she's"}
# 句末常是小品词、不是半截话的（"let's move on""log in""that's it"）：不算
_EN_NOT_TAILS = {"in", "on", "at", "up", "out", "off", "over", "by", "it", "is", "that", "this", "then", "too", "well",
                 "imean", "youknow"}   # 句末的 "you know." "what I mean." 是说完了
_EN_DONE_PAIRS = {"thinkso", "hopeso", "guessso", "believeso", "saidso", "doso", "notso", "evenso", "ithinkso"}
_EN_SOFT = ["so", "then", "well", "um", "uh", "er", "oh", "ah", "and", "just", "alright", "okay", "ok", "yeah", "really", "very",
            "much", "all", "everyone", "guys", "everybody", "claude", "folks", "team"]   # "Thanks, Claude.""Hi Claude." 带名字照样算


class Lexicon:
    def __init__(self, path: Path = LEXICON):
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        self.sets: dict[str, list[str]] = {}
        for k in ("think", "backchannel", "close", "greet", "bye", "hold", "unfinished_tail"):
            items = sorted({_core(x) for x in (d.get(k) or []) if _core(x)}, key=len, reverse=True)
            self.sets[k] = items
        # 我们自己会上碰到过、词表里可能没有的几条（"在的在的"那次的"能听到吗"、"今天先到这"）
        extra = {"greet": ["能听到吗", "听得到吗", "听得见吗", "能听见吗", "在吗", "在不在", "喂"],
                 "bye": ["散会", "今天先到这", "今天就到这", "今天到这", "就到这里", "结束吧", "会议结束"],
                 "close": ["ok", "okay", "好的", "没问题", "行", "可以", "收到", "明白"],
                 "think": ["怎么说呢", "我再想想", "让我想想", "我想想"]}
        for k, xs in extra.items():
            self.sets[k] = sorted(set(self.sets.get(k, [])) | {_core(x) for x in xs}, key=len, reverse=True)
        # 英文半截话结尾：公开词表里的（um / uh / hmm 这类）加上面的连词介词；英文"在想"新补的 now / actually / anyway
        # 放在句末是说完了（"right now.""sounds fine actually."），不能连带算成半截话，所以在合并英文词表之前算
        self.en_tails = ({w for w in self.sets["unfinished_tail"] + self.sets["think"] if w.isascii()} | _EN_TAILS) - _EN_NOT_TAILS
        # 英文（2026-11-13）：公开词表里英文只有个位数几条（附和 9 条、收尾 9 条），英文会里"got it""sounds good"
        # "I see"都认不出来。英文那套单独放一份（turn_taking_en.json）；整句只由这些（加语气词）组成才算
        try:
            den = json.loads(Path(LEXICON_EN).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            den = {}
        for k in ("think", "backchannel", "close", "greet", "bye", "hold"):
            xs = [x for x in (den.get(k) or []) if isinstance(x, str)]
            self.sets[k] = sorted(set(self.sets.get(k, [])) | {_core(x) for x in xs if _core(x)}, key=len, reverse=True)

    def _covers(self, core: str, items: list[str], fillers: list[str] = ()) -> bool:
        """core 能不能完全由 items（加上可选的语气词 fillers）拼出来，而且至少真用上了一个 items 里的词。
        只由语气词拼成的不算（"and""and uh""那就"是话没说完，不是收尾）；只看"包含"也不算（"i think so"里有"hi"，不是打招呼）。"""
        if not core or not items:
            return False
        must = set(items)
        pool = list(dict.fromkeys(list(items) + list(fillers)))
        memo: dict[tuple[int, bool], bool] = {}

        def ok(i: int, got: bool) -> bool:
            if i == len(core):
                return got
            key = (i, got)
            if key in memo:
                return memo[key]
            memo[key] = False
            for w in pool:
                if w and core.startswith(w, i) and ok(i + len(w), got or w in must):
                    memo[key] = True
                    break
            return memo[key]
        return ok(0, False)

    def whole_kind(self, text: str) -> str | None:
        """整句只是打招呼 / 散会 / 让学生等 / 收尾确认 / 附和 / 在想：返回那一类，否则 None。太长的不算。"""
        c = _core(text)
        en = c.isascii()
        if not c or len(c) > (40 if en else 24):         # 英文同样一句话字母多
            return None
        asking = en and (text or "").strip()[-1:] in "?？"   # "Right?""Okay?"是在问，不算附和收尾（中文那边没这条，照旧）
        soft = ["那", "那就", "那么", "我们", "大家", "今天", "吧", "了", "啊", "哈", "呀", "嘛"] + (_EN_SOFT if en else [])
        if self._covers(c, self.sets["think"]):
            return "think"
        for k in ("greet", "bye", "hold"):
            if self._covers(c, self.sets[k], soft + self.sets["close"]):
                return k
        if not asking and self._covers(c, self.sets["close"] + self.sets["backchannel"], soft):
            return "close"
        return None

    def unfinished(self, text: str) -> bool:
        """最后一个分句以"然后""就是""所以""那个""怎么说呢"这类结尾：话没说完。问句不算。"""
        t = (text or "").strip()
        if not t or t[-1:] in "？?":
            return False
        clauses = [c for c in _CLAUSE.split(t) if _core(c)]
        if not clauses:
            return False
        raw = clauses[-1]
        if re.search(r"[A-Za-z]", raw) and not re.search(r"[一-鿿]", raw):
            words = re.findall(r"[A-Za-z']+", raw)
            if not words:
                return False
            if words[-1].lower().replace("’", "'") in _EN_CONTRACTION_TAILS:
                return True
            w1 = words[-1].lower().replace("'", "")
            w2 = "".join(words[-2:]).lower().replace("'", "")
            if w2 in _EN_DONE_PAIRS or words[-1] == "A":     # "I think so""plan A"：说完了
                return False
            return w1 in self.en_tails or w2 in self.en_tails
        last = _core(clauses[-1])
        # 句末的"啊 呀 吧 嘛 呢 哦 哈 嗯"是语气词，说明这句说完了，不算半截话（"没有任何问题啊"）
        tails = [w for w in self.sets["unfinished_tail"] + self.sets["think"] if w not in _FINAL_PARTICLES]
        return any(last.endswith(w) for w in tails)


class SmartTurn:
    """smart-turn v3.2：16 kHz 单声道 float32 音频（这一轮从开口前约 0.5 秒到现在，含末尾一点静音）→ 说完了的概率。"""

    def __init__(self, model: Path = MODEL, threads: int = 4):
        import numpy as np
        import onnxruntime as ort
        from .turn_features import compute_whisper_log_mel_features
        so = ort.SessionOptions()
        so.intra_op_num_threads, so.inter_op_num_threads = int(threads), 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._np, self._feat = np, compute_whisper_log_mel_features
        self._sess = ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
        self._lock = threading.Lock()

    def p_done(self, pcm) -> float:
        np = self._np
        a = np.asarray(pcm, dtype=np.float32)[-8 * SR:]
        if a.size < 8 * SR:
            a = np.pad(a, (8 * SR - a.size, 0))          # 不足 8 秒在前面补零（和 Pipecat 一样）
        f = self._feat(a, do_normalize=True)[None, :, :]
        with self._lock:
            out = self._sess.run(None, {"input_features": f})[0]
        return float(np.asarray(out).reshape(-1)[0])


_lex: Lexicon | None = None
_model: SmartTurn | None = None
_model_failed = False


def lexicon() -> Lexicon:
    global _lex
    if _lex is None:
        _lex = Lexicon()
    return _lex


def model() -> SmartTurn | None:
    """模型懒加载；文件没有或加载失败就返回 None（退回只用词表和停顿）。"""
    global _model, _model_failed
    if _model is None and not _model_failed:
        try:
            if MODEL.exists():
                _model = SmartTurn()
            else:
                _model_failed = True
        except Exception as e:
            log.warning("smart-turn 起不来（%r），只用词表和停顿判停", e)
            _model_failed = True
    return _model


def need(text: str, p_done: float | None) -> float:
    """你要安静多久才算说完（秒，从最后一个有声帧算起）。"""
    lx = lexicon()
    k = lx.whole_kind(text)
    if k in ("greet", "bye", "hold"):
        return 0.5
    if k in ("close", "backchannel"):
        return 1.0
    if k == "think" or lx.unfinished(text):
        return 4.0
    if p_done is not None and p_done < 0.5:
        return 2.5
    return 1.2
