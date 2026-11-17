# -*- coding: utf-8 -*-
"""散会后的"整理"：把逐字稿整理成能给别人看的干净版，每一处删改都列进删改清单（可以一条条恢复）。

四类（cleanup.json 的 kind）：
  retract    说错改口 —— 说到一半改口、重说、结巴重复：只留改过的那一遍（只删字，不加字）
  fact       不符合事实 —— 会上后来被指出是错的话（理由里写清楚是哪一句指出的）：删掉
  sensitive  不宜传播 —— 涉及他人的私下评价、人身攻击、隐私（电话、地址、账号、密码、金额等）、不打算公开的打算：删掉
  profanity  脏话 —— 规则找（zuhui/normalize.py），直接删；开会时实时显示的那一版已经删过了

retract / fact / sensitive 由 DeepSeek V4 Flash 看整场逐字稿挑出来（config.json 的 polish.model 可改，默认 deepseek-flash；
密钥和接口地址同快嘴）。模型给的每一条都要过检查：line 得有、before 得是原文里一字不差的一截、after 只能删字不能加字、
同一行里的条目不能重叠；不合格的丢掉。模型调不通：只做脏话这一类，照样写文件（note 里说明）。

  python -m zuhui.polish <会议id> [--dry]      整理一场会（--dry 只打印，不写文件）
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .meetings import ROOT, apply_cleanup, write_clean, _write_json

log = logging.getLogger("zuhui.polish")
MODEL = "claude-sonnet-5-5"                     # 走用户 Claude 套餐（2026-11-12 起不再用 DeepSeek）
THINKING = True              # 先想再挑（约 70~100 秒）：不想的话"不符合事实"常把纠正别人的那句当成错话（两场会实测）
KINDS = ("retract", "fact", "sensitive", "asr", "pii")
_NOT_CLAIM = re.compile(r"[？?]|吗[。！!]?$|^(?:你们|你)*(让|要不|建议|请|不如|咱们|我们)")

PROMPT = """你在整理一场组会的逐字稿，整理后的干净版要发给别人看。逐字稿每行格式是：#行号 [时间] 说话人：内容。
其中 {me} 是主持人（口述，语音识别出来的），Claude 是 AI 助手（它说的话是它自己生成的原文），其他名字是参会者。

找出下面四类要删改的地方，别的一律不动（语气词、口头禅、口语化的说法都不算，不要润色，不要改写）：
1. retract（说错改口）：同一个人在同一行里自己改口——先说了一个说法，紧接着推翻、换成另一个说法（"先放左边，不对，放右边"
   → "放右边"），或者把同一截话重复说了两遍（"我就想我就想跟它说"→"我就想跟它说"，"这这个"→"这个"）。
   before 必须同时包含说错的那一截和改过的那一截，after 是改过的那一遍（不能是空的）。只删字，不能加字、不能换字。
   这些不算：反驳别人、纠正 Claude（"不是命令行"是在纠正别人，不是自己改口）、复述别人说过的话来质疑、
   解释自己的意思、语音识别认错的字（归第 4 类）。
2. fact（不符合事实）：会上后来被明确指出是错的话（有人当场纠正、或者后面的讨论证明它错了）。只有会上自己能证明错的才算，
   你自己觉得不对但会上没人指出的不算；问句、建议、请求、提议（"你们让某某参与进来""要不要……"）不是陈述，也不算。要删的是"原来那句错话"（它在前面），不是后面指出它错的那句、也不是认错的那句；
   主持人说"不是""你理解错了"去纠正别人时，错的是被纠正的那个人前面说的话。evidence 填指出它错的那一行的行号（比它大），
   reason 里写清楚是谁指出的、为什么错。
3. sensitive（不宜传播）：对具体某个人的私下评价或贬损、人身攻击、收入存款、明显不想让外人知道的打算。
   随口提到的小花销、对软件的吐槽不算。只挑明显的，拿不准就不挑。删掉那一截。
5. pii（个人信息）：住址门牌（小区、楼栋、单元、房号，哪怕是口头念的数字）、电话、证件号、快递单号、账号密码、银行卡号。
   不删，after 换成占位：（住址略）（电话略）（证件号略）（号码略）（账号略）（个人信息略）之一，before 框住整段信息。
4. asr（识别错字）：语音识别把字认错了，联系上下文和论文一看就知道本来是什么（"定语符号"→"定义与符号"，"恒运会议"→"腾讯会议"，
   "接五的话"→"接我的话"，"三点一"不算错）。before 只框认错的那几个字（尽量短），after 是本来的字。论文里的小节名和术语
   见用户消息开头的参照表，对得上就照它写。只改明显认错、改法唯一的；拿不准就不改；Claude 的话不改（它说的是生成的原文）；
   人名、口头禅、口语说法都不算错字。
宁可少挑，不要错删：挑出来的每一条都会从发给别人的版本里删改掉。

输出严格的 JSON，不要别的文字：
{{"items": [{{"line": 行号, "kind": "retract|fact|sensitive|asr|pii", "before": "这一行里要删改的那一截，从原文一字不差地复制，尽量短",
  "after": "删改后这一截变成的样子（整截删掉就是空字符串）", "reason": "一句话理由", "evidence": 行号（只有 fact 要填）}}]}}
要求：before 必须是那一行原文里连续的一截（标点也要一样）；除了 asr 和 pii，after 只能是从 before 里删掉一些字以后剩下的；
一行里有几处就分几条；没有要删改的就输出 {{"items": []}}。说到人时写名字，不写"老师""学生"。"""


def _who(l: dict, me: str) -> str:
    w = l.get("who")
    return me if w == "teacher" else "Claude" if w == "student" else (l.get("name") or "参会者") if w == "guest" else "系统"


def _is_subseq(small: str, big: str) -> bool:
    it = iter(big)
    return all(c in it for c in small)


# 个人信息：规则先找（有固定格式的号码），模型再找住址这类；都换成占位，不直接删
PII_HOLD = ("（住址略）", "（电话略）", "（证件号略）", "（号码略）", "（账号略）", "（个人信息略）")
_CN_DIGIT = "零〇一二三四五六七八九两幺"
_PII_RX = [
    (re.compile(r"(?<![0-9])1[3-9][0-9](?:[ -]?[0-9]){8}(?![0-9])"), "（电话略）"),
    (re.compile(r"(?<![0-9])[1-9][0-9]{16}[0-9Xx](?![0-9])"), "（证件号略）"),
    (re.compile(r"(?<![0-9A-Za-z])[A-Za-z]{0,4}[0-9](?:[ -]?[0-9]){9,}(?![0-9])"), "（号码略）"),
    (re.compile(f"[{_CN_DIGIT}](?:[ ，、]?[{_CN_DIGIT}]){{9,}}"), "（号码略）"),       # 念出来的长串数字（电话、单号）
]


def pii_items(raw: list[dict]) -> list[dict]:
    """有固定格式的号码（不靠模型）：手机号、身份证号、10 位以上的长号码。Claude 的话不查（它不会念别人的号码）。"""
    out = []
    for i, l in enumerate(raw):
        if l.get("who") not in ("teacher", "guest"):
            continue
        text = str(l.get("text") or "")
        taken: list[tuple[int, int]] = []
        for rx, hold in _PII_RX:
            for m in rx.finditer(text):
                s, e = m.start(), m.end()
                if any(not (e <= a or s >= b) for a, b in taken):
                    continue
                taken.append((s, e))
                out.append({"line": i, "kind": "pii", "before": m.group(0), "after": hold, "reason": "个人信息（号码），换成占位", "pos": s})
    return out


def profanity_items(raw: list[dict]) -> list[dict]:
    from .normalize import find_profanity
    out = []
    for i, l in enumerate(raw):
        if l.get("who") not in ("teacher", "guest"):
            continue
        for s, e, w in find_profanity(str(l.get("text") or "")):
            out.append({"line": i, "kind": "profanity", "before": w, "after": "", "reason": "脏话，直接删", "pos": s})
    return out


def _llm_items(cfg: dict, raw: list[dict], me: str, model: str, thinking: bool = False) -> tuple[list[dict], dict]:
    # 走用户 Claude 套餐（不走 API）：config 里还写着 deepseek 的，一律换成 Sonnet
    from .claude_text import SONNET, ask_json
    if not str(model).startswith("claude-"):
        model = SONNET
    lines = [f"#{i} [{l.get('t')}] {_who(l, me)}：{l.get('text')}" for i, l in enumerate(raw)
             if (l.get("text") or "").strip() and l.get("who") in ("teacher", "student", "guest")]
    t0 = time.time()
    data = ask_json(_terms_block() + "\n".join(lines), system=PROMPT.format(me=me), model=model,
                    effort="medium" if thinking else "low", timeout=900)
    meta = {"model": model, "thinking": thinking, "seconds": round(time.time() - t0, 1)}
    return list((data or {}).get("items") or []), meta


def _terms_block() -> str:
    """给第 4 类（识别错字）做参照：当前论文的小节名，加上词库里的术语。拿不到就空着。"""
    out = []
    try:
        from .config import current_paper
        p = Path((current_paper() or {}).get("path") or "")
        if p.is_file():
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                m = re.match(r"^#{1,4}\s+(.+?)\s*$", line)
                if m:
                    out.append(m.group(1)[:40])
    except Exception:
        pass
    try:
        from .lexicon import SOFTWARE
        out += [t[0] for t in SOFTWARE if not str(t[0]).startswith("@")]
    except Exception:
        pass
    out = list(dict.fromkeys(x for x in out if x))[:160]
    return ("【参照表：论文的小节名和术语（只用来认错字，不是逐字稿）】\n" + "；".join(out) + "\n【逐字稿】\n") if out else ""


def _evidence_ok(raw: list[dict], i: int, ev) -> bool:
    """fact 的证据行：在错话后面、存在、不是同一个人自己说的（自己后面改口归 retract）。"""
    try:
        e = int(ev)
    except (TypeError, ValueError):
        return False
    if not (i < e < len(raw)):
        return False
    a, b = raw[i], raw[e]
    if a.get("who") == "teacher" and b.get("who") == "student":
        return False                  # Claude 附和、复述不能证明主持人说错了（宁可少删）
    return (a.get("who"), a.get("name")) != (b.get("who"), b.get("name"))


def validate(raw: list[dict], proposed: list[dict], taken: list[dict]) -> tuple[list[dict], list[dict]]:
    """模型给的条目过一遍检查：合格的返回（带 pos），不合格的连同原因另返回。taken = 已经定了的条目（脏话）。"""
    ok, bad = [], []
    spans: dict[int, list[tuple[int, int]]] = {}
    for it in taken:
        spans.setdefault(it["line"], []).append((it["pos"], it["pos"] + len(it["before"])))
    for it in proposed:
        why = None
        try:
            i = int(it.get("line"))
        except (TypeError, ValueError):
            i = -1
        kind = str(it.get("kind") or "")
        before, after = str(it.get("before") or ""), str(it.get("after") or "")
        reason = str(it.get("reason") or "").strip()
        text = str(raw[i].get("text") or "") if 0 <= i < len(raw) else ""
        if not text:
            why = "没有这一行"
        elif kind not in KINDS:
            why = f"类别不对：{kind}"
        elif not before or before not in text:
            why = "before 不是原文里的一截"
        elif kind == "asr" and (raw[i].get("who") == "student" or not after or len(before) > 16 or len(after) > len(before) + 6):
            why = "识别错字只改人说的、短的一截，而且得给出本来的字"
        elif kind == "pii" and after not in PII_HOLD:
            why = "个人信息要换成占位（住址略）这一类"
        elif kind not in ("asr", "pii") and after and not _is_subseq(after, before):
            why = "after 加了字 / 换了字"
        elif after == before:
            why = "没有改动"
        elif kind == "retract" and raw[i].get("who") == "student":
            why = "Claude 的话不会说错改口"
        elif kind == "retract" and not after.strip("，。！？、,.!? "):
            why = "改口得留下改过的那一遍（after 是空的）"
        elif kind == "fact" and _NOT_CLAIM.search(before):
            why = "问句 / 建议 / 请求不是陈述，谈不上不符合事实"
        elif kind == "fact" and not _evidence_ok(raw, i, it.get("evidence")):
            why = "不符合事实得有后面指出它错的那一行（evidence），而且是别人说的"
        if why is None:
            pos = text.find(before)
            while pos >= 0 and any(not (pos + len(before) <= s or pos >= e) for s, e in spans.get(i, [])):
                pos = text.find(before, pos + 1)
            if pos < 0:
                why = "和别的条目重叠"
            else:
                spans.setdefault(i, []).append((pos, pos + len(before)))
                ok.append({"line": i, "kind": kind, "before": before, "after": after,
                           "reason": (reason or {"retract": "说错改口", "fact": "不符合事实", "sensitive": "不宜传播", "asr": "识别错字",
                                                 "pii": "个人信息"}[kind])[:160],
                           "pos": pos})
                continue
        bad.append(dict(it, rejected=why))
    return ok, bad


def run(mid: str, root: Path | None = None, cfg: dict | None = None, dry: bool = False, use_llm: bool = True) -> dict:
    """整理一场会：写 cleanup.json 和 transcript.clean.jsonl，返回 {"items", "rejected", "meta"}。"""
    import getpass
    from .config import load
    cfg = cfg or load()
    me = str((cfg.get("meeting") or {}).get("me") or getpass.getuser() or "我")
    d = Path(root or ROOT) / Path(str(mid)).name
    raw = []
    for s in (d / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
        if s.strip():
            try:
                raw.append(json.loads(s))
            except ValueError:
                pass
    t0 = time.time()
    items = profanity_items(raw)
    items += [p for p in pii_items(raw)                                    # 号码这类个人信息：规则先定，不靠模型
              if not any(q["line"] == p["line"] and not (p["pos"] + len(p["before"]) <= q["pos"] or p["pos"] >= q["pos"] + len(q["before"]))
                         for q in items)]
    rejected: list[dict] = []
    meta: dict = {"lines": len(raw)}
    model = str((cfg.get("polish") or {}).get("model") or MODEL)
    said = sum(len(str(l.get("text") or "")) for l in raw if l.get("who") in ("teacher", "guest"))
    if said < 20:                                # 几乎没人说话（试麦、空会）：不用叫模型
        use_llm = False
        meta["skipped_llm"] = "没什么话"
    if use_llm and raw:
        thinking = bool((cfg.get("polish") or {}).get("thinking", THINKING))
        for attempt in ([thinking, False] if thinking else [False]):   # 先想再答失败了：不想再来一次
            try:
                proposed, m = _llm_items(cfg, raw, me, model, attempt)
                meta.update(m)
                meta.pop("error", None)
                good, rejected = validate(raw, proposed, items)
                items += good
                break
            except Exception as e:              # 模型调不通：只删脏话，照样出干净版
                meta["error"] = f"{type(e).__name__}: {e}"[:300]
                log.warning("整理：模型调用失败（%s）", meta["error"])
    items.sort(key=lambda x: (x["line"], x["pos"]))
    old = {}
    try:   # 重新整理时：之前恢复过的条目（同一行同一截）保持恢复
        for it in json.loads((d / "cleanup.json").read_text(encoding="utf-8")).get("items") or []:
            old[(it.get("line"), it.get("before"), it.get("kind"))] = bool(it.get("restored"))
    except (OSError, ValueError):
        pass
    for k, it in enumerate(items, 1):
        it["id"] = f"c{k:03d}"
        it["restored"] = old.get((it["line"], it["before"], it["kind"]), False)
    meta["seconds_total"] = round(time.time() - t0, 1)
    data = {"version": 1, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "items": [
        {k: it[k] for k in ("id", "line", "kind", "before", "after", "reason", "restored", "pos")} for it in items],
        "meta": meta}
    if not dry:
        _write_json(d / "cleanup.json", data)
        write_clean(d, apply_cleanup(raw, data["items"]))
    return {"items": data["items"], "rejected": rejected, "meta": meta}


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = run(args[0], dry="--dry" in sys.argv)
    names = {"retract": "说错改口", "fact": "不符合事实", "sensitive": "不宜传播", "profanity": "脏话", "asr": "识别错字",
             "pii": "个人信息"}
    for it in out["items"]:
        print(f"[{it['id']}] #{it['line']} {names[it['kind']]}：「{it['before']}」→「{it['after']}」  {it['reason']}")
    for it in out["rejected"]:
        print(f"[丢掉] {it.get('rejected')}：{json.dumps(it, ensure_ascii=False)[:200]}")
    print(json.dumps(out["meta"], ensure_ascii=False))
