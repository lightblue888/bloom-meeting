# -*- coding: utf-8 -*-
"""访客的话按"一轮"攒起来、什么时候交出去、学生开口前再看一眼（app.py 的 _guest_segment / _guest_flush 那几个函数交给这里）。

2026-11-14 手机访客那场的两个毛病（诊断见那场的会议记录）：
- 学生插话：以前"这一轮说完"靠字什么时候转出来——转写积压 10～22 秒时，访客明明一直在说，两块字送达之间也会超过
  12 秒，按"最后一块字送达"算的 12 秒上限就把半句话交出去答了（18 句里 8 句是这么切断的）；出声那道门只要他静 0.4 秒就开。
- 长话上屏晚：几块字一直攒着，整轮说完、积压的都转完才写进逐字稿（开口到上屏 52～77 秒）。

现在：
- 说完没有按判停（guest_ears 的时间线：每段第一个 / 最后一个有声帧、转完没有），不看字什么时候到：这一轮最后一块的
  we 之后静够 need 秒、而且这一轮送去转的都转完了，才交出去。need = turn.need（打招呼 0.5、收尾 1.0、话没说完 4.0、
  smart-turn 觉得没完 2.5、一般 1.2），一般的话不低于这个人句中停顿的 85 分位数（guest_ears.quiet_need，0.8～2 秒）。
  停顿不到 need 又开口了：同一轮，等那一块字出来接上，不设上限（他还在出声，绝不强制交出）。
- 停够了才又开口、可是这一轮还没交出去时他已经又说了 1 秒以上：不交，和接着说的并成一轮。
- 学生开口前（这一轮第一块音频，mouth._wait_gate 里）：他在被答的那一轮结束后又说了 1 秒以上 → 这次不念，
  这一轮并进他正在说的那一轮，等那一轮说完一起答（stale / on_stale）。
- 出声那道门（gate_ok）：每位访客都要静够 quiet_need 秒；被答的那位从那一轮结束后一直没再出声就不用再等。
- 逐字稿：每转出一块就写进（或接到）这一轮正在写的那一行，看会的人马上能看到；那一行到句末（。？！）、已经攒了
  ROW_COMMIT_S 秒以上，就收成一行，后面的另起一行。交给模型的还是整轮的话（payload 带上写好的行，recorded=True，
  app.handle 不再重复写）。只有半截话（"嗯""那个"）先不写，接着说了再一起写——和以前一样。
"""
from __future__ import annotations

import threading
import time

ROW_COMMIT_S = 15.0        # 正在写的那一行攒了这么久、又到了句末：收成一行
STALE_VOICED_S = 1.0       # 被答的那一轮结束后他又说了这么久：这次的回答过时了
STUCK_S = 30.0             # 同一轮里有一块送去转了这么久还没出字、他也一直没出声：不等了（转写进程卡住）
_END = "。？！?!"


class GuestTurns:
    def __init__(self, app, is_fragment=None):
        self.app = app                              # 要用：_guest_buf / _guest_lock / meetings / events / bridge / log / _en / GUEST_FLUSH_S
        self.is_fragment = is_fragment or (lambda s: False)
        self.answer: dict | None = None             # 学生正在答的那一轮：{"name","ended","text","started","rows","cancel","checked"}

    # ---------- 取东西 ----------
    def ge(self, strict: bool = True):
        """访客的判停 / 转写（guest_ears）。strict：直接转写这条路得是好的（判"说完没有"要用转写的时间线）。"""
        br = getattr(self.app, "bridge", None)
        if br is None or not getattr(br, "connected", False):
            return None
        if strict and not getattr(br, "direct_ok", False):
            return None
        return getattr(br, "guest_ears", None)

    def _join(self, a: str, b: str) -> str:
        if not a:
            return b
        if not b:
            return a
        en = False
        try:
            en = bool(self.app._en())
        except Exception:
            pass
        if en:                                      # 英文：标点后补空格，没标点补 ", "
            return a + (" " if a[-1:] in ",.!?;:" else ", ") + b
        return a + ("" if a[-1:] in "，。！？,.!?；;" else "，") + b

    def need(self, b: dict, name: str) -> float:
        """这一轮静多久算说完（从最后一个有声帧算起）：词表说"话没说完 / 在想"→ turn.need 的 4.0，不再压到 1.6；
        其余和以前一样（smart-turn 打分 → turn.need，最多 1.6；没打分 1.6；打招呼 0.5、收尾 1.0）。
        "按这个人句中停顿自适应"放在出声那道门（gate_ok / quiet_need），不放这里：2026-11-14 离线回放里放这里
        （再把 smart-turn 的 2.5 也放开）轻负载时正常回答慢 0.3～0.6 秒，放门上不慢、插话一样是 0 次。"""
        text, p = b.get("text") or "", b.get("p")
        try:
            from . import turn
            lex = float(turn.need(text, None))
            if lex > 1.2:                           # 话没说完 / 在想：多等，不封顶
                return lex
            if lex < 1.2:                           # 打招呼 0.5 / 收尾、应和 1.0
                return max(0.5, lex)
            fl = float(getattr(self.app, "GUEST_FLUSH_S", 1.6))
            return fl if p is None else min(fl, max(0.5, float(turn.need(text, p))))
        except Exception:
            return float(getattr(self.app, "GUEST_FLUSH_S", 1.6))

    # ---------- 进来一块 ----------
    def piece(self, name: str, text: str, info: dict | None) -> None:
        """一块转出来的字（已经整形过）。info 是 guest_ears 给的（直接转写）；None = 老办法（麦克风里录到的访客）。"""
        app = self.app
        with app._guest_lock:
            b = app._guest_buf.get(name)
            if b:
                if b.get("timer"):
                    b["timer"].cancel()
                b["text"] = self._join(b["text"], text)
            else:
                b = app._guest_buf[name] = {"text": text, "rows": [], "row": None, "row_ws": None, "hold": "",
                                            "hold_at": None}
            b["got"] = time.time()
            ws = (info or {}).get("ws")
            if ws is not None and b.get("ws") is None:
                b["ws"] = ws                        # 这一轮第一块开口的时刻：逐字稿按它排
            we = (info or {}).get("we")
            b["ended"] = max(float(we), float(b.get("ended") or 0.0)) if we is not None else None
            b["p"] = (info or {}).get("p")         # smart-turn 对这块打的分（长话中途切的块没有）
            b["direct"] = info is not None
            b["glue"] = False
            if b["direct"]:
                try:
                    self._row(name, b, text, ws)
                except Exception as e:
                    app.log("fast_error", error=f"访客的话写进逐字稿出错：{e!r}")
            self._arm(name, b)

    def _row(self, name: str, b: dict, text: str, ws) -> None:
        """这一块写进逐字稿：接到这一轮正在写的那一行后面，没有就另起一行。"""
        m = self.app.meetings
        if b["row"] is None:
            first = not b["rows"] and not b.get("pre")      # 这一轮的第一行（并进来的上一轮已经写过的不算）
            pend = self._join(b["hold"], text)
            at = b["hold_at"] if b["hold"] else (b.get("ws") if first else ws)
            if first and self.is_fragment(pend):
                b["hold"], b["hold_at"] = pend, at  # 只有半截话：先不写，接着说了一起写（以前也不单独成行）
                return
            if first:                               # 前面攒着一句半截话（app._merge_fragment 留的）：一起写进这一行
                fr = getattr(self.app, "_frag", None)
                if fr and fr.get("guest") == name and time.time() - float(fr.get("t") or 0) <= 6.0:
                    pend = (fr.get("text") or "").rstrip("。.，,、 ") + "，" + pend
                    if fr.get("started") is not None:
                        at = fr["started"] if at is None else min(at, fr["started"])
            line = m.add_line("guest", pend, name=name, at=at)
            if line is None:
                return
            b["row"], b["row_ws"], b["hold"], b["hold_at"] = line, (at if at is not None else time.time()), "", None
            b["rows"].append(line)
        else:
            old = b["row"].get("text") or ""
            m.extend_line(b["row"], self._join(old, text)[len(old):])
        rt = (b["row"].get("text") or "").rstrip()
        if rt[-1:] in _END and float(b.get("ended") or time.time()) - float(b["row_ws"]) >= ROW_COMMIT_S:
            b["row"] = None                         # 到句末、攒够了：收成一行，后面的另起一行

    # ---------- 什么时候交出去 ----------
    def _arm(self, name: str, b: dict, delay: float | None = None) -> None:
        if delay is None:
            if b.get("direct") and b.get("ended"):
                delay = max(0.05, self.need(b, name) - (time.time() - float(b["ended"])))
            else:
                delay = float(getattr(self.app, "GUEST_FLUSH_S", 1.6))
        t = threading.Timer(delay, self.check, args=(name,))
        t.daemon = True
        b["timer"] = t
        t.start()

    def check(self, name: str) -> None:
        app = self.app
        with app._guest_lock:
            b = app._guest_buf.get(name)
            if not b:
                return
            if b.get("direct") and b.get("ended"):
                try:
                    wait = self._wait(name, b)
                except Exception as e:
                    app.log("fast_error", error=f"判访客说完没有出错：{e!r}")
                    wait = None
                if wait is not None:
                    self._arm(name, b, wait)
                    return
            app._guest_buf.pop(name, None)
        self._emit(name, b)

    def _wait(self, name: str, b: dict) -> float | None:
        """None = 这一轮说完了，交出去；数字 = 过这么久再看。"""
        ge = self.ge()
        if ge is None:
            return None                             # 没有判停的时间线：照旧（定时器到了就交）
        need = self.need(b, name)
        now = time.time()
        for _ in range(64):
            nxt = ge.after(name, float(b["ended"]))
            if nxt is None:
                break
            gap = nxt["vs"] - float(b["ended"])
            if gap < need or b.get("glue"):
                if nxt["done"]:                     # 那一块转完了却没接上（转出来是空的 / 当回声丢了）：跳过它接着看
                    b["ended"] = max(float(b["ended"]), float(nxt.get("we") or nxt["vs"]))
                    continue
                if now - float(b.get("got") or now) > STUCK_S and ge.quiet_for(name) >= need:
                    return None                     # 转写卡住了、他也一直没出声：不等了
                return 0.2                          # 同一轮：等那一块的字（不设上限）
            # 停够了 need 才又开口：这一轮本来说完了。可是还没交出去他已经又说了 1 秒以上 → 并进接着说的这一轮
            if ge.voiced_since(name, float(b["ended"])) >= STALE_VOICED_S:
                if not b.get("glue"):
                    b["glue"] = True
                    self.app.log("guest_glue", guest=name, text=b["text"][-40:],
                                 why="这一轮还没交出去他又说了 1 秒以上：并进接着说的那一轮")
                return 0.2
            if ge.quiet_for(name) < 0.3:
                return 0.1                          # 他刚开口（还不到 1 秒）：看看是不是接着往下说
            return None
        q = ge.quiet_for(name)
        if q >= need:
            return None
        return max(0.05, need - q)

    def _emit(self, name: str, b: dict) -> None:
        text = (b.get("text") or "").strip()
        if not text:
            return
        rows = list(b.get("rows") or [])
        if rows and b.get("hold"):                  # 最后还攒着半截话没写：接到最后一行
            try:
                old = rows[-1].get("text") or ""
                self.app.meetings.extend_line(rows[-1], self._join(old, b["hold"])[len(old):])
            except Exception:
                pass
        self.app.events.put(("guest_said", {"name": name, "text": text, "ended": b.get("ended"), "started": b.get("ws"),
                                            "rows": rows, "recorded": bool(rows)}))

    def flush_all(self, drop: bool = False) -> None:
        app = self.app
        with app._guest_lock:
            bufs, app._guest_buf = app._guest_buf, {}
        for name, b in bufs.items():
            if b.get("timer"):
                b["timer"].cancel()
            if not drop:
                self._emit(name, b)

    # ---------- 学生开口前 ----------
    def answering(self, name: str, g: dict, cancel) -> None:
        """学生开始答这位访客的这一轮了（app._on_guest 里，_start_talk 之后）。"""
        self.answer = {"name": name, "ended": g.get("ended"), "text": g.get("text") or "", "started": g.get("started"),
                       "rows": list(g.get("rows") or []), "cancel": cancel, "checked": False}

    def gate_ok(self) -> bool:
        """出声那道门（访客这边）：每位访客都静够 quiet_need 秒；被答的那位从那一轮结束后没再出声，就不用再等。"""
        ge = self.ge(strict=False)
        if ge is None:
            return True
        a = self.answer
        if a is not None and a.get("cancel") is not getattr(self.app, "_cancel", None):
            a = None
        for name in ge.names():
            if a is not None and a["name"] == name and a.get("ended") and ge.last_voice(name) <= float(a["ended"]) + 0.05:
                continue
            if ge.quiet_for(name) < ge.quiet_need(name):
                return False
        return True

    def stale(self) -> bool:
        """这一轮第一句要出声了：他在被答的那一轮结束后又说了 1 秒以上 → 过时了。只看第一句。"""
        a = self.answer
        if a is None or a.get("checked") or a.get("cancel") is not getattr(self.app, "_cancel", None) or not a.get("ended"):
            return False
        ge = self.ge(strict=False)
        if ge is None:
            return False
        if ge.voiced_since(a["name"], float(a["ended"])) >= STALE_VOICED_S:
            return True
        if ge.quiet_for(a["name"]) >= 0.3 or ge.voiced_since(a["name"], float(a["ended"])) <= 0.0:
            a["checked"] = True                     # 第一句放出去了：后面的句子不再看
        return False

    def on_stale(self) -> None:
        """过时了：这次不念（学生这一轮停下），这一轮并进他正在说的那一轮，等那一轮说完一起答。"""
        a, self.answer = self.answer, None
        if not a:
            return
        app = self.app
        app.log("guest_stale", guest=a["name"], text=a["text"][-40:],
                why="要开口时他又说了 1 秒以上：这次不念，并进他正在说的那一轮，说完一起答")
        try:
            a["cancel"].set()
        except Exception:
            pass
        try:
            app.mouth.stop()
        except Exception:
            pass
        self.requeue(a)

    def requeue(self, a: dict) -> None:
        app = self.app
        name = a["name"]
        with app._guest_lock:
            b = app._guest_buf.get(name)
            if b:
                if b.get("timer"):
                    b["timer"].cancel()
                b["text"] = self._join(a["text"], b["text"])
                if a.get("started") is not None:
                    b["ws"] = a["started"] if b.get("ws") is None else min(float(b["ws"]), float(a["started"]))
                b["rows"] = list(a.get("rows") or []) + list(b.get("rows") or [])
                b["pre"] = True                     # 上一轮的字已经在逐字稿里了（不管是这里写的还是 handle 写的）
            else:
                b = app._guest_buf[name] = {"text": a["text"], "rows": list(a.get("rows") or []), "row": None, "row_ws": None,
                                            "hold": "", "hold_at": None, "ws": a.get("started"), "ended": a.get("ended"),
                                            "p": None, "direct": True, "got": time.time(), "glue": True, "pre": True}
            self._arm(name, b, 0.2)
