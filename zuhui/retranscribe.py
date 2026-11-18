# -*- coding: utf-8 -*-
"""散会后把整场录音里你说的话重新转一遍，换掉逐字稿里实时转出来的文字（实时版另存 transcript.live.jsonl）。

为什么能更准：开会时为了快，实时转写把话切成 ≤10 秒的小块、停 0.5 秒就切，一句话常被切成几截分别听；散会后不赶时间，
同一句话整段（≤14 秒一块）一起听，带上软件词表 + 论文术语当热词；某一块重听丢了字（和实时的比少两成以上），这一块用实时的。

选型实测（2026-11-11，两场会里你说的 ~5.3 分钟、40 段，参考文字是对照几家结果和上下文人工定的，没有逐句听；
混合错误率 = 中文按字、英文按词）：
  开会时的实时转写（Fun-ASR-Nano，≤10 秒小块）        2.15%
  Fun-ASR-Nano 整段重听 + 热词（选它）                 1.03%   CPU 4 线程低优先级，约 0.45 倍说话时长
  Fun-ASR-Nano 整段重听、不带热词                       1.83%
  SeACo-Paraformer large + 热词（显卡 / CPU）          1.99%   显卡 0.08 倍 / CPU 0.29 倍，但 Claude、Opus、
                                                                DeepSeek 这些英文名基本都听错，进程峰值 5.5 GB 内存
  SeACo-Paraformer large 不带热词                       2.39%
云端识别：本机配了 OpenAI 的密钥，但没拿它测（录音要发到第三方中转，没征得同意）。

只换你（主持人）说的行：Claude 的行本来就是它自己生成的原文，访客的行来自会议语音桥、录音里没有。
每一行靠实时转写的分段（录屏目录 live/transcript.jsonl，带录屏里的起止秒）对上录音：同一段话在录音里的起止，
新文字过一遍和实时一样的整形（zuhui/normalize.py，脏话这一步留着，整理时再列进删改清单删）。
对不上的行（回声被剥掉一半的、半截话并进来的）、新文字明显不对（空了、长短差太多）的行，留实时的文字。

占资源：另起一个低优先级进程（zuhui/asr_worker.py），限 4 个线程；转的过程中又开了一场会（录屏开始了），
就把它停掉，等那场录屏结束再接着转（已经转好的段不重转）。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import components
from .config import STATE, load, local_path
from .meetings import ROOT

HERE = Path(__file__).resolve().parent
BLURT_MODELS = components.blurt_home() / "models"
MS = Path.home() / ".cache" / "modelscope" / "models"
_gsv = local_path("gsv_dir")                # 标点模型借 GPT-SoVITS 整合包里的那份（~/.zuhui/paths.json）
ENGINES = {
    # 选这个：中英混说的名字它最准（见文件头的实测）；CPU 上跑，不和语音合成抢显卡
    # 一块最长 14 秒：导出版的提示 + 音频一共 512 个 token，带 24 个热词后长块会被吞掉后半截（实测 20 秒块丢过整句）
    "fun-asr-nano": {"model_dir": BLURT_MODELS / "fun-asr-nano-int8", "max_chunk": 14.0, "uv": True},
    "seaco-paraformer": {"model_dir": MS / "iic--speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch" / "snapshots" / "master",
                         "punc_dir": (Path(_gsv) / "tools" / "asr" / "models" / "punc_ct-transformer_zh-cn-common-vocab272727-pytorch") if _gsv else "",
                         "max_chunk": 60.0, "uv": False},
}
DEFAULT_ENGINE = "fun-asr-nano"
UV_WITH = components.UV_WITH
PAD = 0.2
THREADS = 6                  # 低优先级；又开会了就整个停掉（见 _run_worker），不会和下一场会抢
WORK = STATE / "tmp" / "retranscribe"


def _core(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;…“”\"'（）()\-—/]", "", s or "").lower()


def _load_jsonl(p: Path) -> list[dict]:
    out = []
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _write_jsonl(p: Path, rows: list[dict]) -> None:
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    os.replace(tmp, p)


def _iso(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def _norm(text: str, profanity: bool = True) -> str:
    from .ears import clean_asr
    from .normalize import live
    return live(clean_asr(text or ""), profanity=profanity)


# ---------------------------------------------------------------- 逐字稿的行 ↔ 实时转写的分段
def align(lines: list[dict], segs: list[dict], offset: float) -> tuple[dict[int, list[int]], set[int]]:
    """你说的每一行 → 它用到的那几段（下标）。offset = 录屏比会议早开始了几秒（行的 sec + offset = 录屏里的秒）。
    一行是连着的几段拼起来的（中间补了逗号，整形过）：在这一行落地之前 90 秒内、还没被用过的段里，找拼起来和这一行最像的一串。
    第二个返回值：对上了、但那几段里明显还有别的话的行（Claude 的回声和你的话识别成了一段，开会时把回声剥掉了）——
    重听会把回声也听进来，这些行只记起止，文字留实时的。"""
    seg_core = [_core(_norm(s.get("text") or "")) for s in segs]
    used: set[int] = set()
    out: dict[int, list[int]] = {}
    mixed: set[int] = set()
    for i, l in enumerate(lines):
        if l.get("who") != "teacher":
            continue
        lc = _core(_norm(l.get("text") or ""))
        if len(lc) < 2:
            continue
        try:
            at = float(l.get("in", l.get("sec")) or 0) + offset     # in = 写进来的时刻（sec 是开口时刻）；旧会议没有 in
        except (TypeError, ValueError):
            continue
        cand = [k for k, s in enumerate(segs) if k not in used and at - 90 <= float(s.get("end") or 0) <= at + 2.0]
        best, pick = 0.0, None
        for a in range(len(cand)):
            run = ""
            for b in range(a, len(cand)):
                if b > a and cand[b] != cand[b - 1] + 1:
                    break                                    # 中间隔着用过的段（别的行的）：不连着了
                run += seg_core[cand[b]]
                if len(run) > 2.5 * len(lc) + 10:
                    break
                r = difflib.SequenceMatcher(None, lc, run, autojunk=False).ratio()
                if r > best:
                    best, pick = r, cand[a:b + 1]
        if pick and best >= 0.6:
            out[i] = pick
            used.update(pick)
            run = "".join(seg_core[k] for k in pick)
            same = sum(b.size for b in difflib.SequenceMatcher(None, run, lc, autojunk=False).get_matching_blocks())
            if len(run) - same >= 6:                 # 那几段里有 ≥6 个字这一行里没有（开会时当回声剥掉了）：别重听
                mixed.add(i)
    return out, mixed


def chunks_for(segs: list[dict], idx: list[int], max_chunk: float) -> list[tuple[float, float, list[int]]]:
    """一行用到的几段 → 要重听的几块 (起, 止, 用到的段)（连着说的并成一块，≤ max_chunk 秒，两头各留一点，但不伸进
    前后别的段里：实测两头多听进去的半个词会被认成"我能""对吧"这种多出来的字）。"""
    out: list[list] = []
    for k in idx:
        s, e = float(segs[k]["start"]), float(segs[k]["end"])
        if out and s - out[-1][1] < 1.5 and e - out[-1][0] <= max_chunk:
            out[-1][1] = e
            out[-1][2].append(k)
        else:
            out.append([s, e, [k]])
    starts = sorted(float(x["start"]) for x in segs)
    ends = sorted(float(x["end"]) for x in segs)
    res = []
    for s, e, ks in out:
        prev_end = max([x for x in ends if x <= s + 1e-3] or [0.0])
        next_start = min([x for x in starts if x >= e - 1e-3] or [e + PAD])
        a = max(0.0, s - PAD, (prev_end + s) / 2 if prev_end < s else s)
        b = min(e + PAD, (e + next_start) / 2 if next_start > e else e)
        res.append((a, b, ks))
    return res


# ---------------------------------------------------------------- 录音
def audio_pcm(session: Path, work: Path) -> Path | None:
    """16 kHz 单声道 s16le：实时转写用的那一路（live/audio.pcm，开会时消过回声的）；没有就从 audio.wav / 录屏里抽一份。"""
    p = session / "live" / "audio.pcm"
    if p.exists() and p.stat().st_size > 32000:
        return p
    src = next((session / n for n in ("audio.wav", "recording.mp4") if (session / n).exists()), None)
    ff = components.which("ffmpeg")
    if not src or not ff:
        return None
    out = work / "audio.pcm"
    r = subprocess.run([ff, "-nostdin", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le",
                        str(out)], capture_output=True, creationflags=_NO_WINDOW)
    return out if r.returncode == 0 and out.exists() else None


_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_BELOW_NORMAL = 0x00004000 if os.name == "nt" else 0


def _recording_now() -> bool:
    try:
        from .ears import running_record_job
        return running_record_job() is not None
    except Exception:
        return False


def _worker_cmd(engine: str, job: Path, offline: bool = True) -> list[str]:
    script = str(HERE / "asr_worker.py")
    if ENGINES[engine]["uv"]:
        # uv 找不到 PATH 上的也找 install.ps1 / 桌面端放的地方；offline：只用 uv 缓存里的包（装的时候已缓存）
        return [*components.uv_run(offline), "python", "-I", "-X", "utf8", script, str(job)]
    return [sys.executable, "-I", "-X", "utf8", script, str(job)]


def _run_worker(engine: str, job: Path, status: Path, progress=None, yield_to_meeting: bool = True) -> None:
    """起干活进程，等它转完。中途开了新会（录屏开始）：停掉它，等录屏结束再起（已转好的段不重转）。
    uv 的环境先只用缓存起（不碰网）；起不来（缓存里没这几个包）就联网再起一次。"""
    offline = bool(ENGINES[engine]["uv"])
    while True:
        while yield_to_meeting and _recording_now():
            if progress:
                progress(None, "等这场会开完再接着转")
            time.sleep(5)
        log = open(job.with_suffix(".log"), "a", encoding="utf-8")
        p = subprocess.Popen(_worker_cmd(engine, job, offline), stdout=log, stderr=subprocess.STDOUT, cwd=str(job.parent),
                             creationflags=_NO_WINDOW | _BELOW_NORMAL)
        preempted = False
        while p.poll() is None:
            time.sleep(1.0)
            if progress:
                try:
                    st = json.loads(status.read_text(encoding="utf-8"))
                    progress(st.get("done", 0) / max(1, st.get("total", 1)), st.get("note") or "")
                except (OSError, ValueError):
                    pass
            if yield_to_meeting and _recording_now():
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                preempted = True
                break
        log.close()
        if not preempted:
            if p.returncode != 0 and offline:
                offline = False                     # 缓存里没有（新装的机器、缓存清过）：联网再起一次
                continue
            if p.returncode != 0:
                raise RuntimeError(f"重转写进程退出码 {p.returncode}（日志 {job.with_suffix('.log')}）")
            return


def _join(parts: list[str]) -> str:
    out = ""
    for t in parts:
        t = (t or "").strip()
        if not t:
            continue
        if out and re.match(r"[A-Za-z0-9]", t[0]) and re.match(r"[A-Za-z0-9]", out[-1]):
            out += " "
        elif out and out[-1] not in "，。！？、,.!?；;：:…":
            out += "，"
        out += t
    return out


def chunk_ok(new: str, live: str) -> bool:
    """一块重听出来的字和实时的同一块比：Fun-ASR-Nano 偶尔会把一长块后半截整个吞掉（提示词加热词占了位置，
    16:54 那场里"简单处理就交给 DeepSeek，不过……"整句没了），少了两成以上就不要；多出很多也不要。"""
    nc, lc = _core(_norm(new)), _core(_norm(live))
    if not lc:
        return bool(nc)
    if len(nc) < 0.8 * len(lc) - 2 or len(nc) > 1.4 * len(lc) + 6:
        return False
    sm = difflib.SequenceMatcher(None, lc, nc, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():   # 中间整句没了（实时有 ≥6 个字、重听这里几乎是空的）
        if op in ("delete", "replace") and i2 - i1 >= 6 and (j2 - j1) <= (i2 - i1) / 3:
            return False
    return sm.ratio() >= 0.5


def plausible(new: str, old: str) -> bool:
    """新文字是不是这一行的同一段话：不能空，长短不能差太多，也不能和实时的几乎不沾边。"""
    nc, oc = _core(new), _core(old)
    if len(nc) < 2:
        return False
    if not (0.5 * len(oc) - 4 <= len(nc) <= 1.8 * len(oc) + 6):
        return False
    return difflib.SequenceMatcher(None, oc, nc, autojunk=False).ratio() >= 0.45


# ---------------------------------------------------------------- 总入口
def run(mid: str, root: Path | None = None, cfg: dict | None = None, engine: str | None = None, progress=None,
        hotwords: list[str] | None = None, yield_to_meeting: bool = True, keep_work: bool = False,
        work_root: Path | None = None, on_plan=None) -> dict:
    """重转一场会。返回统计（也写进 <会议>/retranscribe.json）。progress(fraction|None, note) 可选。"""
    t_start = time.time()
    cfg = cfg or load()
    rc = cfg.get("retranscribe") or {}
    engine = engine or rc.get("engine") or _active_engine() or DEFAULT_ENGINE
    if engine not in ENGINES:
        raise ValueError(f"不认识的转写模型：{engine}")
    d = Path(root or ROOT) / Path(str(mid)).name
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8-sig"))
    session = Path(str(meta.get("session") or ""))
    lines = _load_jsonl(d / "transcript.jsonl")
    live_p = d / "transcript.live.jsonl"
    if not live_p.exists():
        shutil.copyfile(d / "transcript.jsonl", live_p)
    live = _load_jsonl(live_p)
    if len(live) != len(lines):          # 两份对不上（不该发生）：以实时版为准重来
        lines = [dict(x) for x in live]
    segs = _load_jsonl(session / "live" / "transcript.jsonl") if session.is_dir() else []
    stats: dict = {"engine": engine, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "lines": len(lines),
                   "teacher_lines": sum(1 for x in live if x.get("who") == "teacher")}
    if not segs:
        stats["skipped"] = "没有录屏的实时分段（回放 / 录屏目录不在了）"
        (d / "retranscribe.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        return stats
    t_meet = _iso(meta.get("started"))
    try:
        rec_meta = json.loads((session / "meta.json").read_text(encoding="utf-8-sig"))
        t_rec = _iso(rec_meta.get("started_at"))
    except (OSError, ValueError):
        t_rec = None
    offset = (t_meet - t_rec) if (t_meet and t_rec and abs(t_meet - t_rec) < 600) else 0.0
    m, mixed = align(live, segs, offset)
    E = ENGINES[engine]
    plan: list[tuple[int, int, list[int]]] = []    # (第几行, 第几块, 这一块用到的段)
    chunks: list[tuple[float, float]] = []
    for i in sorted(m):
        if i in mixed:
            continue
        for a, b, ks in chunks_for(segs, m[i], E["max_chunk"]):
            plan.append((i, len(chunks), ks))
            chunks.append((a, b))
    speech_s = sum(float(segs[k]["end"]) - float(segs[k]["start"]) for i, idx in m.items() if i not in mixed for k in idx)
    stats.update(matched=len(m), mixed=len(mixed), chunks=len(chunks), speech_s=round(speech_s, 1), offset_s=round(offset, 2))
    if on_plan:
        try:
            on_plan(dict(stats))
        except Exception:
            pass
    work = Path(work_root or WORK) / f"{d.name}-{engine}"
    work.mkdir(parents=True, exist_ok=True)
    pcm = audio_pcm(session, work)
    if pcm is None:
        stats["skipped"] = "找不到录音"
        (d / "retranscribe.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        return stats
    if hotwords is None:
        hotwords = _hotwords()
    job = work / "job.json"
    out_p, status_p = work / "out.jsonl", work / "status.json"
    jd = {"engine": engine, "audio": str(pcm), "chunks": [[round(s, 3), round(e, 3)] for s, e in chunks],
          "threads": int(rc.get("threads") or THREADS), "hotwords": hotwords, "model_dir": str(_model_dir(engine)),
          "punc_dir": str(E.get("punc_dir") or ""), "device": rc.get("device") or "cpu",
          "out": str(out_p), "status": str(status_p)}
    try:     # 上次被打断的同一份活（块一样）：接着转；块变了就从头
        old = json.loads(job.read_text(encoding="utf-8"))
        if old.get("chunks") != jd["chunks"] or old.get("hotwords") != hotwords:
            out_p.unlink(missing_ok=True)
    except (OSError, ValueError):
        out_p.unlink(missing_ok=True)
    job.write_text(json.dumps(jd, ensure_ascii=False), encoding="utf-8")
    t0 = time.time()
    _run_worker(engine, job, status_p, progress, yield_to_meeting)
    stats["decode_wall_s"] = round(time.time() - t0, 1)
    got: dict[int, str | None] = {}
    for r in _load_jsonl(out_p):
        got[int(r["k"])] = r.get("text")
    per_line: dict[int, list[str | None]] = {}
    fell_back = 0
    for i, k, ks in plan:
        new_k, live_k = got.get(k), _join([segs[j].get("text") or "" for j in ks])
        if new_k is not None and not chunk_ok(new_k, live_k):
            new_k = live_k                         # 这一块重听丢了字 / 多了一堆：这一块用实时的
            fell_back += 1
        per_line.setdefault(i, []).append(new_k)
    replaced = kept = 0
    changes = []
    new_lines = [dict(x) for x in live]
    for i, idx in m.items():                        # 每一行在录屏里的起止（回顾里点逐字稿跳到那一刻）
        new_lines[i]["start"] = round(float(segs[idx[0]]["start"]), 2)
        new_lines[i]["end"] = round(float(segs[idx[-1]]["end"]), 2)
    for i, parts in per_line.items():
        old = str(live[i].get("text") or "")
        if any(p is None for p in parts):
            kept += 1
            continue
        new = _norm(_join([p or "" for p in parts]), profanity=False)
        if new and new[-1:] not in "。！？!?…" and old[-1:] in "。！？!?…":
            new += old[-1]
        if not plausible(new, old):
            kept += 1
            changes.append({"line": i, "kept": True, "live": old, "new": new})
            continue
        if _core(new) != _core(old):
            changes.append({"line": i, "live": old, "new": new})
        new_lines[i]["text"] = new
        new_lines[i]["asr"] = engine
        replaced += 1
    _write_jsonl(d / "transcript.jsonl", new_lines)
    stats.update(replaced=replaced, kept=kept, changed=sum(1 for c in changes if not c.get("kept")), chunks_fell_back=fell_back,
                 wall_s=round(time.time() - t_start, 1),
                 rtf=round(stats["decode_wall_s"] / max(1.0, speech_s), 3))
    (d / "retranscribe.json").write_text(json.dumps(dict(stats, changes=changes), ensure_ascii=False, indent=1), encoding="utf-8")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return stats


def _hotwords() -> list[str]:
    try:
        from .lexicon import CURRENT, merged_hotwords, learned_rules
        try:
            paper = json.loads(CURRENT.read_text(encoding="utf-8")).get("hotwords") or []
        except (OSError, ValueError):
            paper = []
        hw = merged_hotwords(paper)
        for r in learned_rules():                 # 学到的规则的目标词也当热词（还有位置的话）
            if r.get("to") not in hw and len(hw) < 24 and re.search(r"[A-Za-z]", str(r.get("to"))):
                hw.append(r["to"])
        return hw
    except Exception:
        return []


def _active_engine() -> str | None:
    """训练后换上的模型（zuhui/asr_train.py 验收通过才写 active.json）。"""
    try:
        a = json.loads((STATE / "asr_train" / "active.json").read_text(encoding="utf-8"))
        return a.get("engine") if a.get("engine") in ENGINES else None
    except (OSError, ValueError):
        return None


def _model_dir(engine: str) -> Path:
    try:
        a = json.loads((STATE / "asr_train" / "active.json").read_text(encoding="utf-8"))
        if a.get("engine") == engine and a.get("model_dir") and Path(a["model_dir"]).is_dir():
            return Path(a["model_dir"])
    except (OSError, ValueError):
        pass
    return Path(ENGINES[engine]["model_dir"])


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("mid")
    ap.add_argument("--root")
    ap.add_argument("--engine")
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("--work")
    a = ap.parse_args()
    st = run(a.mid, root=Path(a.root) if a.root else None, engine=a.engine, keep_work=a.keep_work,
             work_root=Path(a.work) if a.work else None,
             progress=lambda f, note: print(f"\r{(f or 0) * 100:5.1f}% {note}", end="", flush=True))
    print()
    print(json.dumps(st, ensure_ascii=False, indent=1))
