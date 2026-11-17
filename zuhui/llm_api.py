# -*- coding: utf-8 -*-
"""直接调模型的 API（不经过 Claude Code）。

设置 → 通用 → 模型来源 选的是 API（DeepSeek、Kimi、智谱 GLM、通义千问、Anthropic API 或别的兼容接口）时，
Bloom 用模型的地方都走这里：会上对话（stream）、纪要和逐字稿整理（complete）、改稿和查论文（hands_api.py）。
选的是"Claude Code 账号"时不用这里，照旧起 Claude Code。

接口是 Anthropic Messages 格式（POST <地址>/v1/messages）：上面几家都提供这个兼容接口，Anthropic 自己更不用说。
两档型号（provider.py 里设置）：fast 给会上对话、快查用；main 给改稿、查论文、纪要用。"""
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


def _endpoint(d: dict) -> tuple[str, dict, str | None]:
    pre = provider.PRESETS.get(d["kind"], {})
    base = str(d.get("base_url") or pre.get("base_url") or "").rstrip("/")
    key = str(d.get("key") or "").strip()
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
    px = proxy()
    if not px:
        return None
    host = re.sub(r"^https?://([^/:]+).*$", r"\1", base).lower()
    if host in ("localhost", "::1") or host.startswith("127."):
        return None
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


def _error(r: httpx.Response, label: str) -> ApiError:
    try:
        j = r.json()
        msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error") or j.get("message")
    except Exception:
        msg = r.text[:200]
    hint = {401: "API key 不对", 402: "余额不足", 403: "没有权限", 404: "接口地址或型号不对", 429: "请求太频繁或额度用完"}.get(r.status_code, "")
    return ApiError(f"{label} 回了 {r.status_code}{('（' + hint + '）') if hint else ''}：{str(msg or '')[:160]}")


def complete(system: str, messages: list[dict], tier: str = "main", max_tokens: int = 4096, timeout: float = 180,
             temperature: float | None = None, d: dict | None = None) -> str:
    """问一次，返回回答的文字。"""
    d = provider.active(d)
    if not d:
        raise ApiError("还没设置模型来源（设置 → 通用 → 模型来源）")
    url, headers, px = _endpoint(d)
    sysm, msgs = _normalize(system, messages)
    body = {"model": model(tier, d), "max_tokens": int(max_tokens), "messages": msgs}
    if sysm:
        body["system"] = sysm
    if temperature is not None:
        body["temperature"] = temperature
    try:
        r = _client(px).post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise ApiError(f"连不上 {provider.label(d)}：{e!r}"[:200]) from None
    if r.status_code != 200:
        raise _error(r, provider.label(d))
    j = r.json()
    text = "".join(b.get("text", "") for b in (j.get("content") or []) if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise ApiError(f"{provider.label(d)} 没回文字（stop_reason={j.get('stop_reason')}）")
    return text


def stream(system: str, messages: list[dict], tier: str = "fast", max_tokens: int = 1024, timeout: float = 60,
           d: dict | None = None) -> Iterator[str]:
    """边生成边吐：一段一段的文字。生成器被提前关掉（打断）就断开连接。"""
    d = provider.active(d)
    if not d:
        raise ApiError("还没设置模型来源（设置 → 通用 → 模型来源）")
    url, headers, px = _endpoint(d)
    sysm, msgs = _normalize(system, messages)
    body = {"model": model(tier, d), "max_tokens": int(max_tokens), "messages": msgs, "stream": True}
    if sysm:
        body["system"] = sysm
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
                        yield piece
                elif t == "error":
                    raise ApiError(f"{provider.label(d)} 出错：{str((ev.get('error') or {}).get('message'))[:160]}")
                elif t == "message_stop":
                    return
    except httpx.HTTPError as e:
        raise ApiError(f"连不上 {provider.label(d)}：{e!r}"[:200]) from None


def ping(d: dict | None = None) -> None:
    """设置页"测试连接"：用快速那档问一个字。连不上抛 ApiError。"""
    complete("", [{"role": "user", "content": "只回一个字：好"}], tier="fast", max_tokens=16, timeout=60, d=d)
