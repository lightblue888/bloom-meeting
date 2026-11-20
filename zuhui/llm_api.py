# -*- coding: utf-8 -*-
"""直接调模型的 API（不经过 Claude Code）。

设置 → 通用 → 模型来源 选的是 API（DeepSeek、Kimi、智谱 GLM、通义千问、Anthropic API 或别的兼容接口）时，
Bloom 用模型的地方都走这里：会上对话（stream）、纪要和逐字稿整理（complete）、改稿和查论文（hands_api.py）。
选的是"Claude Code 账号"时不用这里，照旧起 Claude Code。

两种接口格式（provider.api_format）：
- Anthropic Messages（POST <地址>/v1/messages）：上面几家都提供这个兼容接口，Anthropic 自己更不用说；
- OpenAI Chat Completions（POST <地址>/chat/completions）："OpenAI 兼容接口"、本机的 Ollama、LM Studio。
对外都一样：complete（问一次）、stream（边生成边吐）、chat（带工具，查文献用）；messages 都按 Anthropic 的写法给
（图片是 Anthropic 的 base64 图片块），OpenAI 格式在这里换过去。
两档型号（provider.py 里设置）：fast 给会上对话、快查用；main 给改稿、查论文、纪要用。
每次调用的 token 用量记进 usage.py（这场会的 usage.json 和本月的累计）。"""
from __future__ import annotations

import json
import re
import threading
from typing import Iterator

import httpx

from . import provider

VERSION = "2023-06-01"


_CLIENTS: dict = {}
_CLIENTS_LOCK = threading.Lock()


def _client(px: str | None) -> httpx.Client:
    """连接复用：每个代理设置一个常驻的 client（新建一个光加载证书就要一秒多，会上对话每句都等就慢了）。"""
    with _CLIENTS_LOCK:
        c = _CLIENTS.get(px)
        if c is None or c.is_closed:
            c = _CLIENTS[px] = httpx.Client(proxy=px, trust_env=False, timeout=60)
        return c


class ApiError(RuntimeError):
    """连不上 / 密钥不对 / 余额不足 / 型号不对。消息是给人看的一句，带上服务商回的原话。"""


def enabled(d: dict | None = None) -> bool:
    """模型来源是 API（不是 Claude Code 账号，也不是还没设）。"""
    return provider.mode(d) == "api"


def model(tier: str, d: dict | None = None) -> str:
    """这一档用哪个型号（fast / main）。"[1m]" 是 Claude Code 自己的写法，直接调接口时去掉。"""
    d = provider.active(d) or {}
    pre = provider.PRESETS.get(d.get("kind", ""), {})
    m = str(d.get(tier) or pre.get(tier) or "")
    return re.sub(r"\[1m\]$", "", m)


def _base(d: dict) -> str:
    pre = provider.PRESETS.get(d["kind"], {})
    return str(d.get("base_url") or pre.get("base_url") or "").rstrip("/")


def _endpoint(d: dict) -> tuple[str, dict, str | None]:
    base = _base(d)
    key = str(d.get("key") or "").strip()
    if provider.api_format(d) == "openai":
        headers = {"content-type": "application/json"}
        if key:                                              # Ollama、LM Studio 不要 key：不带这个头
            headers["authorization"] = f"Bearer {key}"
        return base + "/chat/completions", headers, _proxy_for(base)
    headers = {"content-type": "application/json", "anthropic-version": VERSION}
    if d["kind"] == "anthropic":
        headers["x-api-key"] = key
    else:
        headers["authorization"] = f"Bearer {key}"          # 各家兼容接口都认 Bearer（和 Claude Code 的接法一样）
    return base + "/v1/messages", headers, _proxy_for(base)


def _proxy_for(base: str) -> str | None:
    """走不走代理，完全照这台电脑原来的设置：有代理就走，NO_PROXY 里列的不走；本机地址不走（浏览器、curl 也这样）。
    Bloom 自己不加规则，也不改系统或代理软件的设置。（后端启动时把代理变量从自己进程里摘了，原值在 netenv.SAVED。）"""
    import os
    from .netenv import proxy
    if provider.is_local(base):
        return None
    px = proxy()
    if not px:
        return None
    host = re.sub(r"^https?://(\[[^\]]+\]|[^/:]+).*$", r"\1", base).strip("[]").lower()
    for item in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(","):
        item = item.strip().lower().lstrip("*").lstrip(".")
        if item and (host == item or host.endswith("." + item)):
            return None
    return px


def _normalize(system: str, messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI 式的 [{role, content}] → Anthropic 要的：system 单独放；只剩 user / assistant，同角色相邻的并起来，
    第一条是 user，最后一条也是 user。content 可以是字符串，也可以是内容块列表（带图时）。"""
    sys_parts = [system] if system else []
    out: list[dict] = []
    for m in messages:
        role, c = m.get("role"), m.get("content")
        if role == "system":
            if c:
                sys_parts.append(str(c))
            continue
        role = "assistant" if role == "assistant" else "user"
        if out and out[-1]["role"] == role and isinstance(c, str) and isinstance(out[-1]["content"], str):
            out[-1] = {"role": role, "content": out[-1]["content"] + "\n" + c}
        else:
            out.append({"role": role, "content": c if c else "（空）"})
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "（会议开始）"})
    if out[-1]["role"] != "user":
        out.append({"role": "user", "content": "（请接着说）"})
    return "\n\n".join(sys_parts), out


# ---------- OpenAI 格式 ----------
def _oa_content(c):
    """Anthropic 式的内容（字符串或内容块）→ OpenAI 的：文字块并成一个字符串（本机的服务有的只收字符串），
    带图时是 [{"type":"text"}, {"type":"image_url"}]。"""
    if not isinstance(c, list):
        return c if c else "（空）"
    parts = []
    for p in c:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            parts.append({"type": "text", "text": str(p.get("text") or "")})
        elif p.get("type") == "image" and isinstance(p.get("source"), dict):
            s = p["source"]
            parts.append({"type": "image_url", "image_url": {"url": f"data:{s.get('media_type')};base64,{s.get('data')}"}})
    if all(x["type"] == "text" for x in parts):
        return "\n".join(x["text"] for x in parts) or "（空）"
    return parts


def _oa_messages(system: str, messages: list[dict]) -> list[dict]:
    sysm, msgs = _normalize(system, messages)
    out = [{"role": "system", "content": sysm}] if sysm else []
    return out + [{"role": m["role"], "content": _oa_content(m["content"])} for m in msgs]


class _Think:
    """本机的思考模型（Qwen3、DeepSeek-R1 蒸馏版）会把思考过程用 <think>…</think> 夹在正文前面：不念、不当回答。
    流式时一段一段喂进来，吐出去掉思考后的正文。"""

    def __init__(self):
        self.buf, self.state = "", "start"          # start：还不知道有没有 <think>；in：在思考里；out：正文

    def feed(self, s: str) -> str:
        if self.state == "out":
            return s
        self.buf += s
        if self.state == "start":
            t = self.buf.lstrip()
            if not t:
                return ""
            if "<think>".startswith(t[:7]) and len(t) < 7:
                return ""                           # 还看不出是不是 <think>
            if not t.startswith("<think>"):
                self.state, out, self.buf = "out", self.buf, ""
                return out
            self.state, self.buf = "in", t[7:]
        i = self.buf.find("</think>")
        if i < 0:
            self.buf = self.buf[-8:]
            return ""
        self.state, out, self.buf = "out", self.buf[i + 8:].lstrip(), ""
        return out

    def end(self) -> str:
        out = self.buf if self.state == "start" else ""
        self.buf = ""
        return out


def strip_think(text: str) -> str:
    t = _Think()
    return t.feed(text or "") + t.end()


def _oa_body(body: dict, fix: set) -> dict:
    """按接口抱怨过的改：max_tokens → max_completion_tokens（OpenAI 新型号），去掉 temperature、stream_options。"""
    b = dict(body)
    if "max_completion_tokens" in fix and "max_tokens" in b:
        b["max_completion_tokens"] = b.pop("max_tokens")
    if "temperature" in fix:
        b.pop("temperature", None)
    if "stream_options" in fix:
        b.pop("stream_options", None)
    return b


def _oa_fixable(r: httpx.Response, fix: set) -> bool:
    """400 是因为某个参数不认：记下来，改了再发一次。认不出原因返回 False。"""
    if r.status_code != 400:
        return False
    s = r.text[:600].lower()
    for k, hint in (("max_completion_tokens", "max_completion_tokens"), ("stream_options", "stream_options"),
                    ("temperature", "temperature")):
        if hint in s and k not in fix:
            fix.add(k)
            return True
    return False


# ---------- 用量 ----------
def _record(d: dict, tier: str, use: dict | None, est_in: str = "", est_out: str = "") -> None:
    """记一次调用的 token（use 是统一后的 {in, out, cache_read, cache_write}；接口没回用量就按字数粗估）。"""
    from . import usage
    if not use:
        use = {"in": usage.estimate_tokens(est_in), "out": usage.estimate_tokens(est_out)}
    usage.record(d, model(tier, d), use.get("in", 0), use.get("out", 0), use.get("cache_read", 0),
                 use.get("cache_write", 0), label=provider.label(d), base_url=_base(d))


def _use_anthropic(u: dict | None) -> dict | None:
    if not isinstance(u, dict):
        return None
    return {"in": int(u.get("input_tokens") or 0), "out": int(u.get("output_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0)}


def _use_openai(u: dict | None) -> dict | None:
    """OpenAI 的 prompt_tokens 含命中缓存的那部分：拆开（DeepSeek 写在 prompt_cache_hit_tokens，OpenAI 写在
    prompt_tokens_details.cached_tokens）。"""
    if not isinstance(u, dict):
        return None
    prompt = int(u.get("prompt_tokens") or 0)
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens") if isinstance(u.get("prompt_tokens_details"), dict)
                  else 0) or u.get("prompt_cache_hit_tokens") or 0)
    cached = min(cached, prompt)
    return {"in": prompt - cached, "out": int(u.get("completion_tokens") or 0), "cache_read": cached, "cache_write": 0}


def _chars(system: str, messages: list[dict]) -> str:
    """估算输入 token 用的：系统提示词和消息里的文字（图不算）。"""
    out = [system or ""]
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            out += [str(p.get("text") or "") for p in c if isinstance(p, dict)]
        else:
            out.append(str(c or ""))
    return "\n".join(out)


def _error(r: httpx.Response, label: str) -> ApiError:
    try:
        j = r.json()
        msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error") or j.get("message")
    except Exception:
        msg = r.text[:200]
    hint = {401: "API Key 无效", 402: "余额不足", 403: "无权限", 404: "接口地址或型号错误", 429: "请求过于频繁或额度已用完"}.get(r.status_code, "")
    return ApiError(f"{label} 请求失败（{r.status_code}{('，' + hint) if hint else ''}）：{str(msg or '')[:160]}")


UNSET = "未设置模型来源"


def _post(d: dict, body: dict, timeout: float) -> dict:
    """发一次（不流式），返回接口回的 JSON。OpenAI 格式的接口不认某个参数（400）时改了再发，最多改三次。"""
    url, headers, px = _endpoint(d)
    openai = provider.api_format(d) == "openai"
    fix: set = set()
    for _ in range(4):
        b = _oa_body(body, fix) if openai else body
        try:
            r = _client(px).post(url, headers=headers, json=b, timeout=timeout)
        except httpx.HTTPError as e:
            raise ApiError(f"无法连接 {provider.label(d)}：{e!r}"[:200]) from None
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                raise ApiError(f"{provider.label(d)} 回的不是 JSON：{r.text[:120]}") from None
        if not (openai and _oa_fixable(r, fix)):
            raise _error(r, provider.label(d))
    raise _error(r, provider.label(d))


def _text_anthropic(j: dict) -> str:
    return "".join(b.get("text", "") for b in (j.get("content") or []) if isinstance(b, dict) and b.get("type") == "text")


def _msg_openai(j: dict) -> tuple[dict, str]:
    ch = (j.get("choices") or [{}])[0] or {}
    return (ch.get("message") or {}), str(ch.get("finish_reason") or "")


def complete(system: str, messages: list[dict], tier: str = "main", max_tokens: int = 4096, timeout: float = 180,
             temperature: float | None = None, d: dict | None = None) -> str:
    """问一次，返回回答的文字。"""
    d = provider.active(d)
    if not d:
        raise ApiError(UNSET)
    if provider.api_format(d) == "openai":
        body = {"model": model(tier, d), "max_tokens": int(max_tokens), "messages": _oa_messages(system, messages)}
        if temperature is not None:
            body["temperature"] = temperature
        j = _post(d, body, timeout)
        msg, why = _msg_openai(j)
        text = strip_think(str(msg.get("content") or ""))
        _record(d, tier, _use_openai(j.get("usage")), _chars(system, messages), text)
        if not text.strip():
            raise ApiError(f"{provider.label(d)} 没回文字（finish_reason={why or '无'}）")
        return text
    sysm, msgs = _normalize(system, messages)
    body = {"model": model(tier, d), "max_tokens": int(max_tokens), "messages": msgs}
    if sysm:
        body["system"] = sysm
    if temperature is not None:
        body["temperature"] = temperature
    j = _post(d, body, timeout)
    text = _text_anthropic(j)
    _record(d, tier, _use_anthropic(j.get("usage")), _chars(sysm, msgs), text)
    if not text.strip():
        raise ApiError(f"{provider.label(d)} 没回文字（stop_reason={j.get('stop_reason')}）")
    return text


def stream(system: str, messages: list[dict], tier: str = "fast", max_tokens: int = 1024, timeout: float = 60,
           d: dict | None = None) -> Iterator[str]:
    """边生成边吐：一段一段的文字。生成器被提前关掉（打断）就断开连接。用量在结束（或被打断）时记一笔。"""
    d = provider.active(d)
    if not d:
        raise ApiError(UNSET)
    if provider.api_format(d) == "openai":
        yield from _stream_openai(d, system, messages, tier, max_tokens, timeout)
        return
    url, headers, px = _endpoint(d)
    sysm, msgs = _normalize(system, messages)
    body = {"model": model(tier, d), "max_tokens": int(max_tokens), "messages": msgs, "stream": True}
    if sysm:
        body["system"] = sysm
    use: dict = {}
    said: list[str] = []
    try:
        with _client(px).stream("POST", url, headers=headers, json=body, timeout=httpx.Timeout(timeout, connect=15)) as r:
            if r.status_code != 200:
                r.read()
                raise _error(r, provider.label(d))
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                t = ev.get("type")
                if t == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                    piece = ev["delta"].get("text") or ""
                    if piece:
                        said.append(piece)
                        yield piece
                elif t == "message_start":       # 输入（和缓存）的 token 在开头就给了
                    use.update(_use_anthropic((ev.get("message") or {}).get("usage")) or {})
                elif t == "message_delta":       # 输出的 token：累计数，最后一次就是总数
                    u = ev.get("usage") or {}
                    if u.get("output_tokens") is not None:
                        use["out"] = int(u["output_tokens"])
                elif t == "error":
                    raise ApiError(f"{provider.label(d)} 出错：{str((ev.get('error') or {}).get('message'))[:160]}")
                elif t == "message_stop":
                    return
    except httpx.HTTPError as e:
        raise ApiError(f"无法连接 {provider.label(d)}：{e!r}"[:200]) from None
    finally:
        if use or said:
            from . import usage
            if said and use.get("out", 0) <= 1:          # 被打断：输出没报全，按念出来的字估
                use["out"] = max(use.get("out", 0), usage.estimate_tokens("".join(said)))
            if not use.get("in"):
                use["in"] = usage.estimate_tokens(_chars(sysm, msgs))
            _record(d, tier, use)


def _stream_openai(d: dict, system: str, messages: list[dict], tier: str, max_tokens: int, timeout: float):
    url, headers, px = _endpoint(d)
    msgs = _oa_messages(system, messages)
    body = {"model": model(tier, d), "max_tokens": int(max_tokens), "messages": msgs, "stream": True,
            "stream_options": {"include_usage": True}}            # 最后一段带上用量
    fix: set = set()
    use = None
    said: list[str] = []
    think = _Think()
    started = False
    try:
        for _ in range(4):
            with _client(px).stream("POST", url, headers=headers, json=_oa_body(body, fix),
                                    timeout=httpx.Timeout(timeout, connect=15)) as r:
                if r.status_code != 200:
                    r.read()
                    if _oa_fixable(r, fix):
                        continue
                    raise _error(r, provider.label(d))
                started = True
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    if ev.get("error"):
                        e = ev["error"]
                        raise ApiError(f"{provider.label(d)} 出错：{str(e.get('message') if isinstance(e, dict) else e)[:160]}")
                    if isinstance(ev.get("usage"), dict):
                        use = _use_openai(ev["usage"])
                    for ch in ev.get("choices") or []:
                        piece = think.feed(str((ch.get("delta") or {}).get("content") or ""))
                        if piece:
                            said.append(piece)
                            yield piece
                tail = think.end()
                if tail:
                    said.append(tail)
                    yield tail
                return
        raise ApiError(f"{provider.label(d)} 不认这次的请求参数")
    except httpx.HTTPError as e:
        raise ApiError(f"无法连接 {provider.label(d)}：{e!r}"[:200]) from None
    finally:
        if started:
            _record(d, tier, use, _chars(system, messages), "".join(said))


def _tool_specs(tools: list[dict], fmt: str) -> list[dict]:
    if fmt == "openai":
        return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                  "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
                for t in tools]
    return [{"name": t["name"], "description": t.get("description", ""),
             "input_schema": t.get("parameters") or {"type": "object", "properties": {}}} for t in tools]


_NO_TOOLS = re.compile(r"tool|function", re.I)
LAST_ROUND = "（查的次数用完了：别再调用工具，就按上面已经查到的回答。）"


def chat(system: str, messages: list[dict], tools: list[dict], run_tool, tier: str = "main", max_tokens: int = 4096,
         timeout: float = 180, max_rounds: int = 4, d: dict | None = None, cancel=None) -> str:
    """带工具问：模型要调工具就调（run_tool(名字, 参数) → 给模型看的文字），把结果交回去再问，直到它直接回答。
    tools：[{"name", "description", "parameters"（JSON Schema）}]。最多 max_rounds 轮工具调用，最后一轮让它别再调。
    接口不支持工具（400 说到 tool / function）：不带工具再问一次（complete）。timeout 是一共多久。"""
    import time as _t
    d = provider.active(d)
    if not d:
        raise ApiError(UNSET)
    fmt = provider.api_format(d)
    end = _t.time() + timeout
    specs = _tool_specs(tools, fmt)
    if fmt == "openai":
        msgs = _oa_messages(system, messages)
        body = {"model": model(tier, d), "max_tokens": int(max_tokens), "messages": msgs, "tools": specs}
    else:
        sysm, msgs = _normalize(system, messages)
        body = {"model": model(tier, d), "max_tokens": int(max_tokens), "messages": msgs, "tools": specs}
        if sysm:
            body["system"] = sysm
    for rnd in range(max_rounds + 1):
        if cancel is not None and cancel.is_set():
            raise ApiError("被打断")
        left = end - _t.time()
        if left < 1:
            raise ApiError(f"超时 {timeout:.0f} 秒")
        try:
            j = _post(d, body, left)
        except ApiError as e:
            if rnd == 0 and "请求失败（400" in str(e) and _NO_TOOLS.search(str(e)):
                return complete(system, messages, tier=tier, max_tokens=max_tokens, timeout=left, d=d)
            raise
        if fmt == "openai":
            msg, why = _msg_openai(j)
            text = strip_think(str(msg.get("content") or ""))
            _record(d, tier, _use_openai(j.get("usage")), _chars("", msgs), text)
            calls = [c for c in (msg.get("tool_calls") or []) if isinstance(c, dict) and c.get("function")]
            if not calls or rnd == max_rounds:
                if not text.strip():
                    raise ApiError(f"{provider.label(d)} 没回文字（finish_reason={why or '无'}）")
                return text
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                fn = c["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                out = run_tool(str(fn.get("name") or ""), args if isinstance(args, dict) else {})
                msgs.append({"role": "tool", "tool_call_id": c.get("id") or "", "content": str(out)})
            if rnd == max_rounds - 1:
                msgs.append({"role": "user", "content": LAST_ROUND})
        else:
            blocks = [b for b in (j.get("content") or []) if isinstance(b, dict)]
            text = _text_anthropic(j)
            _record(d, tier, _use_anthropic(j.get("usage")), _chars(body.get("system", ""), msgs), text)
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not uses or rnd == max_rounds:
                if not text.strip():
                    raise ApiError(f"{provider.label(d)} 没回文字（stop_reason={j.get('stop_reason')}）")
                return text
            msgs.append({"role": "assistant", "content": blocks})
            res = [{"type": "tool_result", "tool_use_id": u.get("id") or "",
                    "content": str(run_tool(str(u.get("name") or ""), u.get("input") if isinstance(u.get("input"), dict) else {}))}
                   for u in uses]
            if rnd == max_rounds - 1:
                res.append({"type": "text", "text": LAST_ROUND})
            msgs.append({"role": "user", "content": res})
    raise ApiError("查了几轮还没答出来")


def list_models(kind: str, base_url: str, key: str = "", timeout: float = 15) -> list[str]:
    """OpenAI 格式的接口有哪些型号（GET <地址>/models，Ollama、LM Studio 列的就是本机装好的）。不花钱。
    连不上 / 地址不对抛 ApiError。"""
    d = {"kind": kind, "base_url": base_url, "key": key}
    base = _base(d)
    label = provider.label(dict(d, fast="x", main="x", key=key or "x")) or base
    headers = {"authorization": f"Bearer {key}"} if key else {}
    try:
        r = _client(_proxy_for(base)).get(base + "/models", headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise ApiError(f"无法连接 {label}：{e!r}"[:200]) from None
    if r.status_code != 200:
        raise _error(r, label)
    try:
        data = r.json()
    except ValueError:
        raise ApiError(f"{label} 回的不是型号列表") from None
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list) and isinstance(data, dict):
        rows = data.get("models")                     # 有的服务写成 {"models": [...]}
    ids = []
    for x in rows or []:
        mid = x.get("id") or x.get("name") or x.get("model") if isinstance(x, dict) else x
        if isinstance(mid, str) and mid.strip() and mid not in ids:
            ids.append(mid.strip())
    return sorted(ids, key=str.lower)[:500]


def ping(d: dict | None = None) -> None:
    """设置页"测试连接"：用快速那档问一个字。连不上抛 ApiError。"""
    complete("", [{"role": "user", "content": "只回一个字：好"}], tier="fast", max_tokens=16, timeout=60, d=d)
