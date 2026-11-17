# -*- coding: utf-8 -*-
"""论文的本地 Markdown 镜像：切成小节，给快嘴找"老师现在在说哪一段"。"""
import re
from dataclasses import dataclass
from pathlib import Path

HEAD = re.compile(r"^(#{1,4})\s+(.*)$")
NUM = re.compile(r"(\d+)\s*[\.．点]\s*(\d+)")        # 3.1 / 3点1 / 三点一 在 normalize 后
CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn2num(s: str) -> str:
    """把"三点一""第三节"这类口语数字换成阿拉伯数字，方便匹配小节号。"""
    out = []
    for ch in s:
        out.append(str(CN[ch]) if ch in CN else ch)
    return "".join(out)


@dataclass
class Section:
    level: int
    title: str
    start: int          # 行号
    end: int
    text: str

    @property
    def number(self) -> str | None:
        m = re.match(r"\s*(\d+(?:\.\d+)*)", self.title)
        return m.group(1) if m else None


class Paper:
    def __init__(self, path: Path, title: str = "论文"):
        self.path, self.title = Path(path), title
        self.lines: list[str] = []
        self.sections: list[Section] = []
        self.current: Section | None = None
        self.reload()

    def reload(self):
        if not self.path.exists():
            self.lines, self.sections = [], []
            return
        self.lines = self.path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        secs = []
        if self.path.suffix.lower() == ".tex":
            # LaTeX：\section{..} 当二级标题、\subsection 三级，自动编号成 "3 标题" / "3.1 标题"，好让"三点一"能对上
            tex = re.compile(r"^\s*\\(chapter|section|subsection|subsubsection)\*?\s*\{(.+?)\}")
            lvl_of = {"chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4}
            counters = [0, 0, 0, 0]
            for i, ln in enumerate(self.lines):
                m = tex.match(ln)
                if not m:
                    continue
                lvl = lvl_of[m.group(1)]
                counters[lvl - 1] += 1
                for k in range(lvl, 4):
                    counters[k] = 0
                num = ".".join(str(c) for c in counters[1:lvl] if c) if lvl >= 2 else str(counters[0])
                secs.append([lvl, f"{num} {m.group(2).strip()}".strip(), i])
        else:
            for i, ln in enumerate(self.lines):
                m = HEAD.match(ln)
                if m:
                    secs.append([len(m.group(1)), m.group(2).strip(), i])
        self.sections = []
        for j, (lvl, title, start) in enumerate(secs):
            end = secs[j + 1][2] if j + 1 < len(secs) else len(self.lines)
            self.sections.append(Section(lvl, title, start, end, "\n".join(self.lines[start:end])))

    def outline(self) -> str:
        return "\n".join(("  " * (s.level - 1)) + s.title for s in self.sections) or "（还没有论文镜像）"

    def find(self, utterance: str) -> Section | None:
        """按小节号（3.1）> 标题词命中 > 上一次的小节。"""
        u = cn2num(utterance)
        for m in NUM.finditer(u):
            num = f"{m.group(1)}.{m.group(2)}"
            for s in self.sections:
                if s.number == num:
                    self.current = s
                    return s
        m = re.search(r"第\s*(\d+)\s*[节章部分]", u)
        if m:
            for s in self.sections:
                if s.number == m.group(1):
                    self.current = s
                    return s
        best, score = None, 0
        for s in self.sections:
            words = [w for w in re.split(r"[\s《》、，。：:（）()]+", re.sub(r"^\d+(\.\d+)*\s*", "", s.title)) if len(w) >= 2]
            sc = sum(1 for w in words if w in utterance)
            if sc > score:
                best, score = s, sc
        if best:
            self.current = best
        return self.current

    def by_title(self, title: str | None) -> Section | None:
        """按标题找小节：对比页报上来"老师屏幕上正看着哪一节"时用。先精确，再前缀。"""
        if not title:
            return None
        t = re.sub(r"\s+", " ", title).strip()
        for s in self.sections:
            if re.sub(r"\s+", " ", s.title).strip() == t:
                return s
        for s in self.sections:
            st = re.sub(r"\s+", " ", s.title).strip()
            if st and t and (st.startswith(t[:12]) or t.startswith(st[:12])):
                return s
        return None
