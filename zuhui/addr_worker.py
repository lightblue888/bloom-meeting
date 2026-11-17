# -*- coding: utf-8 -*-
"""接话分类模型的独立进程（zuhui/addr.py 拉起）。

为什么单独一个进程：在后端进程里载入 torch 和显卡库要几十秒，载入时占着 Python 全局锁，后端的接口全部卡住；
torch 出了本地崩溃（0xC0000005 这类）也会把整个会议后端带走。放到子进程里，后端照常响应，这边崩了只是退回老办法。

协议（一行一个 JSON，UTF-8）：
  启动后输出 {"ready": true, "dev": "cuda"} 或 {"ready": false, "error": "..."}
  输入 {"id": 1, "a": "这句", "b": "之前的逐字稿"}  →  输出 {"id": 1, "p": 0.97}
  stdin 关了（后端退出）就退出。
    python -X utf8 -u addr_worker.py <模型目录> <auto|cuda|cpu>"""
import json
import sys

MAXLEN = 320


def emit(o: dict) -> None:
    sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    path, dev = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "auto")
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else "cpu"
        tok = AutoTokenizer.from_pretrained(path)
        m = AutoModelForSequenceClassification.from_pretrained(path)
        if dev == "cuda":
            m = m.half()                          # 显卡上用半精度：显存减半、更快（只要概率，精度够）
        m = m.to(dev).eval()

        def prob(a: str, b: str) -> float:
            enc = tok([a], [b], truncation="only_second", max_length=MAXLEN, return_tensors="pt")
            enc = {k: v.to(dev) for k, v in enc.items()}
            with torch.no_grad():
                return float(torch.softmax(m(**enc).logits.float(), -1)[0, 1])

        prob("Claude还没说过话。某人（主持人）说：预热一下", "[00:00] 某人：你好")   # 第一次推理慢，先跑一次
    except Exception as e:
        emit({"ready": False, "error": repr(e)[:300]})
        return
    emit({"ready": True, "dev": dev})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        qid = None
        try:
            q = json.loads(line)
            qid = q.get("id")
            emit({"id": qid, "p": round(prob(q["a"], q["b"]), 5)})
        except Exception as e:
            emit({"id": qid, "error": repr(e)[:200]})


if __name__ == "__main__":
    main()
