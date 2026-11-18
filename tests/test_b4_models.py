# -*- coding: utf-8 -*-
"""第 4 批：OpenAI 兼容接口和本机模型（Ollama、LM Studio）、API 方式查文献、用量和估算费用。

全离线、不花钱：本进程里起一个假接口（OpenAI 的 /chat/completions、/models 和 Anthropic 的 /v1/messages 都认，
照脚本回话、记下每次请求）；查文献的 OpenAlex / arXiv / Crossref 用 mock（换掉 littools._get），不连这些网站。
ZUHUI_HOME 指到临时目录（和别的测试模块一起跑时用 zuhui 已经在用的那个），不碰真实的 ~/.zuhui。

    python -X utf8 -m unittest tests.test_b4_models -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "zuhui.config" not in sys.modules and not os.environ.get("ZUHUI_HOME"):
    os.environ["ZUHUI_HOME"] = tempfile.mkdtemp(prefix="zuhui-test-b4-")
from zuhui import config as zconfig  # noqa: E402

HOME = Path(zconfig.STATE)
if os.path.normcase(str(HOME.resolve())) == os.path.normcase(str((Path.home() / ".zuhui").resolve())):
    raise unittest.SkipTest("zuhui 已经按真实的 ~/.zuhui 导入了：不在真实状态目录里跑")

import httpx  # noqa: E402

from zuhui import app_facts, littools, llm_api, netenv, provider, usage  # noqa: E402
from zuhui import hands as handsmod  # noqa: E402
from zuhui.board import Board  # noqa: E402


# ---------- 假接口 ----------
class Fake:
    seen: list = []
    oa_need_mct = False          # True：带 max_tokens 的 OpenAI 请求回 400（OpenAI 新型号要 max_completion_tokens）
    no_tools = False             # True：带 tools 的请求回 400（本机小模型不支持工具）
    lock = threading.Lock()


def _has(msgs, pred) -> bool:
    return any(pred(m) for m in msgs)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status, d):
        b = json.dumps(d, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _sse(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for e in events:
            self.wfile.write(("data: " + (e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)) + "\n\n").encode("utf-8"))

    def do_GET(self):
        with Fake.lock:
            Fake.seen.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization") or ""})
        if self.path.endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "qwen3:8b"}, {"id": "llama3.1:8b"}, {"id": "qwen3:8b"}]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with Fake.lock:
            Fake.seen.append({"method": "POST", "path": self.path, "body": body, "auth": self.headers.get("Authorization") or "",
                              "xkey": self.headers.get("x-api-key") or ""})
        msgs = body.get("messages") or []
        if self.path.endswith("/chat/completions"):
            if Fake.oa_need_mct and "max_tokens" in body:
                return self._json(400, {"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}})
            if Fake.no_tools and body.get("tools"):
                return self._json(400, {"error": {"message": "registry.ollama.ai/library/tiny does not support tools"}})
            if body.get("tools"):
                if not _has(msgs, lambda m: m.get("role") == "tool"):
                    return self._json(200, {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "search_papers",
                                                                      "arguments": json.dumps({"query": "graph benchmark", "limit": "3"})}}]},
                        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
                tool = [m for m in msgs if m.get("role") == "tool"][-1]["content"]
                return self._json(200, {"choices": [{"message": {"role": "assistant", "content": "查到了：" + tool[:200]},
                                                     "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 200, "completion_tokens": 20,
                                                  "prompt_tokens_details": {"cached_tokens": 50}}})
            if body.get("stream"):
                ev = [{"choices": [{"delta": {"content": p}}]} for p in ("<think>", "想一想", "</think>", "你", "好")]
                ev += [{"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 5}}, "[DONE]"]
                return self._sse(ev)
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": "<think>嗯</think>好"},
                                                 "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "prompt_cache_hit_tokens": 4}})
        if self.path.endswith("/v1/messages"):
            if body.get("tools"):
                done = _has(msgs, lambda m: isinstance(m.get("content"), list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]))
                if not done:
                    return self._json(200, {"content": [{"type": "text", "text": "我查一下"},
                                                        {"type": "tool_use", "id": "tu1", "name": "check_citation",
                                                         "input": {"doi": "10.1145/1234.5678", "year": 2020}}],
                                            "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 10}})
                res = [b for m in msgs if isinstance(m.get("content"), list) for b in m["content"]
                       if isinstance(b, dict) and b.get("type") == "tool_result"][-1]["content"]
                return self._json(200, {"content": [{"type": "text", "text": "核对过了：" + str(res)[:600]}], "stop_reason": "end_turn",
                                        "usage": {"input_tokens": 150, "output_tokens": 8, "cache_read_input_tokens": 40}})
            if body.get("stream"):
                ev = [{"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 1,
                                                                       "cache_read_input_tokens": 5}}}]
                ev += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}} for p in ("你", "好")]
                ev += [{"type": "message_delta", "usage": {"output_tokens": 2}}, {"type": "message_stop"}]
                return self._sse(ev)
            return self._json(200, {"content": [{"type": "text", "text": "好"}], "stop_reason": "end_turn",
                                    "usage": {"input_tokens": 10, "output_tokens": 1}})
        self._json(404, {"error": {"message": "no"}})


# ---------- 查文献的 mock：不连 OpenAlex / arXiv / Crossref ----------
OPENALEX = {"results": [{"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1145/1234.5678",
                         "display_name": "A Graph Benchmark for Meetings", "publication_year": 2020,
                         "authorships": [{"author": {"display_name": "Ada Lovelace"}}, {"author": {"display_name": "Alan Turing"}}],
                         "primary_location": {"source": {"display_name": "CHI"}, "landing_page_url": "https://x"},
                         "cited_by_count": 42, "abstract_inverted_index": {"We": [0], "build": [1], "graphs": [2]}}]}
ARXIV = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry><id>http://arxiv.org/abs/2401.00001v1</id><published>2024-01-01T00:00:00Z</published>
    <title>Graph   Benchmarks
      Revisited</title><summary>We revisit graph benchmarks.</summary>
    <author><name>Grace Hopper</name></author></entry>
  <entry><id>http://arxiv.org/abs/2001.00002v1</id><published>2020-01-01T00:00:00Z</published>
    <title>A Graph Benchmark for Meetings</title><summary>Same paper on arXiv.</summary>
    <author><name>Ada Lovelace</name></author></entry>
</feed>"""
CROSS_DOI = {"message": {"DOI": "10.1145/1234.5678", "title": ["A Graph Benchmark for Meetings"],
                         "author": [{"given": "Ada", "family": "Lovelace"}], "issued": {"date-parts": [[2021, 5]]},
                         "container-title": ["Proc. CHI"], "URL": "https://doi.org/10.1145/1234.5678", "type": "proceedings-article"}}
CROSS_Q = {"message": {"items": [CROSS_DOI["message"],
                                 {"DOI": "10.1/other", "title": ["Something Else Entirely"], "author": [{"family": "Smith"}],
                                  "issued": {"date-parts": [[2019]]}}]}}
LIT_SEEN: list = []


def fake_get(url, params=None):
    LIT_SEEN.append((url, dict(params or {})))
    req = httpx.Request("GET", url)
    if url.startswith(littools.OPENALEX):
        return httpx.Response(200, json=OPENALEX, request=req)
    if url.startswith(littools.ARXIV):
        return httpx.Response(200, content=ARXIV, request=req)
    if url == f"{littools.CROSSREF}/10.1145/1234.5678":
        return httpx.Response(200, json=CROSS_DOI, request=req)
    if url.startswith(littools.CROSSREF + "/10."):
        return httpx.Response(404, text="Resource not found.", request=req)
    if url == littools.CROSSREF:
        return httpx.Response(200, json=CROSS_Q, request=req)
    return httpx.Response(500, request=req)


def setUpModule():
    global SERVER, PORT, SAVED
    SERVER = ThreadingHTTPServer(("127.0.0.1", 0), H)
    PORT = SERVER.server_address[1]
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    SAVED = {"provider": provider.PROVIDER_FILE.read_bytes() if provider.PROVIDER_FILE.exists() else None,
             "lit": littools.LIT_FILE.read_bytes() if littools.LIT_FILE.exists() else None,
             "proxy": netenv.proxy, "get": littools._get}
    netenv.proxy = lambda: None
    littools._get = fake_get


def tearDownModule():
    SERVER.shutdown()
    netenv.proxy, littools._get = SAVED["proxy"], SAVED["get"]
    for f, k in ((provider.PROVIDER_FILE, "provider"), (littools.LIT_FILE, "lit")):
        if SAVED[k] is None:
            f.unlink(missing_ok=True)
        else:
            f.write_bytes(SAVED[k])
    provider._CACHE["mtime"] = None
    littools._CACHE["mtime"] = None
    usage.finish()


def set_provider(d):
    provider.save(provider.clean(d))
    provider._CACHE["mtime"] = None


def set_lit(on: bool):
    littools.set_enabled(on)
    littools._CACHE["mtime"] = None


class Base(unittest.TestCase):
    def setUp(self):
        Fake.seen, Fake.oa_need_mct, Fake.no_tools = [], False, False
        LIT_SEEN.clear()
        set_lit(True)
        self.month = json.loads(usage.month_file().read_text(encoding="utf-8")) if usage.month_file().exists() else None
        usage.finish()

    def tearDown(self):
        usage.finish()
        if self.month is None:
            usage.month_file().unlink(missing_ok=True)
        else:
            usage.month_file().write_text(json.dumps(self.month), encoding="utf-8")

    def ollama(self, **kw):
        set_provider(dict({"kind": "ollama", "base_url": f"http://127.0.0.1:{PORT}/v1", "fast": "qwen3:8b", "main": "qwen3:14b"}, **kw))


# ---------- 模型来源：新的三种 ----------
class Presets(Base):
    def test_clean_and_public(self):
        self.ollama()
        d = provider.load()
        self.assertEqual(d["kind"], "ollama")
        self.assertNotIn("key", d)                                       # 本机的不要 key
        self.assertEqual(provider.mode(), "api")
        self.assertEqual(provider.label(), "Ollama")
        self.assertEqual(provider.api_format(d), "openai")
        pub = provider.public()
        self.assertEqual(pub["presets"]["ollama"], {"label": "Ollama", "base_url": "http://127.0.0.1:11434/v1", "fast": "",
                                                    "main": "", "format": "openai", "key": "none", "local": True})
        self.assertEqual(pub["presets"]["lmstudio"]["base_url"], "http://127.0.0.1:1234/v1")
        self.assertEqual(pub["presets"]["deepseek"]["format"], "anthropic")
        self.assertTrue(pub["lit"])
        self.assertEqual(zconfig.assistant_name({}), "Qwen")              # 没起名：按型号叫
        # 型号必须选；地址带 /v1 可以；整条 /chat/completions 去掉；ftp 不行
        with self.assertRaisesRegex(ValueError, "model"):
            provider.clean({"kind": "ollama"})
        self.assertEqual(provider.clean({"kind": "openai", "base_url": "https://api.openai.com/v1/chat/completions/",
                                         "fast": "gpt-5-mini", "main": "gpt-5"})["base_url"], "https://api.openai.com/v1")
        with self.assertRaisesRegex(ValueError, "base_url"):
            provider.clean({"kind": "openai", "base_url": "ftp://x", "fast": "a", "main": "b"})
        with self.assertRaisesRegex(ValueError, "base_url"):                 # Anthropic 格式照旧不能带 /v1
            provider.clean({"kind": "custom", "base_url": "https://x.com/v1", "key": "k-12345678", "fast": "a", "main": "b"})
        set_provider({"kind": "openai", "base_url": "https://api.openai.com/v1", "fast": "gpt-5-mini", "main": "gpt-5"})
        self.assertEqual(provider.mode(), "api")                           # key 可不填
        self.assertEqual(provider.label(), "OpenAI")                        # 按域名认
        self.assertIn("OpenAI 的 API", provider.facts())

    def test_pretty_model(self):
        p = zconfig.pretty_model
        self.assertEqual([p("qwen3:8b"), p("qwen3:latest"), p("qwen/qwen3-8b"), p("llama3.1:8b-instruct-q4_K_M"),
                          p("deepseek-flash"), p("claude-opus-5-5"), p("kimi-k3"), p("my-own-model")],
                         ["Qwen3 8B", "Qwen3", "Qwen3 8B", "Llama3.1 8B Instruct", "DeepSeek Flash", "Claude Opus 5.5",
                          "Kimi K3", "my-own-model"])

    def test_local_never_proxied(self):
        saved = netenv.proxy
        netenv.proxy = lambda: "http://127.0.0.1:7897"
        old_np = os.environ.get("NO_PROXY")
        os.environ["NO_PROXY"] = "mirror.example.cn"
        try:
            self.assertIsNone(llm_api._proxy_for("http://127.0.0.1:11434/v1"))
            self.assertIsNone(llm_api._proxy_for("http://localhost:1234/v1"))
            self.assertIsNone(llm_api._proxy_for("http://[::1]:1234/v1"))
            self.assertIsNone(llm_api._proxy_for("https://api.mirror.example.cn/v1"))      # NO_PROXY 照原样
            self.assertEqual(llm_api._proxy_for("https://api.openalex.org/works"), "http://127.0.0.1:7897")
        finally:
            netenv.proxy = saved
            if old_np is None:
                os.environ.pop("NO_PROXY", None)
            else:
                os.environ["NO_PROXY"] = old_np


# ---------- OpenAI 格式：一次、流式、工具、拉型号 ----------
class OpenAIFormat(Base):
    def test_complete_stream_and_no_key(self):
        self.ollama()
        self.assertEqual(llm_api.complete("sys", [{"role": "assistant", "content": "开场"}, {"role": "user", "content": "hi"}]), "好")
        b = Fake.seen[-1]
        self.assertTrue(b["path"].endswith("/v1/chat/completions"))
        self.assertEqual(b["auth"], "")                                    # 本机的：不带 Authorization
        self.assertEqual(b["body"]["model"], "qwen3:14b")
        self.assertEqual([m["role"] for m in b["body"]["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual("".join(llm_api.stream("sys", [{"role": "user", "content": "hi"}])), "你好")   # 思考不念
        b = Fake.seen[-1]["body"]
        self.assertEqual((b["model"], b["stream"], b["stream_options"]), ("qwen3:8b", True, {"include_usage": True}))
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        llm_api.complete("", [{"role": "user", "content": [img, {"type": "text", "text": "看图"}]}])
        c = Fake.seen[-1]["body"]["messages"][-1]["content"]
        self.assertEqual(c[0], {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}})
        set_provider({"kind": "openai", "base_url": f"http://127.0.0.1:{PORT}/v1", "key": "sk-oa-1234", "fast": "a", "main": "b"})
        llm_api.complete("", [{"role": "user", "content": "hi"}])
        self.assertEqual(Fake.seen[-1]["auth"], "Bearer sk-oa-1234")

    def test_adapts_max_completion_tokens(self):
        self.ollama()
        Fake.oa_need_mct = True
        self.assertEqual(llm_api.complete("", [{"role": "user", "content": "hi"}], max_tokens=77), "好")
        self.assertEqual(Fake.seen[-1]["body"].get("max_completion_tokens"), 77)
        self.assertNotIn("max_tokens", Fake.seen[-1]["body"])

    def test_tools_loop(self):
        self.ollama()
        steps = []
        out = llm_api.chat("sys", [{"role": "user", "content": "找找相关论文"}], littools.TOOLS,
                           lambda n, a: littools.run(n, a, on_step=lambda k, v: steps.append((k, v))))
        self.assertTrue(out.startswith("查到了："))
        self.assertIn("A Graph Benchmark for Meetings", out)
        self.assertEqual(steps, [("查文献", "graph benchmark")])
        first, second = Fake.seen[-2]["body"], Fake.seen[-1]["body"]
        self.assertEqual(first["tools"][0]["function"]["name"], "search_papers")
        self.assertEqual([m["role"] for m in second["messages"]][-2:], ["assistant", "tool"])
        self.assertEqual(second["messages"][-1]["tool_call_id"], "c1")

    def test_no_tools_falls_back(self):
        self.ollama()
        Fake.no_tools = True
        self.assertEqual(llm_api.chat("", [{"role": "user", "content": "hi"}], littools.TOOLS, lambda n, a: ""), "好")
        self.assertNotIn("tools", Fake.seen[-1]["body"])

    def test_list_models(self):
        self.assertEqual(llm_api.list_models("ollama", f"http://127.0.0.1:{PORT}/v1"), ["llama3.1:8b", "qwen3:8b"])
        self.assertEqual(Fake.seen[-1]["auth"], "")
        llm_api.list_models("openai", f"http://127.0.0.1:{PORT}/v1", "sk-x-9999")
        self.assertEqual(Fake.seen[-1]["auth"], "Bearer sk-x-9999")
        with self.assertRaises(llm_api.ApiError):
            llm_api.list_models("ollama", "http://127.0.0.1:9/v1", timeout=2)


class AnthropicTools(Base):
    def test_tool_loop_and_stream_usage(self):
        set_provider({"kind": "custom", "base_url": f"http://127.0.0.1:{PORT}", "key": "k-test-5678", "fast": "f", "main": "m"})
        out = llm_api.chat("sys", [{"role": "user", "content": "核对一下这条引用"}], littools.TOOLS,
                           lambda n, a: littools.run(n, a))
        self.assertTrue(out.startswith("核对过了："))
        self.assertIn("年份不一样：给的是 2020，Crossref 记的是 2021", out)
        first, second = Fake.seen[-2]["body"], Fake.seen[-1]["body"]
        self.assertEqual(first["tools"][1]["name"], "check_citation")
        self.assertIn("input_schema", first["tools"][1])
        self.assertEqual(second["messages"][-2]["content"][1]["type"], "tool_use")            # 原样交回
        self.assertEqual(second["messages"][-1]["content"][0]["tool_use_id"], "tu1")
        self.assertEqual("".join(llm_api.stream("", [{"role": "user", "content": "hi"}])), "你好")


# ---------- 用量：记进会议和本月，按价算钱 ----------
class Usage(Base):
    def test_record_meeting_and_month(self):
        d = Path(tempfile.mkdtemp(prefix="b4-meeting-"))
        usage.begin(d)
        set_provider({"kind": "custom", "base_url": f"http://127.0.0.1:{PORT}", "key": "k-test-5678", "fast": "f", "main": "m"})
        llm_api.complete("", [{"role": "user", "content": "hi"}])                         # 10 进 1 出
        "".join(llm_api.stream("", [{"role": "user", "content": "hi"}]))                  # 20 进 + 5 缓存 + 2 出
        self.ollama()
        llm_api.complete("", [{"role": "user", "content": "hi"}])                         # 12 = 8 + 缓存 4，3 出
        u = usage.meeting(d)
        self.assertEqual(u["tokens"], 11 + 27 + 15)
        rows = {r["model"]: r for r in u["models"]}
        self.assertEqual((rows["m"]["calls"], rows["m"]["in"], rows["m"]["out"]), (1, 10, 1))
        self.assertEqual((rows["f"]["in"], rows["f"]["cache_read"], rows["f"]["out"]), (20, 5, 2))
        self.assertEqual((rows["qwen3:14b"]["in"], rows["qwen3:14b"]["cache_read"], rows["qwen3:14b"]["out"]), (8, 4, 3))
        self.assertEqual(rows["qwen3:14b"]["pretty"], "Qwen3 14B")
        self.assertTrue(all(r["cost"] is None for r in u["models"]))                     # 不认识的、本机的：不算钱
        self.assertEqual(u["costs"], {})
        usage.finish(d)
        llm_api.complete("", [{"role": "user", "content": "hi"}])                         # 收完尾：只进本月
        self.assertEqual(usage.meeting(d)["tokens"], 53)
        self.assertGreaterEqual(usage.month()["tokens"], 53 + 15)
        other = Path(tempfile.mkdtemp(prefix="b4-meeting-"))
        with usage.bind(other):                                                           # 回顾里重新生成某场的纪要
            llm_api.complete("", [{"role": "user", "content": "hi"}])
        self.assertEqual(usage.meeting(other)["tokens"], 15)
        self.assertIsNone(usage.meeting(Path(tempfile.mkdtemp(prefix="b4-none-"))))      # 没用 API 的会：没有

    def test_prices(self):
        peak = datetime(2026, 11, 18, 2, 30, tzinfo=timezone.utc)           # 北京周三 10:30
        night = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)          # 北京 22:00
        sat = datetime(2026, 11, 21, 2, 30, tzinfo=timezone.utc)            # 北京周六 10:30
        ds = "https://api.deepseek.com/anthropic"
        self.assertEqual(usage.cost_of(ds, "deepseek-flash", 1_000_000, 1_000_000, at=peak), ("CNY", 10.0))
        self.assertEqual(usage.cost_of(ds, "deepseek-flash", 1_000_000, 1_000_000, at=night), ("CNY", 5.0))
        self.assertEqual(usage.cost_of(ds, "deepseek-flash", 1_000_000, 1_000_000, at=sat), ("CNY", 5.0))
        self.assertEqual(usage.cost_of("https://api.deepseek.com/v1", "deepseek-v4-pro", 0, 0, cache_read=1_000_000, at=night),
                         ("CNY", 0.15))                                     # OpenAI 格式填 DeepSeek 的地址也认
        an = "https://api.anthropic.com"
        self.assertEqual(usage.cost_of(an, "claude-opus-5-5", 1_000_000, 1_000_000), ("USD", 24.0))
        self.assertEqual(usage.cost_of(an, "claude-opus-5", 1_000_000, 0), ("USD", 5.0))           # 不认成 opus-5-5
        self.assertEqual(usage.cost_of(an, "claude-haiku-4-5-20251001", 1_000_000, 0), ("USD", 1.0))
        cur, v = usage.cost_of(an, "claude-haiku-5-5", 50_000, 0, cache_read=60_000)              # 超过 10 万按长档
        self.assertEqual(cur, "USD")
        self.assertAlmostEqual(v, 50_000 * 0.5 / 1e6 + 60_000 * 0.05 / 1e6)
        cur, v = usage.cost_of("https://dashscope.aliyuncs.com/apps/anthropic", "qwen3.7-max", 0, 0, cache_read=1_000_000)
        self.assertEqual(cur, "CNY")
        self.assertAlmostEqual(v, 2.4)                                      # 隐式缓存命中按 20%
        self.assertIsNone(usage.cost_of("https://api.moonshot.cn/anthropic", "kimi-k3", 1000, 1000))   # 没核到官网价：不算
        self.assertIsNone(usage.cost_of("http://127.0.0.1:11434/v1", "qwen3:8b", 1000, 1000))
        self.assertIsNone(usage.cost_of(ds, "deepseek-unknown", 1000, 1000))

    def test_priced_view(self):
        d = Path(tempfile.mkdtemp(prefix="b4-meeting-"))
        with usage.bind(d):
            usage.record({"kind": "deepseek"}, "deepseek-flash", 1_000_000, 0, label="DeepSeek", base_url="https://api.deepseek.com/anthropic")
            usage.record({"kind": "deepseek"}, "deepseek-flash", 0, 0, label="DeepSeek", base_url="https://api.deepseek.com/anthropic")   # 0：不记
        u = usage.meeting(d)
        self.assertEqual(u["models"][0]["calls"], 1)
        self.assertEqual(u["models"][0]["currency"], "CNY")
        self.assertIn(u["costs"]["CNY"], (1.0, 2.0))                       # 空闲 / 高峰


# ---------- 查文献 ----------
class Lit(Base):
    def test_search_and_check(self):
        out = littools.search_papers("graph benchmark", limit=3)
        self.assertIn("A Graph Benchmark for Meetings", out)
        self.assertIn("Ada Lovelace, Alan Turing", out)
        self.assertIn("DOI：10.1145/1234.5678", out)
        self.assertIn("链接：https://doi.org/10.1145/1234.5678", out)
        self.assertIn("摘要：We build graphs", out)
        self.assertIn("Graph Benchmarks Revisited", out)                    # arXiv 那条，标题里的换行收成一个空格
        self.assertEqual(out.count("A Graph Benchmark for Meetings"), 1)    # 两边都有的同一篇只留一条
        self.assertIn("https://arxiv.org/abs/2401.00001v1", out)
        self.assertEqual(LIT_SEEN[1][1]["search_query"], "all:graph AND all:benchmark")
        self.assertIn("对得上", littools.check_citation(title="A graph benchmark for meetings", author="Lovelace", year=2021))
        self.assertIn("作者里没有“Knuth”", littools.check_citation(title="A Graph Benchmark for Meetings", author="Knuth"))
        self.assertIn("Crossref 里没有 DOI", littools.check_citation(doi="10.9999/nope"))
        self.assertIn("DOI 格式不对", littools.check_citation(doi="not-a-doi"))
        self.assertIn("对得上", littools.check_citation(doi="https://doi.org/10.1145/1234.5678", title="A Graph Benchmark for Meetings"))
        self.assertEqual(littools.run("nope", {}), "没有这个工具：nope")
        self.assertEqual(littools.run("search_papers", {"query": ""}), "没给检索词。")

    def test_network_errors_are_words(self):
        def broken(url, params=None):
            raise httpx.ConnectError("down")
        littools._get = broken
        try:
            out = littools.search_papers("x")
            self.assertIn("OpenAlex 没查成", out)
            self.assertIn("arXiv 没查成", out)
            self.assertIn("没查成", littools.check_citation(title="x"))
        finally:
            littools._get = fake_get

    def test_toggle_and_facts(self):
        self.assertTrue(littools.enabled())
        self.ollama()
        f = app_facts._models()
        self.assertIn("本机跑的 Ollama", f[0])
        self.assertIn("Qwen3 8B", f[0])
        self.assertIn("OpenAlex", f[2])
        self.assertIn("查论文时能查文献", provider.facts())
        set_lit(False)
        self.assertFalse(littools.enabled())
        self.assertFalse(provider.public()["lit"])
        self.assertIn("查资料不联网", app_facts._models()[2])
        self.assertIn("改稿和查论文不联网", provider.facts())
        self.assertIn("lit.json", app_facts._FILES)                        # 开关一变，提示词就重建
        set_provider({"kind": "claude"})
        self.assertIn("能联网", app_facts._models()[2])                     # Claude Code 那一路不受影响


# ---------- 后台查论文、改稿：开着查文献就带工具 ----------
PAPER = "# 论文\n\n## 1 引言\n\n我们沿用了 Lovelace 等人的图基准。\n"


class HandsLit(Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="b4-hands-"))
        self.paper = self.dir / "paper.md"
        self.paper.write_text(PAPER, encoding="utf-8")
        self.board = Board(self.dir / "board.json")
        self.hands = handsmod.Hands({"hands": {"timeout_s": 30}, "meeting": {"lang": "zh"}}, self.board, lambda *_: None, self.paper)
        self.hands.RETRY_WAITS = (0, 0)

    def test_ask_paper_uses_tools(self):
        self.ollama()
        steps = []
        ans, err = self.hands.ask_paper("别的论文是怎么做图基准的？", None, "", on_step=lambda k, v: steps.append((k, v)))
        self.assertIsNone(err)
        self.assertTrue(ans.startswith("查到了："))
        self.assertEqual(steps, [("读论文", "全文"), ("查文献", "graph benchmark")])
        body = [x for x in Fake.seen if x["method"] == "POST"][0]["body"]
        self.assertIn("search_papers", json.dumps(body["tools"]))
        self.assertIn("查文献的工具", body["messages"][0]["content"])
        self.assertNotIn("查不了网", body["messages"][0]["content"])
        Fake.seen = []
        self.hands.ask_paper("引言第一句写的什么", None, "", quick=True)      # 快查：不带工具
        self.assertNotIn("tools", Fake.seen[0]["body"])
        set_lit(False)
        Fake.seen = []
        self.hands.ask_paper("别的论文是怎么做的？", None, "")
        self.assertNotIn("tools", Fake.seen[0]["body"])
        self.assertIn("查不了网", Fake.seen[0]["body"]["messages"][0]["content"])


# ---------- 后端接口 ----------
class Http(Base):
    def test_endpoints(self):
        import http.client
        import socket
        from zuhui.view import Viewer
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        v = Viewer(port=port)
        v.serve(open_browser=False)

        def req(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read().decode("utf-8") or "null")
        st, d = req("POST", "/api/provider/models", {"kind": "ollama", "base_url": f"http://127.0.0.1:{PORT}/v1"})
        self.assertEqual((st, d), (200, {"models": ["llama3.1:8b", "qwen3:8b"]}))
        self.assertEqual(req("POST", "/api/provider/models", {"kind": "deepseek"})[0], 400)
        self.assertEqual(req("POST", "/api/provider/models", {"kind": "openai", "base_url": "ftp://x"}), (400, {"error": "base_url"}))
        self.assertIn("error", req("POST", "/api/provider/models", {"kind": "ollama", "base_url": "http://127.0.0.1:9/v1"})[1])
        st, d = req("POST", "/api/provider/lit", {"on": False})
        self.assertEqual((st, d["lit"]), (200, False))
        self.assertFalse(littools.enabled())
        st, d = req("POST", "/api/provider", {"kind": "lmstudio", "base_url": f"http://127.0.0.1:{PORT}/v1", "fast": "a", "main": "b"})
        self.assertEqual((st, d["kind"], d["active"], d["label"]), (200, "lmstudio", True, "LM Studio"))
        st, d = req("GET", "/api/usage")
        self.assertEqual(st, 200)
        self.assertEqual((d["month"], d["mode"]), (datetime.now().strftime("%Y-%m"), "api"))
        self.assertIn("tokens", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
