# -*- coding: utf-8 -*-
"""快嘴换成用户 Claude 套餐里的模型（不走 API，不另花钱）：常驻一个 Claude Code 进程（stream-json 进出、边生成边出字）。
实测（2026-11-12，FinTSB 全文约 3.5 万 token 的前缀）：Haiku 5.5、effort low，进程热起来以后首字 0.83–0.88 秒，
和 DeepSeek 差不多；第一句 4.6 秒、第二句 2 秒是起进程和第一次写缓存——所以开会前先预热。Sonnet 5.5 判断题首字 4.4 秒，太慢。

- 进程自己记着对话：每轮只送新的一句（前面再补上这期间软件替它记下的话，见 FastBrain._cli_turn）。
- 系统提示（快嘴提示词 + 论文全文）变了（论文被改了）：后台另起一个进程、预热好再换上，换之前照旧用旧的。
- 一轮被打断（生成器提前关掉）：发 interrupt；不认就等它把这一轮说完（下一轮要排在它后面）。
- 出错 / 碰到额度上限：抛异常，调用方这一句退回 DeepSeek。
只读：--tools "" 不给任何工具；--setting-sources "" 不读用户的设置和 CLAUDE.md；不存会话。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .hands import _resolve_claude, _strip_claude_env


class CliError(RuntimeError):
    pass


class _Proc:
    """一个常驻的 claude 进程：系统提示固定，一轮一轮送话、按轮收事件。"""

    def __init__(self, exe: str, model: str, effort: str, system: str, paper_dir: str | None = None):
        self.system = system
        d = Path(tempfile.mkdtemp(prefix="bloom-cli-brain-"))
        self._tmp = d
        sp = d / "system.md"
        sp.write_text(system, encoding="utf-8")
        args = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--model", model, "--effort", effort,
                "--system-prompt-file", str(sp), "--no-session-persistence", "--setting-sources", ""]
        if paper_dir and Path(paper_dir).is_dir():
            # 只读地在论文里搜、读（数次数、找位置、念原句时用）：进程开在论文所在的文件夹，只给 Read/Grep/Glob
            from .hands import lockdown_args
            args += lockdown_args(paper_dir, editable=None)
            cwd = str(paper_dir)
        else:
            args += ["--tools", ""]
            cwd = str(d)
        flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
        self.p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace", env=_strip_claude_env(),
                                  cwd=cwd, bufsize=1, creationflags=flags)
        self.cv = threading.Condition()
        self.mark = None                     # 这个进程见过的对话到哪一句为止（FastBrain.history 里那一项）；None = 一句都没见过
        self.turns: list[dict] = []          # 每轮：{"deltas": [], "done": bool, "error": str|None}
        self.cur = 0                         # 正在收的是第几轮（按 result 事件推进）
        self.dead = False
        self.err_tail = ""
        threading.Thread(target=self._read, name="cli-brain-out", daemon=True).start()
        threading.Thread(target=self._read_err, name="cli-brain-err", daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with self.cv:
                if self.cur >= len(self.turns):
                    continue                 # 没在等的输出（启动时的 system/init 事件）
                t = self.turns[self.cur]
                if ev.get("type") == "stream_event":
                    d = (ev.get("event") or {}).get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        t["deltas"].append(d["text"])
                elif ev.get("type") == "result":
                    if ev.get("is_error"):
                        t["error"] = str(ev.get("result") or ev.get("subtype") or "error")[:200]
                    t["done"] = True
                    self.cur += 1
                self.cv.notify_all()
        with self.cv:
            self.dead = True
            for t in self.turns[self.cur:]:
                t["done"] = True
                t["error"] = t["error"] or ("进程退出了：" + self.err_tail[-200:])
            self.cv.notify_all()

    def _read_err(self):
        for line in self.p.stderr:
            self.err_tail = (self.err_tail + line)[-2000:]

    def send(self, content: str) -> dict:
        if self.dead or self.p.poll() is not None:
            raise CliError("进程没在跑：" + self.err_tail[-200:])
        t = {"deltas": [], "done": False, "error": None, "t0": time.time()}
        with self.cv:
            self.turns.append(t)
        self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                                      ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        return t

    def interrupt(self):
        try:
            self.p.stdin.write(json.dumps({"type": "control_request", "request_id": f"int-{time.time():.3f}",
                                           "request": {"subtype": "interrupt"}}) + "\n")
            self.p.stdin.flush()
        except Exception:
            pass

    def busy(self) -> bool:
        with self.cv:
            return self.cur < len(self.turns)

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=3)
        except Exception:
            self.p.kill()
        shutil.rmtree(getattr(self, "_tmp", "") or "", ignore_errors=True)   # 进程关了，它的临时目录也删掉


class CliSession:
    """快嘴用的常驻会话：换论文时后台换进程；一轮的文字一段段吐出来。"""

    WARM_TEXT = "（软件预热：这一句不是对方说的，只回 #silent）"

    def __init__(self, cfg: dict, model: str, paper_file: str | None = None):
        self.cfg = cfg
        self.model = model
        self.paper_file = Path(paper_file) if paper_file else None
        self.effort = str((cfg.get("fast") or {}).get("cli_effort") or "low")
        self.exe = _resolve_claude(str((cfg.get("hands") or {}).get("claude", "claude")))
        self.lock = threading.Lock()
        self.proc: _Proc | None = None
        self.next: _Proc | None = None       # 论文变了：后台在预热的新进程
        self.last_proc: _Proc | None = None  # 上一轮用的是哪个进程（这一轮说完，调用方在它身上记"见到哪一句了"）
        # 新进程预热时先补上这场会到现在的对话：返回 (前情文字, 前情的最后一项)。没有就只预热。
        # 2026-11-13 第 2 轮：每改一次稿换一个进程，新进程只收到换之后的几句，前面整场会它都没见过，
        # 把自己刚改的数当成原文、不认自己说过的话。
        self.primer = None

    def _spawn_warm(self, system: str) -> _Proc:
        pf = self.paper_file
        if pf is not None and pf.is_file():
            system = system + (f"\n\n【查原文】论文文件就在当前目录：{pf.name}。要数一个词出现几次、找某句话在哪、念原句、"
                               "核对自己记得对不对，先用 Grep 在这个文件里搜（必要时 Read 那几行），查到再说；别凭印象报数、报位置。"
                               "没查过就别说“我看了全文”“我核对过了”。查完直接说结论，不念行号。")
        p = _Proc(self.exe, self.model, self.effort, system, str(pf.parent) if pf is not None and pf.is_file() else None)
        recap, mark = "", None
        if self.primer is not None:
            try:
                recap, mark = self.primer()
            except Exception:
                recap, mark = "", None
        warm = self.WARM_TEXT
        if recap:
            warm = ("【这场会到现在的对话（论文改过，软件给你换了个新进程，之前说过的都在这里；你说过的话、改过的稿以这里为准）】\n"
                    + recap + "\n\n" + self.WARM_TEXT)
        t = p.send(warm)
        with p.cv:
            p.cv.wait_for(lambda: t["done"], timeout=90)
        if t["error"]:
            p.close()
            raise CliError(t["error"])
        p.mark = mark
        return p

    def warm(self, system: str, block: bool = True) -> None:
        """预热：这个系统提示的进程起好、缓存写好。block=False：后台换（论文改了，照旧用旧进程直到新的好）。"""
        if self.proc and self.proc.system == system and not self.proc.dead:
            return
        if not block:
            if self.next is not None and self.next.system == system:
                return

            def bg():
                try:
                    np = self._spawn_warm(system)
                except Exception:
                    return
                with self.lock:
                    old, self.proc = self.proc, np
                if old is not None:
                    threading.Thread(target=old.close, daemon=True).start()
            self.next = _Proc.__new__(_Proc)
            self.next.system = system
            threading.Thread(target=bg, name="cli-brain-warm", daemon=True).start()
            return
        np = self._spawn_warm(system)
        with self.lock:
            old, self.proc = self.proc, np
        if old is not None:
            old.close()

    def deltas(self, system: str, content, timeout: float = 30.0):
        """送一轮，一段段吐出文字。content：这一轮的话；也可以是 content(进程)，按这个进程见过哪些对话拼
        （新换上的进程要补的前情和旧进程不一样）。生成器被提前关掉（打断）：发 interrupt。出错抛 CliError。"""
        with self.lock:
            p = self.proc
        if p is None or p.dead or (p.system != system and (self.next is None or self.next.system != system)):
            self.warm(system, block=True)         # 第一次、或者进程没了：现起（慢几秒）
            with self.lock:
                p = self.proc
        elif p.system != system:
            self.warm(system, block=False)        # 论文变了：后台换，这一轮先用旧的
        self.last_proc = p
        t = p.send(content(p) if callable(content) else content)
        i = 0
        finished = False
        try:
            while True:
                with p.cv:
                    p.cv.wait_for(lambda: len(t["deltas"]) > i or t["done"], timeout=timeout)
                    new = t["deltas"][i:]
                    i += len(new)
                    done, err = t["done"], t["error"]
                for d in new:
                    yield d
                if done:
                    finished = True
                    if err and i == 0:
                        raise CliError(err)
                    return
                if time.time() - t["t0"] > timeout and i == 0:
                    raise CliError("等了太久没出字")
        finally:
            if not finished:
                p.interrupt()                 # 被打断：让它别再往下说（不认 interrupt 也没关系，下一轮排在后面）
