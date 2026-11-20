# -*- coding: utf-8 -*-
"""会议库：每场会一个目录 ~/.zuhui/meetings/<id>/，里面是
    meta.json          会议信息（id/title/started/ended/state/duration_s/session）；title_auto（名字是按论文自动起的，
                       手改过是 False）、auto_title（手改前自动起的名字）、papers（这场会摊开过的论文 [{id,name,path}]）、
                       active_paper（屏幕上现在是哪篇）。旧会议没有这几个字段：当作只有一篇、名字是自动起的
    transcript.jsonl   逐字稿：{"t":"01:23","sec":83.1,"who":"teacher|student|system","text":..., "tag":...}
                       开会时 = 实时显示的；散会后重转写完（zuhui/retranscribe.py）= 最准的原始版，你说的行多了
                       "start"/"end"（录屏里的秒）
    transcript.live.jsonl  开会时实时显示的那一版（散会时存一份，和 transcript.jsonl 一行对一行）
    transcript.clean.jsonl 整理后的干净版（zuhui/polish.py；行的样子同上，多一个 "i" = 原始版第几行，
                       "edits" = 这一行用了删改清单里哪几条）；删光的行不出现
    cleanup.json       删改清单 {"version":1,"items":[{"id","line","kind","before","after","reason","restored"}]}
                       kind：retract 说错改口 / fact 不符合事实 / sensitive 不宜传播 / profanity 脏话；
                       before = 原始版这一行里被删 / 被改的那一截（原文照抄），after = 换成的（删掉就是 ""）
    transcript.edits.json  回顾里手改的（改一句、整场替换、访客改名，zuhui/transcript_edit.py）：叠在干净版上，上面几份都不动
    board.json         散会时的任务板快照
    diff.html          散会时的修订对比快照（完整页面）
    summary.json/.md   AI 纪要（照腾讯会议的"图文总结"结构；格式按设置 → 纪要选的，字段见下面"纪要"一节）
    carry.json         这场会留下的事（待办、标注、没收下的改动）勾掉了哪些（zuhui/past.py）
    carry_in.json      开会时带进这场的上次遗留（zuhui/past.py）：散会纪要写出每条落实了没有
前端（口喷鸡改版）只读这些，经 view.py 的 /api/*。
"""
from __future__ import annotations
import json
import logging
import re
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import assistant_name, STATE


def join_spoken(*parts: str) -> str:
    """把分段念出来的话拼回一整段：中文直接连；英文两段之间补一个空格。快嘴的回话按句、按逗号切成几段交给嘴，
    每段去了首尾空格，直接连起来英文就成了 "method,not""half a minute.Both"（2026-11-13 三场英文会评审都提到）。
    段首的英文双引号要是在收前面没收的引号（"benchmark," + '" and four'），不补空格。"""
    out = ""
    for p in parts:
        if not p:
            continue
        if (out and re.search(r"[A-Za-z0-9,.;:!?)\]\"'”’%]$", out) and re.match(r"[A-Za-z0-9(\[\"'“‘]", p)
                and not (p[0] == '"' and out.count('"') % 2 == 1)
                and not (re.search(r"\d[,.]$", out) and p[:1].isdigit())):   # "144," + "000"：数字没断开，不补空格
            out += " "
        out += p
    return out


log = logging.getLogger(__name__)
LF = chr(10)
CRLF = chr(13) + chr(10)
_DATE_TAIL = re.compile(r"\s*·\s*\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*$")
_BAD_NAME = re.compile(r'[\\/:*?"<>|]+')

ROOT = STATE / "meetings"
REAL_ROOT = Path.home() / ".zuhui" / "meetings"     # 真的会议库（ZUHUI_HOME 指到别处的是测试库）


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


TITLE_MAX = 60                      # 会议名最长多少字（手改的、按论文文件名自动起的都算；再长顶栏也放不下）


def _clip_title(t) -> str:
    """会议名：换行、连着的空格并成一个，超过 TITLE_MAX 截掉、末尾加省略号（论文文件名可能很长）。"""
    t = " ".join(str(t or "").split())
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1].rstrip() + "…"


def paper_id(path) -> str:
    """一篇论文在会上的编号：规范化后完整路径的 sha1 前 8 位。同一个文件加两次是同一篇；
    两个文件夹里都叫 main.tex 的两篇编号不同（原文基线按它存，不会互相盖掉）。"""
    p = Path(str(path))
    try:
        p = p.resolve()
    except OSError:
        pass
    import hashlib
    return hashlib.sha1(os.path.normcase(str(p)).encode("utf-8")).hexdigest()[:8]


def paper_entry(path, name: str | None = None, source: str | None = None) -> dict:
    """会议 meta 里 papers 列表的一项：{id, name, path, source, added}。source = 拖进来的原文件（docx / pdf 转成的
    镜像不是它）：同一份原文件再拖一次只切过去，不重新导入（重新导入会把会上改过的镜像冲掉）。"""
    p = Path(str(path))
    e = {"id": paper_id(p), "name": str(name or p.stem), "path": str(p), "added": _now_iso()}
    if source:
        e["source"] = str(source)
    return e


def mode_of(meta: dict | None) -> str:
    """这场会是哪种："chat" = 没选论文开的会（只记逐字稿，AI 不接话），"paper" = 改论文的会。旧会议没记：算 paper。"""
    return "chat" if (meta or {}).get("mode") == "chat" else "paper"


def papers_of(meta: dict | None) -> list[dict]:
    """这场会摊开过的论文（先后顺序，第一篇是开会时那篇）。旧会议没有这个字段：空列表，界面和以前一样。"""
    ps = (meta or {}).get("papers")
    return [dict(p) for p in ps if isinstance(p, dict) and p.get("id") and p.get("path")] if isinstance(ps, list) else []


def _write_meta_file(p: Path, obj: dict) -> None:
    """meta.json 先写临时文件再换上去（写到一半断电、或者回顾页正好来读，不会读到半截）。
    Windows 上目标正被别人打开时 os.replace 偶尔被拒：退回直接写。"""
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=1)
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        p.write_text(data, encoding="utf-8")


# 摊开的论文单子（没开会时也有，2026-11-15）："换论文"和"加一篇论文"合成一个"新增"：开会前新增一篇也不把上一篇换掉，
# 单子上的几篇能来回切；开会时整张单子带进会（Meetings.start 的 papers），散会后单子还是会上那几篇。
OPEN_PAPERS = STATE / "open_papers.json"


def load_open_papers() -> dict:
    """{"papers": [paper_entry...]（文件还在的）, "active": 屏幕上那篇的编号}。没有记录时 papers 是空的。"""
    try:
        d = json.loads(OPEN_PAPERS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"papers": [], "active": None}
    ps = d.get("papers") if isinstance(d, dict) else None
    ps = [dict(p) for p in ps if isinstance(p, dict) and p.get("id") and p.get("path") and Path(str(p["path"])).is_file()] \
        if isinstance(ps, list) else []
    return {"papers": ps, "active": d.get("active") if isinstance(d, dict) else None}


def save_open_papers(papers: list[dict], active: str | None) -> None:
    try:
        OPEN_PAPERS.parent.mkdir(parents=True, exist_ok=True)
        _write_meta_file(OPEN_PAPERS, {"papers": [dict(p) for p in papers if isinstance(p, dict) and p.get("id")],
                                       "active": active})
    except OSError:
        pass


def add_open_paper(papers: list[dict], entry: dict) -> list[dict]:
    """单子上加一篇（同一篇已经在了：名字、原文件换成新的，位置不变）。返回新单子。"""
    out, hit = [], False
    for p in papers:
        if p.get("id") == entry.get("id"):
            out.append({**p, **{k: v for k, v in entry.items() if v and k != "added"}})
            hit = True
        else:
            out.append(dict(p))
    return out if hit else out + [dict(entry)]


def _fmt(sec: float) -> str:
    s = int(max(0, sec))
    return f"{s // 60:02d}:{s % 60:02d}"


class Meetings:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cur: dict | None = None
        self._lines: list[dict] = []
        self._t0: float | None = None
        self.last_report: str | None = None
        self.partial: dict | None = None     # 老师正在说、还没说完的那一行（耳朵的 partial）
        self._resume_live()
        _retry_soon()                        # 上次没删掉的录屏，接着删

    def set_partial(self, p: dict | None) -> None:
        with self._lock:
            self.partial = dict(p) if (p and self._cur) else None

    # ---------- 内部 ----------
    def _dir(self, mid: str) -> Path:
        if mid == "sample":                 # 第 8 批：自带的示例会议在会议库旁边，不在库里（zuhui/sample.py）
            from .sample import folder
            return folder(self.root)
        return self.root / mid

    def _write_meta(self):
        if self._cur:
            _write_meta_file(self._dir(self._cur["id"]) / "meta.json", self._cur)

    def _resume_live(self):
        """上次进程没散会就退了（后端崩了、被重启了）。最新那场先留着"进行中"：录音还在、耳朵又接上同一段录音时，
        就接着开这一场（resume），逐字稿不会被切成两场；其余的、以及等了一会儿也没接上的，标成已结束。"""
        self._orphan = None
        for d in sorted(self.root.glob("*"), reverse=True):
            m = self._load_meta(d)
            if m and m.get("state") == "live":
                if self._orphan is None:
                    self._orphan = m
                    continue
                m["state"], m["ended"] = "ended", m.get("ended") or _now_iso()
                (d / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")

    def resume(self, session: str | None) -> dict | None:
        """耳朵接上的录音和崩之前那场会是同一段：接着开那一场，返回它；否则把那场收掉，返回 None。"""
        with self._lock:
            m, self._orphan = getattr(self, "_orphan", None), None
            if not m:
                return None
            if session and m.get("session") and str(m["session"]) == str(session):
                self._cur = m
                self._lines = self._load_lines(self._dir(m["id"]))
                try:
                    self._t0 = datetime.fromisoformat(m["started"]).timestamp()
                except (KeyError, ValueError, TypeError):
                    self._t0 = time.time()
                return dict(m)
            self._close(m)
            return None

    def close_orphan(self) -> None:
        """后端起来一阵子了也没接上录音：崩之前那场会就当散了。"""
        with self._lock:
            m, self._orphan = getattr(self, "_orphan", None), None
            if m:
                self._close(m)

    def _close(self, m: dict) -> None:
        m["state"], m["ended"] = "ended", m.get("ended") or _now_iso()
        try:
            t0 = datetime.fromisoformat(m["started"]).timestamp()
            lines = self._load_lines(self._dir(m["id"]))
            last = max([float(x.get("sec") or 0) for x in lines] or [0.0])
            m["duration_s"] = int(max(m.get("duration_s") or 0, last))
        except Exception:
            pass
        try:
            (self._dir(m["id"]) / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _load_meta(d: Path) -> dict | None:
        try:
            return json.loads((d / "meta.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _load_lines(d: Path) -> list[dict]:
        out = []
        try:
            for raw in (d / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    try:
                        out.append(json.loads(raw))
                    except ValueError:
                        pass
        except OSError:
            pass
        return out

    # ---------- 生命周期 ----------
    def start(self, session: str | None = None, title: str | None = None, lang: str | None = None,
              papers: list[dict] | None = None, active: str | None = None, mode: str | None = None) -> dict:
        """papers：开会时摊开的论文（paper_entry，开会前新增过的几篇都带进来）；active：屏幕上那篇的编号（不给就是第一篇）。
        会议名先按它自动起（title_auto=True），你在界面上改过名（rename）就不再跟着论文走。
        mode="chat"：没选论文开的会（只记逐字稿，AI 不接话）；不给就是改论文的会。"""
        papers = [dict(p) for p in (papers or []) if isinstance(p, dict) and p.get("id")]
        with self._lock:
            if getattr(self, "_orphan", None):          # 崩之前那场没接上：先收掉
                self._close(self._orphan)
                self._orphan = None
            if self._cur:                       # 上一场没散就开了新的（录屏被另一次录屏顶掉）：先把上一场收好，别一直挂着"进行中"
                self._cur["ended"] = _now_iso()
                self._cur["state"] = "ended"
                self._cur["duration_s"] = int(time.time() - (self._t0 or time.time()))
                self._write_meta()
            mid = base = datetime.now().strftime("%Y%m%d-%H%M%S")
            k = 2
            while self._dir(mid).exists():      # 同一秒里开了两场：编号往后加，不能把上一场的文件夹覆盖掉
                mid = f"{base}-{k}"
                k += 1
            d = self._dir(mid)
            d.mkdir(parents=True, exist_ok=True)
            self._cur = {
                "id": mid,
                "title": _clip_title(title) or f"组会 · {datetime.now().strftime('%m月%d日 %H:%M')}",
                "title_auto": True,                     # 名字是按论文自动起的；手改过就是 False，之后不再跟着论文变
                "started": _now_iso(), "ended": None, "state": "live", "duration_s": 0,
                "session": session,
                # 这场会的语言：会议页的卡片、步骤、任务板跟它走（不跟 App 界面语言）；没给就不写，前端跟界面语言
                "lang": lang if lang in ("en", "zh") else None,
                # 这场会上摊开过的论文（会中能加、能来回切）和屏幕上现在是哪篇；任务上记着 paper = 它是哪篇上的
                "papers": papers,
                "active_paper": (active if active in {p["id"] for p in papers} else papers[0]["id"]) if papers else None,
                "mode": "chat" if mode == "chat" else "paper",
            }
            self._lines, self._t0 = [], time.time()
            (d / "transcript.jsonl").write_text("", encoding="utf-8")
            self._write_meta()
            out = dict(self._cur)
        try:                                    # 共同主持人每场重设：上一场的人挪进"上次是共同主持人"，进会时提示恢复
            from . import roles
            roles.begin(mid)
        except Exception:
            pass
        return out

    def add_line(self, who: str, text: str, tag: str | None = None, name: str | None = None,
                 at: float | None = None, extra: dict | None = None) -> dict | None:
        """逐字稿加一行。没有进行中的会时：有上一场就追加到上一场（工人晚到的汇报），都没有就丢。
        at = 开口的墙上时刻（你和访客的话写进来时已经晚了一两秒、长话晚几十秒）：sec / t 按它算，
        行插回按开口时刻该在的位置——逐字稿的顺序是说话的顺序，不是写进来的顺序。不给就按现在。"""
        with self._lock:
            target = self._cur
            if target is None:
                last = self.list()
                if not last:
                    return None
                target = self._load_meta(self._dir(last[0]["id"]))
                if not target:
                    return None
                t0 = datetime.fromisoformat(target["started"]).timestamp()
            else:
                t0 = self._t0 or time.time()
            now = time.time()
            sec = max(0.0, now - t0)
            if at is not None:
                try:
                    a = float(at)
                    if t0 - 5.0 <= a <= now + 1.0:      # 估出来的时刻离谱就不用（音频时钟没对上之类）
                        sec = max(0.0, min(sec, a - t0))
                except (TypeError, ValueError):
                    pass
            line = {"t": _fmt(sec), "sec": round(sec, 1), "who": who, "text": text}
            if at is not None:
                line["in"] = round(max(0.0, now - t0), 1)   # 写进逐字稿的时刻（会后重转写 / 回顾页对录屏按它往回找）
            if tag:
                line["tag"] = tag
            if name:                                   # 访客的话：记下是谁（会议语音桥认出来的）
                line["name"] = name
            for k, v in (extra or {}).items():         # Claude 的行：lat 出声用了几秒、title 任务标题、took 后台查了几秒……
                if v not in (None, "") and k not in line:
                    line[k] = v
            p = self._dir(target["id"]) / "transcript.jsonl"
            if self._cur and target["id"] == self._cur["id"]:
                k = len(self._lines)                   # 按开口时刻插回去（一般就是最后；长话、访客的话会往前插）
                while k > 0 and float(self._lines[k - 1].get("sec") or 0) > line["sec"]:
                    k -= 1
                self._lines.insert(k, line)
                if k == len(self._lines) - 1:
                    with open(p, "a", encoding="utf-8") as f:
                        f.write(json.dumps(line, ensure_ascii=False) + "\n")
                else:
                    p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self._lines), encoding="utf-8")
            else:
                with open(p, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
            live_copy = self._dir(target["id"]) / "transcript.live.jsonl"
            if self._cur is None and live_copy.exists():      # 散会后晚到的一行：实时版也记上，两份一行对一行
                with open(live_copy, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
            if who == "student":
                self.last_report = text
            elif who == "teacher":
                self.partial = None
            return line

    def clock(self, ts: float | None = None) -> str:
        """这场会的钟点 "mm:ss"（逐字稿里那一步发生在第几分几秒）。"""
        t0 = self._t0 or time.time()
        return _fmt(max(0.0, (ts or time.time()) - t0))

    def annotate(self, line: dict | None, **kv) -> None:
        """给逐字稿的一行补几个字段（skip：Claude 没接的原因；cut：Claude 这句说到一半被打断），当前会议的文件整份重写。"""
        if not line:
            return
        with self._lock:
            line.update({k: v for k, v in kv.items() if v not in (None, "")})
            if self._cur and line in self._lines:
                p = self._dir(self._cur["id"]) / "transcript.jsonl"
                p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self._lines), encoding="utf-8")

    def extend_line(self, line: dict, more: str) -> None:
        """学生的一句接着往下念：把这一句补到同一行后面（当前会议的逐字稿文件整份重写，文件很小）。"""
        with self._lock:
            line["text"] = join_spoken(line.get("text") or "", more)   # 英文接句补空格（"No,the""seed.It"）
            if self._cur and line in self._lines:
                p = self._dir(self._cur["id"]) / "transcript.jsonl"
                p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self._lines), encoding="utf-8")

    def end(self, board_tasks: list[dict] | None = None, diff_html: Path | None = None) -> dict | None:
        with self._lock:
            if not self._cur:
                return None
            self._cur["ended"] = _now_iso()
            self._cur["state"] = "ended"
            self._cur["duration_s"] = int(time.time() - (self._t0 or time.time()))
            d = self._dir(self._cur["id"])
            if board_tasks is not None:
                (d / "board.json").write_text(json.dumps({"tasks": board_tasks}, ensure_ascii=False, indent=1), encoding="utf-8")
            if diff_html and Path(diff_html).exists():
                try:
                    shutil.copyfile(diff_html, d / "diff.html")
                except OSError:
                    pass
            try:                                   # 实时显示的那一版存一份：散会后重转写会把 transcript.jsonl 换成更准的
                if (d / "transcript.jsonl").exists() and not (d / "transcript.live.jsonl").exists():
                    shutil.copyfile(d / "transcript.jsonl", d / "transcript.live.jsonl")
            except OSError:
                pass
            self._write_meta()
            m = dict(self._cur)
            self._cur = None
        try:                                    # 散会：这场的共同主持人挪进"上次是共同主持人"
            from . import roles
            roles.end()
        except Exception:
            pass
        return m

    def set_papers(self, papers: list[dict], active: str | None) -> dict | None:
        """开会时加了论文 / 切了论文：记进当前这场的 meta（后端重启接着开时也还是这几篇、这一篇）。
        会议名不跟着变：自动起的名字就是开会时那一篇，手改过的更不动。没有进行中的会返回 None。"""
        with self._lock:
            if not self._cur:
                return None
            self._cur["papers"] = [dict(p) for p in papers if isinstance(p, dict) and p.get("id")]
            self._cur["active_paper"] = active
            self._write_meta()
            return dict(self._cur)

    def rename(self, mid: str, title: str | None) -> dict | None:
        """手改会议名（开着的会、以往的会都行）：写进 meta.json，title_auto=False，之后不再按论文起名。
        空串 = 不要手起的名字了：回到自动起的那个（开会时那篇论文的名字）。没有这场会返回 None。
        正在开的会改的是内存里那份（散会时写 meta 用的就是它）；崩之前那场还没接上的（_orphan）也一起改，
        免得它之后被收掉时拿旧名字写回去。"""
        mid = Path(str(mid or "")).name
        t = " ".join(str(title or "").split())[:TITLE_MAX].strip()   # 换行、连着的空格并成一个
        with self._lock:
            live = self._cur is not None and self._cur.get("id") == mid
            m = self._cur if live else self._load_meta(self._dir(mid))
            if not mid or not m:
                return None
            if t:
                if m.get("title_auto", True) is not False and not m.get("auto_title"):
                    m["auto_title"] = m.get("title")     # 自动起的那个留着：清空会议名时回到它
                m["title"], m["title_auto"] = t, False
            else:
                ps = papers_of(m)
                m["title"] = _clip_title(m.get("auto_title") or (ps[0]["name"] if ps else None) or m.get("title")) or m.get("title")
                m["title_auto"] = True
            if live:
                self._write_meta()
            else:
                _write_meta_file(self._dir(mid) / "meta.json", m)
            orphan = getattr(self, "_orphan", None)
            if orphan and orphan.get("id") == mid and orphan is not m:
                orphan.update({k: m.get(k) for k in ("title", "title_auto", "auto_title")})
            return dict(m)

    def save_participants(self, mid: str, people: list[dict]) -> None:
        """这场会上都有谁（散会时存）：以往会议里照着显示，不拿现在在线的人去冒充。"""
        try:
            (self._dir(mid) / "participants.json").write_text(json.dumps(people, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def save_diff(self, mid: str, diff_html: Path) -> bool:
        """散会后台做好的修订对比快照存进会议目录（散会请求不等它：html-diff 要几秒）。"""
        d = self._dir(Path(str(mid)).name)
        if not d.exists() or not diff_html or not Path(diff_html).exists():
            return False
        try:
            tmp = d / f"diff.html.{os.getpid()}.tmp"
            shutil.copyfile(diff_html, tmp)
            os.replace(tmp, d / "diff.html")
            return True
        except OSError:
            return False

    def save_board(self, mid: str, tasks: list[dict]) -> None:
        d = self._dir(mid)
        if d.exists():
            (d / "board.json").write_text(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=1), encoding="utf-8")

    def set_summary(self, mid: str, summary: dict) -> None:
        d = self._dir(mid)
        if not d.exists():
            return
        summary = dict(summary)
        summary.setdefault("generated", _now_iso())
        summary["markdown"] = summary.get("markdown") or render_markdown(summary)
        (d / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        (d / "summary.md").write_text(summary["markdown"], encoding="utf-8")

    # ---------- 读 ----------
    def current(self) -> dict | None:
        with self._lock:
            if not self._cur:
                return None
            m = dict(self._cur)
            m["duration_s"] = int(time.time() - (self._t0 or time.time()))
            return m

    def current_lines(self) -> list[dict]:
        with self._lock:
            return list(self._lines)

    def live_dir(self) -> Path | None:
        """正在开的这场会的目录（书签存在里面）；没在开会 None。"""
        with self._lock:
            return self._dir(self._cur["id"]) if self._cur else None

    def elapsed(self, ts: float | None = None) -> float | None:
        """正在开的这场会开了几秒（书签记的"此刻"）；没在开会 None。"""
        with self._lock:
            if not self._cur:
                return None
            return max(0.0, (ts or time.time()) - (self._t0 or time.time()))

    @property
    def t0(self) -> float | None:
        """正在开的这场会开始的墙上时刻。"""
        return self._t0 if self._cur else None

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.glob("*"), reverse=True):
            m = self._load_meta(d)
            if not m:
                continue
            lines = self._load_lines(d)
            tasks = []
            try:
                tasks = json.loads((d / "board.json").read_text(encoding="utf-8")).get("tasks", [])
            except (OSError, ValueError):
                pass
            out.append({
                "id": m["id"], "title": m.get("title"), "title_auto": m.get("title_auto", True) is not False,
                "started": m.get("started"), "ended": m.get("ended"),
                "state": m.get("state"), "duration_s": m.get("duration_s", 0),
                "n_tasks": len(tasks), "n_done": sum(1 for t in tasks if t.get("status") == "done"),
                "n_lines": len(lines), "has_summary": (d / "summary.json").exists(), "has_diff": (d / "diff.html").exists(),
                # 这场会摊开过的论文（回顾里按论文筛）
                "papers": [{"id": p["id"], "name": str(p.get("name") or Path(str(p["path"])).stem)} for p in papers_of(m)],
                "mode": mode_of(m),
            })
        return out

    def get(self, mid: str) -> dict | None:
        d = self._dir(mid)
        m = self._load_meta(d)
        if not m:
            return None
        m["mode"] = mode_of(m)                     # 旧会议没记是哪种：算改论文的会
        try:
            board = json.loads((d / "board.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            board = {"tasks": []}
        if isinstance(board, dict) and isinstance(board.get("tasks"), list):   # 标注带上标签（旧会议存的快照里没有）
            from .board import with_tags
            board["tasks"] = [with_tags(t) if isinstance(t, dict) else t for t in board["tasks"]]
        try:
            summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = None
        try:
            people = json.loads((d / "participants.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            people = None
        raw = self._load_lines(d)
        items = load_cleanup(d)
        clean = apply_cleanup(raw, items) if items is not None else [dict(l, i=i) for i, l in enumerate(raw)]
        named, edits = raw, {"n": 0, "last": None}
        try:                                       # 回顾里手改过（第 7 批，transcript_edit.py）：叠在干净版上，文件都不动
            from . import transcript_edit
            ops = transcript_edit.load(d)
            if ops:
                clean = transcript_edit.apply(clean, ops)
                named = transcript_edit.apply_speakers(raw, ops)
                people = transcript_edit.rename_people(people, ops)
            edits = transcript_edit.public(ops)
        except Exception:
            log.warning("会议 %s 的手改没叠上", mid, exc_info=True)
        try:                                       # 每人发言时长：按原始版（说了多久就是多久，整理删掉的话也算；改过名的按新名字）
            talk = talk_time(named)
        except Exception:
            talk = []
        try:                                       # 会上标的书签（第 3 批；旧会议没有：空）
            from . import inmeeting
            marks = inmeeting.load_bookmarks(d)
        except Exception:
            marks = []
        try:                                       # 遗留项勾掉了哪些（carry.json）：回顾的修改清单和大厅用同一份
            done = (json.loads((d / "carry.json").read_text(encoding="utf-8")).get("done") or {})
            carry_done = sorted(k for k in done if isinstance(k, str))
        except (OSError, ValueError, AttributeError):
            carry_done = []
        try:                                       # 用 API 时这场会的 token 用量和估算费用（没用 API 的会：None）
            from . import usage
            use = usage.meeting(d)
        except Exception:
            use = None
        return {"meeting": m, "transcript": clean, "transcript_raw": raw, "cleanup": items, "usage": use,
                "board": board, "summary": summary,
                "diff_url": f"/meetings/{mid}/diff.html" if (d / "diff.html").exists() else None,
                "participants": people, "talk": talk, "bookmarks": marks, "carry_done": carry_done,
                "edits": edits}

    def restore(self, mid: str, ids: list[str], restored: bool = True) -> dict | None:
        """删改清单里的几条恢复成原话（restored=False：再删掉）。改 cleanup.json，重写 transcript.clean.jsonl，
        返回更新后的 get(mid)。没有这场会或还没整理过：返回 None。"""
        mid = Path(str(mid)).name
        d = self._dir(mid)
        with self._lock:
            data = _read_json(d / "cleanup.json")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                return None
            want = {str(x) for x in (ids or [])}
            for it in data["items"]:
                if str(it.get("id")) in want:
                    it["restored"] = bool(restored)
            _write_json(d / "cleanup.json", data)
            write_clean(d, apply_cleanup(self._load_lines(d), data["items"]))
        return self.get(mid)

    def delete(self, mid: str, busy_session: str | None = None) -> dict:
        """删掉一场会：整个文件夹挪进 ~/.zuhui/meetings-trash/（不是真删，找得回来）。正在开的会不能删。
        这场会的录音录屏文件夹（meta.json 的 session）一起送进 Windows 回收站：别的会还在用它、它正在录、
        或者它不在录屏目录里，就不动。"""
        import shutil
        mid = Path(str(mid)).name
        d = self._dir(mid)
        with self._lock:
            if self._cur and self._cur.get("id") == mid:
                return {"ok": False, "error": "会议进行中，无法删除"}
            m = self._load_meta(d)
            if not m:
                return {"ok": False, "error": "会议不存在"}
            rec = self._recording_to_drop(mid, m.get("session"), busy_session)
            kept = None
            if rec is not None and not self._owns_recording(rec):
                kept, rec = rec, None              # 测试用的会议库指着别处（真的）录屏：只删会，不碰录屏，也不排重试
            trash = self.root.parent / "meetings-trash"
            trash.mkdir(parents=True, exist_ok=True)
            dst = trash / f"{mid}-deleted-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            k = 2
            while dst.exists():                   # 同一秒删掉两场同名的会（删了又开、编号复用）：别塞进前一个里
                dst = dst.with_name(f"{dst.name.split('~')[0]}~{k}")
                k += 1
            shutil.move(str(d), str(dst))
        out = {"ok": True, "recording": None}
        if kept is not None:
            log.warning("会议库 %s 不是真的那一个，录屏 %s 不在它自己的目录里：不动", self.root, kept)
            out["recording"] = {"path": str(kept), "deleted": False, "kept": True, "pending": False, "error": None,
                                "why": "这是测试用的会议库，录屏不在它自己的目录里，没动"}
        if rec is not None:
            mb = _dir_mb(rec)
            err = _recycle(rec)
            out["recording"] = {"path": str(rec), "mb": round(mb, 1), "deleted": err is None, "error": err,
                                "pending": err is not None}
            if err:                                    # 刚散会时 App 还在处理这段录音、占着文件：记下来，后台接着删
                log.warning("会议 %s 的录屏暂时删不掉 (%s): %s，稍后重试", mid, rec, err)
                _pending_add(rec)
                _retry_soon()
        return out

    def _owns_recording(self, rec: Path) -> bool:
        """录屏能不能由这个会议库送进回收站：真的会议库（~/.zuhui/meetings）照旧都能；别的（测试用的临时库）
        只能动它自己目录（root 的上一级）里面的录屏——测试库里的会议指着真录屏时，删会也不会把真录屏删掉。"""
        try:
            root = self.root.resolve()
            if root == REAL_ROOT.resolve():
                return True
            Path(rec).resolve().relative_to(root.parent)
            return True
        except (OSError, ValueError):
            return False

    def _recording_to_drop(self, mid: str, session, busy_session) -> Path | None:
        """这场会的录屏文件夹能不能跟着删：得是录屏目录里的会话文件夹，没在录，也没有别的会指着它。"""
        if not session:
            return None
        p = Path(str(session))
        try:
            p = p.resolve()
        except OSError:
            return None
        if not p.is_dir() or not _is_recording_dir(p):
            return None
        if busy_session:
            try:
                if Path(str(busy_session)).resolve() == p:
                    return None
            except OSError:
                return None
        for other in self.root.iterdir():
            if other.name == mid or not other.is_dir():
                continue
            om = self._load_meta(other) or {}
            if om.get("session"):
                try:
                    if Path(str(om["session"])).resolve() == p:
                        return None
                except OSError:
                    continue
        return p

    def export(self, mid: str, fmt: str = "md", version: str = "clean") -> dict:
        """回顾 → 分享：一场会的逐字稿导出成 Markdown 或 TXT（只在主持人本机，访客那边没有这个）。
        version="clean"（默认）= 整理后的干净版（恢复过的条目按恢复后的样子）；"raw" = 原始版。"""
        import getpass
        from .config import load
        mid = Path(str(mid)).name
        g = self.get(mid)
        if not g:
            return {"ok": False, "error": "会议不存在"}
        m = g["meeting"]
        cfg = load()
        try:                                       # 散会时存下的名字优先（会后在设置里改了昵称，逐字稿里不变成新名字、参会人里不出现两个）
            from .export import names_of
            me, ai = names_of(g, cfg)
        except Exception:
            me = str((cfg.get("meeting") or {}).get("me") or getpass.getuser() or "我")
            ai = assistant_name(cfg)

        def who(l: dict) -> str:
            w = l.get("who")
            return me if w == "teacher" else ai if w == "student" else (l.get("name") or "参会者") if w == "guest" else "系统"

        src = g["transcript_raw"] if version == "raw" else g["transcript"]
        lines = [l for l in src if (l.get("text") or "").strip()]
        title = _DATE_TAIL.sub("", str(m.get("title") or "组会")).strip() or "组会"
        try:
            started = datetime.fromisoformat(str(m.get("started")))
            when = started.strftime("%Y-%m-%d %H:%M")
            stamp = started.strftime("%Y%m%d-%H%M")
        except (TypeError, ValueError):
            when, stamp = "", mid
        dur = int(m.get("duration_s") or 0)
        people = []
        for p in (g.get("participants") or []):
            n = str(p.get("name") or "").strip()
            if n and n not in people:
                people.append(n)
        ai_in = mode_of(m) != "chat" or any(l.get("who") == "student" for l in lines)   # 没有论文的会：AI 没参加，不写进参会人
        for n in [me] + ([ai] if ai_in else []) + [who(l) for l in lines if l.get("who") == "guest"]:
            if n not in people:
                people.append(n)
        en = str(m.get("lang") or "") == "en"          # 文件里的标题行跟会议的语言走：英文会就写英文
        L = {"when": "Time", "dur": "Duration", "people": "Participants", "tx": "Transcript", "file": "transcript",
             "raw": " raw", "sep": ", ", "colon": ": ", "len": f"{dur // 60} min {dur % 60} s"} if en else \
            {"when": "时间", "dur": "时长", "people": "参会者", "tx": "逐字稿", "file": "逐字稿",
             "raw": " 原始版", "sep": "、", "colon": "：", "len": f"{dur // 60} 分 {dur % 60} 秒"}
        c = L["colon"]
        if fmt == "txt":
            head = [title, f"{L['when']}{c}{when}{'   ' if en else '　'}{L['dur']}{c}{L['len']}", L["people"] + c + L["sep"].join(people), ""]
            body = [f"[{l.get('t')}] {who(l)}{c}{l.get('text').strip()}" for l in lines]
            text = CRLF.join(head + body) + CRLF
        else:
            head = [f"# {title}", "", f"- {L['when']}{c}{when}", f"- {L['dur']}{c}{L['len']}", f"- {L['people']}{c}" + L["sep"].join(people),
                    "", f"## {L['tx']}", ""]
            body = [f"**{who(l)}** `{l.get('t')}`  " + LF + l.get("text").strip() + LF for l in lines]
            text = LF.join(head + body)
        safe = _BAD_NAME.sub(" ", title).strip() or "组会"
        kind = L["raw"] if version == "raw" else ""
        return {"ok": True, "name": f"{safe} {L['file']}{kind} {stamp}.{'txt' if fmt == 'txt' else 'md'}", "text": text,
                "lines": len(lines), "version": "raw" if version == "raw" else "clean"}

    def diff_path(self, mid: str) -> Path | None:
        p = self._dir(mid) / "diff.html"
        return p if p.exists() else None


# ---------- 整理后的干净版（删改清单 cleanup.json，见 zuhui/polish.py） ----------
def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(p: Path, obj) -> None:
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def load_cleanup(d: Path) -> list[dict] | None:
    """删改清单的条目；还没整理过就是 None。"""
    data = _read_json(Path(d) / "cleanup.json")
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    return None


def _tidy(t: str) -> str:
    try:
        from .normalize import tidy
        return tidy(t)
    except Exception:
        return t.strip()


def apply_cleanup(raw: list[dict], items: list[dict] | None) -> list[dict]:
    """原始版 + 删改清单（恢复过的条目不算）→ 干净版。每行多一个 "i"（原始版第几行）；用到的条目记在 "edits"；
    删光（只剩标点）的行不要。条目的 before 在这一行里找不到（原始版后来又变了）就跳过。"""
    by_line: dict[int, list[dict]] = {}
    for it in items or []:
        try:
            i = int(it.get("line"))
        except (TypeError, ValueError):
            continue
        before = str(it.get("before") or "")
        if before and not (0 <= i < len(raw) and before in str(raw[i].get("text") or "")):
            # 原始版后来变过（行数挪了）：在前后 6 行里找含这一截的、离得最近的那一行
            near = [j for j in range(max(0, i - 6), min(len(raw), i + 7)) if before in str(raw[j].get("text") or "")]
            if near:
                i = min(near, key=lambda j: abs(j - i))
        by_line.setdefault(i, []).append(it)
    out = []
    for i, l in enumerate(raw):
        text = str(l.get("text") or "")
        its = [it for it in by_line.get(i, []) if not it.get("restored")]
        used = []
        if its:
            spans = []
            for it in its:
                before = str(it.get("before") or "")
                if not before:
                    continue
                pos = it.get("pos")
                at = pos if isinstance(pos, int) and text[pos:pos + len(before)] == before else text.find(before)
                if at < 0 or any(not (at + len(before) <= s or at >= e) for s, e, _, _ in spans):
                    continue
                spans.append((at, at + len(before), str(it.get("after") or ""), it.get("id")))
            for s, e, after, iid in sorted(spans, reverse=True):
                text = text[:s] + after + text[e:]
                used.append(iid)
            text = _tidy(text)
        if not re.sub(r"[\s，。！？、,.!?；;：:…“”\"'（）()]", "", text):
            continue
        row = dict(l, text=text, i=i)
        if used:
            row["edits"] = sorted(used, key=str)
        out.append(row)
    return out


def write_clean(d: Path, clean: list[dict]) -> None:
    p = Path(d) / "transcript.clean.jsonl"
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in clean), encoding="utf-8")
    os.replace(tmp, p)


# ---------- 录屏文件夹 ----------
def _is_recording_dir(p: Path) -> bool:
    """只认录屏软件自己建的会话文件夹：…/sessions/<时间戳>/，里面有 meta.json 或 recording.* 或 audio.*。"""
    if p.parent.name != "sessions" or not p.name[:8].isdigit():
        return False
    try:
        names = {c.name for c in p.iterdir()}
    except OSError:
        return False
    return "meta.json" in names or any(n.startswith(("recording.", "audio.")) for n in names)


def _dir_mb(p: Path) -> float:
    n = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                n += f.stat().st_size
        except OSError:
            pass
    return n / 1e6


def _recycle(p: Path) -> str | None:
    """送进 Windows 回收站（找得回来）；不是 Windows 就挪进 ~/.zuhui/recordings-trash。返回错误说明，成功返回 None。"""
    import os
    import shutil
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
        op = SHFILEOPSTRUCTW(None, FO_DELETE, str(p) + chr(0), None,
                             FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc == 0 and not op.fAnyOperationsAborted and not p.exists():
            return None
        return f"回收站没收下（代码 {rc}）" if p.exists() else None
    try:
        dst = Path.home() / ".zuhui" / "recordings-trash"
        dst.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dst / p.name))
        return None
    except OSError as e:
        return str(e)


# ---------- 删不掉的录屏：后台重试 ----------
_PENDING = STATE / "recycle_pending.json"
_retry_lock = threading.Lock()
_retrying = False


def _pending() -> list[str]:
    try:
        v = json.loads(_PENDING.read_text(encoding="utf-8"))
        return [str(x) for x in v] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _pending_save(paths: list[str]) -> None:
    try:
        if paths:
            _PENDING.write_text(json.dumps(sorted(set(paths)), ensure_ascii=False, indent=1), encoding="utf-8")
        elif _PENDING.exists():
            _PENDING.unlink()
    except OSError:
        pass


def _pending_add(p: Path) -> None:
    with _retry_lock:
        _pending_save(_pending() + [str(p)])


def _retry_soon(every: float = 10.0, tries: int = 60) -> None:
    """每 10 秒把还没删掉的录屏再送一次回收站，最多 10 分钟；后端重启后（Meetings 初始化时）接着来。"""
    global _retrying
    with _retry_lock:
        if _retrying or not _pending():
            return
        _retrying = True

    def run():
        global _retrying
        try:
            for _ in range(tries):
                time.sleep(every)
                with _retry_lock:
                    left = []
                    for s in _pending():
                        p = Path(s)
                        if p.exists() and _is_recording_dir(p) and _recycle(p) is not None:
                            left.append(s)
                    _pending_save(left)
                    if not left:
                        return
        finally:
            with _retry_lock:
                _retrying = False

    threading.Thread(target=run, name="recycle-retry", daemon=True).start()


# ---------- 纪要 ----------
# 纪要 JSON（summary.json）：title / speakers / abstract / conclusions / sections[{title, cards[{title, bullets}]}] / todos，
# 新写的纪要另有：
#   template   按哪种格式写的（summary_prefs.TEMPLATES）
#   people     每个人一项 [{name, said, feedback, asks}]：他讲了什么、别人给他的意见、要他做的事
#   各条内容   {"text", "t", "sec"}：t / sec = 逐字稿里说到它的那一行（"mm:ss" 和秒），回顾里点一下跳到逐字稿和录屏；
#              feedback / asks 多一个 by（谁说的），todos 多一个 who（谁做）；sections 和卡片也带 t / sec（这一节从哪开始）
#   carried    开会时带着上次遗留时才有：每条一项 [{id, ref, kind, item, from, started, done, text, t?, sec?}]（_carried）
# 旧纪要的各条是纯字符串、没有 people / template：照原样显示（前端两种都认），这里不改旧文件。
# 每人发言时长不在纪要里：Meetings.get 每次按逐字稿算（talk_time），旧会议也有。
_TIME_RE = re.compile(r"^\[?\s*(?:(\d{1,2}):)?(\d{1,3}):(\d{2})(?:\.\d+)?\s*\]?$")
_TAIL_TIME = re.compile(r"\s*[（(\[【]\s*((?:\d{1,2}:)?\d{1,3}:\d{2})\s*[）)\]】]\s*$")
_ITEM_MAX = 400          # 一条最多多少字（模型偶尔把一整段塞进一条）
_NAME_MAX = 40
_SNAP_S = 90.0           # 模型写的时间离最近的一行超过这么多秒：当它编的，不要


def _one(v, n: int) -> str:
    """一行字：并掉换行和连着的空格，截到 n 字。"""
    return " ".join(str(v if v is not None else "").split())[:n]


def parse_clock(v) -> float | None:
    """"03:21" / "75:12" / "1:02:03" / "[03:21]" / 秒数 → 秒；认不出返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v >= 0 else None
    m = _TIME_RE.match(str(v or "").strip())
    if not m or int(m.group(3)) >= 60:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


class _Clock:
    """纪要里写的时间 → 逐字稿里的那一行：照抄的 "mm:ss" 先找一模一样的那行，没有就找开口时刻最近的；
    离得太远（_SNAP_S）就当它编的，不写时间。"""

    def __init__(self, lines: list[dict]):
        self.rows: list[tuple[float, str]] = []
        self.by_t: dict[str, tuple[str, float]] = {}
        for l in lines or []:
            if l.get("who") == "system" or not str(l.get("text") or "").strip():
                continue
            try:
                sec = float(l.get("sec") or 0)
            except (TypeError, ValueError):
                continue
            t = str(l.get("t") or "") or _fmt(sec)
            self.rows.append((sec, t))
            self.by_t.setdefault(t, (t, sec))

    def at(self, v) -> tuple[str, float] | None:
        if not self.rows or v is None or v == "":
            return None
        s = str(v).strip().strip("[]【】").strip() if isinstance(v, str) else ""
        if s in self.by_t:
            return self.by_t[s]
        x = parse_clock(v)
        if x is None:
            return None
        sec, t = min(self.rows, key=lambda r: abs(r[0] - x))
        return (t, sec) if abs(sec - x) <= _SNAP_S else None


def _item(x, clock: _Clock, keys: tuple[str, ...] = ()) -> dict | None:
    """一条内容 → {"text", "t"?, "sec"?, by/who?}。字符串（旧格式、模型没照做）也收；时间写在句末括号里的也认。"""
    if isinstance(x, str):
        src: dict = {}
        text = x
    elif isinstance(x, dict):
        src = x
        text = x.get("text") or x.get("content") or ""
    else:
        return None
    text = _one(text, _ITEM_MAX * 2)
    tv = src.get("t")
    m = _TAIL_TIME.search(text)
    if m:                                   # "……（12:34）"：时间挪出来
        text = text[:m.start()].rstrip()
        if tv in (None, ""):
            tv = m.group(1)
    text = text[:_ITEM_MAX]
    if not text:
        return None
    out: dict = {"text": text}
    hit = clock.at(tv)
    if hit:
        out["t"], out["sec"] = hit
    for k in keys:
        v = _one(src.get(k), _NAME_MAX)
        if v:
            out[k] = v
    return out


def _items(xs, clock: _Clock, limit: int, keys: tuple[str, ...] = ()) -> list[dict]:
    out = []
    for x in xs if isinstance(xs, list) else []:
        it = _item(x, clock, keys)
        if it:
            out.append(it)
        if len(out) >= limit:
            break
    return out


def _earliest(*groups) -> tuple[str, float] | None:
    best = None
    for g in groups:
        for it in g or []:
            if isinstance(it, dict) and it.get("sec") is not None and (best is None or it["sec"] < best[1]):
                best = (it["t"], it["sec"])
    return best


def normalize_summary(s: dict, lines: list[dict], carried: list[dict] | None = None) -> dict:
    """模型交回来的纪要 → 存盘的样子：类型对、长度有上限、各条的时间对到逐字稿里真有的那一行。
    旧格式（各条是字符串、没有 people）也能过：各条变成 {"text"}，没有时间。别的键（source、generated……）原样留着。"""
    clock = _Clock(lines)
    out = dict(s if isinstance(s, dict) else {})
    out["title"] = _one(out.get("title"), 60)
    speakers = []
    for n in out.get("speakers") if isinstance(out.get("speakers"), list) else []:
        n = _one(n.get("name") if isinstance(n, dict) else n, _NAME_MAX)
        if n and n not in speakers:
            speakers.append(n)
    out["speakers"] = speakers[:40]
    out["abstract"] = " ".join(str(out.get("abstract") or "").split())[:1200]
    out["conclusions"] = _items(out.get("conclusions"), clock, 12)
    sections = []
    for sec in out.get("sections") if isinstance(out.get("sections"), list) else []:
        if not isinstance(sec, dict):
            continue
        cards = []
        for c in sec.get("cards") if isinstance(sec.get("cards"), list) else []:
            if not isinstance(c, dict):
                continue
            bullets = _items(c.get("bullets"), clock, 12)
            title = _one(c.get("title"), 80)
            if not title and not bullets:
                continue
            card: dict = {"title": title, "bullets": bullets}
            hit = clock.at(c.get("t")) or _earliest(bullets)
            if hit:
                card["t"], card["sec"] = hit
            cards.append(card)
            if len(cards) >= 12:
                break
        title = _one(sec.get("title"), 80)
        if not title and not cards:
            continue
        row: dict = {"title": title, "cards": cards}
        hit = clock.at(sec.get("t")) or _earliest(cards)
        if hit:
            row["t"], row["sec"] = hit
        sections.append(row)
        if len(sections) >= 12:
            break
    out["sections"] = sections
    people, seen = [], set()
    for p in out.get("people") if isinstance(out.get("people"), list) else []:
        if not isinstance(p, dict):
            continue
        name = _one(p.get("name"), _NAME_MAX)
        if not name or name in seen:
            continue
        row = {"name": name, "said": _items(p.get("said"), clock, 8),
               "feedback": _items(p.get("feedback"), clock, 8, ("by",)), "asks": _items(p.get("asks"), clock, 8, ("by",))}
        if not (row["said"] or row["feedback"] or row["asks"]):
            continue
        seen.add(name)
        people.append(row)
        if len(people) >= 30:
            break
    out["people"] = people
    out["todos"] = _items(out.get("todos"), clock, 20, ("who",))
    if carried:
        out["carried"] = _carried(out.get("carried"), carried, clock)
    else:
        out.pop("carried", None)
    return out


def _carried(raw, carried: list[dict], clock: _Clock) -> list[dict]:
    """上次遗留的每一条这次落实了没有：[{id, ref, kind, item（原来那条）, from, started, done, text（怎么落实的 / 还差什么）, t?, sec?}]。
    模型漏掉的几条也列上（done=False、text 空），编号对不上的不要。"""
    by_id = {str(c.get("id")): c for c in carried if isinstance(c, dict) and c.get("id")}
    got: dict[str, dict] = {}
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict) or str(x.get("id")) not in by_id or str(x.get("id")) in got:
            continue
        d = x.get("done")
        r = {"done": d is True or str(d).strip().lower() in ("true", "yes", "1", "是"), "text": _one(x.get("text"), _ITEM_MAX)}
        hit = clock.at(x.get("t"))
        if hit:
            r["t"], r["sec"] = hit
        got[str(x["id"])] = r
    out = []
    for cid, c in by_id.items():
        row = {"id": cid, "ref": str(c.get("ref") or ""), "kind": str(c.get("kind") or "todo"), "item": _one(c.get("text"), 200),
               "from": c.get("from"), "started": c.get("started")}
        row.update(got.get(cid) or {"done": False, "text": ""})
        out.append(row)
    return out


# 每人发言时长（不调模型）：重转写过的行有 start / end（录屏里的秒）就用实测的；别的按字数估，且不超过到下一句开口的间隔
CJK_PER_S = 4.5          # 中文每秒几个字（aftermeeting._speech_guess 也是这个数）
WORDS_PER_S = 2.6        # 英文每秒几个词（约 150 词每分钟）
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")


def speech_seconds(text) -> float:
    """按字数估一句话要说几秒。"""
    s = str(text or "")
    return len(_CJK.findall(s)) / CJK_PER_S + len(_WORD.findall(s)) / WORDS_PER_S


def talk_time(lines: list[dict]) -> list[dict]:
    """每个人说了多久：[{who, name, sec, n, share}]，按时长从多到少。who = teacher（主持人）/ student（AI）/ guest（访客，
    name 是名字）；n = 几句；share = 占全场发言的比例。系统行、没念出来的 AI 答案（unspoken）不算。"""
    rows = []
    for l in lines or []:
        if l.get("who") not in ("teacher", "student", "guest") or not str(l.get("text") or "").strip() or l.get("unspoken"):
            continue
        try:
            rows.append((float(l.get("sec") or 0), l))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r[0])
    nxt: list[float | None] = [None] * len(rows)       # 每句之后下一句（开口时刻更晚的）什么时候开口
    later, k = None, len(rows) - 1
    while k >= 0:                                       # 从后往前，同一时刻开口的几句一组
        j = k
        while j >= 0 and rows[j][0] == rows[k][0]:
            j -= 1
        for i in range(j + 1, k + 1):
            nxt[i] = later
        later, k = rows[k][0], j
    acc: dict[tuple[str, str], dict] = {}
    for k, (sec, l) in enumerate(rows):
        dur = None
        try:
            d = float(l["end"]) - float(l["start"])
            if 0 < d < 900:
                dur = d
        except (KeyError, TypeError, ValueError):
            pass
        if dur is None:
            dur = speech_seconds(l.get("text"))
            if nxt[k] is not None:
                dur = min(dur, nxt[k] - sec)
        who = str(l.get("who"))
        name = _one(l.get("name"), _NAME_MAX) if who == "guest" else ""
        r = acc.setdefault((who, name), {"who": who, "name": name or None, "sec": 0.0, "n": 0})
        r["sec"] += max(0.0, dur)
        r["n"] += 1
    total = sum(r["sec"] for r in acc.values())
    out = sorted(acc.values(), key=lambda r: (-r["sec"], r["who"], r["name"] or ""))
    for r in out:
        r["share"] = round(r["sec"] / total, 3) if total > 0 else 0.0
        r["sec"] = round(r["sec"], 1)
    return out


# 每个人那一节三栏的名字（进展汇报换成"进展 / 意见 / 下周任务"）
PEOPLE_LABELS = {"default": ("讲了什么", "收到的意见", "要做的事"), "progress": ("进展", "意见", "下周任务")}


def _md_item(x) -> str:
    if not isinstance(x, dict):
        return str(x or "")
    s = str(x.get("text") or "")
    if x.get("by"):
        s = f"{x['by']}：{s}"
    return s + (f" `{x['t']}`" if x.get("t") else "")


def render_markdown(s: dict) -> str:
    out = [f"# {s.get('title') or '组会纪要'}", ""]
    if s.get("speakers"):
        out.append("发言人：" + "、".join(str(x) for x in s["speakers"]))
    if s.get("abstract"):
        out += ["", "主题摘要：" + s["abstract"]]
    if s.get("conclusions"):
        out += ["", "## 核心结论", ""] + [f"- {_md_item(c)}" for c in s["conclusions"]]
    people: list[str] = []
    if s.get("people"):
        heads = PEOPLE_LABELS.get(s.get("template") or "", PEOPLE_LABELS["default"])
        people = ["", "## 每个人"]
        for p in s["people"]:
            people += ["", f"### {p.get('name', '')}"]
            for head, key in zip(heads, ("said", "feedback", "asks")):
                if p.get(key):
                    people += ["", head, ""] + [f"- {_md_item(x)}" for x in p[key]]
    topics: list[str] = []
    for sec in s.get("sections") or []:
        topics += ["", f"## {sec.get('title', '')}" + (f" `{sec['t']}`" if sec.get("t") else "")]
        for i, card in enumerate(sec.get("cards") or [], 1):
            topics += ["", f"### {i:02d} {card.get('title', '')}", ""] + [f"- {_md_item(b)}" for b in card.get("bullets") or []]
    out += (people + topics) if s.get("template") == "progress" else (topics + people)
    if s.get("todos"):
        out += ["", "## 后续工作安排", ""] + [f"- {_md_item(t)}" for t in s["todos"]]
    if s.get("carried"):
        rows = [c for c in s["carried"] if isinstance(c, dict)]
        out += ["", "## 遗留事项", ""] + [
            f"- [{'x' if c.get('done') else ' '}] {c.get('item') or ''}" + (f"：{c['text']}" if c.get("text") else "")
            + (f" `{c['t']}`" if c.get("t") else "") for c in sorted(rows, key=lambda c: not c.get("done"))]
    return "\n".join(out) + "\n"


# <AI> / <ME> 换成会上 AI 的名字和主持人的名字（AI 叫什么由设置定，这里不写死型号）
SUMMARY_PROMPT = """你是组会的记录员。下面是一场组会的逐字稿和任务板。逐字稿每行是"[时间] 说话人：内容"：<ME> 是主持人；<AI> 是会上的 AI 助手，即时回应、在后台改论文；其余是通过链接进来的参会者。任务板是 <AI> 实际改了什么。
提到人的时候一律写他的名字（逐字稿里的写法），不要写"老师""学生""主持人""助手"这类称呼。
严格用 JSON 输出，字段：
{
 "title": "六到十二字的会议主题",
 "speakers": ["逐字稿里出现过的说话人名字"],
 "abstract": "两三句话的主题摘要：这场会围绕什么、定了什么、改了什么",
 "conclusions": [{"text": "核心结论，一句话", "t": "mm:ss"}],
 "sections": [{"title": "主题名（如：定义与符号章的详略）", "t": "mm:ss", "cards": [{"title": "小标题", "bullets": [{"text": "要点一句话", "t": "mm:ss"}]}]}],
 "people": [{"name": "名字", "said": [{"text": "他讲的内容", "t": "mm:ss"}], "feedback": [{"text": "别人给他的意见", "by": "提意见的人", "t": "mm:ss"}], "asks": [{"text": "要他做的事", "by": "提出的人", "t": "mm:ss"}]}],
 "todos": [{"text": "后续工作，一句话，含谁做、做什么", "who": "负责的人", "t": "mm:ss"}]
}
t：照抄逐字稿行首方括号里的时间，指向这条内容最早说到的那一行；sections 的 t 是这个主题开始讨论的那一行；说不准就不写 t，不要编。
people：会上开口说过话的每个人一项（不含 <AI>），按第一次开口的先后排；said 写他自己讲的内容、汇报的进展、提的观点；feedback 写别人针对他说的意见和评价；asks 写别人要他做的事；没有的给空数组。
conclusions 三到五条；每条一句话。
要求：只写逐字稿和任务板里有的内容，不编造；<AI> "已改"的以任务板为准，"排队/改中/失败"的要写进 todos；用中文，不用 markdown 符号；JSON 之外不要输出任何文字。"""

# 各种格式在上面之外多交代的（自定义 = 设置里写的本组要求）
SUMMARY_TEMPLATES = {
    "default": "",
    "progress": "这是一场进展汇报：大家轮流讲自己这段时间的进展，别人给意见、布置下一步。people 是重点：said 写他汇报的进展"
                "（做了什么、结果怎样、卡在哪里），feedback 写别人给他的意见，asks 写他下周要做的事。sections 只放不属于某一个人的"
                "公共议题，没有就给空数组。todos 汇总每个人下周的任务，who 写是谁的。",
    "paper": "这是一场论文讨论：sections 按论文的章节或讨论到的问题分组（如：第三章 实验设置、相关工作的定位），卡片写具体问题、"
             "各人的看法和定下的改法；conclusions 写定下来的修改决定；todos 写要改的地方，说明是哪一章、谁来改。",
    "proposal": "这是一场开题或预答辩：通常一个人汇报，其余人提问、质疑、给意见。sections 依次是：汇报内容（研究问题、方法、进度安排）；"
                "提问与回答（每张卡片一个问题：谁问的、问了什么、怎么答的）；评审意见（按提意见的人分卡片）。conclusions 写会上给的"
                "总体评价和必须修改的地方，会上没给结论就不要编。todos 写会后必须完成的修改。",
}
# 这场会带着上次遗留的事（开会时抄进来的 carry_in.json，见 zuhui/past.py）：纪要里写出哪些这次落实了
CARRIED_RULE = ('这场会带着上次遗留的事（用户消息最后列着，方括号里是编号）。JSON 里再加一个字段 '
                '"carried": [{"id": "c1", "done": true, "text": "这次怎么落实的，或者还差什么，一句话", "t": "mm:ss"}]：'
                '每条遗留一项，id 照抄；会上说已经做完了、或者任务板上这次改好了的，done 为 true；没做完、没提到的，done 为 false，'
                '没提到就 text 写"会上没提到"。t 指向说到它的那一行，没说到就不写。')
_CARRY_KIND = {"todo": "待办", "note": "标注", "edit": "没收下的改动"}
CUSTOM_HEAD = "本组对纪要的要求（按这些要求组织 sections、conclusions、people、todos 的内容；JSON 字段和格式不变）：\n"


def _chat_summary_prompt() -> str:
    """没有论文的会（mode=chat）：AI 没说话、没有任务板，纪要里不提改论文。照 SUMMARY_PROMPT 改几处。"""
    s = SUMMARY_PROMPT
    head, rest = s.split("\n", 1)
    head = ('你是会议的记录员。下面是一场讨论的逐字稿。逐字稿每行是"[时间] 说话人：内容"：<ME> 是主持人；'
            '其余是通过链接进来的参会者。这场会没有论文，也没有 AI 发言。')
    rest = (rest.replace("定了什么、改了什么", "定了什么")
                .replace("（如：定义与符号章的详略）", "")
                .replace("（不含 <AI>）", "")
                .replace("只写逐字稿和任务板里有的内容，不编造；<AI> \"已改\"的以任务板为准，\"排队/改中/失败\"的要写进 todos；",
                         "只写逐字稿里有的内容，不编造；"))
    return head + "\n" + rest


def summary_prompt(template: str, custom: str = "", me: str = "主持人", ai: str = "AI", chat: bool = False) -> str:
    """这种格式的纪要用的系统提示词。自定义格式没写要求时按默认格式写。chat：没有论文的会（不提改论文、任务板）。"""
    if chat and template == "paper":           # 论文讨论那种格式：这场会没有论文，按默认格式写
        template = "default"
    base = (_chat_summary_prompt() if chat else SUMMARY_PROMPT).replace("<ME>", me).replace("<AI>", ai)
    if template == "custom":
        extra = CUSTOM_HEAD + custom.strip() if custom.strip() else ""
    else:
        extra = SUMMARY_TEMPLATES.get(template, "")
    return base + ("\n\n" + extra if extra else "")


_TODO_ST = {"queued": "排队中", "working": "修改中", "failed": "修改失败"}   # 兜底纪要的待办后面括号里的状态


def summarize(cfg: dict, meeting: dict, lines: list[dict], tasks: list[dict], template: str | None = None,
              custom: str | None = None, fallback: bool = True, carried: list[dict] | None = None,
              bookmarks: list[dict] | None = None) -> dict:
    """生成纪要：template 不给就按设置里选的格式（summary_prefs），custom 不给就用设置里写的自定义要求。
    carried：开会时带进来的上次遗留（past.carry_in）：纪要里多一节 carried，写出每条这次落实了没有。
    走模型来源（claude_text：API 或 Claude Code 账号）。模型没答出来时：fallback=True 返回一份纯规则拼出来的兜底纪要
    （散会后的流水线），False 抛 RuntimeError（回顾里重新生成：别拿兜底盖掉原来的纪要）。"""
    import getpass
    from . import summary_prefs
    prefs = summary_prefs.load()
    template = template if template in summary_prefs.TEMPLATES else prefs["template"]
    custom = str(prefs["custom"] if custom is None else custom)[:summary_prefs.CUSTOM_MAX]
    used = "default" if template == "custom" and not custom.strip() else template
    chat = mode_of(meeting) == "chat"          # 没有论文的会：只有逐字稿，AI 没说话、没有任务板
    if chat and used == "paper":
        used = "default"
    me = str((cfg.get("meeting") or {}).get("me") or getpass.getuser() or "我")
    ai = assistant_name(cfg)

    def who(l: dict) -> str:
        w = l.get("who")
        return me if w == "teacher" else ai if w == "student" else (l.get("name") or "参会者") if w == "guest" else "系统"

    transcript = "\n".join(f"[{l.get('t') or _fmt(float(l.get('sec') or 0))}] {who(l)}：{l.get('text')}" for l in lines)
    multi = len({t.get("paper") for t in tasks if t.get("paper")}) > 1    # 会上改了不止一篇：每条标上是哪篇的
    board = "\n".join(f"{t.get('id')}（{t.get('status')}）" + (f"《{t.get('paper_name') or '论文'}》" if multi else "")
                      + f"{t.get('title')}：{(t.get('result') or t.get('error') or '')[:160]}" for t in tasks) or "（无）"
    user = f"会议：{meeting.get('title')}，时长 {int(meeting.get('duration_s') or 0) // 60} 分钟。\n\n逐字稿：\n{transcript or '（空）'}"
    if not chat:
        user += f"\n\n任务板：\n{board}"
    carried = [c for c in (carried or []) if isinstance(c, dict) and c.get("id")]
    if carried:
        user += "\n\n上次遗留：\n" + "\n".join(
            f"[{c['id']}] {_CARRY_KIND.get(str(c.get('kind')), '待办')}：{_one(c.get('text'), 200)}"
            + (f"（{_one(c.get('who'), 40)}）" if c.get("who") else "") for c in carried)
    from . import inmeeting
    bookmarks = [b for b in (bookmarks or []) if isinstance(b, dict)]
    if bookmarks:                              # 会上点"标记"标的重点：纪要优先写这几处附近的
        user += "\n\n会上标的重点：\n" + inmeeting.bookmarks_block(bookmarks)
    system = (summary_prompt(used, custom, me, ai, chat=chat) + ("\n\n" + CARRIED_RULE if carried else "")
              + ("\n\n" + inmeeting.BOOKMARK_RULE if bookmarks else ""))
    try:
        from .claude_text import SONNET, ask_json

        def ask(extra: str = "") -> dict:
            s0 = ask_json(user, system=system + extra, model=SONNET, timeout=300)
            if not isinstance(s0, dict):
                raise ValueError("纪要不是 JSON 对象")
            return s0
        try:
            s = ask()
        except ValueError:                     # 还是太长被截断：再要一次精简版
            s = ask("\n\n这次务必精简：每条不超过 40 字，sections 最多 4 组，每组最多 4 条，people 每人每栏最多 3 条。")
        s = normalize_summary(s, lines, carried)
        if not s["title"]:
            s["title"] = _one(meeting.get("title"), 60) or "组会纪要"
        s["source"] = "claude"
    except Exception as e:
        if not fallback:
            raise RuntimeError(str(e) or type(e).__name__) from None
        done = [t for t in tasks if t.get("status") == "done"]
        rest = [t for t in tasks if t.get("status") != "done"]
        n_me = sum(1 for l in lines if l.get("who") == "teacher")
        s = {
            "title": meeting.get("title") or "组会纪要",
            "speakers": ([me] if chat else [me, ai]) + sorted({l.get("name") for l in lines if l.get("who") == "guest" and l.get("name")}),
            "abstract": (f"{me} 发言 {n_me} 句" if chat else
                         f"{me} 提出 {n_me} 条意见，{ai} 已修改 {len(done)} 处，{len(rest)} 条待处理"),
            "conclusions": [f"已修改：{t.get('title')}" for t in done][:5],
            "sections": [],
            "todos": [f"{t.get('title')}（{_TODO_ST.get(t.get('status'), t.get('status'))}）" for t in rest],
            "source": f"fallback: {e!r}",
        }
    s["template"] = used
    s["generated"] = _now_iso()
    s["markdown"] = render_markdown(s)
    return s


def regenerate(meetings: "Meetings", cfg: dict, mid: str, template: str) -> dict:
    """回顾里换一种格式重新生成纪要（用户点了才调模型）：按这场会的干净版逐字稿和散会时的任务板写，写好了替换 summary.json。
    模型没答出来抛 RuntimeError，原来的纪要不动；没有这场会抛 KeyError。"""
    mid = Path(str(mid)).name
    g = meetings.get(mid)
    if not g:
        raise KeyError(mid)
    tasks = (g.get("board") or {}).get("tasks") or []
    from . import past
    s = summarize(cfg, g["meeting"], g["transcript"], tasks, template=template, fallback=False,
                  carried=past.carry_in(meetings, mid), bookmarks=g.get("bookmarks"))
    meetings.set_summary(mid, s)
    try:                                       # 纪要说落实了的遗留项：在原来那场会里勾掉
        past.settle(meetings, mid, s)
    except Exception:
        pass
    return s


# ---------- 自测：python -m zuhui.meetings selftest（临时目录，不碰真的会议库和录屏） ----------
def selftest() -> int:
    import tempfile
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(("OK  " if cond else "BAD ") + label + (f" | {detail}" if detail else ""))

    tmp = Path(tempfile.mkdtemp(prefix="meetings-selftest-"))
    outside = Path(tempfile.mkdtemp(prefix="pretend-real-"))       # 假装是"真的"录屏：在测试库外面

    def recording(base: Path, ts: str) -> Path:
        p = base / "sessions" / ts
        p.mkdir(parents=True)
        (p / "meta.json").write_text("{}", encoding="utf-8")
        (p / "recording.mp4").write_bytes(b"0" * 1000)
        return p

    def meeting(mt: "Meetings", mid: str, session: Path, lines: list[dict]) -> None:
        d = mt.root / mid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": mid, "title": "组会 · 11月11日 10:00", "started": "2026-11-11T10:00:00",
                                                 "ended": "2026-11-11T10:30:00", "state": "ended", "duration_s": 1800,
                                                 "session": str(session)}, ensure_ascii=False), encoding="utf-8")
        (d / "transcript.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")

    mt = Meetings(root=tmp / "meetings")
    # 1. 测试库里的会指着库外面的录屏：删会，录屏不动，也不排重试
    far = recording(outside, "20261111-100000")
    meeting(mt, "20261111-100000", far, [{"t": "00:01", "sec": 1, "who": "teacher", "text": "你好"}])
    before = _pending()
    r = mt.delete("20261111-100000")
    check("测试库删会：成功", r.get("ok"))
    check("测试库删会：库外的录屏没动", far.exists() and (r.get("recording") or {}).get("kept") is True, json.dumps(r, ensure_ascii=False))
    check("测试库删会：没排重试", _pending() == before)
    # 2. 测试库里的会指着库里面的录屏：照常送进回收站
    near = recording(tmp, "20261111-110000")
    meeting(mt, "20261111-110000", near, [{"t": "00:01", "sec": 1, "who": "teacher", "text": "你好"}])
    r = mt.delete("20261111-110000")
    check("测试库删会：库里的录屏照常删", r.get("ok") and not near.exists() and (r.get("recording") or {}).get("deleted"),
          json.dumps(r, ensure_ascii=False))
    # 3. 真的会议库照旧（只判断，不真删）
    real = object.__new__(Meetings)
    real.root = REAL_ROOT
    check("真的会议库：录屏目录里的录屏都能删（行为不变）", real._owns_recording(far))
    # 4. 整理后的干净版：get / restore / export 的约定
    raw = [{"t": "00:01", "sec": 1, "who": "teacher", "text": "我我我觉得这一节，我他妈又忘了，要改。"},
           {"t": "00:05", "sec": 5, "who": "student", "text": "好的，第三节是对的。"},
           {"t": "00:09", "sec": 9, "who": "teacher", "text": "不对，第三节有错。"},
           {"t": "00:12", "sec": 12, "who": "teacher", "text": "电话是 13800000000。"}]
    meeting(mt, "20261111-120000", tmp / "nowhere", raw)
    g = mt.get("20261111-120000")
    check("没整理过：transcript = 原始版、cleanup = None",
          g["cleanup"] is None and [x["text"] for x in g["transcript"]] == [x["text"] for x in raw] and g["transcript"][0]["i"] == 0)
    items = [{"id": "c001", "line": 0, "kind": "retract", "before": "我我我觉得", "after": "我觉得", "reason": "重复", "restored": False},
             {"id": "c002", "line": 0, "kind": "profanity", "before": "他妈", "after": "", "reason": "脏话", "restored": False},
             {"id": "c003", "line": 1, "kind": "fact", "before": "好的，第三节是对的。", "after": "", "reason": "第 2 行指出有错", "restored": False},
             {"id": "c004", "line": 3, "kind": "sensitive", "before": "13800000000", "after": "", "reason": "隐私", "restored": False}]
    d = mt.root / "20261111-120000"
    _write_json(d / "cleanup.json", {"version": 1, "items": items})
    write_clean(d, apply_cleanup(raw, items))
    g = mt.get("20261111-120000")
    texts = [x["text"] for x in g["transcript"]]
    check("干净版：改口留一遍、脏话删掉", texts[0] == "我觉得这一节，我又忘了，要改。", texts[0])
    check("干净版：整行删掉的不出现，行号 i 对得上原始版", [x["i"] for x in g["transcript"]] == [0, 2, 3], str([x["i"] for x in g["transcript"]]))
    check("干净版：隐私删掉", texts[-1] == "电话是。", texts[-1])
    check("transcript_raw 原样、cleanup 是条目列表", g["transcript_raw"] == raw and len(g["cleanup"]) == 4)
    check("edits 记着用了哪几条", g["transcript"][0].get("edits") == ["c001", "c002"])
    g = mt.restore("20261111-120000", ["c003", "c002"])
    texts = [x["text"] for x in g["transcript"]]
    check("恢复两条：那一行回来了、脏话回来了", texts[1] == "好的，第三节是对的。" and "他妈" in texts[0], str(texts[:2]))
    clean_file = [json.loads(x)["text"] for x in (d / "transcript.clean.jsonl").read_text(encoding="utf-8").splitlines()]
    check("恢复后 transcript.clean.jsonl 也跟着变", clean_file == texts)
    g = mt.restore("20261111-120000", ["c002"], restored=False)
    check("再删掉：restored=False", "他妈" not in g["transcript"][0]["text"])
    e1, e2 = mt.export("20261111-120000", "md"), mt.export("20261111-120000", "txt", version="raw")
    check("导出干净版（默认）", e1["ok"] and "他妈" not in e1["text"] and e1["version"] == "clean" and e1["name"].endswith(".md"))
    check("导出原始版", e2["ok"] and "他妈" in e2["text"] and "原始版" in e2["name"] and e2["name"].endswith(".txt"))
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(outside, ignore_errors=True)
    print("selftest", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["selftest"]:
        raise SystemExit(selftest())
