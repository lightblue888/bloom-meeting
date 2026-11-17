# -*- coding: utf-8 -*-
"""论文检索：老师闲聊、提问时，把论文里最相关的几段喂给快嘴，让它"接得住"。

纯词法（不装任何向量库）：中文按字二元组、英文/数字按词；每段打分 = 命中项的 IDF 之和（BM25 的简化版）。
索引对象：当前论文镜像 + 同目录下其他 .md/.tex（比如英文各章）。文件变了自动重建。
"""
import math
import re
import time
from pathlib import Path

HEAD_MD = re.compile(r"^(#{1,4})\s+(.*)$")
HEAD_TEX = re.compile(r"^\s*\\(chapter|section|subsection|subsubsection)\*?\s*\{(.+?)\}")
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*|\d+(?:\.\d+)?")   # 单个字母也要（符号 R、U、θ 这类老师常问）
STOP = set("的了是在和与及或也就都而及其这那一个我们你们他们它们对于关于可以能够进行通过使用以及如果但是因为所以因此然后这个那个什么怎么老师同学看看一下这里那里".split())


def tokens(s: str) -> list[str]:
    s = s.lower()
    out = TOKEN.findall(s)
    han = re.sub(r"[^一-鿿]", " ", s)
    for seg in han.split():
        if len(seg) == 1:
            continue
        out += [seg[i:i + 2] for i in range(len(seg) - 1)]
    return [t for t in out if t not in STOP]


class Recall:
    def __init__(self, path: Path, extra_glob: str = "*.md"):
        self.path = Path(path)
        self.extra_glob = extra_glob
        self.chunks: list[dict] = []   # {"doc","section","text","tf":{tok:n},"len":n}
        self.df: dict[str, int] = {}
        self._stamp = None

    # ---------- 索引 ----------
    def _files(self) -> list[Path]:
        files = [self.path] if self.path.exists() else []
        try:
            for f in sorted(self.path.parent.glob(self.extra_glob)) + sorted(self.path.parent.glob("*.tex")):
                if f.resolve() != self.path.resolve() and f.stat().st_size < 2_000_000 and not f.name.startswith("original"):
                    files.append(f)
        except OSError:
            pass
        return files

    def _signature(self):
        sig = []
        for f in self._files():
            try:
                sig.append((str(f), f.stat().st_mtime_ns))
            except OSError:
                pass
        return tuple(sig)

    def refresh(self) -> None:
        sig = self._signature()
        if sig == self._stamp:
            return
        self._stamp = sig
        chunks, df = [], {}
        for f in self._files():
            try:
                text = f.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            section = ""
            for para in re.split(r"\n\s*\n", text):
                para = para.strip()
                if not para:
                    continue
                first = para.splitlines()[0]
                m = HEAD_MD.match(first) or HEAD_TEX.match(first)
                if m:
                    section = m.group(2).strip()
                    body = "\n".join(para.splitlines()[1:]).strip()
                    if not body:
                        continue
                    para = body
                if len(para) < 20:
                    continue
                tf = {}
                for t in tokens(para):
                    tf[t] = tf.get(t, 0) + 1
                for t in tokens(section):              # 小节标题也算进来："第 600 节""3.1 那段"才找得到
                    tf[t] = tf.get(t, 0) + 2
                for t in tf:
                    df[t] = df.get(t, 0) + 1
                chunks.append({"doc": f.stem, "section": section, "text": para, "tf": tf, "len": sum(tf.values())})
        self.chunks, self.df = chunks, df

    # ---------- 查询 ----------
    def search(self, query: str, k: int = 4, budget: int = 1500) -> list[dict]:
        self.refresh()
        if not self.chunks:
            return []
        try:                                           # 口语的"三点一"→ 3.1，"第六百节"→ 第600节
            from .paper import cn2num
            query = cn2num(query)
        except Exception:
            pass
        q = tokens(query)
        if not q:
            return []
        n = len(self.chunks)
        avg = max(1.0, sum(c["len"] for c in self.chunks) / n)
        scored = []
        for c in self.chunks:
            s = 0.0
            for t in set(q):
                f = c["tf"].get(t)
                if not f:
                    continue
                idf = math.log(1 + (n - self.df.get(t, 0) + 0.5) / (self.df.get(t, 0) + 0.5))
                s += idf * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * c["len"] / avg))
            if s > 0:
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        out, used = [], 0
        for s, c in scored[:k * 3]:
            t = c["text"] if len(c["text"]) <= 600 else c["text"][:600] + "…"
            if used + len(t) > budget and out:
                break
            out.append({"doc": c["doc"], "section": c["section"], "text": t, "score": round(s, 2)})
            used += len(t)
            if len(out) >= k:
                break
        return out

    def context(self, query: str, k: int = 4, budget: int = 1500) -> str:
        hits = self.search(query, k, budget)
        if not hits:
            return ""
        lines = ["论文里和老师这句话最相关的几段（供你接话、答问，别照抄）："]
        for h in hits:
            where = f"{h['doc']} › {h['section']}" if h["section"] else h["doc"]
            lines.append(f"- [{where}] {h['text']}")
        return "\n".join(lines)
