# -*- coding: utf-8 -*-
"""散会后的收尾流水线（后台线程，散会请求本身不等它）：

  结束会议 save  ：等刚才那句话停下、存修订对比快照、等录屏收尾（录屏文件和 meta.json 的时长写好）
  转写 transcript：整场录音里你说的话重转一遍（zuhui/retranscribe.py），转完顺手学这场会听错的词（lexicon.learn_meeting）
  整理 polish    ：干净版 + 删改清单（zuhui/polish.py）
  纪要 summary   ：按干净版写（meetings.summarize）
  （之后：累计你说话的时长，够 5 小时触发一次转写模型训练，zuhui/asr_train.py，界面不显示）

进度写 ~/.zuhui/postmeeting.json（悬浮岛和回顾页在读）。原有字段不变：meeting / session / started / title /
steps[{key,label,state}] / progress / done / error；每一步另有 started / ended（time.time() 秒）、expected_s（按以往实测
估的这一步要多久，悬浮岛拿 started + expected_s 自己画平滑的进度）、frac（这一步自己报的进度 0~1，转写有）、note。
progress 按 expected_s 加权（不是每步 25%），done = 四步都结束了（done / failed）。
某一步失败不拦后面的：重转写失败就用开会时的实时转写，整理失败就只删脏话，纪要失败才标 failed。
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
import traceback
from pathlib import Path

from .config import STATE

POSTMEETING = STATE / "postmeeting.json"
STATS = STATE / "postmeeting_stats.json"
STEPS = (("save", "结束会议"), ("transcript", "转写"), ("polish", "整理"), ("summary", "纪要"))
# 没有实测记录时的估计（2026-11-11 实测：录屏收尾 10~40 秒；重转写 Fun-ASR-Nano 低优先级 0.45 倍（机器闲）~0.95 倍（满载）说话时长；
# 整理 DeepSeek Flash 先想再挑 70~100 秒；纪要 10~20 秒）
DEFAULT_RATE = {"save": (15.0, 0.0), "transcript": (20.0, 0.7), "polish": (25.0, 0.5), "summary": (12.0, 0.02)}
_pipe_lock = threading.Lock()              # 一次只跑一场的收尾（上一场还在转，下一场散了就排队）


def _write_atomic(path: Path, obj) -> None:
    try:
        tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------- 每一步要多久（按以往实测）
def _stats() -> dict:
    try:
        d = json.loads(STATS.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def record(step: str, seconds: float, size: float) -> None:
    """记一次实测：这一步用了 seconds 秒，规模 size（转写 = 说话秒数，整理 / 纪要 = 逐字稿行数，结束会议 = 1）。"""
    d = _stats()
    rows = d.setdefault(step, [])
    rows.append({"s": round(float(seconds), 1), "x": round(float(size), 1), "at": time.strftime("%Y-%m-%d %H:%M")})
    d[step] = rows[-20:]
    _write_atomic(STATS, d)


def expected(step: str, size: float) -> float:
    base, rate = DEFAULT_RATE.get(step, (10.0, 0.0))
    rows = [r for r in _stats().get(step, []) if r.get("s")]
    if len(rows) >= 2:
        if rate and any(r.get("x") for r in rows):
            rate = statistics.median([max(0.0, r["s"] - base) / r["x"] for r in rows if r.get("x")])
        else:
            base = statistics.median([r["s"] for r in rows])
    return round(base + rate * max(0.0, float(size)), 1)


# ---------------------------------------------------------------- postmeeting.json
class PostMeeting:
    """散会后的收尾进度，写 ~/.zuhui/postmeeting.json（形状只加不改：悬浮岛、回顾页在读）。
    只有最新的那一场写这个文件：上一场还在后台收尾时，它的进度不去盖掉新一场的。"""
    STEPS = STEPS
    _latest: "PostMeeting | None" = None

    def __init__(self, mid: str, session: str | None, title: str | None = None, sizes: dict | None = None):
        self.lock = threading.Lock()
        sizes = sizes or {}
        self._last_write = 0.0
        self.d = {"meeting": mid, "session": session, "started": time.time(), "title": title or None,
                  "steps": [{"key": k, "label": label, "state": "pending", "started": None, "ended": None,
                             "expected_s": expected(k, sizes.get(k, 1.0)), "frac": None, "note": None}
                            for k, label in self.STEPS],
                  "progress": 0.0, "done": False, "error": None}
        PostMeeting._latest = self
        self._write()

    def _step(self, key: str) -> dict | None:
        return next((s for s in self.d["steps"] if s["key"] == key), None)

    def state(self, key: str) -> str:
        with self.lock:
            s = self._step(key)
            return s["state"] if s else "pending"

    def set(self, key: str, state: str, error: str | None = None, note: str | None = None) -> None:
        with self.lock:
            s = self._step(key)
            if s and not (s["state"] in ("done", "failed") and state in ("pending", "running")):
                now = time.time()
                if state == "running" and not s["started"]:
                    s["started"] = now
                if state in ("done", "failed"):
                    s["started"] = s["started"] or now
                    s["ended"] = now
                    s["frac"] = 1.0
                s["state"] = state
                if note is not None:
                    s["note"] = note
            if error and not self.d["error"]:
                self.d["error"] = str(error)[:200]
            self._recalc()
            self._write()

    def expect(self, key: str, seconds: float) -> None:
        """这一步的规模知道了（转写：要重听多少秒）：重估它要多久。"""
        with self.lock:
            s = self._step(key)
            if s:
                s["expected_s"] = round(float(seconds), 1)
            self._recalc()
            self._write()

    def step_progress(self, key: str, frac: float | None, note: str | None = None) -> None:
        with self.lock:
            s = self._step(key)
            if not s or s["state"] != "running":
                return
            old = s.get("frac") or 0.0
            if frac is not None:
                s["frac"] = round(max(0.0, min(1.0, float(frac))), 3)
            if note:
                s["note"] = note
            self._recalc()
            if time.time() - self._last_write >= 0.5 or abs((s.get("frac") or 0.0) - old) >= 0.05:
                self._write()

    def _recalc(self) -> None:
        tot = done = 0.0
        now = time.time()
        for s in self.d["steps"]:
            w = max(1.0, float(s.get("expected_s") or 1.0))
            tot += w
            if s["state"] in ("done", "failed"):
                done += w
            elif s["state"] == "running":
                f = s.get("frac")
                if f is None and s.get("started"):
                    f = min(0.95, (now - s["started"]) / w)
                done += w * min(0.98, float(f or 0.0))
        self.d["progress"] = round(done / tot, 3) if tot else 0.0
        self.d["done"] = all(s["state"] in ("done", "failed") for s in self.d["steps"])

    def _write(self) -> None:
        if PostMeeting._latest is not self:
            return
        self._last_write = time.time()
        _write_atomic(POSTMEETING, self.d)


# ---------------------------------------------------------------- 流水线
def _speech_guess(lines: list[dict]) -> float:
    """开会时你说的话大概多少秒（每秒四个多字），估转写这一步要多久用。"""
    import re
    n = sum(len(re.sub(r"[\s，。！？、,.!?：:；;…]", "", str(l.get("text") or ""))) for l in lines if l.get("who") == "teacher")
    return n / 4.5


def wait_saved(post: PostMeeting, session: str | None, m: dict, timeout: float = 30 * 60) -> None:
    """录屏收尾完了没有：录屏文件在、meta.json 写了时长（record.py 收完尾才写）。没有录屏目录（回放）就直接算完。"""
    if not session or not Path(session).is_dir():
        post.set("save", "done")
        return
    sd = Path(session)
    try:
        from datetime import datetime
        t_start = datetime.fromisoformat(m.get("started")).timestamp() - 5
    except Exception:
        t_start = 0.0
    deadline = time.time() + timeout
    while time.time() < deadline and post.state("save") != "done":
        try:
            meta = json.loads((sd / "meta.json").read_text(encoding="utf-8-sig")) if (sd / "meta.json").exists() else {}
            video = sd / str(meta.get("video") or "recording.mp4")
            if meta.get("duration") and video.exists() and (sd / "meta.json").stat().st_mtime >= t_start:
                post.set("save", "done")
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    if post.state("save") != "done":
        post.set("save", "done", note="录屏收尾超时，先用已经有的录音")


def run(app, post: PostMeeting, m: dict, session: str | None, talk: threading.Thread | None = None) -> None:
    """散会后台收尾（app = app.Meeting）。散会请求早已返回：会已经标成结束了。"""
    mid = m["id"]
    log = getattr(app, "log", None) or (lambda *a, **k: None)
    post.set("save", "running")
    t0 = time.time()
    # 0. 刚才那句话停下（打断在散会时已经做了，这里只是等它收干净）；修订对比快照（html-diff，要几秒）
    if talk is not None:
        talk.join(timeout=5.0)
    viewer = getattr(app, "viewer", None)
    if viewer is not None:
        try:
            diff = viewer.snapshot_final()
            if diff:
                app.meetings.save_diff(mid, diff)
        except Exception as e:
            log("fast_error", error=f"散会快照失败：{e!r}")
    with _pipe_lock:
        _pipeline(app, post, m, session, t0, log)


def _pipeline(app, post: PostMeeting, m: dict, session: str | None, t0: float, log) -> None:
    from .meetings import summarize
    mid = m["id"]
    cfg = app.cfg
    wait_saved(post, session, m)
    record("save", time.time() - t0, 1)

    # 1. 转写：整场重转
    post.set("transcript", "running")
    t1 = time.time()
    try:
        from . import retranscribe

        def prog(f, note):
            post.step_progress("transcript", f, note or None)
        st = retranscribe.run(mid, root=app.meetings.root, cfg=cfg, progress=prog,
                              on_plan=lambda p: post.expect("transcript", expected("transcript", p.get("speech_s") or 0)))
        if st.get("skipped"):
            post.set("transcript", "done", note=f"用开会时的实时转写（{st['skipped']}）")
        else:
            post.set("transcript", "done", note=f"重转 {st.get('replaced', 0)} 行，改动 {st.get('changed', 0)} 行")
            record("transcript", time.time() - t1, st.get("speech_s") or 0)
            log("meeting", state="重转写", text=f"{st.get('engine')}：{st.get('replaced')} 行，{st.get('wall_s')} 秒")
        try:
            from .lexicon import learn_meeting
            learned = learn_meeting(app.meetings.root / mid)
            if learned:
                log("meeting", state="词表", text="学到：" + "、".join(f"{r['from']}→{r['to']}" for r in learned))
        except Exception as e:
            log("fast_error", error=f"学词失败：{e!r}")
    except Exception as e:
        log("fast_error", error=f"重转写失败：{e!r}")
        post.set("transcript", "done", note=f"用开会时的实时转写（重转写失败：{type(e).__name__}）")
        traceback.print_exc()

    # 2. 整理：干净版 + 删改清单
    post.set("polish", "running")
    t2 = time.time()
    n_lines = 0
    try:
        from . import polish
        out = polish.run(mid, root=app.meetings.root, cfg=cfg)
        n_lines = out["meta"].get("lines") or 0
        kinds = {}
        for it in out["items"]:
            kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
        note = f"删改 {len(out['items'])} 处" + (f"（模型没调通，只删了脏话）" if out["meta"].get("error") else "")
        post.set("polish", "done", note=note)
        try:                                         # 整理完：识别错字收成纠错规则、高频词收成热词
            from .lexicon import learn_cleanup
            got = learn_cleanup(app.meetings.root / mid)
            if got.get("rules") or got.get("hot"):
                log("meeting", state="词表", text="学到：" + "、".join(
                    [f"{r['from']}→{r['to']}" for r in got.get("rules") or []] + [f"热词 {w}" for w in got.get("hot") or []]))
        except Exception as e:
            log("fast_error", error=f"整理后学词失败：{e!r}")
        record("polish", time.time() - t2, n_lines)
        log("meeting", state="整理", text=note + " " + json.dumps(kinds, ensure_ascii=False))
    except Exception as e:
        log("fast_error", error=f"整理失败：{e!r}")
        post.set("polish", "done", note=f"没整理成（{type(e).__name__}），纪要按原始版写")

    # 3. 纪要：按干净版写
    post.set("summary", "running")
    t3 = time.time()
    try:
        got = app.meetings.get(mid)
        lines = got["transcript"] if got else []
        s = summarize(cfg, got["meeting"] if got else m, lines, app.board.tasks())
        app.meetings.set_summary(mid, s)
        log("meeting", state="纪要", text=s.get("abstract", "")[:120])
        post.set("summary", "done")
        record("summary", time.time() - t3, len(lines))
    except Exception as e:
        log("fast_error", error=f"纪要生成失败：{e!r}")
        post.set("summary", "failed", error=f"纪要生成失败：{e}")

    # 4. 你说话的时长：够 5 小时就在后台训练一次转写模型（低优先级，界面不显示）
    try:
        from . import asr_train
        asr_train.after_meeting(mid, root=app.meetings.root, cfg=cfg)
    except Exception as e:
        log("fast_error", error=f"说话时长记账失败：{e!r}")


def sizes_for(lines: list[dict]) -> dict:
    """新建 PostMeeting 时各步的规模（估 expected_s 用）。"""
    n = len(lines)
    return {"save": 1.0, "transcript": _speech_guess(lines), "polish": n, "summary": n}
