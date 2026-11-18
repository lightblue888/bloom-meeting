# -*- coding: utf-8 -*-
"""邀请链接：会议中继是用户自己 VPS 上的 LiveKit（入会页 + token 服务 + 信令，见 VPS 的 /opt/bloom/README.md）。

- 每场会一个新房间号（随机、够长、猜不到，房间号就是口令），存在 ~/.zuhui/share.json。散会后中继把这场标成结束、
  踢掉还在房间里的人，这边立刻换一个新房间号给下一场：旧链接只能看到"已结束"，再也进不来。没开会时复制的链接
  就是下一场会的。
- 邀请链接 = <中继网址>/room/<房间号>（新房间号是 10 位小写字母数字；中继网址是 config.json 的 share.base_url）。
  对方用浏览器打开就进，身份默认访客。旧格式 ?room=bloom-…&role=guest 照样能开（中继的 Caddy 把 /room/* 交给入会页）。
- 谁通过链接进来了：每隔几秒用 LiveKit 的 RoomService.ListParticipants 问一次中继（管理员 token 本地签，
  密钥只在 ~/.zuhui/livekit.env），结果给顶栏的参会人。
- 中继怎么搭见 relay/README.md：一台有公网地址的服务器，LiveKit + relay/token_server.py + Caddy（HTTPS）。
- 公共中继（默认，PUBLIC_RELAY；设置 → 通用 → 邀请 里可以换成自己的或关掉）：电脑上没有 LiveKit 密钥，第一次用时
  向中继领一个通行码（~/.zuhui/relay_device.json），推送、问谁进来了、语音桥进房间都用它，要密钥的事中继代办。
  公共中继只在打开邀请之后才连（armed）：没邀请人，论文、字幕、声音一样都不发出去。
  自签根证书 ~/.zuhui/bloom-ca.crt 在就一并信任，从不关校验。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from .config import STATE

ENV = STATE / "livekit.env"
CA = STATE / "bloom-ca.crt"
SHARE = STATE / "share.json"
DEFAULT_BASE = ""                 # 没配中继：不开邀请链接
PUBLIC_RELAY = "https://meet.blo0m.com:8443/"   # 公共中继：大家共用，装好就能邀请（relay_cfg 默认用它）。直连广州，不经东京
DEVICE = STATE / "relay_device.json"


def device(base: str, register: bool = True) -> dict | None:
    """公共中继给这台电脑的通行码 {"base", "device", "secret", "ws"}；没有就去领一个（register=False 只读）。领不到 None。"""
    try:
        d = json.loads(DEVICE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("base") == base and d.get("device") and d.get("secret"):
            return d
    except (OSError, ValueError):
        pass
    if not register or not base:
        return None
    import httpx
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=6.0), verify=_trust(), trust_env=False) as c:
            r = c.post(base.rstrip("/") + "/dev/register", content=b"{}", headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        j = r.json()
        d = {"base": base, "device": str(j["device"]), "secret": str(j["secret"]), "ws": str(j.get("ws") or "")}
    except Exception:
        return None
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = DEVICE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    try:
        import os
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(DEVICE)
    return d


def host_token(base: str, identity: str, name: str, role: str) -> tuple[str, str]:
    """公共中继：主持人这边进语音房间的 (token, wss 地址)，中继用自己的密钥代签。"""
    import httpx
    d = device(base)
    if not d:
        raise RuntimeError("公共中继没领到通行码")
    with httpx.Client(timeout=httpx.Timeout(10.0, connect=6.0), verify=_trust(), trust_env=False) as c:
        r = c.post(f"{base.rstrip('/')}/push/{quote(room())}/join", headers={"Authorization": f"Device {d['device']}:{d['secret']}"},
                   content=json.dumps({"identity": identity, "name": name, "role": role}, ensure_ascii=False).encode("utf-8"))
    r.raise_for_status()
    j = r.json()
    return str(j["token"]), str(j.get("url") or d.get("ws") or "")


def _host(u: str) -> str:
    return (urlsplit(u).hostname or "").lower() if u else ""


def has_secret(base: str | None = None) -> bool:
    """这台电脑有这个中继的 LiveKit 密钥（自己的服务器）。给了 base 就要地址对得上：换成公共中继以后，
    以前手动放的、别的服务器的 livekit.env 不拿去签。"""
    e = _env()
    if not (e.get("LIVEKIT_API_KEY") and e.get("LIVEKIT_API_SECRET")):
        return False
    return not base or not e.get("LIVEKIT_URL") or _host(e["LIVEKIT_URL"]) == _host(base)


def _env() -> dict:
    out = {}
    try:
        for ln in ENV.read_text(encoding="utf-8-sig").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _trust():
    """中继现在用 Let's Encrypt 的 IP 证书（系统/certifi 信任链就认）；老的自签根证书还在的话一并信任，兼容旧地址。"""
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    if CA.exists():
        try:
            ctx.load_verify_locations(str(CA))
        except (OSError, ssl.SSLError):
            pass
    return ctx


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _jwt(key: str, secret: str, video: dict, ttl: int = 300, identity: str = "bloom-host", extra: dict | None = None) -> str:
    now = int(time.time())
    head = {"alg": "HS256", "typ": "JWT"}
    body = {"iss": key, "sub": identity, "nbf": now - 10, "exp": now + ttl, "video": video}
    body.update(extra or {})
    msg = _b64(json.dumps(head, separators=(",", ":")).encode()) + "." + _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    return msg + "." + _b64(sig)


def _state() -> dict:
    try:
        return json.loads(SHARE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_ROOM_LOCK = threading.Lock()


def _save(s: dict) -> None:
    tmp = SHARE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(SHARE)
    except OSError:
        pass


_ID_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"     # 去掉 i l o 0 1：念出来、手抄都不会认错
_ID_LEN = 10                                       # 31^10 ≈ 8e14（约 50 位熵）：房间号就是口令，猜不到


def _fresh_id() -> str:
    """新房间号：10 位小写字母数字，放进短链接 /room/<房间号>。以前的 bloom-xxxx 房间号照样认（中继和入会页都不看前缀）。"""
    return "".join(secrets.choice(_ID_CHARS) for _ in range(_ID_LEN))


CAPTION_LINES = 6        # 访客页字幕：只推最近这么多句
CAPTION_CHARS = 200      # 每句最多这么多字（长句留结尾：字幕要的是正在说的那部分）


def captions(lines: list[dict], host: str, ai: str = "Claude") -> list[dict]:
    """访客页字幕用的最近几句：[{who, name, text, sec}]，顺序和逐字稿一样（按开口时刻，不一定是追加）。
    who 是 teacher（主持人）/ student（Claude）/ guest（链接进来的人），system 之类的不给。"""
    out = []
    for l in reversed(lines or []):
        who = l.get("who")
        text = " ".join(str(l.get("text") or "").split())
        if who not in ("teacher", "student", "guest") or not text:
            continue
        if len(text) > CAPTION_CHARS:
            text = "…" + text[-(CAPTION_CHARS - 1):]
        name = ai if who == "student" else (str(l.get("name") or "").strip() or (host if who == "teacher" else "参会者"))
        try:
            sec = round(float(l.get("sec") or 0), 1)
        except (TypeError, ValueError):
            sec = 0.0
        out.append({"who": who, "name": name, "text": text, "sec": sec})
        if len(out) >= CAPTION_LINES:
            break
    out.reverse()
    return out


REC_CACHE_S = 3.0
_rec_cache: tuple[float, str | None] = (0.0, None)


def rec_mode(now: float | None = None) -> str | None:
    """访客页的录制告知（进会页一句话、会中顶栏一个小标记）：主持人这边正在录屏 "screen"、只录音 "audio"、没在录 None。
    开会时录屏软件录 Bloom 窗口和麦克风（record 任务在跑）；任务停了（录屏崩了、被关了）就是没在录。
    推送线程每秒问一次：record 任务的文件要一个个读，结果留 REC_CACHE_S 秒。"""
    global _rec_cache
    now = time.time() if now is None else now
    at, v = _rec_cache
    if 0 <= now - at < REC_CACHE_S:
        return v
    try:
        from .ears import running_record_job
        job = running_record_job()
    except Exception:
        job = None
    v = None
    if job:
        v = "audio" if str(job.get("mode") or "").lower() in ("audio", "audio_only") else "screen"
    _rec_cache = (now, v)
    return v


def room() -> str:
    """这一场（或下一场）会的房间号：没有就随机生成一个（见 _fresh_id）。散会后 new_room() 换掉。"""
    with _ROOM_LOCK:
        s = _state()
        r = str(s.get("room") or "")
        if not r:
            r = s["room"] = _fresh_id()
            s["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
            s["used"] = False
            _save(s)
        return r


def mark_used() -> None:
    """这个房间号开过会了（推过"开会中"）：散会后就得换。"""
    with _ROOM_LOCK:
        s = _state()
        if s.get("room") and not s.get("used"):
            s["used"] = True
            s["used_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save(s)


def room_used() -> bool:
    return bool(_state().get("used"))


def new_room() -> str:
    """换一个新房间号（散会后）：旧的记进 old（只留最近 20 个，查问题用），新的还没开过会。"""
    with _ROOM_LOCK:
        s = _state()
        old = [x for x in (s.get("old") or []) if isinstance(x, dict)]
        if s.get("room"):
            old.append({"room": s["room"], "created": s.get("created"), "retired": time.strftime("%Y-%m-%d %H:%M:%S")})
        r = _fresh_id()
        _save({"room": r, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "used": False, "old": old[-20:]})
        return r


class Share:
    """邀请链接 + 谁通过链接进来了（后台线程轮询中继）。"""

    POLL_S = 4.0

    def __init__(self, cfg: dict):
        self.cfg = cfg
        sc = dict(cfg.get("share") or {})
        self.base = str(sc.get("base_url") or DEFAULT_BASE).strip()
        self.enabled = sc.get("enabled", True) is not False and bool(self.base)   # 没配中继网址就不开
        if self.base and not self.base.endswith("/"):
            self.base += "/"
        self.guests: list[dict] = []
        self._seen: dict[str, tuple[dict, float]] = {}   # 访客 id -> (记录, 最后一次看到的时刻)，见 _poll_guests
        self._seen_room: str | None = None
        self.error: str | None = None
        self.checked = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = None
        # 访客看会：开会时把逐字稿和论文快照推到中继（见 set_feed）
        self._feed = None
        self._push_thread: threading.Thread | None = None
        self.push_error: str | None = None
        self.pushed = 0.0
        self.live = False             # 推送线程看到的"正在开会"
        # 访客在会议页上做的事（新增 / 切论文、确认 / 撤回改动……）：中继排着，开会时每秒取一次（见 set_actions）
        self._act_handler = None
        self._act_thread: threading.Thread | None = None
        self.act_error: str | None = None

    # ---------- 对外 ----------
    @property
    def configured(self) -> bool:
        """能邀请：有中继地址，并且有 LiveKit 密钥（自己的中继）或者用通行码（公共中继，用到时再领）。"""
        return self.enabled and bool(self.base)

    @property
    def public(self) -> bool:
        """用通行码（电脑上没有 LiveKit 密钥）：公共中继。"""
        return self.enabled and bool(self.base) and not has_secret(self.base)

    @property
    def armed(self) -> bool:
        """现在该不该连中继：自己的中继一直连；公共中继要打开邀请才连（复制邀请链接 / 允许别人加入）。"""
        if not self.configured:
            return False
        if not self.public:
            return True
        try:
            from .relay_cfg import invite_on
            return invite_on()
        except Exception:
            return False

    def _device(self) -> dict:
        d = device(self.base)
        if not d:
            raise RuntimeError("公共中继没领到通行码")
        return d



    def invite(self) -> dict:
        if not self.enabled:
            return {"ok": False, "error": "没有配会议中继（config.json 的 share.base_url，见 relay/README.md），邀请链接不可用"}
        r = room()
        return {"ok": True, "room": r, "base": self.base, "url": f"{self.base}room/{quote(r)}",
                "relay": self.configured, "public": self.public, "armed": self.armed,
                "guests": len(self.guests), "error": self.error}

    def reconfigure(self, base: str | None) -> None:
        """设置里粘了连接码 / 移除了中继：换地址，配好了就把后台线程起来（不用重启 Bloom）。"""
        self.base = (base or "").strip()
        if self.base and not self.base.endswith("/"):
            self.base += "/"
        self.enabled = bool(self.base) and (self.cfg.get("share") or {}).get("enabled", True) is not False
        self.error = None
        if self.configured:
            self.start()
            if self._feed:
                self.set_feed(*self._feed)
            if self._act_handler:
                self.set_actions(self._act_handler)

    def start(self) -> None:
        if not self.configured or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._loop, name="share-guests", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_feed(self, state_fn, paper_key_fn, paper_fn) -> None:
        """访客能看到什么：state_fn() 没在开会时返回 None，开会时返回给访客的会议信息（标题、参会人、字幕用的最近几句 captions）；
        paper_key_fn() 便宜地算论文快照的版本，变了才调 paper_fn() -> (html, {文件名: (类型, 字节)}) 重新推。
        只在开会时推；散会就让中继把论文和图删掉。"""
        self._feed = (state_fn, paper_key_fn, paper_fn)
        if self.configured and not (self._push_thread and self._push_thread.is_alive()):
            self._push_thread = threading.Thread(target=self._push_loop, name="share-push", daemon=True)
            self._push_thread.start()

    def set_actions(self, handler) -> None:
        """handler(act, fetch_upload) -> 结果 dict：访客在会议页上做的一件事（act = {id, by, path, body}），办完的结果推回中继给他。
        fetch_upload(id) -> (文件名, 字节) 取访客传上来的文件。只在开会时取。"""
        self._act_handler = handler
        if self.configured and not (self._act_thread and self._act_thread.is_alive()):
            self._act_thread = threading.Thread(target=self._act_loop, name="share-acts", daemon=True)
            self._act_thread.start()

    def fetch_upload(self, uid: str) -> tuple[str, bytes]:
        import httpx
        from urllib.parse import unquote
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=5.0), verify=_trust(), trust_env=False) as c:
            resp = c.get(f"{self.base.rstrip('/')}/push/{quote(room())}/upload/{quote(str(uid))}", headers=self._admin())
            resp.raise_for_status()
            return unquote(resp.headers.get("X-Bloom-Name") or "paper"), resp.content

    def _act_loop(self) -> None:
        import httpx
        client = httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), verify=_trust(), trust_env=False)
        while not self._stop.is_set():
            wait = 1.0
            try:
                if not self.live or self._act_handler is None or not self.armed:
                    wait = 2.0
                else:
                    resp = client.get(f"{self.base.rstrip('/')}/push/{quote(room())}/inbox", headers=self._admin())
                    resp.raise_for_status()
                    acts = resp.json().get("acts") or []
                    for a in acts:              # 导入论文要几十秒：每件事各开一个线程，别挡着后面的
                        threading.Thread(target=self._do_act, args=(a,), name="share-act", daemon=True).start()
                    wait = 0.4 if acts else 1.0
                self.act_error = None
            except Exception as ex:
                self.act_error = f"{type(ex).__name__}: {ex}"[:200]
                wait = 5.0
            self._stop.wait(wait)

    def _do_act(self, a: dict) -> None:
        try:
            res = self._act_handler(a, self.fetch_upload)
        except Exception as ex:
            res = {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:200]}
        try:
            body = json.dumps(res if isinstance(res, dict) else {"ok": bool(res)}, ensure_ascii=False).encode("utf-8")
            self._push("result/" + quote(str(a.get("id") or "")), body, "application/json")
        except Exception as ex:
            self.act_error = f"result: {type(ex).__name__}: {ex}"[:200]

    # ---------- 内部 ----------
    def _http_base(self) -> str:
        u = _env().get("LIVEKIT_URL") or self.base
        p = urlsplit(u)
        scheme = {"wss": "https", "ws": "http"}.get(p.scheme, p.scheme or "https")
        return f"{scheme}://{p.netloc}"

    def _list(self) -> list[dict]:
        import httpx
        if self.public:                                 # 公共中继：中继代问
            if self._client is None:
                self._client = httpx.Client(timeout=6.0, verify=_trust(), trust_env=False)
            resp = self._client.get(f"{self.base.rstrip('/')}/push/{quote(room())}/participants", headers=self._admin())
            resp.raise_for_status()
            return self._guests_of(resp.json().get("participants") or [])
        e = _env()
        r = room()
        tok = _jwt(e["LIVEKIT_API_KEY"], e["LIVEKIT_API_SECRET"], {"roomAdmin": True, "room": r, "roomList": True})
        if self._client is None:
            self._client = httpx.Client(timeout=6.0, verify=_trust(), trust_env=False)
        resp = self._client.post(self._http_base() + "/twirp/livekit.RoomService/ListParticipants",
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
                                 content=json.dumps({"room": r}))
        if resp.status_code == 404 or (resp.status_code >= 400 and "not found" in resp.text.lower()):
            return []                                   # 房间还没人进过：中继上还没有这个房间
        resp.raise_for_status()
        return self._guests_of(resp.json().get("participants") or [])

    def _guests_of(self, parts: list) -> list[dict]:
        """LiveKit 的 participants → 访客（主持人这边的两路不算）。"""
        out = []
        for p in parts:
            meta = {}
            try:
                meta = json.loads(p.get("metadata") or "{}")
            except ValueError:
                pass
            if meta.get("bloom_host"):
                continue                                # 我们自己（以后桌面端也进房间时）不算访客
            ident = str(p.get("identity") or p.get("sid") or "")
            out.append({"id": ident if ident.startswith("guest-") else "guest-" + ident, "name": p.get("name") or p.get("identity") or "访客",
                        "role": meta.get("role") or "guest", "joined": p.get("joined_at") or p.get("joinedAt")})
        return out

    GUEST_HOLD_S = 60.0          # 访客最后一次在房间里被看到之后，这么久内仍算在场

    def _poll_guests(self) -> None:
        """问一次中继谁在房间里，更新 self.guests。
        访客网络一抖，LiveKit 那边会短暂没了这个人（1、1、0、1……）：最后一次看到后 GUEST_HOLD_S 秒内仍算在场，
        免得桌面端头像闪、两人 / 三人的轮流说话规则来回切。中继问不到（异常）当成"没有新消息"：记下错误，
        已在场的人照样等满 60 秒才去掉。换了房间号（散会换号）就从头算。"""
        now = time.time()
        try:
            r = room()
        except Exception:                               # 读不了 share.json 也别让这个线程死掉
            r = self._seen_room
        if r != self._seen_room:
            self._seen, self._seen_room = {}, r
        try:
            for g in self._list():
                self._seen[g["id"]] = (g, now)          # 已有的人原地更新（顺序不变），新来的排在后面
            self.error = None
        except Exception as ex:                         # 中继连不上：邀请链接照样能给，在场的人先留着
            self.error = f"{type(ex).__name__}: {ex}"[:200]
        self._seen = {k: v for k, v in self._seen.items() if now - v[1] <= self.GUEST_HOLD_S}
        self.guests = [g for g, _ in self._seen.values()]
        self.checked = now

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.armed:
                self._poll_guests()
            elif self.guests:
                self.guests, self._seen = [], {}
            self._stop.wait(self.POLL_S)

    # ---------- 推给访客 ----------
    def _admin(self) -> dict:
        if self.public:
            d = self._device()
            return {"Authorization": f"Device {d['device']}:{d['secret']}"}
        e = _env()
        r = room()
        return {"Authorization": "Bearer " + _jwt(e["LIVEKIT_API_KEY"], e["LIVEKIT_API_SECRET"], {"roomAdmin": True, "room": r})}

    def _push(self, what: str, body: bytes, ctype: str) -> dict:
        import httpx
        if self._push_client is None:
            self._push_client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), verify=_trust(), trust_env=False)
        h = self._admin()
        h["Content-Type"] = ctype
        resp = self._push_client.post(f"{self.base.rstrip('/')}/push/{quote(room())}/{what}", headers=h, content=body)
        resp.raise_for_status()
        return resp.json()

    def _have(self) -> dict:
        import httpx
        if self._push_client is None:
            self._push_client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), verify=_trust(), trust_env=False)
        resp = self._push_client.get(f"{self.base.rstrip('/')}/push/{quote(room())}/have", headers=self._admin())
        resp.raise_for_status()
        return resp.json()

    _push_client = None

    _AV_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml"}

    def _avatar_file(self, name: str | None):
        """~/.zuhui/avatars 里的头像：<名字>.jpg/png/webp/svg（不分大小写），Claude 用 claude.svg，没有就 None。"""
        d = STATE / "avatars"
        if not name or not d.is_dir():
            return None
        want = str(name).strip().lower()
        for f in d.iterdir():
            if f.is_file() and f.stem.lower() == want and f.suffix.lower() in self._AV_TYPES:
                return f
        return None

    def _with_avatars(self, st: dict, sent: dict) -> dict:
        """给访客页的参会人配上和软件里一样的头像：图推到中继（av- 开头，散会也留着），状态里写上地址。"""
        st = dict(st)
        base = f"/view/{room()}/fig/"

        def push(f) -> str | None:
            try:
                data = f.read_bytes()
            except OSError:
                return None
            if not data or len(data) > 2_000_000:
                return None
            h = hashlib.sha1(data).hexdigest()
            name = "av-" + h[:16] + f.suffix.lower()
            if sent.get(name) != h:
                self._push("asset/" + quote(name), data, self._AV_TYPES[f.suffix.lower()])
                sent[name] = h
            return base + name

        people = []
        for p in st.get("people") or []:
            p = dict(p)
            if p.get("role") == "teacher":                # 主持人：设置里选的（me.*）优先，和软件里一样
                f = self._avatar_file("me") or self._avatar_file(p.get("name"))
            else:
                f = self._avatar_file("claude" if p.get("role") == "student" else p.get("name"))
            url = push(f) if f else None
            if url:
                p["avatar"] = url
            people.append(p)
        st["people"] = people
        if st.get("participants"):                        # v2：和主持人界面一样的参会人列表
            parts = []
            for p in st["participants"]:
                p = dict(p)
                if p.get("rank") == "host":
                    f = self._avatar_file("me") or self._avatar_file(p.get("name"))
                else:
                    f = self._avatar_file("claude" if p.get("id") == "claude" else p.get("name"))
                url = push(f) if f else None
                if url:
                    p["avatar"] = url
                parts.append(p)
            st["participants"] = parts
        dflt = self._avatar_file("bloom")                 # 链接进来的人默认用它（和软件里一样）
        url = push(dflt) if dflt else None
        if url:
            st["avatar_default"] = url
        return st

    def _upcoming(self) -> dict:
        """还没开的下一场：访客打开链接看到"未开始"（拿不到进会的 token），开会后自动变成能进。"""
        from .config import assistant_name, current_paper
        me = str((self.cfg.get("meeting") or {}).get("me") or "主持人")
        title = ((current_paper() or {}).get("name") or "组会").strip()
        return {"upcoming": True, "live": False, "title": title, "host": me, "paper": False,
                "people": [{"name": me, "role": "teacher"}, {"name": assistant_name(self.cfg), "role": "student"}]}

    RESUME_GRACE_S = 90.0         # 后端刚起来这么久内，"没在开会"不当成散会（多半是重启后还没接回会议）

    @staticmethod
    def _recording() -> bool:
        """口喷鸡还在录屏（这场会还在开，只是后端刚重启、还没接回来）。"""
        try:
            from .ears import running_record_job
            return running_record_job() is not None
        except Exception:
            return False

    def _push_loop(self) -> None:
        t_boot = time.time()
        last_state = None          # 上次推的会议信息（json 文本）
        last_key = None            # 上次推的论文版本
        last_paper = None          # 上次推的论文快照的哈希
        last_lines = None          # 上次推的逐字稿 + 任务板的版本
        sent: dict[str, str] = {}  # 已推的图：文件名 -> 哈希
        was_live = False
        next_check = 0.0
        upcoming_room, next_idle = None, 0.0
        while not self._stop.is_set():
            wait = 1.0
            if not self.armed:                                   # 公共中继没打开邀请：什么都不发
                self._stop.wait(2.0)
                continue
            try:
                state_fn, key_fn, paper_fn = self._feed
                st = state_fn()
                if st is None:                                   # 没在开会
                    if (not was_live and room_used()
                            and (time.time() - t_boot < self.RESUME_GRACE_S or self._recording())):
                        # 房间开过会、但不是这个进程里散的：后端刚重启、录屏还在跑，会马上接回来——
                        # 这时推 clear 会把访客踢出去、换掉房间号（他们只能等重发链接），所以什么都不动，等着
                        self._stop.wait(1.0)
                        continue
                    if was_live or room_used():                  # 散会（或上次散会时没收尾成，比如后端在散会后崩了）
                        self._push("clear", b"", "application/json")   # 中继：标成结束、踢人；失败就过几秒再来
                        new_room()                               # 下一场换新房间号：旧链接再也进不来
                        was_live, last_state, last_key, last_paper, sent, last_lines = False, None, None, None, {}, None
                        upcoming_room, next_idle = None, 0.0
                    if room() != upcoming_room or time.time() >= next_idle:   # 下一场的链接先在中继上登记成"未开始"
                        if room() != upcoming_room or not self._have().get("sv"):
                            body = json.dumps(self._with_avatars(self._upcoming(), sent), ensure_ascii=False, sort_keys=True)
                            self._push("state", body.encode("utf-8"), "application/json")
                        upcoming_room, next_idle = room(), time.time() + 60
                    wait = 2.0
                else:
                    if not was_live:
                        mark_used()
                    was_live = True
                    if time.time() >= next_check:                # 中继重启过（内存清空）就补推
                        have = self._have()
                        if not have.get("sv"):
                            last_state = None
                        if not have.get("lines"):
                            last_lines = None
                        if not have.get("paper"):
                            last_key, last_paper = None, None
                        names = set(have.get("assets") or [])
                        sent = {k: v for k, v in sent.items() if k in names}
                        next_check = time.time() + 20
                    key = key_fn()
                    if key != last_key:
                        html_text, assets = paper_fn()
                        for name, (ctype, data) in (assets or {}).items():
                            h = hashlib.sha1(data).hexdigest()
                            if sent.get(name) != h:
                                self._push("asset/" + quote(name), data, ctype)
                                sent[name] = h
                        ph = hashlib.sha1(html_text.encode("utf-8")).hexdigest()
                        if ph != last_paper:
                            self._push("paper", html_text.encode("utf-8"), "text/html; charset=utf-8")
                            last_paper = ph
                        last_key = key
                    st = self._with_avatars(st, sent)
                    # 逐字稿、任务板是大块，几秒才变一次；"Claude 在干什么"之类每秒都变。分开推：大块变了才推一份，
                    # 状态里只带它的版本号（lv），访客 lv 变了才去拉（两小时的会，逐字稿几百 KB，不再每秒重下）
                    if st.get("v", 0) >= 2:
                        heavy = {"transcript": st.pop("transcript", None), "board": st.pop("board", None)}
                        if "comments" in st:                     # 第 6 批：论文上的批注也是大块，跟逐字稿一起推
                            heavy["comments"] = st.pop("comments")
                        hb = json.dumps(heavy, ensure_ascii=False, sort_keys=True).encode("utf-8")
                        lv = hashlib.sha1(hb).hexdigest()[:12]
                        if lv != last_lines:
                            self._push("lines", hb, "application/json")
                            last_lines = lv
                        st["lv"] = lv
                    body = json.dumps(st, ensure_ascii=False, sort_keys=True)
                    if body != last_state:
                        self._push("state", body.encode("utf-8"), "application/json")
                        last_state = body
                    self.pushed = time.time()
                self.push_error = None
                self.live = was_live
            except Exception as ex:                              # 中继连不上：过几秒再试，开会不受影响
                self.push_error = f"{type(ex).__name__}: {ex}"[:200]
                next_check = 0.0
                wait = 5.0
            self._stop.wait(wait)
