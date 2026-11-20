# -*- coding: utf-8 -*-
"""任务板：老师每说一条要改的，就记一张任务卡；工人按序领走、改完回填。

JSON 持久化在 STATE/board.json，所有公开方法都加锁，可从主循环 / 工人线程并发调用。
任务卡字段（一字不差，主循环和快嘴都按这个读）：
    id, idx, title, remark, shot, section, status, result, error, created, started, finished
    paper / paper_name / paper_path：这条是哪篇论文上的（会中能摊开几篇、来回切；旧任务没有，算这场会第一篇）
status ∈ queued / working / done / failed
工人改的时候另有 progress（hands.WorkerProgress 写，会议页任务卡和快嘴都读）：
    {"step": 正在干什么, "section": 在哪一节或 None, "edits": 已改几处, "started": unix, "updated": unix, "log": 最近 5 步}
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import STATE

STATUS_CN = {"queued": "排队（还没开始改）", "working": "改中（还没改完）", "done": "已改", "failed": "失败"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def _result_note(result, title) -> str:
    """改完那条的记录给快嘴看：第一句（改了什么）之外，第二句（没做的部分、拿不准的）不是"无"也带上。
    以前只截前 60 字，第二句"频率那半条没改"被截掉，快嘴问进度时说"六条都改好了"（英文老师场评审）。"""
    r = " ".join(str(result or "").split())
    if not r:
        return _clip(title, 60)
    parts = re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+(?=[A-Z])", r, maxsplit=1)
    first, second = parts[0], (parts[1].strip() if len(parts) > 1 else "")
    if second and not re.fullmatch(r"(?:无|没有|None|N/?A|Nothing)[。.]?", second, re.I):
        return _clip(first, 70) + "（另：" + _clip(second, 90) + "）"
    return _clip(first, 70)


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60} 分 {sec % 60} 秒" if sec >= 60 else f"{sec} 秒"


def task_paper(t: dict | None, first: str | None = None) -> str | None:
    """这条任务是哪篇论文上的（论文编号，见 meetings.paper_id）。旧任务（多篇论文之前记的）没有这个字段：
    算这场会的第一篇（first）；连第一篇都不知道就是 None，和别的 None 算同一篇。"""
    return (t or {}).get("paper") or first


# 说的是开会软件本身（界面、声音、转写、功能）的那几条：对话模型给的标题以"软件："开头，或者工人看过原文说"这条是在反馈软件"
_APP_TITLE = re.compile(r"^\s*(?:软件|应用|App|APP|app|Software|software)\s*[：:]")
_APP_SAID = re.compile(r"反馈软件|软件的问题|软件意见|说的是软件|是软件的事|在说软件|软件本身|不是论文的事|和论文无关|与论文无关|跟论文无关"
                       r"|(?i:about the app\b|app feedback|software feedback|not about the paper|the app itself)")


def is_app_task(t: dict | None) -> bool:
    """这条说的是开会软件本身，不是论文（2026-11-14 组会："软件：一句话里同时给我和后台下指令"被记成了一条改稿任务）：
    论文页上不画（不打勾、不插旗、不进"还没落到正文里的"），任务板和修改清单里照样有。"""
    if not t:
        return False
    if t.get("app") or t.get("kind") == "app":
        return True
    if _APP_TITLE.match(str(t.get("title") or "")):
        return True
    return bool(t.get("note") and t.get("unchanged") and _APP_SAID.search(str(t.get("result") or "")))


# 标注（会上改不了的那几条）身上的标签，按标题、"怎么改""要补"里的字眼归类，小旗卡片、修改清单、导出都画（2026-11-19）。
# 顺序固定，一条最多三个。"会后改 / 已处理"是状态，不在这里：界面按 resolved 自己画
NOTE_TAGS = (
    ("rerun", re.compile(r"重跑|重新跑|再跑|补跑|跑一?[组次遍轮]|回归|实验|消融|对照|稳健性|鲁棒|训练|微调"
                         r"|(?i:ablation|regress|experiment|re-?run|robustness|retrain|fine-?tun)")),
    ("data", re.compile(r"数据|样本|观测|问卷|采集|调研|(?i:\bdata\b|dataset|\bsamples?\b|survey|observations)")),
    ("figure", re.compile(r"[图表]\s*\d|[张幅个]图|[张个]表(?![达述示明现面])|图表|配图|重画|画(?:一张|一幅|个|图)|作图"
                          r"|示意图|折线图|柱状图|散点图|热力图|流程图|框架图|曲线图|表格"
                          r"|(?i:\bfig(?:ure)?s?\b|\btables?\b|\bplots?\b|\bcharts?\b)")),
    ("lit", re.compile(r"文献|引用|参考|引文|相关工作|(?i:citation|literature|references?\b|\bcite|related work)")),
    ("discuss", re.compile(r"再讨论|讨论一下|待讨论|商量|拍板|待定|再定|确认|请示|问一?下|问(?:老师|导师)"
                           r"|(?i:\bdecide\b|discuss with|check with|confirm|to be decided|\bTBD\b)")),
)
NOTE_TAG_KEYS = tuple(k for k, _ in NOTE_TAGS)


def note_tags(t: dict | None) -> list[str]:
    """标注的标签（NOTE_TAG_KEYS 里的键）。任务上存了 tags 就用存的，否则按标题、怎么改、要补现推（旧会议的也能显示）。
    不是标注、软件意见：[]。"""
    if not t or not t.get("note") or is_app_task(t):
        return []
    have = t.get("tags")
    if isinstance(have, list):
        return [k for k in NOTE_TAG_KEYS if k in have][:3]
    text = " ".join(str(t.get(k) or "") for k in ("title", "plan", "need"))
    return [k for k, rx in NOTE_TAGS if rx.search(text)][:3]


def with_tags(t: dict) -> dict:
    """给标注补上 tags（副本）；别的任务原样返回。"""
    return dict(t, tags=note_tags(t)) if t.get("note") else t


def _paper_dict(paper) -> dict:
    """add / add_note 的 paper 参数：{id, name, path}（meetings.paper_entry 那样的）→ 任务上记的三个字段。"""
    if isinstance(paper, dict) and paper.get("id"):
        return {"paper": str(paper["id"]), "paper_name": paper.get("name"), "paper_path": paper.get("path")}
    return {}


def _progress_note(t: dict) -> str:
    """改中的那条后面跟的实时进度："（已改 2 处，正在：在 3.2 节改第 3 处，开始 1 分 30 秒）"。"""
    p = t.get("progress") or {}
    started = p.get("started")
    if not started and t.get("started"):
        try:
            started = datetime.fromisoformat(t["started"]).timestamp()
        except ValueError:
            started = None
    parts = [f"已改 {int(p.get('edits') or 0)} 处" if p.get("edits") else "还没动笔"]
    if p.get("step"):
        parts.append(f"正在：{_clip(p['step'], 30)}")
    if started:
        parts.append(f"开始 {_ago(time.time() - float(started))}")
    return "（" + "，".join(parts) + "）"


class Board:
    """任务板可能被多个进程同时用（主程序加任务、CCD 工人经命令行领任务/回填），
    所以每个公开方法都先从磁盘重读再操作；写是 tmp + os.replace 原子替换。"""

    def __init__(self, path: Path | None = None, recover_working: bool = False):
        self.path = Path(path) if path else STATE / "board.json"
        self._lock = threading.RLock()
        self._tasks: list[dict] = []
        self._next_idx = 1
        self._mtime = None
        self._load()
        # 只有"自己起工人线程"的进程才把遗留的 working 放回队列；CCD 模式下 working 是别的会话在改，不能动
        if recover_working:
            with self._lock:
                dirty = False
                for t in self._tasks:
                    if t.get("status") == "working":
                        t["status"], t["started"], dirty = "queued", None, True
                if dirty:
                    self._save()

    # ---------- 持久化 ----------
    def reload(self) -> None:
        """磁盘上有别的进程写过就重读。"""
        with self._lock:
            try:
                m = self.path.stat().st_mtime_ns
            except OSError:
                return
            if m != self._mtime:
                self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                data = json.load(f)
            self._mtime = self.path.stat().st_mtime_ns
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            tasks = data.get("tasks") or []
            self._next_idx = int(data.get("next_idx") or 1)
        else:  # 容忍旧格式：裸列表
            tasks = data
        self._tasks = [t for t in tasks if isinstance(t, dict) and "id" in t]
        if self._tasks:
            self._next_idx = max(self._next_idx, max(int(t.get("idx", 0)) for t in self._tasks) + 1)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tasks": self._tasks, "next_idx": self._next_idx}, f, ensure_ascii=False, indent=1)
        for i in range(10):                       # Windows：别的进程正开着 board.json 读时换不上，稍等再换
            try:
                os.replace(tmp, self.path)
                break
            except PermissionError:
                if i == 9:
                    raise
                time.sleep(0.03 * (i + 1))
        try:
            self._mtime = self.path.stat().st_mtime_ns
        except OSError:
            pass

    def _find(self, task_id: str) -> dict | None:
        for t in self._tasks:
            if t["id"] == task_id:
                return t
        return None

    # ---------- 接口 ----------
    def add(self, remark: str, title: str, shot: str | None = None, section: str | None = None,
            focus: dict | None = None, paper: dict | None = None) -> dict:
        """paper：这条是哪篇论文上的（开会时屏幕上那篇，{id, name, path}）。工人按任务上的 paper_path 改那一篇，
        中途切到别篇也不会改错；撤回、打勾也只在那一篇里算。"""
        with self._lock:
            self.reload()
            idx = self._next_idx
            self._next_idx += 1
            task = {
                "id": f"T{idx}",
                "idx": idx,
                "title": (title or "").strip() or _clip(remark, 20),
                "remark": remark,
                "shot": shot,
                "section": section,
                "focus": focus,          # 老师说这句时对比页正显示的位置 {"section","text","no"}
                "status": "queued",
                "result": None,
                "error": None,
                "created": _now(),
                "started": None,
                "finished": None,
                **_paper_dict(paper),
            }
            self._tasks.append(task)
            self._save()
            return dict(task)

    def add_note(self, remark: str, title: str, section: str | None = None, focus: dict | None = None,
                 result: str = "已记录，未修改文档", plan_job: bool = False, paper: dict | None = None) -> dict:
        """只记不改的一条（"先别改，记一下""put it on the list""flag that number"）：进任务板，note=True 让任务板和汇报都说"记着、没改"。
        三人英文会评审：嘴上说 "I'll flag it"，任务板上什么都没有。
        plan_job=True（标注模式，2026-11-14）：先排队交给工人只读地看一遍原文，写出钉在哪一段、怎么改、要补什么（mode=annotate），
        工人不改论文；写好之前它照样算一条标注，只是卡片上还没有改法。"""
        with self._lock:
            self.reload()
            idx = self._next_idx
            self._next_idx += 1
            task = {"id": f"T{idx}", "idx": idx, "title": (title or "").strip() or _clip(remark, 20), "remark": remark,
                    "shot": None, "section": section, "focus": focus, "status": "queued" if plan_job else "done", "note": True,
                    "result": None if plan_job else result, "error": None, "created": _now(), "started": None,
                    "finished": None if plan_job else _now(), **_paper_dict(paper)}
            if plan_job:
                task["mode"] = "annotate"
            self._tasks.append(task)
            self._save()
            return dict(task)

    def claim_next(self) -> dict | None:
        """原子地把最早的 queued 置为 working 并返回；没有就 None。"""
        with self._lock:
            self.reload()
            for t in sorted(self._tasks, key=lambda x: x["idx"]):
                if t["status"] == "queued":
                    t["status"], t["started"] = "working", _now()
                    self._save()
                    return dict(t)
            return None

    def claim(self, task_id: str) -> dict:
        """领指定的一条（CCD 工人用）：queued → working；不是 queued 就原样返回。"""
        with self._lock:
            self.reload()
            t = self._find(task_id)
            if t is None:
                raise KeyError(f"任务板里没有 {task_id}")
            if t["status"] == "queued":
                t["status"], t["started"] = "working", _now()
                self._save()
            return dict(t)

    def finish(self, task_id: str, result: str | None = None, error: str | None = None) -> dict:
        with self._lock:
            self.reload()
            t = self._find(task_id)
            if t is None:
                raise KeyError(f"任务板里没有 {task_id}")
            t["status"] = "failed" if error else "done"
            t["result"], t["error"], t["finished"] = result, error, _now()
            self._save()
            return dict(t)

    def to_note(self, task_id: str, plan: str | None = None, need: str | None = None, anchor: str | None = None,
                section: str | None = None, result: str | None = None) -> dict | None:
        """这条会上改不了（要新数据、新结果，或者只记不改）：标成标注，正文没动。工人看过原文后调用。
        plan=怎么改，need=要补什么，anchor=那一段开头的原文（对比页按它把小旗钉到那一段），section=小节标题。"""
        with self._lock:
            self.reload()
            t = self._find(task_id)
            if t is None:
                return None
            t.update(note=True, status="done", finished=_now(), error=None)
            if plan:
                t["plan"] = plan.strip()
            if need:
                t["need"] = need.strip()
            if section:
                t["section"] = section.strip()
            if anchor:
                f = dict(t.get("focus") or {})
                f["text"] = anchor.strip()
                if section:
                    f["section"] = section.strip()
                t["focus"] = f
            t["result"] = result or t.get("result") or "会中无法修改，已加入修改清单"
            self._save()
            return dict(t)

    def annotate(self, task_id: str, **fields) -> None:
        """给任务补几个字段（比如讨论里哪几句和这次改动有关），别的字段不动。"""
        with self._lock:
            self.reload()
            t = self._find(task_id)
            if t is None:
                return
            t.update(fields)
            self._save()

    def set_progress(self, task_id: str, progress: dict) -> None:
        """工人改到哪了（hands.WorkerProgress 调，最多一秒一次）：写在任务卡的 progress 上，别的字段不动。"""
        with self._lock:
            self.reload()
            t = self._find(task_id)
            if t is None:
                return
            t["progress"] = dict(progress)
            self._save()

    def resolve(self, task_id: str, accepted: bool = False, rejected: bool = False) -> dict | None:
        """用户在对比页给这条打了勾：看过了。accepted = 它的改动已并进原文快照（修订标记也消了）。
        rejected = 点的是 ×：这条改动不要了，已经从稿子里撤回去。"""
        with self._lock:
            self.reload()
            t = self._find(task_id)
            if t is None:
                return None
            t["resolved"], t["accepted"], t["resolved_at"] = True, bool(accepted), _now()
            if rejected:
                t["rejected"] = True
                # 结果那行改成"已撤回"：界面任务卡、会后报告直接显示 result，原来写的"已改成 a benchmark"留着就像没撤
                # （英文捣乱场评审）。原来的记录另存；按原记录的语言写
                if not t.get("result_before_undo"):
                    old = str(t.get("result") or "")
                    t["result_before_undo"] = old
                    en = old and sum(c.isascii() for c in old) / len(old) > 0.9
                    t["result"] = ("Undone; the original text is restored." if en else "已撤回，原文已恢复。") + (
                        f" ({_clip(old, 80)})" if en and old else f"（原来：{_clip(old, 60)}）" if old else "")
            self._save()
            return dict(t)

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            self.reload()
            t = self._find(task_id)
            return dict(t) if t else None

    def tasks(self) -> list[dict]:
        """每条的副本。软件意见一律带上 app=True：工人看过原文才说"这条是在反馈软件"的那种（is_app_task 认得、自己没标），
        以前计数算软件、修改清单（MeetingSummary TodoList 只认 app / kind）却把它放进论文那一节（2026-11-15 联调）。
        标注带上 tags（note_tags）。"""
        with self._lock:
            self.reload()
            return [with_tags(dict(t, app=True) if not t.get("app") and is_app_task(t) else dict(t))
                    for t in sorted(self._tasks, key=lambda x: x["idx"])]

    def counts(self) -> dict:
        with self._lock:
            self.reload()
            c = {"queued": 0, "working": 0, "done": 0, "failed": 0, "notes": 0, "app": 0}
            for t in self._tasks:
                if is_app_task(t):
                    c["app"] += 1                    # 软件意见：不是论文的改动，也不是论文标注，单独数
                    continue
                if t.get("note") and t["status"] == "done":
                    c["notes"] += 1                  # 标注不算"已改"：正文没动（问进度、散会时分开报）
                    continue
                c[t["status"]] = c.get(t["status"], 0) + 1
            return c

    def summary(self, max_items: int = 40) -> str:
        """给快嘴看的中文摘要，一任务一行，最近的在前。"""
        with self._lock:
            self.reload()
            if not self._tasks:
                return "（暂无任务）"
            lines = []
            ordered = sorted(self._tasks, key=lambda x: x["idx"], reverse=True)
            multi = len({t.get("paper") for t in self._tasks if t.get("paper")}) > 1   # 会上摊开了不止一篇：每条标上是哪篇的
            for t in ordered[:max_items]:
                st = t["status"]
                tid = f"{t['id']}《{_clip(t.get('paper_name') or '论文', 24)}》" if multi else t["id"]
                if t.get("note"):
                    extra = "".join(f"；{k}{_clip(t[f], 50)}" for k, f in (("怎么改：", "plan"), ("要补：", "need")) if t.get(f))
                    said = _clip(str(t.get("remark") or "").split("（Claude 当场复述")[0], 50)
                    at = (t.get("created") or "")[11:16]
                    lines.append(f"{tid} 标注（会上没改正文，记进修改清单）：{_clip(t.get('title'), 60)}{extra}"
                                 + (f"（当时的原话{('，' + at + ' 记下') if at else ''}：{said}）" if said else ""))
                    continue
                if st == "done" and t.get("rejected"):
                    body = "你不要这条，已经撤回原样：" + _clip(t.get("title"), 50)
                elif st == "done":
                    body = _result_note(t.get("result"), t.get("title"))
                    try:                                  # 改完了却没动论文（查过发现不用改 / 等你确认）：别报成"改好了"
                        ch = json.loads((STATE / "changes" / f"{t['id']}.json").read_text(encoding="utf-8"))
                        if ch.get("changed") is False:
                            lines.append(f"{tid} 查过没改（论文没动）：{_clip(t.get('title'), 30)}；{body}")
                            continue
                    except (OSError, ValueError):
                        pass
                elif st == "failed":
                    body = _clip(t.get("error") or "改失败了", 60)
                elif st == "working":
                    body = _clip(t.get("title"), 60) + _progress_note(t)
                else:
                    body = _clip(t.get("title"), 60)
                said = _clip(str(t.get("remark") or "").split("（Claude 当场复述")[0], 40)
                lines.append(f"{tid} {STATUS_CN.get(st, st)}：{body}" + (f"（当时的原话：{said}）" if said else ""))
            if len(ordered) > max_items:
                lines.append(f"（另有 {len(ordered) - max_items} 条更早的没列）")
            return "\n".join(lines)

    def cancel_queued(self, task_id: str) -> bool:
        """还在排队的一条不要了：记成"你不要这条、已撤回"（没动过论文）。不是排队中就不动，返回 False。"""
        with self._lock:
            self.reload()
            t = self._find(task_id)
            if not t or t["status"] != "queued":
                return False
            t.update(status="done", rejected=True, finished=_now(), result="已撤回，未修改文档")
            self._save()
            return True

    def last_done(self, paper: str | None = None, first: str | None = None) -> dict | None:
        """最近改好的一条。paper：只看这一篇论文上的（task_paper；first = 这场会第一篇，旧任务算它的）。"""
        with self._lock:
            self.reload()
            done = [t for t in self._tasks if t["status"] == "done"
                    and (paper is None or task_paper(t, first) == paper)]
            if not done:
                return None
            return dict(max(done, key=lambda x: (x.get("finished") or "", x["idx"])))

    def reset(self) -> None:
        """清空（新会议）。"""
        with self._lock:
            self._tasks, self._next_idx = [], 1
            self._save()
