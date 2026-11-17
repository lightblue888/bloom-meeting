# -*- coding: utf-8 -*-
"""接不接：单独问一次"这句是对谁说的"（2026-11-13）。

以前"接不接"和"怎么回"是快嘴同一次调用里一起定的：它手里有整篇论文和一长串规矩，判断接不接只是顺带，
八轮模拟会准确率一直在 85–91% 晃，错的多是"别人在说话它插进来"。现在把这件事拆出来：一个只看最近几句逐字稿的判断，
和快嘴的回话同时跑。

第一版直接问"该不该接"，要它同时套一堆规矩，点了名的话也会答"不接"（冒烟 8 题错 3 题）。现在只问它两件事实：
这句是对谁说的（C Claude / P 会上某个人 / S 自言自语、附和、半截话 / A 对大家），以及是不是主持人或导师拍板了一处改稿；
接不接由代码按规矩定（decide）。

为了快：不带论文，提示词很短；进程提前起好一个备着（起进程要一两秒），用一次就换新的（每次都是干净的上下文）。
走用户的 Claude 套餐（claude -p），只读、不给工具。"""
from __future__ import annotations

import threading
import time

from .cli_brain import CliError, _Proc
from .hands import _resolve_claude

SYSTEM = """你在一个组会软件里，只看刚说的这一句话，回答两件事。会上有主持人（论文作者）、AI 助手 Claude，有时还有同组同学或导师。
逐字稿是语音识别出来的，有错字和半截话（Claude 常被识别成 cloud、克劳德）。

第一件：这句话是对谁说的。
C = 对 Claude 说：叫了它的名字、在对它说话；或者没叫名字，但 Claude 刚说完，这句在回应它、追问它、让它办事。
P = 对会上某个人说（不是 Claude）：叫了那个人的名字，或者在接着那个人的话说、回答那个人的问题。
S = 没对谁说：自言自语、想事情、口头禅、“嗯”“对”“好”这类附和、说了半截。
A = 对大家说：宣布、总结、提议、问在场所有人。

第二件：这句是不是主持人或导师拍板了一处具体的改稿（已经定下来要怎么改，比如“那就去掉吧”“就改成 X”“这个就这么定了”）。是写 1，不是写 0。只是在讨论、还没定的写 0。

第三件：这句有没有叫 Claude 停下、先别说、等一下、别抢话。有写 1，没有写 0。

只输出三个字符，比如 C00、P10、C01、S00。不要别的字。"""


def prompt_for(people: list[dict], lines: list[str], speaker: str, role: str, text: str, since_claude: float | None) -> str:
    """people: [{"name","rank"}]；lines: 之前的逐字稿（"[mm:ss] 名字：话"，含 Claude 说的）；role: 主持人 / 导师 / 同学。"""
    who = "、".join(f"{p['name']}（{p['rank']}）" for p in people)
    ago = "" if since_claude is None else (f"Claude 上一句话在 {since_claude:.0f} 秒前说完。\n" if since_claude < 600 else "")
    return (f"参会：{who}\n{ago}\n之前的逐字稿：\n" + ("\n".join(lines[-10:]) or "（刚开始）") +
            f"\n\n刚说的这句（{speaker}，{role}）：{text}\n\n只输出三个字符。")


def decide(to: str | None, edit: bool, role: str, stop: bool = False) -> bool | None:
    """按规矩定接不接：叫它停下的不接；对 Claude 说的接；主持人 / 导师拍板了改稿，对谁说的都接（Claude 要记下来）；
    对某个人说的、没对谁说的不接；对大家说的拿不准（None：交给快嘴自己判断）。"""
    if stop:
        return False
    if to == "C":
        return True
    if edit and role in ("主持人", "导师"):
        return True
    if to in ("P", "S"):
        return False
    return None


class Gate:
    """备着一个起好的 claude 进程；judge() 用掉它、后台再起一个。出错 / 超时返回 None（调用方照旧走老办法）。"""

    def __init__(self, cfg: dict):
        f = cfg.get("fast") or {}
        g = cfg.get("gate") or {}
        self.exe = _resolve_claude(str((cfg.get("hands") or {}).get("claude", "claude")))
        self.model = str(g.get("model") or f.get("model") or "claude-haiku-5-5")
        self.effort = str(g.get("effort") or "low")
        self.timeout = float(g.get("timeout_s") or 6)
        self._spare: _Proc | None = None
        self._lock = threading.Lock()
        self._warming = False
        self.last_ms: float | None = None
        self.last_raw: str | None = None

    def warm(self) -> None:
        with self._lock:
            if self._spare is not None or self._warming:
                return
            self._warming = True
        threading.Thread(target=self._make_spare, name="gate-warm", daemon=True).start()

    def _make_spare(self) -> None:
        try:
            p = _Proc(self.exe, self.model, self.effort, SYSTEM, None)
        except Exception:
            p = None
        with self._lock:
            self._warming = False
            if self._spare is None:
                self._spare = p
                p = None
        if p is not None:
            p.close()

    def _take(self) -> _Proc:
        with self._lock:
            p, self._spare = self._spare, None
        if p is None or p.dead or p.p.poll() is not None:
            p = _Proc(self.exe, self.model, self.effort, SYSTEM, None)
        return p

    def ask(self, prompt: str) -> str | None:
        t0 = time.time()
        p = self._take()
        self.warm()                                   # 下一个先起着
        try:
            t = p.send(prompt)
            with p.cv:
                while not t["done"]:
                    left = self.timeout - (time.time() - t0)
                    if left <= 0:
                        raise CliError("超时")
                    p.cv.wait(left)
            if t.get("error"):
                raise CliError(t["error"])
            self.last_ms = (time.time() - t0) * 1000
            return "".join(t["deltas"]).strip()
        except Exception:
            return None
        finally:
            threading.Thread(target=p.close, daemon=True).start()

    def facts(self, people: list[dict], lines: list[str], speaker: str, role: str, text: str,
              since_claude: float | None = None) -> tuple[str | None, bool, bool]:
        """(对谁说 C/P/S/A 或 None, 是不是拍板改稿, 是不是叫它停下)。"""
        out = self.ask(prompt_for(people, lines, speaker, role, text, since_claude))
        self.last_raw = out
        if not out:
            return None, False, False
        s = out.strip().upper()
        to = s[0] if s[:1] in ("C", "P", "S", "A") else None
        return to, s[1:2] == "1", s[2:3] == "1"

    def judge(self, people: list[dict], lines: list[str], speaker: str, role: str, text: str,
              since_claude: float | None = None) -> bool | None:
        """True = 该接，False = 不该接，None = 拿不准 / 没判出来。"""
        to, edit, stop = self.facts(people, lines, speaker, role, text, since_claude)
        return decide(to, edit, role, stop)

    def close(self) -> None:
        with self._lock:
            p, self._spare = self._spare, None
        if p is not None:
            p.close()
