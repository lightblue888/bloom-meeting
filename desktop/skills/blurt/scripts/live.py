# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy==2.3.4; python_version >= '3.11'", "numpy==2.2.6; python_version < '3.11'", "sherpa-onnx==1.13.8", "truststore==0.10.5", "opencc-python-reimplemented==0.1.7"]
# ///
"""Live transcript while a recording runs (L1), so it is ready when the take ends. docs/design/snap-and-live.md §4.

  live.py run <session> [--watch-pid PID]     started by the recorder when recording begins; exits by itself
  live.py simulate <audio> <session> [--speed N] [--run]
                                              feed an audio file into <session>/live/audio.pcm in real time (N× faster;
                                              0 = as fast as possible), then create live/stop — for tests and timing.
                                              --run also starts `live.py run` and reports stop → transcript latency
  live.py status <session>                    print live/status.json

`run` tails live/audio.pcm (16 kHz mono s16le, written by the recorder, pause-gated: byte offset / 32000 = video time),
cuts it at pauses with the same VAD and chunking as `transcribe.py run`, transcribes each chunk with the configured ASR
backend as soon as it closes, and appends live/transcript.jsonl. When live/stop appears it drains the rest and writes
<session>/transcript.json + transcript.txt — the same document `transcribe.py run` makes from the whole file (it then
returns instantly). Chunks are cut exactly where a whole-file run cuts them, so the text is the same.

Config ($BLURT_HOME/config.json → record): live (default true). No model drafts items while recording: the agent
that organizes the take reads the finished transcript.

Files in <session>/live/: audio.pcm, stop (recorder) · transcript.jsonl, status.json. Our stderr goes to
<session>/live.log (kept across restarts).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _common import IS_WIN, die, ffmpeg_bin, global_config, load_json, save_json  # noqa: E402

SR = 16000
BPS = SR * 2                    # bytes per second of audio.pcm
CLOSE_MARGIN = 1.0              # see LiveChunker
SOFT_CHUNK = 3.0                # live_fast default for soft_chunk (see LiveChunker.soft_len); "soft_chunk": 0 turns it off


def fast_opts() -> dict:
    """Low-latency chunking for a live conversation (Bloom's meetings). config.json → record.live_fast, e.g.
    {"max_gap": 0.5, "close_margin": 0.1, "max_chunk": 10}: a chunk then closes ~0.8 s after the speaker stops instead
    of ~2.2 s, and long speech is cut at shorter pauses, so most of it is decoded while they are still talking. The
    price: the chunks no longer equal a whole-file run's (see LiveChunker), which is why it is opt-in. With live_fast a
    piece is also sent at the first pause once it is soft_chunk (default SOFT_CHUNK) seconds long."""
    try:
        o = (global_config().get("record") or {}).get("live_fast") or {}
    except Exception:
        o = {}
    return o if isinstance(o, dict) else {}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ Chinese and English only
# Fun-ASR-Nano knows Japanese too. Not told the language, it picks the script itself: a short 嗯 / 呃 on its own — the
# same sound and meaning as Japanese うん — now and then comes back as kana, and an English word it can't place in
# katakana ("Markdown" → "マップアウト", "Claude" → "クレイ"). The live transcript is Chinese and English (asr.languages
# without ja / ko), so its recognizer is built told so (chinese_english), and whatever kana still turns up becomes the
# paper's term it sounds like (Bloom's lexicon, the hotwords, a few everyday names) or goes (ScriptFix).
KANA = "぀-ヿㇰ-ㇿｦ-ﾟ"
HANGUL = "ᄀ-ᇿ㄰-㆏가-힯"
FOREIGN = re.compile(f"[{KANA}{HANGUL}]+")
SIL = re.compile(r"\s*/sil\b[。，、,.？?！!]*")   # Fun-ASR-Nano's mark for a piece with no words in it
EVERYDAY = ("Markdown", "PDF", "Word", "TXT", "LaTeX", "Excel", "PPT", "PowerPoint", "GitHub", "Python", "Claude",
            "DeepSeek", "ChatGPT", "prompt", "API", "app", "bug", "Zoom", "Bloom", "Overleaf", "Figure", "Table",
            "baseline", "benchmark", "paper", "OK")
_ROMAJI = dict(zip(
    "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲンガギグゲゴザジズゼゾ"
    "ダヂヅデドバビブベボパピプペポヴァィゥェォャュョヮ",
    "a i u e o ka ki ku ke ko sa shi su se so ta chi tsu te to na ni nu ne no ha hi fu he ho ma mi mu me mo ya yu yo "
    "ra ri ru re ro wa o n ga gi gu ge go za ji zu ze zo da ji zu de do ba bi bu be bo pa pi pu pe po vu a i u e o "
    "ya yu yo wa".split()))


def foreign_languages() -> bool:
    """Japanese or Korean is among the user's languages (asr.languages): then kana / hangul are real words."""
    langs = (global_config().get("asr") or {}).get("languages") or []
    return any(str(x).split("-")[0] in ("ja", "ko") for x in langs)


def romaji(kana: str) -> str:
    """Katakana / hiragana as a loanword's English letters, roughly: マークダウン → "markdaun", メタトレーダー →
    "metatredar" (ー after a / e is the English r; the vowels Japanese puts between consonants dropped)."""
    out, double = "", False
    for ch in kana:
        if "ぁ" <= ch <= "ゖ":
            ch = chr(ord(ch) + 0x60)   # hiragana → katakana
        if ch in "ッ":
            double = True
            continue
        if ch == "ー":
            out += "r" if out[-1:] in ("a", "e") else ""
            continue
        if ch == "・":
            continue
        r = _ROMAJI.get(ch, "")
        if ch in "ャュョ" and out.endswith("i"):
            out, r = out[:-1], r       # キャ → kya
        if ch in "ァィゥェォ" and out:
            out = out[:-1]             # ファ → fa, ティ → ti
        if double and r:
            r = r[0] + r
        double = False
        out += r
    # the vowels Japanese puts after a consonant: "mappuauto" → "mapaut", "toreda" → "treda"
    out = re.sub(r"([bcdfghjklmnpqrstvwxyz])\1", r"\1", out)
    out = re.sub(r"(?<=[ptkbdgsfzrm])u(?=[aeiou]|$)|(?<=[td])o$", "", out)
    out = re.sub(r"(?<=[bdfgkprstz])[uo](?=[bcdfgjklmnpqrstvxz])", "", out)
    return out


def _sound(w: str) -> str:
    """Letters as a Japanese ear folds them: c / q → k, l → r, v → b, ph → f."""
    w = re.sub(r"[^a-z]", "", w.lower())
    for a, b in (("ph", "f"), ("ck", "k"), ("sh", "s"), ("ch", "c"), ("ts", "t"), ("qu", "k"), ("x", "ks"), ("q", "k"),
                 ("l", "r"), ("v", "b")):
        w = w.replace(a, b)
    return w.replace("c", "k")


def _skeleton(w: str) -> str:
    return re.sub(r"[aeiouyhw]", "", w)


class ScriptFix:
    """Kana / hangul runs in a line → the term they sound like (≥ 0.75 similar, letters compared) or nothing."""

    def __init__(self, extra: list[str] | None = None):
        import difflib
        self.ratio = lambda a, b: difflib.SequenceMatcher(None, a, b).ratio()
        terms = list(EVERYDAY) + list(extra or [])
        lex = Path.home() / ".zuhui" / "lexicon" / "current.json"   # Bloom: the paper being discussed
        try:
            d = json.loads(lex.read_text(encoding="utf-8")) if lex.exists() else {}
            terms += [t.get("term") for t in d.get("terms") or [] if isinstance(t, dict)]
            terms += list(d.get("hotwords") or []) + list((d.get("variants") or {}).values())
        except (OSError, ValueError, AttributeError):
            pass
        self.terms: dict[str, str] = {}
        for t in terms:   # the first of two that sound alike wins (the everyday names, then the paper's by rank)
            if isinstance(t, str) and re.search("[A-Za-z]{2}", t):
                self.terms.setdefault(_sound(t), t)

    def term(self, run: str) -> str:
        r = _sound(romaji(run))
        sk = _skeleton(r)
        if len(sk) < 2:
            return ""
        best, pick = 0.0, ""
        for k, t in self.terms.items():
            ks = _skeleton(k)
            short = min(len(sk), len(ks)) < 3
            if not ks or (short and sk != ks):
                continue
            # a loanword is carried by its consonants (Japanese adds and bends the vowels): both must be close
            full = self.ratio(r, k)
            score = 0.6 * self.ratio(sk, ks) + 0.4 * full
            if score > best and (not short or full >= 0.75):
                best, pick = score, t
        return pick if best >= 0.75 else ""

    def known(self, text: str) -> str | None:
        """The line with its kana / hangul as terms, when every run of them sounds like one; else None."""
        runs = FOREIGN.findall(text or "")
        return self(text) if runs and all(self.term(r) for r in runs) else None

    def __call__(self, text: str) -> str:
        if not FOREIGN.search(text or ""):
            return text
        def sub(m):
            t = self.term(m.group(0))
            return f" {t} " if t else ""
        out = FOREIGN.sub(sub, text)
        out = re.sub(r"\s{2,}", " ", out).strip()
        out = re.sub(r"\s+([，。？！、,.?!])", r"\1", out)
        return out if re.search(r"[\w一-鿿]", out) else ""


def chinese_english() -> None:
    """Fun-ASR-Nano (FunAsrOnnx): its recognizer is built with language 中文, so the prompt says 语音转写成中文 — no
    kana, and English words stay English. Same takes, same model, told vs not told (2026-11-12: 343 pieces of the
    founder's meetings, the golden set, an all-English talk): kana in 4 pieces → 0; golden 40 lines 1.51 % → 1.35 %;
    the English talk 3.44 % → 3.44 % with not one Chinese character; short Chinese pieces no longer come back as English
    ("听到了" → "food"); decode time the same. sherpa-onnx 1.13.8 fixes the language when the recognizer is built (a
    stream option or set_config is ignored), so this is the one recognizer, Chinese from the start, patched in before
    asr_setup builds it: no second recognizer (~1 GB) for a re-decode. "/sil" — what it writes for a piece with no
    words in it, told the language or not — is no text. Other backends: as is."""
    import inspect

    import transcribe as T
    cls = getattr(T, "FunAsrOnnx", None)
    if cls is None or getattr(cls, "_zh_only", False):
        return
    plain = cls._decode

    def build(self, hotwords: str):
        import sherpa_onnx
        d = self.dir
        params = inspect.signature(sherpa_onnx.OfflineRecognizer.from_funasr_nano).parameters
        kw = {"max_new_tokens": self.MAX_NEW_TOKENS} if "max_new_tokens" in params else {}
        if "language" in params:
            kw["language"] = "中文"
        return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
            encoder_adaptor=str(d / "encoder_adaptor.int8.onnx"), llm=str(d / "llm.int8.onnx"),
            embedding=str(d / "embedding.int8.onnx"), tokenizer=str(d / "Qwen3-0.6B"), num_threads=self.threads,
            hotwords=hotwords, **kw)

    def decode(self, x):
        text, lang = plain(self, x)
        text = SIL.sub("", text).strip()
        if FOREIGN.search(text):
            log(f"kana though told Chinese: {text!r}")
        return (text if re.search(r"\w", text) else ""), lang

    cls._build, cls._decode, cls._zh_only = build, decode, True


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return True
    if IS_WIN:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ chunking
class LiveChunker:
    """Streaming twin of transcribe.vad_segments + group_chunks: the same VAD fed the same windows, the same padding
    and the same greedy grouping, so the chunks (and the text) equal a whole-file run over the same samples.

    A chunk is closed when the next speech segment can't join it (gap > max_gap or too long) — exactly the
    whole-file rule — or early, once the stream is max_gap + CLOSE_MARGIN past its end and the VAD hears no speech:
    a later segment then starts after that gap (the VAD's look-back is ~0.3 s + 0.15 s padding), so it could not
    have joined anyway. Early closing is what makes captions appear ~2 s after a pause rather than at the next one."""

    def __init__(self, make_vad, max_chunk: float, max_gap: float = 1.0, close_margin: float = CLOSE_MARGIN,
                 force_cut: bool = False, soft_len: float = 0.0):
        from transcribe import pad_segment
        self.pad = pad_segment
        self.vad, self.win = make_vad(min(25, max_chunk))
        self.max_len, self.max_gap, self.close_margin = max_chunk, max_gap, close_margin
        # live_fast only: a piece already soft_len seconds long is sent at the speaker's next pause the VAD hears
        # (≥ 0.3 s) instead of waiting max_gap to see whether they go on. A long turn is then decoded piece by piece
        # while it is being spoken, and what is left to decode after the last word is the short tail, not up to
        # max_chunk seconds of it (the decode of the last piece is most of the wait before a reply).
        self.soft_len = float(soft_len or 0.0)
        self.rest = np.zeros(0, np.float32)
        self.fed = 0                      # samples given to the VAD
        self.cur: list[float] | None = None
        self.last_speech_end = 0.0
        # live_fast only: the VAD keeps one unbroken stretch of speech as one segment however long it gets (a 26 s
        # piece was seen), and Fun-ASR loops on pieces that long. Past max_chunk the stretch is cut at its quietest
        # moment and sent on its own; the VAD's segment, when it comes, is trimmed to what follows the cut.
        self.force_cut = force_cut
        self.cut_at = 0.0                 # audio before this second was already sent as forced pieces
        self.speech_from: float | None = None   # where the stretch of speech going on right now began (estimate)
        self.hist = np.zeros(0, np.float32)     # the last few seconds fed, to find a quiet moment

    @property
    def pos(self) -> float:
        return self.fed / SR

    def in_speech(self) -> bool:
        return bool(self.vad.is_speech_detected())

    def feed(self, x: np.ndarray) -> list[tuple[float, float]]:
        buf = np.concatenate([self.rest, x]) if len(self.rest) else x
        n = len(buf) // self.win * self.win
        out: list[tuple[float, float]] = []
        for i in range(0, n, self.win):
            self.vad.accept_waveform(buf[i:i + self.win])
            self.fed += self.win
            if self.in_speech():
                if self.speech_from is None:
                    self.speech_from = max(self.cut_at, self.pos - 0.45)
            else:
                self.speech_from = None
            out += self._drain()
        if self.force_cut and n:
            self.hist = np.concatenate([self.hist, buf[:n]])[-SR * 6:]
        self.rest = buf[n:]
        if self.cur and not self.in_speech() and self.pos > self.cur[1] + self.max_gap + self.close_margin:
            out.append(tuple(self.cur))
            self.cur = None
        if self.force_cut and self.in_speech():
            st = self.open_start()
            if st is not None and self.pos - st >= self.max_len:
                t = self._quiet_point(max(st + self.max_len * 0.5, self.pos - 3.0), self.pos - 0.3)
                out.append((st, t))
                self.cur, self.cut_at, self.speech_from = None, t, t
        return out

    def open_start(self) -> float | None:
        """Where the piece still being spoken begins (the open chunk, or the stretch of speech going on now)."""
        st = self.cur[0] if self.cur else self.speech_from
        return None if st is None else max(st, self.cut_at)

    def _quiet_point(self, a: float, b: float) -> float:
        """The quietest 50 ms between a and b seconds (cut there: between words, not through one)."""
        h = self.hist
        if b - a < 0.2 or not len(h):
            return b
        end = self.pos
        i0, i1 = int((a - (end - len(h) / SR)) * SR), int((b - (end - len(h) / SR)) * SR)
        i0, i1 = max(0, i0), min(len(h), i1)
        step = SR // 20
        if i1 - i0 < step * 2:
            return b
        frames = [(float(np.sqrt(np.mean(h[k:k + step] ** 2))), k) for k in range(i0, i1 - step, step // 2)]
        k = min(frames)[1] + step // 2
        return end - (len(h) - k) / SR

    def candidate(self) -> tuple[float, float] | None:
        """The open chunk as it stands, when the VAD hears no speech right now (LiveRun.speculate)."""
        return tuple(self.cur) if (self.cur and not self.in_speech()) else None

    def finish(self, duration: float) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        if len(self.rest):  # the whole-file run feeds the last partial window too
            self.vad.accept_waveform(self.rest)
            self.fed += len(self.rest)
            self.rest = np.zeros(0, np.float32)
        self.vad.flush()
        out += self._drain(duration)
        if self.cur:
            out.append(tuple(self.cur))
            self.cur = None
        return out

    def _drain(self, duration: float = float("inf")) -> list[tuple[float, float]]:
        out = []
        while not self.vad.empty():
            seg = self.vad.front
            s, e = self.pad(seg.start / SR, (seg.start + len(seg.samples)) / SR, duration)
            self.last_speech_end = (seg.start + len(seg.samples)) / SR
            self.vad.pop()
            if self.cut_at:
                if e <= self.cut_at + 0.3:
                    continue                  # all of it already went out as forced pieces
                s = max(s, self.cut_at)
            if self.cur and s - self.cur[1] <= self.max_gap and e - self.cur[0] <= self.max_len:
                self.cur[1] = e
            else:
                if self.cur:
                    out.append(tuple(self.cur))
                self.cur = [s, e]
            if self.soft_len and self.cur[1] - self.cur[0] >= self.soft_len:
                out.append(tuple(self.cur))   # long enough: send it now, at this pause (see soft_len)
                self.cur = None
        return out


# ------------------------------------------------------------------ the live run
class Discarded(Exception):
    pass


class LiveRun:
    def __init__(self, session: Path, watch_pid: int | None = None, backend=None, make_vad=None, name: str | None = None,
                 workers: int | None = None):
        self.session, self.live = session, session / "live"
        self.pcm, self.stop_file = self.live / "audio.pcm", self.live / "stop"
        self.watch_pid = watch_pid
        self.backend, self.name, self._make_vad = backend, name, make_vad
        self.workers = workers
        self.asr, self.note = "running", ""
        self.fix = None                 # ScriptFix: Chinese and English only (cmd_run)
        self.last_status = 0.0
        self.decode_seconds = 0.0

    # --- status.json: {"asr","updated","note"} (+ audio_seconds, pid)
    def status(self, force: bool = False) -> None:
        if not force and time.time() - self.last_status < 1:
            return
        self.last_status = time.time()
        if not self.live.is_dir():
            return  # discarded: never recreate the folder
        st = {"asr": self.asr, "updated": round(time.time(), 2), "note": self.note,
              "audio_seconds": round(getattr(self, "offset", 0) / BPS, 1), "pid": os.getpid()}
        try:
            save_json(self.live / "status.json", st)
        except OSError:
            pass

    def reset(self) -> None:
        from transcribe import make_vad
        self.offset, self.ino = 0, None
        self.segments: list[dict] = []
        self.speech = self.blank = 0.0
        o = fast_opts()
        self.fast, self.spec = o, None
        if o:
            log(f"live_fast: {o}")
        self.chunker = LiveChunker(self._make_vad or make_vad,
                                   min(self.backend.max_chunk, float(o.get("max_chunk") or self.backend.max_chunk)),
                                   max_gap=float(o.get("max_gap") or 1.0),
                                   close_margin=float(o["close_margin"]) if "close_margin" in o else CLOSE_MARGIN,
                                   force_cut=bool(o) and o.get("force_cut", True) is not False,
                                   soft_len=float(o.get("soft_chunk", SOFT_CHUNK) or 0.0) if o else 0.0)
        self.pending: deque = deque()
        self.pv, self.pv_at = None, 0.0
        if self.live.is_dir():
            (self.live / "transcript.jsonl").write_text("", encoding="utf-8")

    def read_slice(self, s: float, e: float) -> np.ndarray:
        a, b = int(s * SR), int(e * SR)
        try:
            with open(self.pcm, "rb") as fh:
                fh.seek(a * 2)
                raw = fh.read((b - a) * 2)
        except FileNotFoundError:  # live/ went away between pull() and here: a restart or a discard
            raise Discarded()
        return np.frombuffer(raw[:len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768

    def _decode(self, s: float, e: float):
        from transcribe import decode_slice
        x = self.read_slice(s, e)

        def work(x=x, s=s, e=e):
            t0 = time.time()
            r = decode_slice(self.backend, x, s, e)
            return r, time.time() - t0
        return self.pool.submit(work)

    @staticmethod
    def _same(a, b) -> bool:
        return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6

    def submit(self, chunks) -> None:
        for s, e in chunks:
            self.speech += e - s
            spec, self.spec = getattr(self, "spec", None), None
            if spec and self._same(spec[0], (s, e)):
                fut = spec[1]           # decoded ahead while we waited to see whether the speaker went on
            else:
                fut = self._decode(s, e)
            self.pending.append(((s, e), fut))

    def speculate(self) -> None:
        """live_fast.speculate: the speaker has paused (the VAD closed a segment) but the chunk stays open for max_gap
        in case they go on — decode it now anyway. If they stay quiet the chunk closes with its text already there;
        if they go on, the result is thrown away. One guess at a time, and never while real chunks are waiting."""
        if not getattr(self, "fast", {}).get("speculate") or self.pending:
            return
        cand = self.chunker.candidate()
        if not cand or cand[1] - cand[0] < 0.3:
            return
        spec = getattr(self, "spec", None)
        if spec and (self._same(spec[0], cand) or not spec[1].done()):
            return
        self.spec = (cand, self._decode(*cand))

    def preview(self) -> None:
        """live_fast.preview = N seconds: while the speaker is still talking, decode the piece so far every N seconds
        and write live/preview.json {start, end, text, wall} — a caption to show until the real text lands. Only when
        nothing real is waiting to be decoded, one at a time."""
        every = float(getattr(self, "fast", {}).get("preview") or 0)
        pv = self.pv
        if pv and pv[1].done():
            self.pv = None
            try:
                r, _ = pv[1].result()
                text = "".join(t for _, _, t, *_ in r if t).strip()
                if self.fix:
                    text = self.fix(text)
                if text and self.live.is_dir():
                    tmp = self.live / "preview.json.tmp"
                    tmp.write_text(json.dumps({"start": round(pv[0][0], 2), "end": round(pv[0][1], 2), "text": text,
                                               "wall": round(time.time(), 2)}, ensure_ascii=False), encoding="utf-8")
                    os.replace(tmp, self.live / "preview.json")
            except Exception as e:
                log(f"preview skipped: {type(e).__name__}: {e}")
        if every <= 0 or self.pv or self.pending or not self.chunker.in_speech():
            return
        spec = getattr(self, "spec", None)
        if spec and not spec[1].done():
            return
        if time.time() - self.pv_at < every:
            return
        st = self.chunker.open_start()
        if st is None or self.chunker.pos - st < 1.2:
            return
        self.pv_at = time.time()
        span = (st, self.chunker.pos)
        self.pv = (span, self._decode(*span))

    def commit(self, wait: bool = False) -> None:
        from transcribe import is_blank
        while self.pending and (wait or self.pending[0][1].done()):
            (s, e), fut = self.pending.popleft()
            r, secs = fut.result()
            self.decode_seconds += secs
            if is_blank(r):
                self.blank += e - s
            if self.fix:
                r = [(a, b, self.fix(t), *([None] if lg and lg[0] in ("ja", "ko") else lg)) for a, b, t, *lg in r]
            lines = [{"start": round(a, 2), "end": round(b, 2), "text": t, **({"language": lg[0]} if lg else {})}
                     for a, b, t, *lg in r if t]
            self.segments += lines
            if lines and self.live.is_dir():
                with open(self.live / "transcript.jsonl", "a", encoding="utf-8") as fh:
                    for ln in lines:
                        fh.write(json.dumps(ln, ensure_ascii=False) + "\n")

    def pull(self) -> bool:
        """Read what the recorder appended. False when nothing new. Raises Discarded when live/ went away."""
        try:
            st = self.pcm.stat()
        except FileNotFoundError:
            raise Discarded()
        if self.ino is not None and (st.st_ino != self.ino or st.st_size < self.offset):
            log("audio.pcm was restarted (new take) — starting over")
            self.reset()
        self.ino = st.st_ino
        n = (st.st_size - self.offset) // 2 * 2
        if n <= 0:
            return False
        with open(self.pcm, "rb") as fh:
            fh.seek(self.offset)
            raw = fh.read(n)
        n = len(raw) // 2 * 2
        self.offset += n
        x = np.frombuffer(raw[:n], dtype=np.int16).astype(np.float32) / 32768
        for i in range(0, len(x), SR * 5):  # big backlog (slow start): keep chunks flowing
            self.submit(self.chunker.feed(x[i:i + SR * 5]))
            self.commit()
        self.speculate()
        self.preview()
        return True

    def wait_for_take(self, timeout: float) -> bool:
        t0 = time.time()
        seen = self.session.exists()
        while time.time() - t0 < timeout:
            if self.pcm.exists():
                return True
            seen = seen or self.session.exists()
            if (seen and not self.session.exists()) or not pid_alive(self.watch_pid):
                return False
            time.sleep(0.2)
        return False

    def run(self) -> int:
        if not self.wait_for_take(60):
            log("no live/audio.pcm appeared — nothing to do")
            return 0
        self.offset = 0
        self.pool = ThreadPoolExecutor(self.workers or 1)
        self.reset()
        self.status(force=True)
        dead_since = None
        while True:
            try:
                got = self.pull()
                self.commit()
                self.status()
                if self.stop_file.exists():
                    self.pull()
                    return self.finish()
                if not pid_alive(self.watch_pid):  # the recorder died without a stop: keep what was said
                    dead_since = dead_since or time.time()
                    if time.time() - dead_since > 3:
                        log("recorder exited without live/stop — finishing anyway")
                        return self.finish()
                if not got:
                    time.sleep(0.1)
            except Discarded:
                if not self.session.exists():
                    log("session removed (discarded) — exiting")
                    return 0
                log("live/ removed (discarded / restarting) — waiting for a new take")
                if not self.wait_for_take(30):
                    return 0
                self.reset()
                self.status(force=True)

    def finish(self) -> int:
        from transcribe import build_transcript, write_transcript
        duration = self.offset / BPS
        self.submit(self.chunker.finish(duration))
        self.commit(wait=True)
        recs = sorted(self.session.glob("recording.*"))
        source = recs[0].name if recs else "recording.mp4"
        results = [(s["start"], s["end"], s["text"], None, s.get("language")) for s in self.segments]

        class A:  # what build_transcript wants to know about the run's options
            model = None
        data = build_transcript(source, self.name, self.backend, A, duration, self.speech, self.blank, results,
                                self.decode_seconds)
        files = write_transcript(self.session, data)
        self.asr, self.note = "done", f"{len(data['segments'])} segments, {duration:.0f}s"
        self.status(force=True)
        log(f"transcript written: {files[0]} ({len(data['segments'])} segments)")
        self.pool.shutdown(wait=False)
        return 0


# ------------------------------------------------------------------ commands
def asr_setup():
    """(name, backend, workers) for the live model (asr.models.live / the recommendation, installed only), or
    (None, reason, 0) when it can't run here right now. Capped like every ASR run (K9): asr.limits.threads threads
    (leave CPU to the app being recorded), user-initiated priority, MLX memory cap. A backend whose package is
    missing re-runs this process under `uv run --with <package>` (transcribe.ensure_package)."""
    import argparse
    import transcribe as T

    a = argparse.Namespace(backend=None, model=None, model_id=None, language=None, prompt=None, concurrency=4,
                           threads=None)
    try:
        name = T.resolve_backend(a, "live")
        why = T.backend_ready(name, a.model_id)
        if why:
            return None, why, 0
        T.throttle("live")
        backend = T.make_backend(name, a, "live")
    except T.Fail as e:
        return None, f"{e} ({e.code})", 0
    return name, backend, 1 if name in T.LOCAL else a.concurrency


def cmd_run(a) -> int:
    session = Path(a.session).resolve()
    live = session / "live"
    os.environ["BLURT_NO_DOWNLOAD"] = "1"
    if global_config().get("record", {}).get("live") is False:
        log("record.live is false — off")
        return 0
    run = LiveRun(session, a.watch_pid)
    if not run.wait_for_take(60):
        log("no live/audio.pcm appeared — nothing to do")
        return 0
    zh_en = not foreign_languages()   # no Japanese / Korean in this transcript (see ScriptFix)
    if zh_en:
        try:
            chinese_english()         # before asr_setup builds the recognizer
        except Exception as e:
            log(f"chinese_english skipped: {type(e).__name__}: {e}")
    try:
        name, backend, workers = asr_setup()
    except (SystemExit, Exception) as e:  # die() / an import error inside a backend — the recording must not notice
        name, backend, workers = None, f"ASR setup failed ({type(e).__name__}: {e})"[:300], 0
    if name is None:
        # no usable ASR: say so and get out of the way (the audio sidecar isn't needed then)
        run.asr, run.note = "off", str(backend)
        run.status(force=True)
        log(f"ASR off: {backend}")
        while live.is_dir() and not (live / "stop").exists() and pid_alive(a.watch_pid):
            time.sleep(1)
            run.status()
        (live / "audio.pcm").unlink(missing_ok=True)
        return 0
    run.backend, run.name, run.workers = backend, name, workers
    log(f"live: {name}")
    vf = fast_opts().get("vocab_file")
    if zh_en:
        try:
            words = json.loads(Path(os.path.expanduser(str(vf))).read_text(encoding="utf-8")) if vf else []
        except (OSError, ValueError):
            words = []
        run.fix = ScriptFix([w for w in words if isinstance(w, str)])
    if vf and hasattr(backend, "set_vocab"):   # 会议模式：当前论文的术语当热词（Bloom 的 zuhui/lexicon.py 生成）
        try:
            words = json.loads(Path(os.path.expanduser(str(vf))).read_text(encoding="utf-8"))
            words = [w for w in words if isinstance(w, str) and w.strip()]
            if words:
                t0 = time.time()
                backend.set_vocab([{"term": w} for w in words])
                log(f"hotwords: {len(words)} in {time.time() - t0:.1f}s")
        except Exception as e:
            log(f"hotwords skipped: {type(e).__name__}: {e}")
    if fast_opts():   # 会议模式：先空跑一次解码，第一句话就不用等模型第一次推理的初始化
        try:
            from transcribe import decode_slice
            t0 = time.time()
            decode_slice(backend, np.zeros(SR // 2, np.float32), 0.0, 0.5)
            log(f"warm-up decode {time.time() - t0:.1f}s")
        except Exception as e:
            log(f"warm-up decode skipped: {type(e).__name__}: {e}")
    try:
        return run.run()
    except Exception as e:
        run.asr, run.note = "error", f"{type(e).__name__}: {e}"[:300]
        run.status(force=True)
        raise


def cmd_simulate(a) -> int:
    src, session = Path(a.audio), Path(a.session).resolve()
    if not src.exists():
        die(f"{src} not found")
    live = session / "live"
    live.mkdir(parents=True, exist_ok=True)
    for f in ("stop", "status.json", "transcript.jsonl"):
        (live / f).unlink(missing_ok=True)
    (session / "transcript.json").unlink(missing_ok=True)
    raw = subprocess.run([ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(SR),
                          "-f", "s16le", "-"], capture_output=True).stdout
    if not raw:
        die(f"no audio decoded from {src}")
    proc = None
    with open(live / "audio.pcm", "wb") as fh:
        if a.run:
            proc = subprocess.Popen([sys.executable, __file__, "run", str(session), "--watch-pid", str(os.getpid())])
        step = BPS // 10  # 100 ms, like the recorder
        t0 = time.time()
        for i in range(0, len(raw), step):
            fh.write(raw[i:i + step])
            fh.flush()
            if a.speed > 0:
                time.sleep(max(0.0, t0 + (i + step) / BPS / a.speed - time.time()))
    feed_s = time.time() - t0
    (live / "stop").touch()
    t_stop = time.time()
    out = {"audio_seconds": round(len(raw) / BPS, 1), "feed_seconds": round(feed_s, 1), "pcm": str(live / "audio.pcm")}
    if proc:
        while proc.poll() is None and (load_json(live / "status.json", {}) or {}).get("asr") != "done":
            time.sleep(0.05)
        out["stop_to_transcript_seconds"] = round(time.time() - t_stop, 2)
        out["transcript"] = str(session / "transcript.json") if (session / "transcript.json").exists() else None
        proc.wait(timeout=60)
        out["exit"] = proc.returncode
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_status(a) -> int:
    print(json.dumps(load_json(Path(a.session) / "live" / "status.json", {}) or {"asr": None}, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="tail <session>/live/audio.pcm until live/stop")
    r.add_argument("session")
    r.add_argument("--watch-pid", type=int, help="the recorder's pid: if it dies without live/stop, finish anyway")
    s = sub.add_parser("simulate", help="write an audio file into <session>/live/audio.pcm in real time, then stop")
    s.add_argument("audio")
    s.add_argument("session")
    s.add_argument("--speed", type=float, default=1.0, help="N× real time; 0 = as fast as possible")
    s.add_argument("--run", action="store_true", help="also run `live.py run` and time stop → transcript.json")
    st = sub.add_parser("status")
    st.add_argument("session")
    a = p.parse_args()
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit({"run": cmd_run, "simulate": cmd_simulate, "status": cmd_status}[a.cmd](a))


if __name__ == "__main__":
    main()
