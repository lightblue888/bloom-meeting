# -*- coding: utf-8 -*-
"""快识别（初稿）常驻进程：Paraformer-large（GPT-SoVITS 整合包自带的 FunASR 模型，非自回归，整段一次出字）。
CPU 4 线程解你说的一段中位约 0.17 秒（Fun-ASR-Nano 逐字生成要 1 秒左右）。只用来让模型提前开始想，
逐字稿和送模型的最终文字仍然是 Fun-ASR 的；初稿和最终结果对不上就作废重想（见 draft_match.py）。

用 GPT-SoVITS 的 runtime python 跑（它有 torch + funasr + pypinyin）：
    python -X utf8 -u draft_asr_worker.py <模型目录> [线程数]
stdin 一行一个 JSON 请求，stdout 一行一个 JSON 回答（其余输出全走 stderr）：
    {"op":"asr","id":1,"pcm":"…/live/audio.pcm","s":12.3,"e":15.8} → {"id":1,"text":"…","ms":170}
    {"op":"asr","id":3,"b64":"<16 kHz s16le 单声道，base64>"}       → 同上（访客那一路：声音在内存里）
    {"op":"pinyin","id":2,"text":"互"}                              → {"id":2,"pinyin":["hu"]}
    {"op":"quit"}
启动后先回一行 {"ready":true,"pid":…}。只用 CPU（CUDA_VISIBLE_DEVICES 置空），不占显存、不和 GPT-SoVITS 抢显卡。"""
import base64
import json
import os
import re
import sys
import time

os.environ["CUDA_VISIBLE_DEVICES"] = ""
PROTO = sys.stdout
sys.stdout = sys.stderr            # funasr / tqdm 的打印别混进协议
CJK_GAP = re.compile(r"(?<=[一-鿿]) +| +(?=[一-鿿])")


def out(obj) -> None:
    PROTO.write(json.dumps(obj, ensure_ascii=False) + "\n")
    PROTO.flush()


def main() -> None:
    model = sys.argv[1]
    threads = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    import numpy as np
    import torch
    torch.set_num_threads(threads)
    from funasr import AutoModel
    from pypinyin import lazy_pinyin
    t0 = time.time()
    m = AutoModel(model=model, device="cpu", disable_update=True, disable_pbar=True, disable_log=True)
    for _ in range(2):                                      # 预热（第一遍慢）
        m.generate(input=np.zeros(16000, np.float32) + 1e-4, batch_size_s=60)
    out({"ready": True, "pid": os.getpid(), "load_s": round(time.time() - t0, 1)})
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except Exception:
            continue
        rid, op = req.get("id"), req.get("op")
        try:
            if op == "asr":
                t = time.time()
                if req.get("b64"):
                    raw = base64.b64decode(req["b64"])
                else:
                    a, b = int(float(req["s"]) * 16000), int(float(req["e"]) * 16000)
                    with open(req["pcm"], "rb") as f:
                        f.seek(max(0, a) * 2)
                        raw = f.read(max(0, b - a) * 2)
                x = np.frombuffer(raw[:len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
                text = ""
                if len(x) >= 3200:                              # 不到 0.2 秒不解
                    res = m.generate(input=x, batch_size_s=60)
                    text = CJK_GAP.sub("", ((res[0].get("text") if res else "") or "").strip())
                out({"id": rid, "text": text, "ms": round((time.time() - t) * 1000)})
            elif op == "pinyin":
                out({"id": rid, "pinyin": lazy_pinyin(req.get("text") or "")})
            elif op == "quit":
                break
        except Exception as e:
            out({"id": rid, "error": repr(e)[:300]})


if __name__ == "__main__":
    main()
