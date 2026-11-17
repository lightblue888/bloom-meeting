"""The words list the recogniser is biased with (dictation_vocab.py, the live transcript's hotwords): the store,
ranking, the ASR prompt formats and the biased note path (a fake backend stands in for the model). No model, no
network.

  uv run --with pytest pytest tests/test_dictation_vocab.py -q
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "blurt" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import dictation_vocab as V  # noqa: E402

TERMS = V.normalize([
    {"term": "Claude Code", "sounds_like": ["cloud code", "clock code"], "source": "project"},
    {"term": "Claude", "source": "manual"},
    {"term": "SenseVoice"}, {"term": "GitHub"}, {"term": "Swift"},
    {"term": "口喷鸡", "sounds_like": ["口喷机", "鸡"], "source": "project"},
    {"term": "blurt", "sounds_like": ["blur", "blurred"]},
    {"term": "worktree", "sounds_like": ["work tree"]},
    {"term": "tmux", "sounds_like": ["t mux"]},
    {"term": "BA-12"},
    {"term": "Rejected", "state": "rejected"},
    {"term": "Pending", "state": "suggested", "source": "learned"},
])


class Store(unittest.TestCase):
    def test_only_active_terms_deduplicated(self):
        names = [t["term"] for t in TERMS]
        self.assertNotIn("Rejected", names)
        self.assertNotIn("Pending", names)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(V.normalize(["a", "a", {"term": "b"}, 3, None]), [
            {"term": "a", "sounds_like": [], "hits": 0, "source": "manual"},
            {"term": "b", "sounds_like": [], "hits": 0, "source": "manual"}])

    def test_load_reads_the_apps_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "vocabulary.json"
            p.write_text(json.dumps({"schema": 1, "terms": [{"term": "Codex", "source": "project", "hits": 2}]}))
            self.assertEqual([t["term"] for t in V.load(p)], ["Codex"])
            p.write_text("not json")
            self.assertEqual(V.load(p), [])
            self.assertEqual(V.load(Path(d) / "missing.json"), [])


def heard(texts, vocab):
    """What transcribe.NoteEngine.note(vocab=…) hands on for each text a (fake) speech model writes: with the words
    asked for, the model is biased, and nothing after it rewrites the text."""
    import test_asr_select as A
    T = A.T
    queue = list(texts)

    class Model(A.FakeBackend):
        def set_vocab(self, terms):
            return bool(terms)

        def transcribe(self, x):
            return [(0.0, len(x) / A.SR, queue.pop(0), "zh")]

    old, oldvad = T.BACKENDS, T.vad_segments
    T.BACKENDS = {**T.BACKENDS, "sensevoice": Model}
    T.vad_segments = lambda x, max_speech=25: [(0.0, len(x) / A.SR)]
    real = A.fake_installed("sensevoice-small")
    try:
        with tempfile.TemporaryDirectory() as d, A.config({"asr": {"auto": False, "backend": "sensevoice"}}):
            wav = A.write_wav(Path(d) / "n.wav", 1.0)
            eng = T.NoteEngine()
            return [eng.note(wav, vocab=vocab)["text"] for _ in texts]
    finally:
        T.BACKENDS, T.vad_segments, T.installed = old, oldvad, real


class Ranking(unittest.TestCase):
    def test_heard_terms_first(self):
        order = [t["term"] for t in V.ranked(TERMS, "用 kodex 和 cloud code 跑一下")]
        self.assertEqual(order[0], "Claude Code")
        order = [t["term"] for t in V.ranked(TERMS, "nothing here", context="push it to GitHub")]
        self.assertEqual(order[0], "GitHub")
        self.assertEqual(len(V.ranked(TERMS * 10, "", limit=5)), 5)

    def test_similar(self):
        self.assertTrue(V.similar("cloud", "Claude"))
        self.assertTrue(V.similar("Sense Voice", "SenseVoice"))
        self.assertTrue(V.similar("kodex", "Codex"))
        self.assertFalse(V.similar("table", "Claude"))

    def test_prompt_formats(self):
        t = TERMS[:3]
        self.assertEqual(V.whisper_prompt(t), "Claude Code, Claude, SenseVoice")
        self.assertEqual(V.qwen_context(t), "Notes about Claude Code, Claude and SenseVoice.")
        self.assertEqual(V.qwen_context(t[:1]), "Notes about Claude Code.")
        self.assertEqual(V.qwen_context([]), "")
        # sherpa-onnx splits hotwords on ASCII commas: never one inside
        self.assertEqual(V.hotwords(V.normalize(["a, b", "c"])), "a  b; c")


class BiasedNote(unittest.TestCase):
    """transcribe.NoteEngine.note(vocab=…): set_vocab before decoding, cleared after; the words are hints, so what the
    model wrote is what comes back (no sounds-like is substituted)."""

    def test_note_with_vocab(self):
        import test_asr_select as A
        T = A.T

        class Biasable(A.FakeBackend):
            seen = []

            def set_vocab(self, terms):
                Biasable.seen.append([t["term"] for t in terms])
                return bool(terms)

            def transcribe(self, x):
                return [(0.0, len(x) / A.SR, "我用cloud code开了work tree", "zh")]

        old = T.BACKENDS
        T.BACKENDS = {**T.BACKENDS, "sensevoice": Biasable}
        oldvad = T.vad_segments
        T.vad_segments = lambda x, max_speech=25: [(0.0, len(x) / A.SR)]
        real = A.fake_installed("sensevoice-small")
        try:
            with tempfile.TemporaryDirectory() as d, A.config({"asr": {"auto": False, "backend": "sensevoice"}}):
                wav = A.write_wav(Path(d) / "n.wav", 1.5)
                r = T.NoteEngine().note(wav, vocab=[{"term": "Claude Code", "sounds_like": ["cloud code"]}, "worktree"])
                plain = T.NoteEngine().note(wav)
                T.NoteEngine().note(wav, vocab=["worktree"], app="飞书")
                T.NoteEngine().note(wav, vocab=["飞书"], app="飞书")
        finally:
            T.BACKENDS, T.vad_segments, T.installed = old, oldvad, real
        self.assertEqual(r["text"], "我用cloud code开了work tree")
        self.assertEqual(r["asr_text"], "我用cloud code开了work tree")
        self.assertNotIn("terms", r)
        self.assertTrue(r["biased"])
        self.assertEqual(Biasable.seen[0][:2], ["Claude Code", "worktree"])
        self.assertEqual(Biasable.seen[1], [])                # cleared: the next voice note isn't biased
        self.assertNotIn("asr_text", plain)                   # no vocab asked: the plain voice-note answer
        asked = [s for s in Biasable.seen if s]
        self.assertEqual(asked[1:], [["飞书", "worktree"], ["飞书"]])  # the app the text goes into: named first, once

    def test_streamed_piece_gets_the_text_so_far(self):
        """A streamed dictation's piece: `prior` (the text so far) reaches the backend for this request only."""
        import test_asr_select as A
        T = A.T

        class Prior(A.FakeBackend):
            seen = []

            def set_vocab(self, terms):
                return bool(terms)

            def set_prior(self, text):
                Prior.seen.append(text)

            def transcribe(self, x):
                return [(0.0, len(x) / A.SR, "然后跑一遍评测", "zh")]

        old = T.BACKENDS
        T.BACKENDS = {**T.BACKENDS, "sensevoice": Prior}
        oldvad = T.vad_segments
        T.vad_segments = lambda x, max_speech=25: [(0.0, len(x) / A.SR)]
        real = A.fake_installed("sensevoice-small")
        try:
            with tempfile.TemporaryDirectory() as d, A.config({"asr": {"auto": False, "backend": "sensevoice"}}):
                wav = A.write_wav(Path(d) / "p.wav", 1.5)
                r = T.NoteEngine().note(wav, vocab=["Gemini"], prior="先检查一下 Gemini API 的配额" * 20)
                T.NoteEngine().note(wav, vocab=["Gemini"])
        finally:
            T.BACKENDS, T.vad_segments, T.installed = old, oldvad, real
        self.assertEqual(r["text"], "然后跑一遍评测")
        self.assertEqual(len(Prior.seen), 2)                  # set, then cleared
        self.assertEqual(len(Prior.seen[0]), 80)              # the last 80 characters (transcribe.PRIOR_CHARS)
        self.assertTrue(Prior.seen[0].endswith("的配额"))
        self.assertEqual(Prior.seen[1], "")
        self.assertIn("load_ms", r)

    def test_a_piece_that_comes_back_empty_with_the_prior_is_tried_once_without(self):
        """Qwen3-ASR sometimes says nothing for a short last piece after a whole sentence of context: the VAD heard
        speech, so the piece is decoded once more without the prior (and not when there was no prior)."""
        import test_asr_select as A
        T = A.T

        class Shy(A.FakeBackend):
            prior = ""
            calls = 0

            def set_vocab(self, terms):
                return bool(terms)

            def set_prior(self, text):
                Shy.prior = text

            def transcribe(self, x):
                Shy.calls += 1
                return [(0.0, len(x) / A.SR, "" if Shy.prior else "and won't be back in time", "en")]

        old = T.BACKENDS
        T.BACKENDS = {**T.BACKENDS, "sensevoice": Shy}
        oldvad = T.vad_segments
        T.vad_segments = lambda x, max_speech=25: [(0.0, len(x) / A.SR)]
        real = A.fake_installed("sensevoice-small")
        try:
            with tempfile.TemporaryDirectory() as d, A.config({"asr": {"auto": False, "backend": "sensevoice"}}):
                wav = A.write_wav(Path(d) / "p.wav", 1.5)
                r = T.NoteEngine().note(wav, vocab=["Gemini"], prior="Most of the team is traveling on Wednesday.")
                n = Shy.calls
                plain = T.NoteEngine().note(wav, vocab=["Gemini"])
        finally:
            T.BACKENDS, T.vad_segments, T.installed = old, oldvad, real
        self.assertEqual(r["text"], "and won't be back in time")
        self.assertTrue(r["retried"])
        self.assertEqual(n, 2)
        self.assertNotIn("retried", plain)

    def test_dictation_models_rows(self):
        import test_asr_select as A
        T = A.T
        real = A.fake_installed("sensevoice-small")
        try:
            with A.config({"asr": {"auto": False, "backend": "sensevoice", "languages": ["de", "en"]}}):
                d = T.dictation_models(A.hw())
                # by Mac (dictation_tier, 2026-10-31): the 1.7B on 16 GB+ Macs fast enough for it
                self.assertEqual(d["recommended"], "qwen3-asr-1.7b-mlx")
                self.assertEqual(d["selected"], "sensevoice-small")             # … until it is installed
                self.assertEqual(d["models"][0]["id"], "qwen3-asr-0.6b-mlx")
                self.assertEqual({m["id"]: m["vocab"] for m in d["models"]}["sensevoice-small"], "none")
                self.assertEqual(T.dictation_models(A.hw(ram=16.0))["recommended"], "qwen3-asr-1.7b-mlx")   # standard
                # a base M1 / M2, not measured: the 0.6B
                self.assertEqual(T.dictation_models({**A.hw(ram=16.0), "chip": "Apple M1"})["recommended"],
                                 "qwen3-asr-0.6b-mlx")
            T.installed = lambda m: m["id"] in ("sensevoice-small", "qwen3-asr-0.6b-mlx")
            with A.config({"asr": {"auto": False, "backend": "sensevoice"}}):
                # the 0.6B here, the 1.7B not yet: the 0.6B runs until it is
                self.assertEqual(T.dictation_models(A.hw())["selected"], "qwen3-asr-0.6b-mlx")
            T.installed = lambda m: m["id"] in ("sensevoice-small", "qwen3-asr-0.6b-mlx", "qwen3-asr-1.7b-mlx")
            with A.config({"asr": {"auto": False, "backend": "sensevoice"}}):
                # both here: the recommended one for the Mac
                self.assertEqual(T.dictation_models(A.hw())["selected"], "qwen3-asr-1.7b-mlx")
                self.assertEqual(T.dictation_models(A.hw(ram=16.0))["selected"], "qwen3-asr-1.7b-mlx")
                self.assertEqual(T.dictation_models({**A.hw(ram=16.0), "chip": "Apple M2"})["selected"],
                                 "qwen3-asr-0.6b-mlx")
            with A.config({"asr": {"auto": False, "backend": "sensevoice"},
                           "record": {"dictation": {"asr_model": "sensevoice-small"}}}):
                d = T.dictation_models(A.hw())
                self.assertEqual((d["configured"], d["selected"]), ("sensevoice-small", "sensevoice-small"))
            with A.config({"asr": {"auto": False, "backend": "sensevoice", "languages": ["de", "en"]}}):
                # 8 GB: the 0.6B, with no local polish beside it (dictation-eval-2026-10-31 §6.2)
                self.assertEqual(T.dictation_models(A.hw(ram=8.0))["recommended"], "qwen3-asr-0.6b-mlx")
            with A.config({"asr": {"auto": False, "backend": "sensevoice", "languages": ["zh", "en"]}}):
                # … Fun-ASR-Nano for someone who speaks only its languages (docs/design/dictation.md §Defaults by Mac)
                self.assertEqual(T.dictation_models(A.hw(ram=8.0))["recommended"], "fun-asr-nano-mlx")
        finally:
            T.installed = real

    def test_an_installed_qwen_is_used_when_it_fits_next_to_polish(self):
        # a 16 GB Mac that has only the 1.7B (moved from a bigger Mac, or the recommended one since 2026-10-31): the memory check
        # counts the pair a dictation holds — recogniser + polish model — not the recogniser alone
        import test_asr_select as A
        T = A.T
        real = A.fake_installed("sensevoice-small", "qwen3-asr-1.7b-mlx")
        try:
            base = {"asr": {"auto": False, "backend": "sensevoice"}}
            with A.config({**base, "record": {"dictation": {"polish_model": "gemma4-e4b"}}}):
                # a big polish model (Gemma 4 E4B, 5.4 GB) next to either Qwen is over a 16 GB Mac's share: SenseVoice
                self.assertGreater(T.polish_ram_mb(A.hw(ram=16.0)), 5000)
                d = T.dictation_models(A.hw(ram=16.0))
                self.assertEqual((d["recommended"], d["selected"]), ("sensevoice-small", "sensevoice-small"))
                self.assertFalse({m["id"]: m["fits"] for m in d["models"]}["qwen3-asr-1.7b-mlx"])
                # 24 GB (still standard): the pair fits — the 1.7B, which takes the Words list, runs until the 0.6B
                self.assertEqual(T.dictation_models(A.hw(ram=24.0))["selected"], "qwen3-asr-1.7b-mlx")
            with A.config(base):
                # the default polish (Gemma 4 E2B, 2.3 GB since its per-layer embeddings stay in their file) + 1.7B fits
                self.assertEqual(T.polish_ram_mb(A.hw(ram=16.0)), 2300)
                self.assertEqual(T.dictation_models(A.hw(ram=16.0))["selected"], "qwen3-asr-1.7b-mlx")
            with A.config({**base, "record": {"dictation": {"polish": "off"}}}):
                # no polish model held next to it: the 1.7B alone fits 16 GB
                self.assertEqual(T.polish_ram_mb(A.hw(ram=16.0)), 0)
                self.assertEqual(T.dictation_models(A.hw(ram=16.0))["selected"], "qwen3-asr-1.7b-mlx")
            with A.config({**base, "record": {"dictation": {"polish_model": "qwen3.5-0.8b"}}}):
                # a small polish model chosen by hand: 3.1 + 1.6 GB fits
                self.assertEqual(T.dictation_models(A.hw(ram=16.0))["selected"], "qwen3-asr-1.7b-mlx")
            with A.config(base):
                # never off Apple Silicon (MLX)
                self.assertEqual(T.dictation_models(A.hw(os_="linux", arch="x86_64", ram=16.0))["selected"], "sensevoice-small")
        finally:
            T.installed = real

    def test_progress_lines_name_what_is_fetched_on_the_way(self):
        # the VAD (1.7 MB) before a model: its line must not read as the model's own progress (the bar leapt to 40 %)
        import test_asr_select as A
        T = A.T
        self.assertEqual(T.progress_line(812 * 2**20, 2468 * 2**20), "  812.0/2468.0 MB")
        self.assertEqual(T.progress_line(1.5 * 2**20, 1.7 * 2**20, "vad"), "  vad 1.5/1.7 MB")
        src = (A.SCRIPTS / "transcribe.py").read_text()
        self.assertIn('download("silero_vad.onnx", aux="vad")', src)


# ------------------------------------------------------------------ names inside Chinese speech (v0.8 names work)
class CodeSwitch(unittest.TestCase):
    """Qwen3-ASR decodes a mixed sentence in the language of its tag: tagged English, the Chinese around the names
    comes back translated. The tag policy and the checks on an English answer."""

    def test_the_tag_goes_to_english_only_when_the_model_is_all_but_sure(self):
        self.assertEqual(V.choose_tag(0.9999, 0.0001, "Chinese"), "English")     # English speech: ≥ 0.9985 measured
        self.assertEqual(V.choose_tag(0.98, 0.02, "Chinese"), "Chinese")         # mixed pieces it translated: 0.95–1.0
        self.assertEqual(V.choose_tag(0.3, 0.6, "Chinese"), "Chinese")
        self.assertEqual(V.choose_tag(0.0, 0.0, "Chinese"), "Chinese")

    def test_an_english_answer_that_is_translated_chinese(self):
        names = ["OpenAI", "Anthropic", "Dario", "Sam Altman", "Musk", "Grok"]
        # Chinese characters left in an English answer: the model switched mid-sentence (the WeChat case, paraphrased)
        self.assertTrue(V.translated_glue("念几个名字：open AI and Musk's Grok, and呃，CEO是Dario的Asopic", names))
        # nothing but names and function words is no longer second-guessed (that took a list of English function
        # words): an all-English answer is the speech model's call
        self.assertFalse(V.translated_glue("Anthropic, the CEO, Dario.", names))
        # real English sentences
        for t in ["DeepSeek and Kimi both shipped new models.", "I asked Claude and Gemini the same question.",
                  "Please send the Cursor invoice to finance.", "Musk said Grok will be open source next month."]:
            self.assertFalse(V.translated_glue(t, names), t)
        self.assertFalse(V.translated_glue("", names))

    def test_the_word_list_written_out_is_a_context_dump(self):
        names = ["Claude Code", "Codex", "blurt", "口喷鸡", "Gemini", "Claude"]
        self.assertTrue(V.context_dump("目前，Claude Code、Codex、blurt、口喷鸡、Gemini、Claude", names))
        self.assertFalse(V.context_dump("我用 Claude Code 和 Codex 写代码", names))
        self.assertFalse(V.context_dump("Gemini、Claude、Codex、blurt", names))       # not in the list's order
        self.assertFalse(V.context_dump("", names))

    def test_cjk_share(self):
        self.assertEqual(V.cjk_share("hello world"), 0.0)
        self.assertEqual(V.cjk_share("你好"), 1.0)
        self.assertAlmostEqual(V.cjk_share("我用 Claude"), 2 / 3)

    def test_the_language_a_user_mixes_english_into(self):
        import transcribe as T
        self.assertEqual(T.switch_language(["zh", "en"]), "Chinese")
        self.assertEqual(T.switch_language(["en", "ja"]), "Japanese")
        self.assertIsNone(T.switch_language(["en", "de"]))


class TaggedDecode(unittest.TestCase):
    """Qwen3Mlx.decode without the model: the preferred language goes to the tag-aware decoder, a failing decoder
    falls back to the library for good, and a word list written out is decoded again without it."""

    def backend(self, prefer="Chinese"):
        import transcribe as T

        class Lib:
            calls = []

            @staticmethod
            def transcribe(x, model=None, context="", language=None):
                Lib.calls.append((context, language))
                return type("R", (), {"text": " 普通解码 ", "language": "Chinese"})

        b = object.__new__(T.Qwen3Mlx)
        b.m, b.repo, b.args = Lib, "fake", type("A", (), {"language": None})()
        b.context = b.base_context = ""
        b.names, b.prefer, b.tagged, b.last, b.prior = [], prefer, True, {}, ""
        return b, Lib

    def test_prefers_the_users_language(self):
        b, lib = self.backend()
        seen = []
        b._decode_tagged = lambda x, ctx, prefer: (seen.append(prefer), ("Anthropic 的 CEO 是 Dario", "zh"))[1]
        out = b.decode([0.0] * 1600)
        self.assertEqual(out[0][2:], ("Anthropic 的 CEO 是 Dario", "zh"))
        self.assertEqual(seen, ["Chinese"])
        self.assertEqual(lib.calls, [])

    def test_english_only_users_keep_the_models_own_tag(self):
        b, lib = self.backend(prefer=None)
        self.assertEqual(b.decode([0.0] * 1600)[0][2], "普通解码")
        self.assertEqual(lib.calls, [("", None)])
        # …unless the dictation so far is Chinese
        b.prior = "我们先把这个需求过一遍"
        b._decode_tagged = lambda x, ctx, prefer: ("好的", "zh")
        self.assertEqual(b.decode([0.0] * 1600)[0][2], "好的")

    def test_a_failing_decoder_falls_back_to_the_library_once_and_for_all(self):
        b, lib = self.backend()

        def boom(*_a):
            raise AttributeError("no step_many in this version")
        b._decode_tagged = boom
        self.assertEqual(b.decode([0.0] * 1600)[0][2], "普通解码")
        self.assertFalse(b.tagged)
        b.decode([0.0] * 1600)
        self.assertEqual(len(lib.calls), 2)

    def test_a_context_dump_is_decoded_again_with_only_the_prior(self):
        b, lib = self.backend()
        b.names = ["Claude Code", "Codex", "blurt", "口喷鸡", "Gemini"]
        b.context = V.qwen_context(V.normalize(b.names))
        b.prior = "上一句"
        ctxs = []

        def dec(x, ctx, prefer):
            ctxs.append(ctx)
            return ("Claude Code、Codex、blurt、口喷鸡、Gemini" if len(ctxs) == 1 else "马斯克发布了新版本", "zh")
        b._decode_tagged = dec
        out = b.decode([0.0] * 1600)
        self.assertEqual(out[0][2], "马斯克发布了新版本")
        self.assertEqual(ctxs[1], "上一句")
        self.assertTrue(b.last.get("context_dump"))


class LearnFromCorrection(unittest.TestCase):
    """dictation_vocab.learn(raw, corrected): the Dictation Lab's (and any tool's) way to teach a misheard name — the
    same rules as the app's DictationLearn + DictationVocabulary.learn, written to the app's vocabulary.json."""

    def test_fixes(self):
        self.assertEqual(V.fixes("后来他去了Asopic做研究", "后来他去了Anthropic做研究"),
                         [{"heard": "Asopic", "fixed": "Anthropic", "kind": "spelling"}])
        self.assertEqual(V.fixes("我喜欢口喷机", "我喜欢口喷鸡"), [{"heard": "口喷机", "fixed": "口喷鸡", "kind": "sound"}])
        self.assertEqual(V.fixes("open the pool request", "open the pull request"),
                         [{"heard": "pool request", "fixed": "pull request", "kind": "spelling"}])
        self.assertEqual(V.fixes("there car", "their car"), [])                # grammar, not vocabulary
        self.assertEqual(V.fixes("今天下午开会", "明天上午我们一起去公司吃饭然后开会"), [])   # a rewrite
        self.assertEqual(V.fixes("same text", "same text"), [])

    def test_two_fixes_side_by_side_are_two_never_one_with_a_comma(self):
        # dogfood d54: 绘画 BA 三 → 会话，BA-3 was learned as one active word "会话，BA-3" (the app's rule, ported)
        self.assertEqual(V.fixes("先看绘画 BA 三的进度", "先看会话，BA-3的进度"),
                         [{"heard": "绘画", "fixed": "会话", "kind": "sound"}])
        self.assertEqual([f["fixed"] for f in V.fixes("ask cloud, code it", "ask Claude; Codex it")], ["Claude", "Codex"])
        self.assertEqual([f["fixed"] for f in V.fixes("open cloud code in tmux", "open Claude Code in tmux")], ["Claude Code"])
        with tempfile.TemporaryDirectory() as d:
            doc: dict = {}
            self.assertIsNone(V.learn_fix(doc, "a b", "Alpha, Beta"))
            self.assertTrue(V.has_sentence_punctuation("会话，BA-3"))
            self.assertFalse(V.has_sentence_punctuation("Node.js"))
            self.assertFalse((Path(d) / "vocabulary.json").exists())

    def test_learn_writes_the_apps_file_and_the_next_dictation_uses_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "dictation" / "vocabulary.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"schema": 1, "terms": [{"term": "Claude", "state": "active", "source": "default"}]}))
            r = V.learn("后来他去了Asopic做研究", "后来他去了Anthropic做研究", path=path)
            self.assertTrue(r["written"])
            self.assertEqual(r["fixes"][0]["result"], "learned")
            terms = V.load(path)
            a = next(t for t in terms if t["term"] == "Anthropic")
            self.assertEqual(a["sounds_like"], ["Asopic"])
            # a word, not a rule: the next dictation that sounds like it lists the word to the models first, and
            # nothing rewrites the text by itself
            self.assertEqual(V.ranked(terms, "CEO 是 Dario 的 Asopic 吧")[0]["term"], "Anthropic")
            self.assertEqual(heard(["CEO 是 Dario 的 Asopic 吧"], terms), ["CEO 是 Dario 的 Asopic 吧"])
            # the same fix again: known, hits counted, nothing duplicated
            self.assertEqual(V.learn("Asopic 的模型", "Anthropic 的模型", path=path)["fixes"][0]["result"], "known")
            doc = json.loads(path.read_text())
            self.assertEqual([t["term"] for t in doc["terms"]].count("Anthropic"), 1)
            self.assertEqual(doc["terms"][0]["term"], "Claude")                # the rest untouched

    def test_suggest_rejected_and_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "vocabulary.json"
            r = V.learn("用Tipless听写", "用Typeless听写", explicit=False, path=path)
            self.assertEqual(r["fixes"][0]["result"], "suggested")
            self.assertEqual(V.load(path), [])                                  # a suggestion isn't used yet
            self.assertEqual(V.learn("用Tipless听写", "用Typeless听写", explicit=False, path=path)["fixes"][0]["result"],
                             "learned")                                          # the second time it is
            doc = json.loads(path.read_text())
            doc["terms"][0]["state"] = "rejected"
            path.write_text(json.dumps(doc))
            self.assertIsNone(V.learn("用Typles听写", "用Typeless听写", path=path)["fixes"][0]["result"])
            before = path.read_text()
            self.assertFalse(V.learn("我用Kimmi", "我用Kimi", path=path, dry_run=True)["written"])
            self.assertEqual(path.read_text(), before)

    def test_cli(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "vocabulary.json"
            out = subprocess.run([sys.executable, str(SCRIPTS / "dictation_vocab.py"), "learn", "--raw", "我用Typless听写",
                                  "--corrected", "我用Typeless听写", "--file", str(path)],
                                 capture_output=True, text=True, check=True).stdout
            r = json.loads(out)
            self.assertTrue(r["ok"] and r["written"])
            self.assertEqual(V.load(path)[0]["term"], "Typeless")
            self.assertEqual(json.loads(path.read_text())["schema"], V.SCHEMA)


if __name__ == "__main__":
    unittest.main()


class SoundsAlike(unittest.TestCase):
    """A Chinese term misheard as characters of the same sound is found (and listed for the model), never replaced."""

    def test_a_misheard_term_is_found(self):
        self.assertEqual(V.sounds_in("空中云汇", "我们用空中云会收款"), "空中云会")
        self.assertEqual(V.sounds_in("口喷鸡", "打开口喷机"), "口喷机")

    def test_the_term_written_right_needs_nothing(self):
        self.assertIsNone(V.sounds_in("空中云汇", "我们用空中云汇收款"))

    def test_two_characters_both_different_are_too_loose(self):
        self.assertIsNone(V.sounds_in("会话", "飞书绘画里面的消息"))

    def test_relevant_and_ranked_bring_the_term_first(self):
        terms = [{"term": t, "sounds_like": [], "source": "manual", "hits": 0} for t in ("Notion", "空中云汇")]
        raw = "我们用空中云会收款"
        self.assertEqual([t["term"] for t in V.relevant(terms, raw)], ["空中云汇"])
        self.assertEqual(V.ranked(terms, raw)[0]["term"], "空中云汇")
