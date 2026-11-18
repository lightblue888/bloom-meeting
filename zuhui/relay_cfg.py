# -*- coding: utf-8 -*-
"""会议中继（设置 → 通用 → 邀请）：公共中继 / 自己的服务器 / 关闭，和"允许别人加入"。

- 公共中继（默认）：大家共用 share.PUBLIC_RELAY，装好就能邀请。电脑上没有 LiveKit 密钥，第一次用时向中继领通行码
  （share.device）。只在打开邀请之后才连（invite_on：复制邀请链接或打开"允许别人加入"）。
- 自己的服务器：服务器上跑 relay/setup.sh，最后打印一行连接码 bloom1.<base64url({"url", "key", "secret"})>，粘到设置里：
  密钥进 ~/.zuhui/livekit.env（share.py、bridge.py 读它），地址进 ~/.zuhui/relay.json。一直连。
- 关闭：没有邀请链接，一个人开会。
config.load() 调 apply() 把地址叠到 share.base_url 上。config.json 里手动写了 share.base_url 的（以前的配法）照旧用它。

出错抛 ValueError，参数是界面翻译用的代码：code（连接码不对）、mode。"""
from __future__ import annotations

import base64
import json
import os
import re
import time

from .config import STATE

PREFIX = "bloom1."
RELAY_FILE = STATE / "relay.json"        # {"mode": public|own|off, "url": 自己服务器的地址, "invite_on": bool}
ENV_FILE = STATE / "livekit.env"
MODES = ("public", "own", "off")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def make(url: str, key: str, secret: str) -> str:
    raw = json.dumps({"url": url, "key": key, "secret": secret}, separators=(",", ":")).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse(code: str) -> dict:
    """连接码 → {"url": "https://域名/", "ws": "wss://域名", "key", "secret"}。不对抛 ValueError("code")。"""
    s = re.sub(r"\s+", "", str(code or ""))
    if not s.startswith(PREFIX):
        raise ValueError("code")
    body = s[len(PREFIX):]
    try:
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("code") from None
    url, key, secret = str(d.get("url") or ""), str(d.get("key") or ""), str(d.get("secret") or "")
    m = re.match(r"^https://([A-Za-z0-9.-]+(?::\d+)?)/?$", url)
    if not m or not _KEY_RE.match(key) or len(secret) < 20 or len(secret) > 200 or not re.match(r"^[\x21-\x7e]+$", secret):
        raise ValueError("code")
    host = m.group(1)
    return {"url": f"https://{host}/", "ws": f"wss://{host}", "key": key, "secret": secret}


def _read() -> dict:
    try:
        d = json.loads(RELAY_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    d = dict(d, at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    tmp = RELAY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, RELAY_FILE)


def _public_relay() -> str:
    from .share import PUBLIC_RELAY
    return PUBLIC_RELAY


def mode(cfg: dict | None = None) -> str:
    """public / own / off。设置里没选过：config.json 里手动写了中继地址就是 own（以前的配法），否则公共中继。"""
    m = str(_read().get("mode") or "")
    if m in MODES:
        return m
    manual = str(((cfg or {}).get("share") or {}).get("base_url") or "") if cfg is not None else ""
    return "own" if manual and manual.rstrip("/") != _public_relay().rstrip("/") else "public"


def base_url() -> str | None:
    """设置里配的自己服务器的地址；没配 None。"""
    d = _read()
    return str(d.get("url") or "") or None if d.get("mode") == "own" else None


def invite_on() -> bool:
    """公共中继：打开了邀请（复制过邀请链接，或打开了"允许别人加入"）。"""
    return bool(_read().get("invite_on"))


def apply(cfg: dict) -> None:
    """叠到 config 上：share.base_url。"""
    sc = cfg.setdefault("share", {})
    d = _read()
    m = d.get("mode")
    if m == "own" and d.get("url"):
        sc["base_url"] = d["url"]
    elif m == "off":
        sc["base_url"] = ""
    elif m == "public" or not str(sc.get("base_url") or ""):
        sc["base_url"] = _public_relay()


def _env() -> dict:
    out = {}
    try:
        for ln in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def save(code: str) -> dict:
    """粘了自己服务器的连接码：密钥写进 livekit.env，切到 own。"""
    d = parse(code)
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = ENV_FILE.with_suffix(".env.tmp")
    tmp.write_text(f"LIVEKIT_API_KEY={d['key']}\nLIVEKIT_API_SECRET={d['secret']}\nLIVEKIT_URL={d['ws']}\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ENV_FILE)
    _write({**_read(), "mode": "own", "url": d["url"], "managed": True})
    return d


def set_mode(m: str) -> None:
    """切到公共中继 / 关闭（自己的服务器要粘连接码，走 save）。设置里粘的密钥删掉；手动放的 livekit.env 不动。"""
    if m not in ("public", "off"):
        raise ValueError("mode")
    d = _read()
    if d.get("managed"):
        ENV_FILE.unlink(missing_ok=True)
    _write({"mode": m, "invite_on": bool(d.get("invite_on"))})


def set_invite(on: bool) -> None:
    _write({**_read(), "invite_on": bool(on)})


def clear() -> None:
    """移除设置里配的自己的服务器，回到公共中继。"""
    set_mode("public")


def public(cfg: dict | None = None) -> dict:
    """设置页看的：模式、地址（只给域名）、密钥只给最后四位、允许别人加入没有。"""
    m = mode(cfg)
    e = _env()
    url = str(((cfg or {}).get("share") or {}).get("base_url") or "") or (base_url() or "")
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url) if url else ""
    key = e.get("LIVEKIT_API_KEY") or ""
    own_ok = bool(url and key and e.get("LIVEKIT_API_SECRET"))
    from .share import has_secret
    if m == "public" and url and has_secret(url):   # 公共中继那台就是自己的服务器（手里有它的密钥）：按自己的服务器算
        m = "own"
    return {"mode": m, "configured": own_ok if m == "own" else m == "public", "host": host if m != "off" else "",
            "key_tail": key[-4:] if m == "own" and len(key) >= 8 else "", "managed": bool(_read().get("managed")) and m == "own",
            "invite_on": invite_on() if m == "public" else m == "own"}


def test(share) -> dict:
    """测一下：中继能连上、认得这台电脑（GET /push/<房间>/have），自己的服务器再测 LiveKit。
    → {"ok": True, "ms"} 或 {"ok": False, "error": unset|unreachable|auth|livekit|register}"""
    import httpx
    from . import share as _share
    if not (share and share.base and share.configured):
        return {"ok": False, "error": "unset"}
    t0 = time.time()
    try:
        if share.public and not _share.device(share.base):
            return {"ok": False, "error": "register"}
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=6.0), verify=_share._trust(), trust_env=False) as c:
            r = c.get(f"{share.base.rstrip('/')}/push/{_share.room()}/have", headers=share._admin())
            if r.status_code in (401, 403):
                return {"ok": False, "error": "auth"}
            if r.status_code != 200:
                return {"ok": False, "error": "unreachable"}
            if not share.public:
                e = _share._env()
                tok = _share._jwt(e["LIVEKIT_API_KEY"], e["LIVEKIT_API_SECRET"], {"roomList": True})
                lk = c.post(share._http_base() + "/twirp/livekit.RoomService/ListRooms",
                            headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}, content="{}")
                if lk.status_code != 200:
                    return {"ok": False, "error": "livekit"}
    except httpx.HTTPError:
        return {"ok": False, "error": "unreachable"}
    return {"ok": True, "ms": int((time.time() - t0) * 1000)}
