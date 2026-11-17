# -*- coding: utf-8 -*-
"""散会后重转写的干活进程（zuhui/retranscribe.py 起它，低优先级、限线程）。不 import zuhui 的任何东西：
Fun-ASR-Nano 跑在 Blurt 那个 uv 环境里（sherpa-onnx 1.13.8），SeACo-Paraformer 跑在主 Python 里（funasr + torch）。

  python -I asr_worker.py <job.json>

job.json：{"engine": "fun-asr-nano" | "seaco-paraformer", "audio": <16 kHz 单声道 s16le 的 .pcm>, "chunks": [[起秒, 止秒], ...],
           "threads": 4, "hotwords": [...], "model_dir": ..., "punc_dir": ..., "device": "cpu"|"cuda",
           "out": <结果 .jsonl，每段一行 {"k": 第几段, "text": ...}；已经有的段跳过（被打断后接着转）>,
           "status": <进度 .json：{"done","total","pid","engine","t"}>}
"""
import json
import os
import sys
import time

SR = 16000


def low_priority() -> None:
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
        else:
            os.nice(10)
    except Exception:
        pass


def load_pcm(path: str):
    import numpy as np
    return np.memmap(path, dtype="<i2", mode="r")


def slice_f32(pcm, s: float, e: float):
    import numpy as np
    a, b = max(0, int(s * SR)), min(len(pcm), int(e * SR))
    return (np.asarray(pcm[a:b], dtype=np.float32) / 32768.0).copy()


class Nano:
    """Fun-ASR-Nano int8（和开会时实时转写同一个模型），整段整段地听：块比实时的大（≤20 秒），带热词。"""
    def __init__(self, job):
        import inspect
        import sherpa_onnx
        d = job["model_dir"]
        kw = {"max_new_tokens": 32 + 8 * 20} if "max_new_tokens" in inspect.signature(
            sherpa_onnx.OfflineRecognizer.from_funasr_nano).parameters else {}
        self.rec = sherpa_onnx.OfflineRecognizer.from_funasr_nano(
            encoder_adaptor=os.path.join(d, "encoder_adaptor.int8.onnx"), llm=os.path.join(d, "llm.int8.onnx"),
            embedding=os.path.join(d, "embedding.int8.onnx"), tokenizer=os.path.join(d, "Qwen3-0.6B"),
            num_threads=int(job.get("threads") or 4), hotwords="; ".join(job.get("hotwords") or [])[:600], **kw)

    def __call__(self, x) -> str:
        st = self.rec.create_stream()
        st.accept_waveform(SR, x)
        self.rec.decode_stream(st)
        return st.result.text.strip()


class Seaco:
    """SeACo-Paraformer large（funasr），可以带热词；中文好，英文名字差（实测）。"""
    def __init__(self, job):
        import torch
        torch.set_num_threads(int(job.get("threads") or 4))
        from funasr import AutoModel
        kw = {"punc_model": job["punc_dir"]} if job.get("punc_dir") else {}
        self.m = AutoModel(model=job["model_dir"], device=job.get("device") or "cpu", disable_update=True,
                           disable_pbar=True, disable_log=True, **kw)
        self.hot = " ".join(job.get("hotwords") or [])

    def __call__(self, x) -> str:
        r = self.m.generate(input=x, batch_size_s=60, **({"hotword": self.hot} if self.hot else {}))
        return (r[0].get("text") or "").strip() if r else ""


def main() -> int:
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    low_priority()
    done = set()
    if os.path.exists(job["out"]):
        for raw in open(job["out"], encoding="utf-8"):
            try:
                done.add(int(json.loads(raw)["k"]))
            except (ValueError, KeyError, TypeError):
                pass
    chunks = job["chunks"]

    def status(n: int, note: str = "") -> None:
        tmp = job["status"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"done": n, "total": len(chunks), "pid": os.getpid(), "engine": job["engine"], "t": time.time(),
                       "note": note}, f)
        os.replace(tmp, job["status"])

    status(len(done), "loading")
    t0 = time.time()
    eng = Nano(job) if job["engine"] == "fun-asr-nano" else Seaco(job)
    load_s = time.time() - t0
    pcm = load_pcm(job["audio"])
    n = len(done)
    status(n, f"loaded in {load_s:.1f}s")
    with open(job["out"], "a", encoding="utf-8") as out:
        for k, (s, e) in enumerate(chunks):
            if k in done:
                continue
            x = slice_f32(pcm, float(s), float(e))
            try:
                text = eng(x) if len(x) > SR * 0.2 else ""
            except Exception as ex:   # 一段出错不影响别的段：这一段就用实时的
                text, err = None, f"{type(ex).__name__}: {ex}"[:200]
            row = {"k": k, "text": text}
            if text is None:
                row["error"] = err
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            n += 1
            status(n)
    status(n, f"done load {load_s:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
