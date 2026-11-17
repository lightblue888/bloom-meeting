#!/usr/bin/env python3
"""bloom 中继的小服务 (只监听 127.0.0.1:8787, Caddy 反代):

1. 发 LiveKit token:  GET /token?room=<id>&name=<名字>   只发给正在开的会 (403 否则), 身份一律 guest
   返回 {"token": <LiveKit access token>, "url": <wss 地址>, ...}
2. 访客看会 (论文 + 实时逐字稿):
   - Bloom 桌面端推:  POST /push/<room>/state       JSON: 标题、是否在开、参会人、字幕用的最近几句 (captions)
                                                     (整份逐字稿 lines / 半句 partial 推上来也丢掉; captions 只在开会时有,
                                                      散会就删, 也不写盘)
                      POST /push/<room>/paper       text/html: 论文只读快照 (修订对比页)
                      POST /push/<room>/asset/<名>  图片 (论文里的图), Content-Type 照传
                      POST /push/<room>/clear       散会: 论文和图删掉(头像 av-* 留着), 只留"已结束", 删 LiveKit 房间踢人
                      GET  /push/<room>/have        中继这边现在有什么 (重启后桌面端据此补推)
     鉴权: Authorization: Bearer <JWT HS256, 用 LIVEKIT_API_SECRET 签, iss=API_KEY, video.roomAdmin=true, video.room=<room>>
     (桌面端本来就有这对密钥, 不另设口令)。
   - 访客读:          GET /view/<room>/state?sv=<上次的版本>   没变就只回版本号
                      GET /view/<room>/paper
                      GET /view/<room>/fig/<名>
     知道房间号就能看 (房间号随机、猜不到; 和进会是同一个口令)。
   全放内存, 不落盘; 服务重启就清空, 桌面端会补推。
3. 访客在会议页上做事 (2026-11-17 起访客页和主持人用同一套会议界面):
   - 访客:            POST /view/<room>/act      {"path": 白名单里的接口, "body": {...}}   切论文 / 确认、撤回改动 / 改一段原文
                      POST /view/<room>/upload?name=<文件名>   原样的文件字节 (论文, <=50 MB)：加进这场会
                      GET  /view/<room>/result/<id>            主持人那边办完的结果 (没办完 {"done": false})
     鉴权: Authorization: Bearer <进会时发的 LiveKit token>，验签、房间对得上、没过期; 谁做的 (by) 取 token 里的名字。
     谁能做什么由主持人那边 (Bloom) 按共同主持人名单定, 中继只认人、排队。
   - Bloom 桌面端取:  GET  /push/<room>/inbox          取走排着的事 (取了就清)
                      GET  /push/<room>/upload/<id>    取走访客传的文件 (取了就删)
                      POST /push/<room>/result/<id>    办完的结果 (JSON), 访客来取, 2 分钟后删
   都只在内存里, 不落盘。
4. 访客诊断:            POST /view/<room>/diag   访客页报语音断开 / 重连 / 切后台这些事 (sendBeacon, JSON, <=1 KB)
   每条在 journal 里记一行 "diag room=.. id=.. ev=.. why=.. ua=..", 按 IP 限速, 不记 token。
   看:  sudo journalctl -u bloom-token -o short-iso --since today | grep ' diag'
密钥从 /opt/bloom/livekit.env 读 (LIVEKIT_API_KEY / LIVEKIT_API_SECRET / LIVEKIT_URL)。
"""
import base64, json, os, re, sys, threading, time, uuid, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import jwt  # python3-jwt (PyJWT)

ENV_FILE = os.environ.get("BLOOM_ENV") or "/opt/bloom/livekit.env"   # 本机测试可以换一份
ROLES = ("teacher", "student", "guest")
TTL = 6 * 3600  # token 有效 6 小时

ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
ASSET_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
HASHED_RE = re.compile(r"(?:-[0-9a-f]{10}\.[A-Za-z0-9]+$|^av-[0-9a-f]{16}\.)")   # 名字里带内容哈希的图：可以长期缓存
MAX_BODY = 25 * 1024 * 1024          # 一次推送最多 25 MB (论文页 + 公式)
MAX_UPLOAD = 50 * 1024 * 1024       # 访客传一份论文最多 50 MB
UP_EXT = (".pdf", ".docx", ".md", ".markdown", ".txt", ".tex")
ACT_PATHS = ("/api/paper/switch", "/api/board/resolve", "/api/board/reject", "/api/para/source", "/api/para/save")
MAX_INBOX = 100
MAX_UPLOADS = 4                       # 一个房间同时排着的上传最多几份 (主持人那边每秒取走)
RESULT_TTL = 120
UPLOAD_TTL = 600
MAX_ROOMS = 100                       # 最多记这么多个房间, 超了丢最久没动的 (丢了的旧链接就显示"链接无效")
MAX_ASSETS = 80
HOST_STALE = 120                      # 主持人那边这么久没联系 (推送 / have 每 20 秒一次) 就当这场会已经结束
LK_HTTP = "http://127.0.0.1:7880"     # 本机 LiveKit 的 HTTP 口 (散会时删房间, 把还在里面的人请出去)
ASSET_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "text/html"}
STORE = os.path.join(os.environ.get("STATE_DIRECTORY") or "/var/lib/bloom", "rooms.json")   # systemd StateDirectory=bloom
DIAG_MAX = 1024                       # 访客诊断一条最多 1 KB
DIAG_PER_IP = 60                      # 每个 IP 每分钟最多记 60 条 (同一个 Wi-Fi 下几个人共用一个 IP, 够用)
DIAG_ALL = 600                        # 所有访客加起来每分钟最多记 600 条, 防有人刷日志

env = {}
with open(ENV_FILE, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
API_KEY, API_SECRET = env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"]
WS_URL = env.get("LIVEKIT_URL", "")

LOCK = threading.Lock()
NO_DISK = ("captions", "transcript", "partial", "board", "brain", "focus")   # 会议内容只在内存里: 中继重启就没了, 桌面端补推
DIRTY = threading.Event()
ROOMS = {}   # room -> {"state": dict|None, "sv": int, "paper": bytes|None, "pv": int, "assets": {name: (ctype, bytes)}, "touched": float}


def make_token(room, name, role):
    now = int(time.time())
    claims = {
        "iss": API_KEY,
        "sub": name,            # identity
        "nbf": now - 10,
        "exp": now + TTL,
        "jti": uuid.uuid4().hex,
        "name": name,
        "metadata": json.dumps({"role": role}, ensure_ascii=False),
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    return jwt.encode(claims, API_SECRET, algorithm="HS256")


def _sweep(r):
    """过期的结果、没人取的上传删掉 (调用方持锁)。"""
    now = time.time()
    r["results"] = {k: v for k, v in r.get("results", {}).items() if now - v[1] < RESULT_TTL}
    r["uploads"] = {k: v for k, v in r.get("uploads", {}).items() if now - v[3] < UPLOAD_TTL}


def _room(room, create=False):
    r = ROOMS.get(room)
    if r is None and create:
        if len(ROOMS) >= MAX_ROOMS:
            oldest = min(ROOMS, key=lambda k: ROOMS[k]["touched"])
            ROOMS.pop(oldest, None)
        r = ROOMS[room] = {"state": None, "sv": 0, "paper": None, "pv": 0, "assets": {}, "touched": time.time(), "host_seen": 0.0,
                           "inbox": [], "results": {}, "uploads": {}}
    if r is not None:
        r["touched"] = time.time()
    return r


def _load():
    """重启后接着认得这些房间: 开过/结束了/下一场。只存状态和头像, 论文和图不存 (散会就删了)。"""
    try:
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for room, d in (data or {}).items():
        if not ROOM_RE.match(room) or not isinstance(d, dict):
            continue
        av = {}
        for name, (ctype, b64) in (d.get("av") or {}).items():
            try:
                av[name] = (ctype, base64.b64decode(b64))
            except Exception:
                pass
        ROOMS[room] = {"state": d.get("state"), "sv": int(d.get("sv") or 1), "paper": None, "pv": 0, "assets": av,
                       "touched": float(d.get("touched") or time.time()), "host_seen": float(d.get("host_seen") or 0),
                       "inbox": [], "results": {}, "uploads": {}}


def _saver():
    """有变动就 2 秒内写一次盘 (原子替换)。"""
    while True:
        DIRTY.wait()
        time.sleep(2)
        DIRTY.clear()
        with LOCK:
            data = {room: {"state": {k: v for k, v in r["state"].items() if k not in NO_DISK}, "sv": r["sv"], "touched": r["touched"], "host_seen": r.get("host_seen", 0),   # 字幕不落盘
                           "av": {k: [c, base64.b64encode(b).decode()] for k, (c, b) in r["assets"].items() if k.startswith("av-")}}
                    for room, r in ROOMS.items() if r["state"] is not None}
        try:
            tmp = STORE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, STORE)
        except OSError as ex:
            print(f"save rooms: {ex}", file=sys.stderr)


def _end_state(r):
    """把这场标成结束 (状态里 live=False + 结束时间)。调用方持锁。"""
    st = dict(r["state"] or {})
    if st.get("live") is False and st.get("ended"):
        return
    st.pop("captions", None)              # 字幕只在开会时给: 散会了旧链接看不到说过的话
    st.update({"live": False, "partial": None, "pushed_at": time.time(),
               "ended": st.get("ended") or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())})
    r["state"] = st
    r["sv"] += 1
    DIRTY.set()


def _live(r):
    """正在开会: 主持人推的状态是 live, 而且最近 HOST_STALE 秒内联系过。过期的顺手标成结束。调用方持锁。"""
    if not r or not (r["state"] or {}).get("live"):
        return False
    if time.time() - r.get("host_seen", 0) > HOST_STALE:
        _end_state(r)
        r["paper"], r["assets"] = None, {k: v for k, v in r["assets"].items() if k.startswith("av-")}
        r["pv"] += 1
        return False
    return True


def _kick(room):
    """散会: 删掉 LiveKit 上的这个房间, 还连着的人都会被断开。失败不要紧 (房间空了 LiveKit 自己也会收)。"""
    def run():
        try:
            import urllib.request
            now = int(time.time())
            tok = jwt.encode({"iss": API_KEY, "sub": "bloom-relay", "nbf": now - 10, "exp": now + 60,
                              "video": {"roomCreate": True, "roomAdmin": True, "room": room}}, API_SECRET, algorithm="HS256")
            req = urllib.request.Request(LK_HTTP + "/twirp/livekit.RoomService/DeleteRoom", data=json.dumps({"room": room}).encode(),
                                         headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as ex:
            print(f"kick {room}: {ex}", file=sys.stderr)
    threading.Thread(target=run, daemon=True).start()


# ---------- 访客诊断 ----------
DIAG_LOCK = threading.Lock()
DIAG_IP = {}                          # ip -> [这一分钟从几时算起, 条数]
DIAG_WIN = [0.0, 0]                   # 全局: [这一分钟从几时算起, 条数]
_JWTISH = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*)?")
_CTRL = re.compile(r"[\x00-\x1f\x7f  ]")


def _log_line(line):
    """记一行到 journal (stdout 一定按 UTF-8 写, 名字是中文也不出错)。"""
    try:
        sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
        sys.stdout.flush()
    except Exception:
        pass


def _diag_allow(ip):
    """限速: True 记; "first" 这个 IP 这一分钟刚超 (记一行说后面的不记了); False 不记。"""
    now = time.time()
    with DIAG_LOCK:
        if now - DIAG_WIN[0] >= 60:
            DIAG_WIN[0], DIAG_WIN[1] = now, 0
        if len(DIAG_IP) > 5000:                     # 记的 IP 太多: 过期的先扔, 还多就全清
            for k in [k for k, v in DIAG_IP.items() if now - v[0] >= 60]:
                DIAG_IP.pop(k, None)
            if len(DIAG_IP) > 5000:
                DIAG_IP.clear()
        w = DIAG_IP.get(ip)
        if w is None or now - w[0] >= 60:
            w = DIAG_IP[ip] = [now, 0]
        w[1] += 1
        if w[1] > DIAG_PER_IP or DIAG_WIN[1] >= DIAG_ALL:
            return "first" if w[1] == DIAG_PER_IP + 1 else False
        DIAG_WIN[1] += 1
        return True


def _clean(v, n):
    """日志里的一个值: 去控制字符、像 token 的一律抹掉、引号换成单引号、截短。"""
    if v is None or isinstance(v, (dict, list)):
        return ""
    s = _CTRL.sub(" ", str(v))
    s = _JWTISH.sub("[jwt]", s).replace('"', "'").strip()
    return s[:n]


def _q(v):
    return '"%s"' % v if (" " in v or "=" in v) else v


_APPS = ((r"wxwork/([\d.]+)", "WeCom"), (r"MicroMessenger/([\d.]+)", "WeChat"), (r"\bQQ/([\d.]+)", "QQ"),
         (r"DingTalk/([\d.]+)", "DingTalk"), (r"(?:Lark|Feishu)/([\d.]+)", "Feishu"), (r"Weibo(?:/|__)?([\d.]*)", "Weibo"),
         (r"MQQBrowser/([\d.]+)", "QQBrowser"), (r"EdgiOS/([\d.]+)", "Edge"), (r"EdgA?/([\d.]+)", "Edge"),
         (r"CriOS/([\d.]+)", "Chrome"), (r"FxiOS/([\d.]+)", "Firefox"), (r"Firefox/([\d.]+)", "Firefox"),
         (r"Chrome/([\d.]+)", "Chrome"), (r"Version/([\d.]+).*Safari/", "Safari"))


def _ua_short(ua):
    """UA 缩成 "iOS17.5/WeChat8.0.50/wk" 这样: 系统、App/浏览器、是不是 App 里的网页 (wk = iOS WKWebView, wv = 安卓 WebView)。"""
    ua = ua or ""
    m = re.search(r"(?:iPhone|CPU) OS (\d+)[_.](\d+)", ua)
    if m:
        osn = "iOS%s.%s" % m.groups()
    elif "iPad" in ua:
        osn = "iPadOS"
    else:
        m = re.search(r"Android (\d+(?:\.\d+)?)", ua)
        osn = ("Android" + m.group(1)) if m else "Win" if "Windows" in ua else "Mac" if "Macintosh" in ua else "Linux" if "Linux" in ua else "?"
    app = ""
    for pat, name in _APPS:
        m = re.search(pat, ua)
        if m:
            app = name + ".".join((m.group(1) or "").split(".")[:3])
            break
    out = [osn, app] if app else [osn]
    if re.search(r"iPhone|iPad|iPod", ua) and "Safari/" not in ua:
        out.append("wk")
    elif "; wv)" in ua:
        out.append("wv")
    return "/".join(out)[:48]


class Handler(BaseHTTPRequestHandler):
    server_version = "bloom-relay/0.2"

    def _send(self, code, body, ctype, extra=None, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   {"Access-Control-Allow-Origin": "*"})

    def _authed(self, room):
        h = self.headers.get("Authorization") or ""
        if not h.startswith("Bearer "):
            return False
        try:
            c = jwt.decode(h[7:], API_SECRET, algorithms=["HS256"], options={"verify_aud": False})
        except Exception:
            return False
        v = c.get("video") or {}
        return c.get("iss") == API_KEY and bool(v.get("roomAdmin")) and v.get("room") == room

    def _guest(self, room):
        """访客的 token: 验签、房间对得上、没过期 (PyJWT 自己查 exp)。返回 token 里的名字, 不对返回 None。"""
        h = self.headers.get("Authorization") or ""
        if not h.startswith("Bearer "):
            return None
        try:
            c = jwt.decode(h[7:], API_SECRET, algorithms=["HS256"], options={"verify_aud": False})
        except Exception:
            return None
        v = c.get("video") or {}
        if c.get("iss") != API_KEY or v.get("room") != room or not v.get("roomJoin"):
            return None
        return str(c.get("name") or c.get("sub") or "").strip() or None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_BODY:
            return None
        return self.rfile.read(n) if n else b""

    # ---------- GET ----------
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        if u.path == "/healthz":
            return self._json(200, {"ok": True})
        if u.path == "/token":
            return self._token(urllib.parse.parse_qs(u.query))
        if len(parts) == 4 and parts[0] == "view" and parts[2] == "result" and ROOM_RE.match(parts[1]):
            if not self._guest(parts[1]):
                return self._json(401, {"error": "unauthorized"})
            with LOCK:
                r = _room(parts[1])
                if r:
                    _sweep(r)
                got = (r or {}).get("results", {}).get(parts[3])
            return self._json(200, {"done": True, "result": got[0]} if got else {"done": False})
        if len(parts) >= 3 and parts[0] == "view" and ROOM_RE.match(parts[1]):
            return self._view(parts[1], parts[2:], urllib.parse.parse_qs(u.query))
        if len(parts) == 3 and parts[0] == "push" and parts[2] == "inbox" and ROOM_RE.match(parts[1]):
            if not self._authed(parts[1]):
                return self._json(401, {"error": "unauthorized"})
            with LOCK:
                r = _room(parts[1])
                acts = list((r or {}).get("inbox") or [])
                if r:
                    r["inbox"] = []
                    r["host_seen"] = time.time()
            return self._json(200, {"acts": acts})
        if len(parts) == 4 and parts[0] == "push" and parts[2] == "upload" and ROOM_RE.match(parts[1]):
            if not self._authed(parts[1]):
                return self._json(401, {"error": "unauthorized"})
            with LOCK:
                r = _room(parts[1])
                up = (r or {}).get("uploads", {}).pop(parts[3], None)
            if not up:
                return self._json(404, {"error": "no such upload"})
            name, data, by, _t = up
            return self._send(200, data, "application/octet-stream",
                              {"X-Bloom-Name": urllib.parse.quote(name), "X-Bloom-By": urllib.parse.quote(by)})
        if len(parts) == 3 and parts[0] == "push" and parts[2] == "have" and ROOM_RE.match(parts[1]):
            if not self._authed(parts[1]):
                return self._json(401, {"error": "unauthorized"})
            with LOCK:
                r = _room(parts[1])
                if r is not None:
                    r["host_seen"] = time.time()
                have = {"sv": r["sv"] if r else 0, "pv": r["pv"] if r else 0,
                        "paper": bool(r and r["paper"]), "assets": sorted((r or {}).get("assets", {}).keys())}
            return self._json(200, have)
        return self._json(404, {"error": "not found"})

    def _token(self, q):
        room = q.get("room", [""])[0].strip()
        name = q.get("name", [""])[0].strip()
        role = q.get("role", ["guest"])[0].strip().lower()
        if not room or not name:
            return self._json(400, {"error": "room 和 name 必填"})
        if len(room) > 64 or len(name) > 64:
            return self._json(400, {"error": "room/name 太长 (<=64)"})
        if not ROOM_RE.match(room):
            return self._json(400, {"error": "房间号不对"})
        with LOCK:
            live = _live(_room(room))
        if not live:                               # 只有正在开的会能进: 旧链接 / 还没开的会都拿不到 token
            return self._json(403, {"error": "这场会没在开，进不了"})
        role = "guest"                             # 身份由主持人在 Bloom 里定, 链接里写什么都不算
        token = make_token(room, name, role)
        return self._json(200, {"token": token, "url": WS_URL, "room": room, "identity": name, "role": role, "ttl": TTL})

    def _view(self, room, rest, q):
        with LOCK:
            r = _room(room)
            what = rest[0]
            if what == "state":
                if r:
                    _live(r)                       # 主持人掉线太久: 当成已结束
                if not r or r["state"] is None:
                    return self._json(200, {"sv": 0, "pv": 0, "state": None})
                try:
                    since = int(q.get("sv", ["-1"])[0])
                except ValueError:
                    since = -1
                if since == r["sv"]:
                    return self._json(200, {"sv": r["sv"], "pv": r["pv"], "same": True})
                return self._json(200, {"sv": r["sv"], "pv": r["pv"], "state": r["state"]})
            if what == "paper":
                if not r or not r["paper"]:
                    return self._send(404, "论文还没推上来".encode("utf-8"), "text/plain; charset=utf-8")
                return self._send(200, r["paper"], "text/html; charset=utf-8")
            if what == "fig" and len(rest) == 2 and r:
                a = r["assets"].get(rest[1])
                if a:
                    ctype, data = a
                    extra = {"Content-Security-Policy": "sandbox"} if ctype == "text/html" else None
                    # 名字里带内容哈希的图（桌面端 view._guest_asset_name：fig1-3f2a9c1b7d.png；头像 av-<哈希>）：同名就是同一份，
                    # 手机缓存一天，来回切论文、每次改稿换新版不用再下一遍；别的照旧不缓存
                    hashed = bool(HASHED_RE.search(rest[1]))
                    return self._send(200, data, ctype, extra, cache="private, max-age=86400, immutable" if hashed else "no-store")
        return self._json(404, {"error": "not found"})

    # ---------- POST ----------
    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        if len(parts) == 3 and parts[0] == "view" and parts[2] == "diag" and ROOM_RE.match(parts[1]):
            return self._diag(parts[1])
        if len(parts) == 3 and parts[0] == "view" and parts[2] in ("act", "upload") and ROOM_RE.match(parts[1]):
            return self._guest_act(parts[1], parts[2], urllib.parse.parse_qs(u.query))
        if len(parts) == 4 and parts[0] == "push" and parts[2] == "result" and ROOM_RE.match(parts[1]):
            if not self._authed(parts[1]):
                return self._json(401, {"error": "unauthorized"})
            body = self._body()
            try:
                obj = json.loads((body or b"{}").decode("utf-8"))
            except ValueError:
                return self._json(400, {"error": "bad json"})
            with LOCK:
                r = _room(parts[1])
                if r is None:
                    return self._json(404, {"error": "no room"})
                _sweep(r)
                r["results"][parts[3][:64]] = (obj, time.time())
            return self._json(200, {"ok": True})
        if len(parts) < 3 or parts[0] != "push" or not ROOM_RE.match(parts[1]):
            return self._json(404, {"error": "not found"})
        room, what = parts[1], parts[2]
        if not self._authed(room):
            return self._json(401, {"error": "unauthorized"})
        body = self._body()
        if body is None:
            return self._json(413, {"error": "too large"})
        with LOCK:
            r = _room(room, create=True)
            r["host_seen"] = time.time()
            if what == "state":
                try:
                    st = json.loads(body.decode("utf-8"))
                except ValueError:
                    return self._json(400, {"error": "bad json"})
                if not isinstance(st, dict):
                    return self._json(400, {"error": "bad json"})
                st.pop("lines", None)                  # 旧字段；新版逐字稿在 transcript 里 (访客和主持人看到的一样)
                st["pushed_at"] = time.time()
                r["state"] = st
                r["sv"] += 1
                DIRTY.set()
                return self._json(200, {"ok": True, "sv": r["sv"], "pv": r["pv"]})
            if what == "paper":
                r["paper"] = body
                r["pv"] += 1
                r["sv"] += 1                       # 访客按 sv 轮询, 顺带看到 pv 变了
                return self._json(200, {"ok": True, "sv": r["sv"], "pv": r["pv"]})
            if what == "asset" and len(parts) == 4 and ASSET_RE.match(parts[3]):
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in ASSET_TYPES:
                    return self._json(415, {"error": "type"})
                if parts[3] not in r["assets"] and len(r["assets"]) >= MAX_ASSETS:
                    return self._json(413, {"error": "too many assets"})
                r["assets"][parts[3]] = (ctype, body)
                if parts[3].startswith("av-"):
                    DIRTY.set()
                return self._json(200, {"ok": True})
            if what == "clear":
                r["inbox"], r["results"], r["uploads"] = [], {}, {}
                r["paper"] = None
                r["assets"] = {k: v for k, v in r["assets"].items() if k.startswith("av-")}   # 头像留着，散会页也要用
                r["pv"] += 1
                _end_state(r)
                _kick(room)                        # 还在房间里的人请出去; 这个房间号以后不再用
                return self._json(200, {"ok": True, "sv": r["sv"], "pv": r["pv"]})
        return self._json(404, {"error": "not found"})

    def _guest_act(self, room, what, q):
        """访客在会议页上做的一件事: 排进 inbox 等主持人那边取。上传的文件先存着, inbox 里放一条 upload。"""
        by = self._guest(room)
        if not by:
            return self._json(401, {"error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        limit = MAX_UPLOAD if what == "upload" else 256 * 1024
        if n < 0 or n > limit:
            return self._json(413, {"error": "too large"})
        body = self.rfile.read(n) if n else b""
        aid = uuid.uuid4().hex[:16]
        if what == "upload":
            name = os.path.basename(str(q.get("name", [""])[0]).replace("\\", "/")).strip()[:120]
            if not name or not name.lower().endswith(UP_EXT) or not body:
                return self._json(415, {"error": "只收 PDF、Word、Markdown、LaTeX、纯文本"})
            act = {"id": aid, "by": by, "path": "upload", "body": {"upload": aid, "name": name, "size": len(body)}, "at": time.time()}
        else:
            try:
                obj = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            path = str(obj.get("path") or "")
            if path not in ACT_PATHS or not isinstance(obj.get("body", {}), dict):
                return self._json(403, {"error": "这个做不了"})
            act = {"id": aid, "by": by, "path": path, "body": obj.get("body") or {}, "at": time.time()}
        with LOCK:
            r = _room(room)
            if not _live(r):
                return self._json(409, {"error": "会议没在开"})
            _sweep(r)
            if what == "upload":
                if len(r["uploads"]) >= MAX_UPLOADS:
                    return self._json(429, {"error": "传得太快了, 稍等再传"})
                r["uploads"][aid] = (act["body"]["name"], body, by, time.time())
            if len(r["inbox"]) >= MAX_INBOX:
                return self._json(429, {"error": "太快了"})
            r["inbox"].append(act)
        _log_line(f"act room={room} by={_q(_clean(by, 40))} path={act['path']} id={aid}")
        return self._json(200, {"ok": True, "id": aid})

    def _diag(self, room):
        """访客页报上来的一件事 (断开、重连、切后台...) 记一行。只认已知房间, 按 IP 限速, 只挑白名单字段记。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > DIAG_MAX:
            return self._send(413, b"", "text/plain")
        body = self.rfile.read(n) if n else b""
        done = lambda: self._send(204, b"", "text/plain")     # 不管记没记都回 204 (sendBeacon 也不看)
        with LOCK:
            known = room in ROOMS
        if not known:
            return done()
        fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()   # Caddy 填的真实访客 IP
        ok = _diag_allow(fwd or self.client_address[0])
        if ok is not True:
            if ok == "first":
                _log_line("diag-drop room=%s 同一个 IP 这一分钟报得太多, 后面的先不记" % room)
            return done()
        try:
            d = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            d = None
        if not isinstance(d, dict):
            return done()
        ev = re.sub(r"[^A-Za-z0-9_.:-]", "", str(d.get("ev") or ""))[:32] or "?"
        hdr_ua = self.headers.get("User-Agent") or ""
        kv = [("room", room), ("id", _clean(d.get("id"), 32)), ("ev", ev), ("why", _clean(d.get("why"), 120)),
              ("n", _clean(d.get("n"), 4)), ("vis", _clean(d.get("vis"), 8)), ("st", _clean(d.get("st"), 20)),
              ("t", _clean(d.get("t"), 8)), ("ua", _ua_short(hdr_ua or str(d.get("ua") or "")))]
        if ev == "join":                               # 进会那一下: 整条 UA、SDK 版本、是不是 App 里打开的
            kv += [("lk", _clean(d.get("lk"), 16)), ("app", _clean(d.get("app"), 16)),
                   ("ua_full", _clean(d.get("ua") or hdr_ua, 300))]
        _log_line("diag " + " ".join("%s=%s" % (k, _q(v)) for k, v in kv if v not in ("", None)))
        return done()

    def log_message(self, fmt, *args):
        # 访客轮询很频繁, 只记出错的
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    _load()
    threading.Thread(target=_saver, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("BLOOM_PORT") or 8787)), Handler)
    srv.daemon_threads = True
    print("bloom relay on 127.0.0.1:8787", flush=True)
    srv.serve_forever()
