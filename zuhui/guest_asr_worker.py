# -*- coding: utf-8 -*-
"""访客语音转写的常驻干活进程（zuhui/guest_ears.py 起它，开会期间一直开着，模型只加载一次）。不 import zuhui 的任何东西：
Fun-ASR-Nano int8 跑在 Blurt 那个 uv 环境里（sherpa-onnx 1.13.8），低优先级、限线程。

  python -I guest_asr_worker.py <job.json>

job.json：{"model_dir": ..., "threads": 4, "hotwords": [...]}（可选 "language"：默认 "中文"，"" = 不指定，见 Nano）
          可选 "hotwords_lite": [...]：主识别器好了以后，后台再建一个只带这几个热词的（积压时用；多占约 1 GB 内存）。
标准输入：一段一段地来，每段 = 一行 JSON 头 {"id": n, "samples": N}（16 kHz 单声道），紧跟 N×2 字节 s16le；
          头里 "hw": "lite" = 用少带热词的那个识别器（还没建好就照常用主识别器）；{"cmd": "quit"} 退出。
标准输出：每段一行 {"id": n, "text": ..., "ms": 解码毫秒, "hw": "lite"（用了少热词的才有）}；
          启动好了先打一行 {"ready": true, "load_s": ...}；有 hotwords_lite 时，那个建好了再打一行 {"lite": true}（建不起来 false）。
热词只能建识别器时给：sherpa-onnx 1.13.8 的 create_stream(hotwords=...) 只有 transducer 模型认（Nano 报
"Only transducer models support contextual biasing" 然后进程直接退出），所以少带热词只能另建一个识别器。
实测（2026-11-14，金标准 6 段 39 秒，4 线程）：24 个热词 0.39 倍语音时长，8 个 0.344（快 12%），不带 0.289（快 26%）。
"""
import json
import os
import re
import sys
import threading
import time

SR = 16000
MAX_SEG_S = 14                      # 一段最长（耳朵那边 12 秒就切；留点余量）
LOOP_CPS, LOOP_SLACK = 30, 40       # 比这还多的字 = 解码打转（Blurt transcribe.py 同一条规则）
LANGUAGE = "中文"                   # 告诉模型转成中文（见 Nano）
SIL = re.compile(r"\s*/sil\b[。，、,.？?！!]*")   # 模型给"没听到字"打的标记，不是字
KANA = re.compile("[぀-ヿㇰ-ㇿｦ-ﾟ]")


def low_priority(normal: bool = False, affinity: int = 0) -> None:
    """低优先级（BELOW_NORMAL）。affinity：只在这些逻辑核上跑（位掩码，0 = 不限）。"""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetCurrentProcess()
            if not normal:
                k.SetPriorityClass(h, 0x4000)                        # BELOW_NORMAL
            if affinity:
                k.SetProcessAffinityMask(h, ctypes.c_size_t(affinity))
        else:
            if not normal:
                os.nice(10)
            if affinity:
                os.sched_setaffinity(0, {i for i in range(64) if affinity >> i & 1})
    except Exception:
        pass


def collapse_repeats(text: str, min_unit: int = 4, times: int = 4, max_unit: int = 200) -> str:
    """解码打转：同一截连说四遍以上只留一遍（和 Blurt transcribe.py 的 collapse_repeats 一样）。"""
    n, i, out = len(text), 0, []
    while i < n:
        best = None
        for u in range(min_unit, min(max_unit, (n - i) // times) + 1):
            unit, k = text[i:i + u], 1
            while text.startswith(unit, i + k * u):
                k += 1
            if k >= times:
                best = (u, k)
                break
        if best:
            u, k = best
            out.append(text[i:i + u])
            j = i + k * u
            if j < n and text[i:i + u].startswith(text[j:]):
                j = n
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out).strip()


class Nano:
    """Fun-ASR-Nano 会中、英、日三种文字。不告诉它语言，提示词只是"语音转写："，写成哪种文字由它自己挑：单独一声
    "嗯""呃"，日语里是同一个音、同一个意思（うん），前后没别的字，它时不时就挑日文——2026-11-11 23:51 那场，访客的
    "嗯"有六次出来是"うん""うんうん"；它认不准的英文词也会写成片假名（"Claude"→"クレイ"）。所以建识别器时就告诉它
    language="中文"（提示词变成"语音转写成中文："），从根上不写假名。2026-11-12 同一套录音、同一个模型实测（343 段）：
      假名：不指定 4 段（"うん。""クレイ、然后。""マクタオ""ローテンでさ"）→ 中文 0 段；
      英文：全英文演讲 10 段错误率 3.1% → 2.5%，25 个英文短句，两种都没有一个汉字；
            金标准 40 句（中英混说）1.27% → 1.51%（多错 3 个字词，两处 Claude 写成 Cloud）；
            另一个模型（Blurt 自带那份）上反过来 1.51% → 1.35%；单独一句很短的英文偶尔会被写成中文。
      解码耗时：没变（每段新/旧中位数 0.96～0.99 倍，在测量误差里）；/sil 照样会出，所以下面当没转出字。
    sherpa-onnx 1.13.8 的语言在建识别器时就定死了（流上 set_option("language")、set_config 实测都不起作用），
    所以只建一个识别器、一直用中文：不用为"遇到假名再按中文转一遍"多开一个（每个约 1 GB），也不多花时间。
    job 里 "language": "" 可以改回不指定。"""

    def __init__(self, job, hotwords=None):
        import inspect
        import sherpa_onnx
        d = job["model_dir"]
        hotwords = job.get("hotwords") if hotwords is None else hotwords
        kw = {"max_new_tokens": 32 + 8 * MAX_SEG_S} if "max_new_tokens" in inspect.signature(
            sherpa_onnx.OfflineRecognizer.from_funasr_nano).parameters else {}
        lang = job.get("language", LANGUAGE)
        if lang and "language" in inspect.signature(sherpa_onnx.OfflineRecognizer.from_funasr_nano).parameters:
            kw["language"] = str(lang)
        self.rec = sherpa_onnx.OfflineRecognizer.from_funasr_nano(
            encoder_adaptor=os.path.join(d, "encoder_adaptor.int8.onnx"), llm=os.path.join(d, "llm.int8.onnx"),
            embedding=os.path.join(d, "embedding.int8.onnx"), tokenizer=os.path.join(d, "Qwen3-0.6B"),
            num_threads=int(job.get("threads") or 4), hotwords="; ".join(hotwords or [])[:600], **kw)

    def __call__(self, x) -> str:
        st = self.rec.create_stream()
        st.accept_waveform(SR, x)
        self.rec.decode_stream(st)
        text = st.result.text.strip()
        if len(text) > LOOP_CPS * len(x) / SR + LOOP_SLACK:
            text = collapse_repeats(text)
        # "/sil"：没听出字时模型写的标记（几乎没声音、或者只是一声含糊的鼻音），语言设成什么都会有：当没转出字
        text = SIL.sub("", text).strip()
        if not re.search(r"\w", text):
            text = ""
        if KANA.search(text):
            print(f"kana despite language: {text!r}", file=sys.stderr, flush=True)
        return text


def main() -> int:
    import numpy as np
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    low_priority(bool(job.get("normal_priority")), int(job.get("affinity") or 0))
    out = sys.stdout
    t0 = time.time()
    try:
        eng = Nano(job)
        eng(np.zeros(SR // 2, dtype=np.float32))          # 第一次解码要分配内存，慢：先空跑一次
    except Exception as ex:
        out.write(json.dumps({"ready": False, "error": f"{type(ex).__name__}: {ex}"[:300]}) + "\n")
        out.flush()
        return 1
    out.write(json.dumps({"ready": True, "load_s": round(time.time() - t0, 2), "pid": os.getpid()}) + "\n")
    out.flush()
    wlock = threading.Lock()                             # 建少热词识别器的线程也要往标准输出写一行
    lite = {"eng": None}
    if isinstance(job.get("hotwords_lite"), list):
        def build_lite():
            try:
                e = Nano(job, hotwords=job["hotwords_lite"])
                e(np.zeros(SR // 2, dtype=np.float32))
                lite["eng"] = e
                row = {"lite": True, "n": len(job["hotwords_lite"])}
            except Exception as ex:
                row = {"lite": False, "error": f"{type(ex).__name__}: {ex}"[:300]}
            with wlock:
                out.write(json.dumps(row) + "\n")
                out.flush()
        threading.Thread(target=build_lite, name="build-lite", daemon=True).start()
    inp = sys.stdin.buffer
    while True:
        head = inp.readline()
        if not head:
            return 0                                     # 主进程没了：跟着退
        try:
            h = json.loads(head)
        except ValueError:
            continue
        if h.get("cmd") == "quit":
            return 0
        n = int(h.get("samples") or 0)
        raw = inp.read(n * 2) if n > 0 else b""
        if len(raw) < n * 2:
            return 0
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        use = lite["eng"] if (h.get("hw") == "lite" and lite["eng"] is not None) else eng
        t1 = time.perf_counter()
        try:
            text, err = (use(x[: SR * MAX_SEG_S]) if n >= SR * 0.2 else ""), None
        except Exception as ex:
            text, err = "", f"{type(ex).__name__}: {ex}"[:200]
        row = {"id": h.get("id"), "text": text, "ms": round((time.perf_counter() - t1) * 1000)}
        if use is not eng:
            row["hw"] = "lite"
        if err:
            row["error"] = err
        with wlock:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()


if __name__ == "__main__":
    sys.exit(main())
