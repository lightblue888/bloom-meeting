# -*- coding: utf-8 -*-
"""会议房间（LiveKit）的连接不走本机代理。

LiveKit 的 Rust SDK 会读进程环境里的 HTTPS_PROXY：本机开着代理软件时，会议信令就被带去绕一圈代理，
实测代理一分钟里连报上百次 WSAENOBUFS，信令断开、Claude 掉线又重进。
后端一启动就把代理变量从本进程环境里拿掉（strip_proxy），会议连接、掉线后自动重连都直连；
真要出国网的子进程（改稿 / 查资料的 claude、备用嗓子 edge-tts、训练时下载）用 with_proxy() 把原来的代理加回去。
不改系统设置、不动代理软件。"""
from __future__ import annotations

import os

KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
SAVED: dict[str, str] = {}


def strip_proxy() -> dict:
    """把代理变量从本进程环境里拿掉（存进 SAVED）。返回拿掉的那些。"""
    for k in KEYS:
        v = os.environ.pop(k, None)
        if v:
            SAVED[k] = v
    return dict(SAVED)


def with_proxy(env: dict | None = None) -> dict:
    """要出国网的子进程用：在 env（默认当前环境）上把原来的代理加回去。"""
    e = dict(os.environ if env is None else env)
    e.update(SAVED)
    return e


def proxy() -> str | None:
    """原来的 HTTPS 代理（没有就 HTTP）。"""
    return (SAVED.get("HTTPS_PROXY") or SAVED.get("https_proxy") or SAVED.get("HTTP_PROXY") or SAVED.get("http_proxy")
            or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
