# -*- coding: utf-8 -*-
"""你说话的时长 → 攒够 5 小时，拿你自己的话训练一次转写模型；验收更准才换上，然后计数清零。界面不显示。

已经做好、测过（python -m zuhui.asr_train selftest：临时目录里假训练一遍，触发 / 验收 / 换上 / 清零都走一遍）：
  · 计时：每场会散会收尾的最后一步（zuhui/aftermeeting.py）把这场你说话的秒数加进 ~/.zuhui/asr_train/state.json
    （重转写对上的那些段的时长；同一场只算一次）。
  · 触发：满 5 小时（config.json asr_train.threshold_h 可改）就在后台起一个低优先级进程训练（一天最多试一次）。
  · 训练集：攒下的这些会里，重转写过的、你自己说的行（不含回声混进来的行），每行 = 录音里那一段 + 重转写的文字
    （过了词表规则）。按行的编号哈希留出 10% 做验收集。音频不复制，训练时才切出来、用完删。
  · 验收：固定的"金标准"——2026-11-11 两场会里 ~5 分钟你说的话、人工对过的文字（golden.json + golden.pcm，
    第一次用时从录屏里切出来存一份）；新模型在上面的错误率比现在用的低 15% 以上、而且少错至少 4 个字才换上；
    同时在留出的那 10% 上不能明显变差（不超过现在用的 + 2 个百分点，那份的文字是机器转的，只当护栏）。
  · 换上：写 ~/.zuhui/asr_train/active.json（retranscribe.py 读它：用哪个引擎、哪个模型目录），旧的留着可以换回。
  · 清零：训练跑完（换没换都算）计数清零，历次结果记在 state.json 的 runs 里。

真训练（trainer "funasr-nano"，zuhui/asr_finetune.py）：用 Fun-ASR-Nano 的 PyTorch 原版
（~/.cache/modelscope/models/FunAudioLLM--Fun-ASR-Nano-2512，2026-11-11 经你同意下载，2.14 GB）只微调音频适配层
（1260 万参数，编码器和 Qwen3 语言模型都冻住），导出成和现在一模一样格式的 encoder_adaptor.int8.onnx（导出后自动
和 PyTorch 对一遍，对不上就不要），llm / embedding 两个大文件硬链接复用。笔记本显卡上跑，低优先级；开会时存好进度
让路、散会后接着训。没换上的候选模型删掉大文件，只留报告；验收结论写进 state.json 的 runs 和 train.log。
trainer "fake" 只给自测用。公司那台 3060 机器现在不需要（显存峰值 3.5 GB，笔记本放得下）；真要用，动它之前先问你。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import STATE

HOME = STATE / "asr_train"
THRESHOLD_H = 5.0
HOLDOUT = 0.10
REL_GAIN = 0.15          # 金标准上错误率至少低 15%
ABS_GAIN = 4             # 而且至少少错 4 个字（金标准只有 ~1250 个字，太小的差别是噪声）
GUARD_PTS = 0.02         # 留出集上不能比现在的差 2 个百分点以上
VALID_MAX = 80           # 护栏只用留出集里的前 80 段


def _home(home: Path | None) -> Path:
    h = Path(home or HOME)
    h.mkdir(parents=True, exist_ok=True)
    return h


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(p: Path, obj) -> None:
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def load_state(home: Path | None = None) -> dict:
    s = _read(_home(home) / "state.json", None)
    if not isinstance(s, dict):
        s = {"version": 1, "seconds": 0.0, "meetings": {}, "since": time.strftime("%Y-%m-%dT%H:%M:%S"), "runs": []}
    return s


# ---------------------------------------------------------------- 计时
def speech_seconds(d: Path) -> float:
    """这场会你说话的秒数：重转写对上的那些段（retranscribe.json 的 speech_s）；没重转写过就按行的起止，再不行按字数估。"""
    st = _read(d / "retranscribe.json", {})
    if st.get("speech_s"):
        return float(st["speech_s"])
    lines = [json.loads(x) for x in (d / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()] \
        if (d / "transcript.jsonl").exists() else []
    spans = [float(l["end"]) - float(l["start"]) for l in lines if l.get("who") == "teacher" and "start" in l and "end" in l]
    if spans:
        return float(sum(spans))
    chars = sum(len(re.sub(r"[\s，。！？、,.!?：:；;…]", "", str(l.get("text") or ""))) for l in lines if l.get("who") == "teacher")
    return chars / 4.5


def count(mid: str, root: Path, home: Path | None = None) -> dict:
    """把这场会你说话的时长记上（同一场只记一次）。"""
    s = load_state(home)
    if mid not in s["meetings"]:
        sec = round(speech_seconds(Path(root) / mid), 1)
        s["meetings"][mid] = {"seconds": sec, "root": str(root), "counted": time.strftime("%Y-%m-%dT%H:%M:%S")}
        s["seconds"] = round(float(s.get("seconds") or 0) + sec, 1)
        _write(_home(home) / "state.json", s)
    return s


def due(s: dict, cfg: dict | None = None) -> bool:
    th = float(((cfg or {}).get("asr_train") or {}).get("threshold_h") or THRESHOLD_H) * 3600
    return float(s.get("seconds") or 0) >= th


def after_meeting(mid: str, root: Path, cfg: dict | None = None, home: Path | None = None) -> dict:
    """散会收尾最后一步：记时长；够了就在后台起训练（低优先级，一天最多试一次）。"""
    s = count(mid, root, home)
    if not due(s, cfg) or ((cfg or {}).get("asr_train") or {}).get("enabled") is False:
        return s
    last = float(s.get("last_try") or 0)
    if time.time() - last < 20 * 3600:
        return s
    s["last_try"] = time.time()
    _write(_home(home) / "state.json", s)
    flags = (0x08000000 | 0x00004000) if os.name == "nt" else 0     # 不弹窗、低优先级
    log = open(_home(home) / "train.log", "a", encoding="utf-8")
    from .netenv import with_proxy
    subprocess.Popen([sys.executable, "-X", "utf8", "-m", "zuhui.asr_train", "train"], stdout=log, stderr=subprocess.STDOUT,
                     cwd=str(Path(__file__).resolve().parent.parent), creationflags=flags, env=with_proxy())
    return s


# ---------------------------------------------------------------- 训练集
def _golden_spans(home: Path | None = None) -> dict[str, list[tuple[float, float]]]:
    """金标准用到的录音（录屏目录名 → [(起, 止)]）：训练集里和它重叠的段要拿掉，不然验收等于考原题。"""
    out: dict[str, list[tuple[float, float]]] = {}
    for g in golden(home):
        try:
            sess, span = str(g.get("from") or "").split(" ")
            a, b = span.split("-")
            out.setdefault(sess, []).append((float(a) - 1.0, float(b) + 1.0))
        except ValueError:
            continue
    return out


def build_dataset(s: dict, holdout: float = HOLDOUT, home: Path | None = None) -> dict:
    """攒下的会 → {"train": [...], "valid": [...]}，每条 {key, audio(16k s16le pcm), start, end, text}。
    和金标准重叠的段不要（金标准是从 11-11 两场会里切的）。"""
    items = []
    gold = _golden_spans(home)
    dropped = 0
    for mid, info in (s.get("meetings") or {}).items():
        d = Path(info.get("root") or "") / mid
        meta = _read(d / "meta.json", {})
        pcm = Path(str(meta.get("session") or "")) / "live" / "audio.pcm"
        if not pcm.exists():
            continue
        st = _read(d / "retranscribe.json", {})
        if not st.get("replaced"):
            continue
        try:
            lines = [json.loads(x) for x in (d / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            continue
        for i, l in enumerate(lines):
            if l.get("who") != "teacher" or not l.get("asr") or "start" not in l:
                continue
            dur = float(l["end"]) - float(l["start"])
            text = str(l.get("text") or "").strip()
            if not (0.8 <= dur <= 25.0) or len(re.sub(r"\W", "", text)) < 2:
                continue
            sess = Path(str(meta.get("session") or "")).name
            if any(float(l["start"]) < b and float(l["end"]) > a for a, b in gold.get(sess, [])):
                dropped += 1
                continue
            items.append({"key": f"{mid}-{i:04d}", "audio": str(pcm), "start": float(l["start"]), "end": float(l["end"]),
                          "text": text})
    train, valid = [], []
    for it in items:
        h = int(hashlib.md5(it["key"].encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        (valid if h < holdout else train).append(it)
    return {"train": train, "valid": valid, "seconds": round(sum(x["end"] - x["start"] for x in items), 1),
            "dropped_golden_overlap": dropped}


# ---------------------------------------------------------------- 金标准（人工对过的 ~5 分钟）
def golden(home: Path | None = None) -> list[dict]:
    """[{audio, start, end, text}]：golden.json（一次性从 2026-11-11 两场会的录音切出来存成 golden.pcm）。"""
    g = _read(_home(home) / "golden.json", [])
    return g if isinstance(g, list) else []


def install_golden(reference: Path, sessions: Path, home: Path | None = None) -> int:
    """把人工对过的参考（reference.json：[{sess,start,end,ref}]）连同录音片段存进 asr_train/golden.pcm，录屏删了也还在。"""
    import array
    h = _home(home)
    rows = json.loads(Path(reference).read_text(encoding="utf-8"))
    out, buf, pos = [], array.array("h"), 0.0
    for r in rows:
        src = Path(sessions) / r["sess"] / "live" / "audio.pcm"
        a, b = max(0.0, r["start"] - 0.2), r["end"] + 0.2
        with open(src, "rb") as f:
            f.seek(int(a * 16000) * 2)
            raw = f.read(int((b - a) * 16000) * 2)
        x = array.array("h")
        x.frombytes(raw[: len(raw) // 2 * 2])
        out.append({"audio": "golden.pcm", "start": round(pos, 3), "end": round(pos + len(x) / 16000, 3), "text": r["ref"],
                    "from": f"{r['sess']} {r['start']:.2f}-{r['end']:.2f}"})
        buf.extend(x)
        buf.extend(array.array("h", [0] * 3200))           # 段与段之间垫 0.2 秒静音
        pos += len(x) / 16000 + 0.2
    (h / "golden.pcm").write_bytes(buf.tobytes())
    _write(h / "golden.json", out)
    return len(out)


# ---------------------------------------------------------------- 错误率
def tokens(t: str) -> list[str]:
    cn = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
    t = (t or "").lower()
    t = re.sub(r"[她它]", "他", t).replace("地", "的")
    t = re.sub(r"(?<![a-z])([a-z](?: [a-z])+)(?![a-z])", lambda m: m.group(1).replace(" ", ""), t)
    t = re.sub(r"斜杠|杠", " ", t)
    t = re.sub(r"[零一二两三四五六七八九]", lambda m: cn[m.group(0)], t)
    return re.findall(r"[a-z]+|\d|[一-鿿]", t)


def errors(ref: str, hyp: str) -> tuple[int, int]:
    a, b = tokens(ref), tokens(hyp)
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
    return prev[len(b)], len(a)


def evaluate(engine: str, model_dir: str | None, items: list[dict], home: Path | None = None, base: Path | None = None) -> dict:
    """在 items 上转一遍、算错误率（文字都过一遍实时整形，两边同样处理）。"""
    from . import retranscribe as R
    from .normalize import live as norm
    work = _home(home) / "eval"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    by_audio: dict[str, list[int]] = {}
    for k, it in enumerate(items):
        by_audio.setdefault(it["audio"], []).append(k)
    hyp: dict[int, str] = {}
    for audio, ks in by_audio.items():
        ap = Path(audio) if Path(audio).is_absolute() else Path(base or _home(home)) / audio
        job = work / f"job{len(hyp)}.json"
        out, status = work / f"out{len(hyp)}.jsonl", work / f"st{len(hyp)}.json"
        job.write_text(json.dumps({"engine": engine, "audio": str(ap), "chunks": [[items[k]["start"], items[k]["end"]] for k in ks],
                                   "threads": 6, "hotwords": R._hotwords(), "model_dir": model_dir or str(R.ENGINES[engine]["model_dir"]),
                                   "punc_dir": str(R.ENGINES[engine].get("punc_dir") or ""), "device": "cpu",
                                   "out": str(out), "status": str(status)}, ensure_ascii=False), encoding="utf-8")
        R._run_worker(engine, job, status, None, True)
        for r in R._load_jsonl(out):
            hyp[ks[int(r["k"])]] = r.get("text") or ""
    e = n = 0
    for k, it in enumerate(items):
        x, y = errors(norm(it["text"]), norm(hyp.get(k, "")))
        e, n = e + x, n + y
    return {"errors": e, "tokens": n, "cer": round(e / max(1, n), 4)}


# ---------------------------------------------------------------- 训练
class NotReady(RuntimeError):
    pass


def train_funasr_nano(ds: dict, out: Path, cfg: dict) -> dict:
    """真训练（zuhui/asr_finetune.py，单独的低优先级进程）：只训音频适配层，导出成同样的 int8 ONNX。
    开会时它会存好进度退出（退出码 75），这里等录屏结束再接着训。"""
    from .asr_finetune import BASE, ONNX_BASE, PREEMPTED, _recording
    from . import retranscribe as R
    c = cfg.get("asr_train") or {}
    base = Path(c.get("base_model_dir") or BASE)
    if not (base / "model.pt").exists():
        raise NotReady(f"没有 Fun-ASR-Nano 的 PyTorch 原版权重（{base}）")
    if not (ROOT_ONNX_PY().exists()):
        raise NotReady("找不到带 onnx 包的 Python（.venv-mineru），量化不了")
    if not ds["train"]:
        raise NotReady("训练集是空的")
    out.mkdir(parents=True, exist_ok=True)
    items = list(ds["train"])
    if c.get("max_items"):                      # 只拿一部分（试跑）：打乱后取前 N 条
        import random
        random.Random(0).shuffle(items)
        items = items[: int(c["max_items"])]
    job = {"base": str(base), "onnx_base": str(ONNX_BASE), "out": str(out), "train": items,
           "epochs": int(c.get("epochs") or 2), "lr": float(c.get("lr") or 1e-5), "accum": int(c.get("accum") or 8),
           "encoder_blocks": int(c.get("encoder_blocks") or 0), "hotwords": R._hotwords(),
           "device": c.get("device") or None, "seed": 0}
    (out / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    flags = (0x08000000 | 0x00004000) if os.name == "nt" else 0
    re_dir = out.parent / "reexport-base"           # 没训练、只走一遍同样导出流程的原版：验收时的对照
    if not (re_dir / "encoder_adaptor.int8.onnx").exists():
        re_dir.mkdir(parents=True, exist_ok=True)
        (re_dir / "job.json").write_text(json.dumps({"base": str(base), "onnx_base": str(ONNX_BASE), "out": str(re_dir),
                                                       "train": [], "stage": "export"}), encoding="utf-8")
        with open(re_dir / "train.log", "a", encoding="utf-8") as log:
            subprocess.run([sys.executable, "-X", "utf8", "-m", "zuhui.asr_finetune", str(re_dir / "job.json")],
                           cwd=str(Path(__file__).resolve().parent.parent), stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    def stage(job_path: Path, log_path: Path) -> int:
        with open(log_path, "a", encoding="utf-8") as log:
            return subprocess.run([sys.executable, "-X", "utf8", "-m", "zuhui.asr_finetune", str(job_path)],
                                  cwd=str(Path(__file__).resolve().parent.parent), stdout=log, stderr=subprocess.STDOUT,
                                  creationflags=flags).returncode

    while True:                                   # 第一步：训练（整个模型进显存；开会了就存好进度退出，散会再接着）
        while _recording():
            time.sleep(30)
        rc = stage(out / "job.json", out / "train.log")
        if rc == PREEMPTED:
            continue
        if rc != 0 or not (out / "adapted.pt").exists():
            raise RuntimeError(f"训练进程退出码 {rc}（日志 {out / 'train.log'}）")
        break
    ej = dict(job, stage="export", train=[])        # 第二步：只搭编码器 + 适配层导出（另起进程，内存小得多）
    (out / "export_job.json").write_text(json.dumps(ej, ensure_ascii=False), encoding="utf-8")
    rc = stage(out / "export_job.json", out / "train.log")
    if rc != 0 or not (out / "encoder_adaptor.int8.onnx").exists():
        raise RuntimeError(f"导出进程退出码 {rc}（日志 {out / 'train.log'}）")
    rep = _read(out / "train_report.json", {})
    rep.pop("losses", None)
    return {"engine": "fun-asr-nano", "model_dir": str(out), "report": rep,
            "reexport_base": str(re_dir) if (re_dir / "encoder_adaptor.int8.onnx").exists() else None}


def ROOT_ONNX_PY() -> Path:
    return Path(__file__).resolve().parent.parent / ".venv-mineru" / "Scripts" / "python.exe"


def train_fake(ds: dict, out: Path, cfg: dict) -> dict:
    """自测用：不训练，只造一个"新模型"目录。"""
    out.mkdir(parents=True, exist_ok=True)
    (out / "FAKE").write_text(json.dumps({"train": len(ds["train"]), "valid": len(ds["valid"])}), encoding="utf-8")
    return {"engine": "fun-asr-nano", "model_dir": str(out)}


TRAINERS = {"funasr-nano": train_funasr_nano, "fake": train_fake}


def decide(base_g: dict, cand_g: dict, base_v: dict | None, cand_v: dict | None) -> tuple[bool, str]:
    """换不换：金标准上明显更准（低 15% 以上、少错至少 4 个字），留出集上没明显变差。"""
    if not base_g.get("tokens"):
        return False, "没有金标准，不换"
    gain = base_g["errors"] - cand_g["errors"]
    if cand_g["cer"] > base_g["cer"] * (1 - REL_GAIN) or gain < ABS_GAIN:
        return False, f"金标准上没有明显更准（{base_g['cer']:.2%} → {cand_g['cer']:.2%}，少错 {gain} 个字）"
    if base_v and cand_v and base_v.get("tokens") and cand_v["cer"] > base_v["cer"] + GUARD_PTS:
        return False, f"留出集上变差了（{base_v['cer']:.2%} → {cand_v['cer']:.2%}）"
    return True, f"金标准 {base_g['cer']:.2%} → {cand_g['cer']:.2%}（少错 {gain} 个字）"


def run_training(cfg: dict | None = None, trainer: str | None = None, evaluator=None, home: Path | None = None) -> dict:
    """攒够了：建训练集 → 训练 → 验收 → 更准才换上 → 计数清零。evaluator(engine, model_dir, items) 可换成假的（自测）。"""
    from .config import load
    cfg = cfg or load()
    h = _home(home)
    s = load_state(home)
    trainer = trainer or ((cfg.get("asr_train") or {}).get("trainer")) or "funasr-nano"
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "trainer": trainer, "seconds": s.get("seconds")}
    ds = build_dataset(s, home=home)
    report.update(train=len(ds["train"]), valid=len(ds["valid"]), data_seconds=ds["seconds"],
                  dropped_golden_overlap=ds.get("dropped_golden_overlap", 0))
    active = _read(h / "active.json", {}) or {}
    base_engine = active.get("engine") or "fun-asr-nano"
    base_dir = active.get("model_dir")
    ev = evaluator or (lambda eng, md, items: evaluate(eng, md, items, home=home))
    try:
        out = h / "models" / time.strftime("%Y%m%d-%H%M%S")
        k = 2
        while out.exists():
            out = out.with_name(f"{out.name.split('~')[0]}~{k}")
            k += 1
        cand = TRAINERS[trainer](ds, out, cfg)
    except NotReady as e:
        report["result"] = "not_ready"
        report["why"] = str(e)
        s.setdefault("runs", []).append(report)
        _write(h / "state.json", s)              # 不清零：前提补齐后再训
        return report
    gold = golden(home)
    base_g, cand_g = ev(base_engine, base_dir, gold), ev(cand["engine"], cand["model_dir"], gold)
    # 只是"重新导出一遍、没训练"的原版（量化方式和官方导出不完全一样，实测在金标准上就有差别）：新模型也得比它明显更准，
    # 这样换上才说明是训练起了作用，而不是导出方式的差别
    re_dir = cand.get("reexport_base")
    re_g = ev(cand["engine"], re_dir, gold) if re_dir else None
    ref_g, ref_name = base_g, "现在用的"
    if re_g and re_g.get("tokens") and re_g["errors"] < base_g["errors"]:
        ref_g, ref_name = re_g, "没训练、只重新导出的原版"
    ref_dir = re_dir if ref_g is re_g else base_dir
    valid = ds["valid"][:VALID_MAX]                 # 留出集最多拿 80 段当护栏（5 小时的一成有 160 多段，CPU 上转一遍要半小时）
    base_v = ev(base_engine, ref_dir, valid) if valid else None
    cand_v = ev(cand["engine"], cand["model_dir"], valid) if valid else None
    ok, why = decide(ref_g, cand_g, base_v, cand_v)
    why = f"对照的是{ref_name}：{why}"
    report.update(golden_before=base_g, golden_reexport=re_g, golden_after=cand_g, valid_before=base_v, valid_after=cand_v,
                  switched=ok, why=why, candidate=cand)
    if ok:
        if active:
            _write(h / f"active.before-{time.strftime('%Y%m%d-%H%M%S')}.json", active)
        _write(h / "active.json", {"engine": cand["engine"], "model_dir": cand["model_dir"], "switched": report["at"],
                                   "golden_cer": cand_g["cer"], "previous": active or {"engine": base_engine}})
    report["result"] = "switched" if ok else "kept"
    if not ok and trainer != "fake" and cand.get("model_dir") != re_dir:   # 没换上：大文件删掉（硬链接只删链接），报告留着
        for f in Path(cand["model_dir"]).glob("*.onnx"):
            f.unlink(missing_ok=True)
        shutil.rmtree(Path(cand["model_dir"]) / "Qwen3-0.6B", ignore_errors=True)
    with open(h / "train.log", "a", encoding="utf-8") as f:     # 验收结论记一笔
        f.write(json.dumps({"at": report["at"], "result": report["result"], "why": why,
                            "golden_before": base_g, "golden_reexport": re_g, "golden_after": cand_g},
                           ensure_ascii=False) + chr(10))
    s["runs"] = (s.get("runs") or []) + [report]
    s["seconds"], s["since"] = 0.0, time.strftime("%Y-%m-%dT%H:%M:%S")       # 训练跑完：清零
    s["meetings"] = {k: dict(v, trained=True) for k, v in (s.get("meetings") or {}).items()}
    _write(h / "state.json", s)
    return report


# ---------------------------------------------------------------- 自测：假训练把触发 / 验收 / 换上 / 清零走一遍
def selftest() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="asr-train-test-"))
    root = tmp / "meetings"
    sess = tmp / "sessions" / "s1"
    (sess / "live").mkdir(parents=True)
    (sess / "live" / "audio.pcm").write_bytes(b"\0\0" * 16000 * 40)
    ok = True

    def mk(mid: str, speech_s: float):
        d = root / mid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": mid, "session": str(sess)}), encoding="utf-8")
        lines = [{"t": "00:01", "sec": 1, "who": "teacher", "text": f"第{k}句话说的是转写模型", "start": 2.0 * k, "end": 2.0 * k + 1.5,
                  "asr": "fun-asr-nano"} for k in range(12)]
        (d / "transcript.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
        (d / "retranscribe.json").write_text(json.dumps({"speech_s": speech_s, "replaced": 12}), encoding="utf-8")

    def check(label, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(("OK  " if cond else "BAD ") + label)

    home = tmp / "asr_train"
    cfg = {"asr_train": {"threshold_h": 5}}
    (home).mkdir(parents=True)
    _write(home / "golden.json", [{"audio": str(sess / "live" / "audio.pcm"), "start": 0, "end": 2, "text": "测试"}])
    mk("m1", 2 * 3600)
    s = count("m1", root, home)
    check("一场 2 小时：还没到 5 小时", not due(s, cfg))
    s = count("m1", root, home)
    check("同一场不重复记", abs(s["seconds"] - 7200) < 1)
    mk("m2", 2.5 * 3600)
    mk("m3", 1 * 3600)
    count("m2", root, home)
    s = count("m3", root, home)
    check("三场 5.5 小时：到了", due(s, cfg) and abs(s["seconds"] - 5.5 * 3600) < 1)
    ds = build_dataset(s)
    check(f"训练集 {len(ds['train'])} 条 + 留出 {len(ds['valid'])} 条（36 行里留出约一成）",
          len(ds["train"]) + len(ds["valid"]) == 36 and 0 < len(ds["valid"]) < 12)

    # 新模型明显更准 → 换上、清零
    def better(eng, md, items):
        if md and "models" in str(md):
            return {"errors": 20, "tokens": 1000, "cer": 0.02}
        return {"errors": 30, "tokens": 1000, "cer": 0.03}
    rep = run_training(cfg, trainer="fake", evaluator=better, home=home)
    act = _read(home / "active.json", {})
    check(f"更准（{rep.get('why')}）→ 换上", rep["result"] == "switched" and "models" in str(act.get("model_dir")))
    check("换上后计数清零", load_state(home)["seconds"] == 0)

    # 新模型差不多 → 不换，计数照样清零
    for k, sec in (("m4", 3 * 3600), ("m5", 2.2 * 3600)):
        mk(k, sec)
        count(k, root, home)
    before = _read(home / "active.json", {})
    def same(eng, md, items):
        return {"errors": 19, "tokens": 1000, "cer": 0.019}
    rep = run_training(cfg, trainer="fake", evaluator=same, home=home)
    check(f"没明显更准（{rep.get('why')}）→ 不换", rep["result"] == "kept" and _read(home / "active.json", {}) == before)
    check("不换也清零", load_state(home)["seconds"] == 0)

    # 金标准更准但留出集变差 → 不换
    for k, sec in (("m6", 5.1 * 3600),):
        mk(k, sec)
        count(k, root, home)
    def guard(eng, md, items):
        cand = md and "models" in str(md) and str(md) != str(before.get("model_dir"))
        if items and items[0].get("text") == "测试":
            return {"errors": 10 if cand else 20, "tokens": 1000, "cer": 0.01 if cand else 0.02}
        return {"errors": 90 if cand else 20, "tokens": 1000, "cer": 0.09 if cand else 0.02}
    rep = run_training(cfg, trainer="fake", evaluator=guard, home=home)
    check(f"留出集变差（{rep.get('why')}）→ 不换", rep["result"] == "kept")

    # 真训练前提不全 → 不清零，报告写明缺什么
    mk("m7", 5.2 * 3600)
    count("m7", root, home)
    rep = run_training({"asr_train": {"threshold_h": 5, "base_model_dir": str(tmp / "no-such-model")}}, trainer="funasr-nano",
                       evaluator=same, home=home)
    check(f"真训练前提不全 → 不清零（{rep.get('why', '')[:40]}…）", rep["result"] == "not_ready" and load_state(home)["seconds"] > 5 * 3600)
    shutil.rmtree(tmp, ignore_errors=True)
    print("selftest", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "selftest":
        raise SystemExit(selftest())
    if cmd == "train":
        try:
            if os.name == "nt":
                import ctypes
                ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        except Exception:
            pass
        print(json.dumps(run_training(), ensure_ascii=False, indent=1))
        raise SystemExit(0)
    if cmd == "golden":          # python -m zuhui.asr_train golden <reference.json> [sessions 目录]
        n = install_golden(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) > 3 else Path.home() / "Blurt" / "inbox" / "sessions")
        print(f"金标准 {n} 段存好了：{HOME}")
        raise SystemExit(0)
    s = load_state()
    print(f"你说话累计 {s['seconds'] / 3600:.2f} 小时（{len(s['meetings'])} 场，自 {s.get('since')}），"
          f"满 {THRESHOLD_H:g} 小时训练一次；历次：{len(s.get('runs') or [])}")
