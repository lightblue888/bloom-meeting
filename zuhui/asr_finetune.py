# -*- coding: utf-8 -*-
"""转写模型的真训练（zuhui/asr_train.py 起它，单独一个低优先级进程）：

  python -X utf8 -m zuhui.asr_finetune <job.json>

做什么：拿你自己的话（训练集：录音片段 + 重转写的文字）微调 Fun-ASR-Nano 的 PyTorch 原版
（~/.cache/modelscope/models/FunAudioLLM--Fun-ASR-Nano-2512），再导出成和现在一样的 sherpa-onnx int8 格式。

只训"音频适配层"（audio_adaptor，2 层 Transformer，1260 万参数；可选再放开编码器最后几层），编码器主体（2.2 亿）
和 Qwen3-0.6B 语言模型都冻住。为什么：
  · 换人 / 换麦克风 / 口音适配，动的就是声学到语言模型之间这一截；5 小时一个人的数据放开整个模型很容易训坏；
  · 语言模型不动，sherpa-onnx 那两个大文件（llm.int8.onnx 600 MB、embedding.int8.onnx 155 MB）原样复用，
    只重新导出 encoder_adaptor 一个文件（导出图和原版一致：输入 LFR 特征 x[1,T,560]，输出 encoder_out[1,T',1024]，
    元数据照抄原版）；LoRA 改语言模型就得重导 28 层带 KV 缓存的那个图，风险大得多；
  · 显存小（模型 2.3 GB + 训练 ~1 GB），笔记本显卡和别的程序一起也放得下；优化器状态只有 ~150 MB。
词表里的名字（Claude、DeepSeek……）主要还是靠热词和词表规则；这次训练管的是"你的声音"。

开会时让路：每一步都看一眼有没有在录屏，有就存好进度、退出（退出码 75），asr_train 等录屏结束再接着训。

job.json：{"base": 原版目录, "onnx_base": 现在用的 int8 目录, "out": 输出目录, "train": [{audio,start,end,text}],
           "epochs": 1, "lr": 1e-5, "accum": 8, "encoder_blocks": 0, "hotwords": [...], "device": "cuda"|"cpu",
           "max_items": null, "seed": 0}
输出：<out>/encoder_adaptor.int8.onnx（+ 硬链接过来的 llm / embedding / 分词器，整个目录就是一个可用的模型）、
      <out>/train_report.json（步数、损失、用时、显存 / 内存峰值）。
"""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path.home() / ".cache" / "modelscope" / "models" / "FunAudioLLM--Fun-ASR-Nano-2512"
ONNX_BASE = Path.home() / ".blurt" / "models" / "fun-asr-nano-int8"
ROOT = Path(__file__).resolve().parent.parent
PREEMPTED = 75
SYSTEM = "You are a helpful assistant."


def _low_priority() -> None:
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        else:
            os.nice(10)
    except Exception:
        pass


def _recording() -> bool:
    try:
        from .ears import running_record_job
        return running_record_job() is not None
    except Exception:
        return False


def _peak_rss_mb() -> float | None:
    try:
        import psutil
        mi = psutil.Process().memory_info()
        return round(getattr(mi, "peak_wset", mi.rss) / 1e6)
    except Exception:
        return None


def load(base: Path, device: str):
    import functools
    import torch
    from funasr import AutoModel
    # 2 GB 的 model.pt 按需映射着读（不整份读进内存）：这台机器的提交内存很紧，峰值能少 2 GB
    if not getattr(torch.load, "_bloom_mmap", False):
        _orig = torch.load

        def _mm(*a, **k):
            k.setdefault("mmap", True)
            try:
                return _orig(*a, **k)
            except (RuntimeError, ValueError):
                k.pop("mmap", None)
                return _orig(*a, **k)
        _mm._bloom_mmap = True
        torch.load = _mm
    am = AutoModel(model=str(base), device=device, disable_update=True, disable_pbar=True, disable_log=True)
    torch.manual_seed(0)
    return am


def _slice(audio: str, s: float, e: float):
    import numpy as np
    x = np.memmap(audio, dtype="<i2", mode="r")
    a, b = max(0, int(s * 16000)), min(len(x), int(e * 16000))
    return (np.asarray(x[a:b], dtype=np.float32) / 32768.0).copy()


def example(m, tok, fe, audio, text: str, hotwords: list[str]):
    """一条训练样本：和推理时一模一样的提示（带 / 不带热词），答案是这一段的文字。"""
    import torch
    prompt = m.get_prompt(hotwords, None, True)
    contents = {"system": [SYSTEM], "user": [[prompt + "<|startofspeech|>!!<|endofspeech|>", torch.from_numpy(audio)]],
                "assistant": [text]}
    b = m.data_load_speech(contents, tok, fe, meta_data={})
    b["labels_ids"] = b["labels_ids"][None, :]
    return {k: v for k, v in b.items() if k in ("speech", "speech_lengths", "input_ids", "attention_mask", "labels_ids",
                                                 "fbank_beg", "fake_token_len", "fbank_mask")}


def trainable(m, encoder_blocks: int) -> list:
    for p in m.parameters():
        p.requires_grad = False
    params = list(m.audio_adaptor.parameters())
    if encoder_blocks > 0:
        params += [p for blk in list(m.audio_encoder.tp_encoders)[-encoder_blocks:] for p in blk.parameters()]
    for p in params:
        p.requires_grad = True
    return params


def train(job: dict, am, report: dict) -> None:
    import torch
    m, tok, fe = am.model, am.kwargs["tokenizer"], am.kwargs["frontend"]
    dev = next(m.parameters()).device
    out = Path(job["out"])
    items = list(job["train"])
    random.Random(job.get("seed", 0)).shuffle(items)
    if job.get("max_items"):
        items = items[: int(job["max_items"])]
    epochs, accum = int(job.get("epochs", 1)), int(job.get("accum", 8))
    params = trainable(m, int(job.get("encoder_blocks", 0)))
    m.audio_adaptor.train()
    if int(job.get("encoder_blocks", 0)) > 0:
        for blk in list(m.audio_encoder.tp_encoders)[-int(job["encoder_blocks"]):]:
            blk.train()
    opt = torch.optim.AdamW(params, lr=float(job.get("lr", 1e-5)), weight_decay=0.0)
    total = max(1, math.ceil(len(items) * epochs / accum))
    warm = max(1, total // 10)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / warm) * 0.5 * (1 + math.cos(math.pi * min(1.0, s / total))))
    ck = out / "resume.pt"
    start = 0
    if ck.exists():                               # 开会时让过路：接着上次的进度
        st = torch.load(ck, map_location="cpu", weights_only=False)
        m.audio_adaptor.load_state_dict(st["adaptor"])
        if st.get("encoder"):
            m.audio_encoder.load_state_dict(st["encoder"], strict=False)
        opt.load_state_dict(st["opt"])
        sched.load_state_dict(st["sched"])
        start = st["k"]
        report.update(st.get("report") or {})
    prog = None
    try:
        sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".claude", "progress"))
        from claude_progress import Progress
        prog = Progress("helperE_asr_finetune", total=len(items) * epochs)
        prog.set(start)
    except Exception:
        pass
    hot = list(job.get("hotwords") or [])
    losses = report.setdefault("losses", [])
    t0 = time.time()
    k = start
    rnd = random.Random(1234)
    for ep in range(epochs):
        for j, it in enumerate(items):
            kk = ep * len(items) + j
            if kk < start:
                continue
            if _recording():                       # 开会了：存好进度，让路
                torch.save({"adaptor": m.audio_adaptor.state_dict(), "opt": opt.state_dict(), "sched": sched.state_dict(),
                            "k": kk, "report": report, "encoder": _enc_state(m, job)}, ck)
                report["preempted_at"] = kk
                raise SystemExit(PREEMPTED)
            a = _slice(it["audio"], float(it["start"]), float(it["end"]))
            b = example(m, tok, fe, a, it["text"], hot if rnd.random() < 0.5 else [])
            b = {key: v.to(dev) if hasattr(v, "to") else v for key, v in b.items()}
            loss, stats, _ = m(**b)
            (loss / accum).backward()
            losses.append(round(float(loss.detach().float().cpu()), 4))
            k = kk + 1
            if k % accum == 0 or k == len(items) * epochs:
                torch.nn.utils.clip_grad_norm_(params, 5.0)
                opt.step()
                sched.step()
                opt.zero_grad(set_to_none=True)
            if prog:
                prog.set(k, note=f"ep{ep + 1} loss {sum(losses[-20:]) / len(losses[-20:]):.3f}")
    report["steps"] = k
    report["train_s"] = round(report.get("train_s", 0) + time.time() - t0, 1)
    report["loss_first20"] = round(sum(losses[:20]) / max(1, len(losses[:20])), 4)
    report["loss_last20"] = round(sum(losses[-20:]) / max(1, len(losses[-20:])), 4)
    if prog:
        prog.done()
    ck.unlink(missing_ok=True)


def _enc_state(m, job) -> dict | None:
    n = int(job.get("encoder_blocks", 0))
    if n <= 0:
        return None
    keep = {f"tp_encoders.{i}." for i in range(len(m.audio_encoder.tp_encoders) - n, len(m.audio_encoder.tp_encoders))}
    return {k: v for k, v in m.audio_encoder.state_dict().items() if any(k.startswith(p) for p in keep)}


# ---------------------------------------------------------------- 导出成 sherpa-onnx 用的 int8
class Light:
    """只有编码器 + 适配层（导出用）：按 config.yaml 搭，权重从 model.pt 按需映射读（不整份读进内存，也不建 0.6B 的语言模型）；
    再叠上训练好的适配层。forward(x) = FunASRNano.forward_export(x, 长度)。"""

    def __new__(cls, base: Path, adapted: Path | None = None):
        import torch
        import yaml
        from funasr.frontends.wav_frontend import WavFrontend
        from funasr.models.llm_asr.adaptor import Transformer as Adaptor
        from funasr.models.sense_voice.model import SenseVoiceEncoderSmall
        conf = yaml.safe_load((Path(base) / "config.yaml").read_text(encoding="utf-8"))
        fconf = dict(conf["frontend_conf"])
        fe = WavFrontend(**fconf)
        ec = {k: v for k, v in conf["audio_encoder_conf"].items() if k not in ("freeze", "freeze_layer_num", "hub")}
        enc = SenseVoiceEncoderSmall(input_size=fconf["n_mels"] * fconf["lfr_m"], **ec)
        ac = {k: v for k, v in conf["audio_adaptor_conf"].items() if k not in ("freeze",)}
        ada = Adaptor(**ac)
        sd = torch.load(Path(base) / "model.pt", map_location="cpu", mmap=True, weights_only=True)
        enc.load_state_dict({k[len("audio_encoder."):]: v for k, v in sd.items() if k.startswith("audio_encoder.")})
        ada.load_state_dict({k[len("audio_adaptor."):]: v for k, v in sd.items() if k.startswith("audio_adaptor.")})
        del sd
        if adapted and Path(adapted).exists():
            st = torch.load(adapted, map_location="cpu", weights_only=True)
            ada.load_state_dict(st["adaptor"])
            if st.get("encoder"):
                enc.load_state_dict(st["encoder"], strict=False)

        class _M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.audio_encoder, self.audio_adaptor = enc, ada

            def forward_export(self, speech, lengths):
                x, olens = self.audio_encoder(speech, lengths)
                return self.audio_adaptor(x, olens)

        m = _M().float().eval()
        return m, fe


def export(m, fe, out: Path, onnx_base: Path = ONNX_BASE) -> Path:
    """encoder + adaptor → ONNX（fp32）→ 动态量化 int8（和原版一样只量化 MatMul，权重 uint8）→ 元数据照抄原版；
    再把 llm / embedding / 分词器硬链接过来，<out> 就是一个完整可用的模型目录。m = Light(...) 搭出来的那个。"""
    import torch

    class Wrap(torch.nn.Module):
        def __init__(self, mm):
            super().__init__()
            self.mm = mm

        def forward(self, x):
            # 长度得从张量算出来（不能是 Python 整数），不然导出的图把长度写死成样例的 120 帧
            lens = torch.ones_like(x[:, :, 0], dtype=torch.int32).sum(1).to(torch.int32)
            y, _ = self.mm.forward_export(x, lens)
            return y

    # 适配层的 make_pad_mask 用 int(max(长度))：导出时换成全用张量算的版本（结果一样，只是长度不写死）
    import funasr.models.llm_asr.adaptor as _adaptor

    def _pad_mask(lengths, xs=None, length_dim=-1, maxlen=None):
        lengths = torch.as_tensor(lengths)
        rng = torch.arange(lengths.max(), device=lengths.device)
        return rng[None, :] >= lengths[:, None]

    _adaptor.make_pad_mask = _pad_mask

    # Wrap 一定要 eval：torch.onnx.export 导完会把顶层模块"恢复"成它原来的模式，新建的 Module 默认是 train，会打开 dropout
    w = Wrap(m).eval()
    x = torch.randn(1, 120, 560)
    with torch.no_grad():                         # 先正常跑一遍长一点的输入再导出（实测这样导出的图和 PyTorch 完全一致）
        w(torch.randn(1, 600, 560) * 3 + 10)
    fp32 = out / "encoder_adaptor.fp32.onnx"
    try:
        import onnx  # noqa: F401
    except ImportError:
        # 主 Python 里没有 onnx 包（不往里装）：老导出器只在"插自定义 onnxscript 函数"那一步用它，这个图没有，跳过即可
        from torch.onnx._internal.torchscript_exporter import onnx_proto_utils as _opu
        _opu._add_onnxscript_fn = lambda model_bytes, custom_opsets: model_bytes
    torch.onnx.export(w, (x,), str(fp32), input_names=["x"], output_names=["encoder_out"],
                      dynamic_axes={"x": {1: "T"}, "encoder_out": {1: "T_out"}}, opset_version=18, dynamo=False)
    q = out / "encoder_adaptor.int8.onnx"
    r = subprocess.run([str(_onnx_python()), "-I", "-X", "utf8", str(Path(__file__).with_name("asr_quant.py")), str(fp32), str(q),
                        str(onnx_base / "encoder_adaptor.int8.onnx")], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0 or not q.exists():
        raise RuntimeError(f"量化失败：{r.stderr[-800:]}")
    check = parity(w, fp32, q, fe)                # 导出的图和 PyTorch 对得上（换一个长度试，长度不能写死）
    (out / "export_check.json").write_text(json.dumps(check), encoding="utf-8")
    if check["fp32_cos"] < 0.98 or check["int8_cos"] < 0.97:
        raise RuntimeError(f"导出的模型和 PyTorch 对不上：{check}")
    for f in list(out.glob("encoder_adaptor.fp32.onnx*")):
        f.unlink(missing_ok=True)
    for name in ("llm.int8.onnx", "embedding.int8.onnx"):
        _link(onnx_base / name, out / name)
    (out / "Qwen3-0.6B").mkdir(exist_ok=True)
    for f in (onnx_base / "Qwen3-0.6B").iterdir():
        _link(f, out / "Qwen3-0.6B" / f.name)
    return q


def parity(w, fp32: Path, q: Path, fe=None) -> dict:
    """同一段真实语音的特征（长度和导出时的样例不一样）过 PyTorch、fp32 ONNX、int8 ONNX，比余弦相似度。
    随机数当特征不行：原版 int8 在随机特征上也只有 0.8 几（实测），真实语音上是 0.995。"""
    import numpy as np
    import onnxruntime as ort
    import torch
    x = None
    g = Path.home() / ".zuhui" / "asr_train" / "golden.pcm"
    if fe is not None and g.exists():
        from funasr.utils.load_utils import extract_fbank
        a = (np.fromfile(g, dtype="<i2", count=16000 * 13).astype(np.float32) / 32768.0)
        x, _ = extract_fbank(torch.from_numpy(a)[None], data_type="sound", frontend=fe, is_final=True)
        x = x.float()
    if x is None:
        x = torch.randn(1, 211, 560) * 3 + 10
    w.eval()
    with torch.no_grad():
        ref = w(x).numpy()
    res = {"T": int(x.shape[1])}
    for key, path in (("fp32", fp32), ("int8", q)):
        s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        y = s.run(None, {"x": x.numpy()})[0]
        if y.shape != ref.shape:
            res[key + "_cos"] = -1.0
            res[key + "_shape"] = list(y.shape)
            continue
        a, b = ref.ravel(), y.ravel()
        res[key + "_cos"] = round(float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)), 5)
    return res


def _link(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    try:
        os.link(src, dst)                         # 同一个盘：硬链接，不占地方
    except OSError:
        shutil.copyfile(src, dst)


def _onnx_python() -> Path:
    """带 onnx 包的 Python（量化要用）：项目里 MinerU 的那个虚拟环境。"""
    p = ROOT / ".venv-mineru" / "Scripts" / "python.exe"
    if p.exists():
        return p
    raise RuntimeError("找不到带 onnx 包的 Python（.venv-mineru）")


def main() -> int:
    """job["stage"]："train"（默认）= 载入整个模型训练，存 adapted.pt；"export" = 只搭编码器 + 适配层导出（内存小得多）。"""
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    _low_priority()
    import torch
    out = Path(job["out"])
    out.mkdir(parents=True, exist_ok=True)
    rp = out / "train_report.json"
    report = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
    stage = job.get("stage") or ("export" if job.get("export_only") else "train")
    t0 = time.time()
    if stage == "train":
        report.update(items=len(job["train"]), seconds_audio=round(sum(float(i["end"]) - float(i["start"]) for i in job["train"]), 1),
                      epochs=job.get("epochs", 1), encoder_blocks=job.get("encoder_blocks", 0), lr=job.get("lr", 1e-5))
        dev = job.get("device") or ("cuda" if torch.cuda.is_available() else "cpu")
        report["device"] = dev
        am = load(Path(job.get("base") or BASE), dev)
        report["load_s"] = round(time.time() - t0, 1)
        try:
            train(job, am, report)
        except SystemExit:
            rp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
            raise
        torch.save({"adaptor": {k: v.detach().float().cpu() for k, v in am.model.audio_adaptor.state_dict().items()},
                    "encoder": _enc_state(am.model, job)}, out / "adapted.pt")
        if dev == "cuda":
            report["peak_vram_mb"] = round(torch.cuda.max_memory_allocated() / 1e6)
        report["train_peak_ram_mb"] = _peak_rss_mb()
        report["train_total_s"] = round(time.time() - t0, 1)
    else:
        m, fe = Light(Path(job.get("base") or BASE), out / "adapted.pt")
        export(m, fe, out, Path(job.get("onnx_base") or ONNX_BASE))
        report["export_s"] = round(time.time() - t0, 1)
        report["export_peak_ram_mb"] = _peak_rss_mb()
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "losses"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
