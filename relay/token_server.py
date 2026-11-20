#!/usr/bin/env python3
"""bloom 中继的小服务 (只监听 127.0.0.1:8787, Caddy 反代):

1. 发 LiveKit token:  GET /token?room=<id>&name=<名字>[&claim=<钥匙>]   只发给正在开的会 (403 否则), 身份一律 guest
   返回 {"token": <LiveKit access token>, "url": <wss 地址>, "claim": <钥匙>, ...}
   名字一场会里归第一个用它的浏览器: 以后再用这个名字要带上当时给的钥匙, 别人用 409 name_taken (共同主持人按名字定,
   不能让人顶替); 主持人、AI 的名字和 bloom-host / bloom-student 访客都不能用。
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
   放内存里; 会议名、参会人、头像另存一份到 rooms.json (STATE_DIRECTORY), 服务重启后读回来。论文、逐字稿、字幕、任务板只在内存里 (NO_DISK), 重启就没了, 桌面端补推。
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
5. 公共中继 (livekit.env 里 BLOOM_PUBLIC=1 才开; 自己搭的中继不开, 上面的照旧):
   - 每台 Bloom 第一次用时领通行码: POST /dev/register -> {"device", "secret", "ws"}   (按 IP 限速)
   - 之后主持人那边的 /push/... 都用 Authorization: Device <device>:<secret> (电脑上没有 LiveKit 密钥)。
     房间谁先用就归谁 (存在 rooms.json 的 owner), 别的电脑碰不了; 一台电脑最多占 PUB_ROOMS_PER_DEV 个房间。
   - 中继代办要密钥的两件事: GET  /push/<room>/participants  谁在房间里 (LiveKit ListParticipants)
                              POST /push/<room>/join          {"identity": bloom-host|bloom-student, "name", "role"} -> 进语音房间的 token
   - 上限: 同时在开的会 BLOOM_MAX_LIVE, 一场会的人数 BLOOM_MAX_GUESTS, 一次推送 12 MB。
4. 访客诊断:            POST /view/<room>/diag   访客页报语音断开 / 重连 / 切后台这些事 (sendBeacon, JSON, <=1 KB)
   每条在 journal 里记一行 "diag room=.. id=.. ev=.. why=.. ua=..", 按 IP 限速, 不记 token。
   看:  sudo journalctl -u bloom-token -o short-iso --since today | grep ' diag'
6. GET /healthz -> {"ok": true, "public": .., "live": 在开的会有几场}   自动更新 (bloom_update.py) 看 live, 有会在开就不重启 (见 _live_count)
密钥从 /opt/bloom/livekit.env 读 (LIVEKIT_API_KEY / LIVEKIT_API_SECRET / LIVEKIT_URL)。
"""
import base64, hashlib, hmac, json, os, re, secrets, sys, threading, time, uuid, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import jwt  # python3-jwt (PyJWT)

ENV_FILE = os.environ.get("BLOOM_ENV") or "/opt/bloom/livekit.env"   # 本机测试可以换一份
TTL = 6 * 3600  # token 有效 6 小时

ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
ASSET_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
HASHED_RE = re.compile(r"(?:-[0-9a-f]{10}\.[A-Za-z0-9]+$|^av-[0-9a-f]{16}\.)")   # 名字里带内容哈希的图：可以长期缓存
MAX_BODY = 25 * 1024 * 1024          # 一次推送最多 25 MB (论文页 + 公式)
MAX_UPLOAD = 50 * 1024 * 1024       # 访客传一份论文最多 50 MB
UP_EXT = (".pdf", ".docx", ".pptx", ".md", ".markdown", ".txt", ".tex")
ACT_PATHS = ("/api/paper/switch", "/api/board/resolve", "/api/board/reject", "/api/para/source", "/api/para/save",
             # 第 3 批: 打字问 AI、标记、先选中再说 (主持人那边再按人定一遍, zuhui/live_api.py)
             "/api/ask", "/api/bookmark", "/api/anchor",
             # 第 6 批: 论文上打字批注、回复、解决 (谁都能; 删只有共同主持人, 主持人那边查, zuhui/comments.py)
             "/api/comment")
# 只有共同主持人能做的: 中继先按主持人推来的参会人名单挡一道 (rank = cohost), 主持人那边还会再查
COHOST_PATHS = ("/api/ask", "/api/anchor")
MAX_INBOX = 100
MAX_UPLOADS = 4                       # 一个房间同时排着的上传最多几份 (主持人那边每秒取走)
RESULT_TTL = 120
UPLOAD_TTL = 600
MAX_RESULTS = 200                     # 一个房间同时留着的结果最多几份 (2 分钟就删)
MAX_RESULT_BODY = 1024 * 1024         # 一份结果最多 1 MB (都是小 JSON)
MAX_ROOMS = 100                       # 最多记这么多个房间, 超了丢最久没动的、没在开的 (丢了的旧链接就显示"链接无效"); 在开的会不丢
CLAIMS_MAX = 2000                     # 一个房间最多认领这么多个名字 (见 _claim)
CLAIMS_PER_IP = 20                    # 一个 IP 在一个房间里最多认领这么多个新名字 (同一个 Wi-Fi 下几个人共用一个 IP, 够用)
INFLIGHT_MAX = 300 * 1024 * 1024      # 同时在收的请求体加起来最多这么多 (几十个人一起传 50 MB 也撑不爆内存)
MAX_ASSETS = 80
HOST_STALE = 120                      # 主持人那边这么久没联系 (推送 / have 每 20 秒一次) 就当这场会已经结束
LIVE_WINDOW = 15 * 60                 # /healthz 的 live 只算这么久内主持人那边联系过的会 (见 _live_count)
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

# ---------- 公共中继 (BLOOM_PUBLIC=1) ----------
PUBLIC = str(env.get("BLOOM_PUBLIC") or os.environ.get("BLOOM_PUBLIC") or "").strip().lower() in ("1", "true", "yes")
PUB_MAX_LIVE = int(env.get("BLOOM_MAX_LIVE") or 40)          # 同时在开 (推了 live) 的会
PUB_MAX_GUESTS = int(env.get("BLOOM_MAX_GUESTS") or 12)      # 一场会里最多几个人 (含主持人那边的两路)
PUB_MAX_BODY = 12 * 1024 * 1024                              # 通行码的推送一次最多 12 MB
PUB_ROOMS_PER_DEV = 4                                        # 一台电脑最多占几个房间 (换房间号会占新的)
PUB_REG_PER_IP = 10                                          # 一个 IP 一天最多领几个通行码
PUB_REG_ALL = 300                                            # 所有人一小时最多领几个
PUB_MAX_ROOMS = int(env.get("BLOOM_MAX_ROOMS") or 1000)      # 公共中继最多记这么多个房间
PUB_ROOM_BYTES = int(env.get("BLOOM_ROOM_BYTES") or 160 * 1024 * 1024)    # 一个房间在内存里最多占这么多 (论文、图、逐字稿、上传、结果)
PUB_ALL_BYTES = int(env.get("BLOOM_MAX_BYTES") or 1536 * 1024 * 1024)     # 所有房间加起来最多这么多, 满了新推的回 503
if PUBLIC:
    MAX_ROOMS = PUB_MAX_ROOMS
CLAIMS_PER_IP = int(env.get("BLOOM_CLAIMS_PER_IP") or CLAIMS_PER_IP)
DEV_STORE = os.path.join(os.path.dirname(STORE), "devices.json")
OWNER_STORE = os.path.join(os.path.dirname(STORE), "owners.json")
OWNERS = {}                                                  # room -> device: 房间从内存里丢掉以后也还是它的, 别人领不走
OWNERS_MAX = 50000
DEVICES = {}                                                 # device -> {"h": sha256(secret), "at": 领的时间}
DEV_LOCK = threading.Lock()
REG_IP = {}                                                  # ip -> [这一天从几时算起, 个数]
REG_ALL = [0.0, 0]
HOST_IDS = ("bloom-host", "bloom-student")


def _dev_load():
    try:
        with open(DEV_STORE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            DEVICES.update({k: v for k, v in d.items() if isinstance(v, dict) and v.get("h")})
    except (OSError, ValueError):
        pass


def _dev_save():
    try:
        tmp = DEV_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DEVICES, f)
        os.replace(tmp, DEV_STORE)
    except OSError as ex:
        print(f"save devices: {ex}", file=sys.stderr)


def _owners_load():
    try:
        with open(OWNER_STORE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            OWNERS.update({k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)})
    except (OSError, ValueError):
        pass


def _owner_keep(room, dev):
    """房间从内存里丢掉时记下它是谁的 (调用方持锁)。记太多了扔最早的。"""
    if not dev:
        return
    OWNERS.pop(room, None)
    OWNERS[room] = dev
    while len(OWNERS) > OWNERS_MAX:
        OWNERS.pop(next(iter(OWNERS)))


def _reg_allow(ip):
    now = time.time()
    with DEV_LOCK:
        if now - REG_ALL[0] >= 3600:
            REG_ALL[0], REG_ALL[1] = now, 0
        w = REG_IP.get(ip)
        if w is None or now - w[0] >= 86400:
            w = REG_IP[ip] = [now, 0]
        if w[1] >= PUB_REG_PER_IP or REG_ALL[1] >= PUB_REG_ALL:
            return False
        w[1] += 1
        REG_ALL[1] += 1
        if len(REG_IP) > 20000:
            REG_IP.clear()
        return True


def _lk_call(method, body, video):
    """本机 LiveKit 的管理接口 (twirp), 用中继自己的密钥签。"""
    import urllib.request
    now = int(time.time())
    tok = jwt.encode({"iss": API_KEY, "sub": "bloom-relay", "nbf": now - 10, "exp": now + 60, "video": video},
                     API_SECRET, algorithm="HS256")
    req = urllib.request.Request(LK_HTTP + "/twirp/livekit.RoomService/" + method, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=6) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _participants(room):
    try:
        return _lk_call("ListParticipants", {"room": room}, {"roomAdmin": True, "room": room}).get("participants") or []
    except Exception as ex:
        if "404" in str(ex) or "not found" in str(ex).lower():
            return []
        raise

LOCK = threading.Lock()
NO_DISK = ("captions", "transcript", "partial", "board", "brain", "focus")   # 会议内容只在内存里: 中继重启就没了, 桌面端补推
DIRTY = threading.Event()
ROOMS = {}   # room -> {"state": dict|None, "sv": int, "paper": bytes|None, "pv": int, "assets": {name: (ctype, bytes)}, "touched": float}
INFLIGHT = [0]                        # 正在收的请求体一共多少字节 (见 INFLIGHT_MAX)
INFLIGHT_LOCK = threading.Lock()


def _take(n):
    with INFLIGHT_LOCK:
        if INFLIGHT[0] + n > INFLIGHT_MAX:
            return False
        INFLIGHT[0] += n
        return True


def _give(n):
    with INFLIGHT_LOCK:
        INFLIGHT[0] = max(0, INFLIGHT[0] - n)


def _size(r):
    """这个房间在内存里大概占多少字节 (调用方持锁)。"""
    if not r:
        return 0
    n = r.get("state_n", 0) + len(r.get("paper") or b"") + len(r.get("lines") or b"")
    n += sum(len(b) for _c, b in r.get("assets", {}).values())
    n += sum(len(u[1]) for u in r.get("uploads", {}).values())
    n += sum(v[2] for v in r.get("results", {}).values() if len(v) > 2)
    return n


def _fits(r, old, new):
    """公共中继: 房间里 old 字节的东西换成 new 字节以后, 这个房间、所有房间加起来还在上限里吗 (调用方持锁)。
    放得下 None, 否则错误码 (room_full / busy)。自己搭的中继只有自己推, 不限。"""
    if not PUBLIC:
        return None
    if _size(r) - old + new > PUB_ROOM_BYTES:
        return "room_full"
    if sum(_size(x) for x in ROOMS.values()) - old + new > PUB_ALL_BYTES:
        return "busy"
    return None


def _name_key(s):
    """名字怎么算同一个: 和 Bloom 那边认共同主持人的规矩一样 (zuhui/roles.py 的 _key: 空白并成一个、不分大小写)。"""
    return " ".join(str(s or "").split()).lower()


def _claim(r, name, key, ip=""):
    """名字认领 (调用方持锁): 一场会里第一次用这个名字的浏览器拿到一把钥匙 (访客页存着), 以后再用这个名字领 token
    要带上它; 别人拿同一个名字领不到 (共同主持人是按名字定的, 不然谁都能顶替)。主持人、AI 的名字访客不能用。
    返回 (钥匙, None) 或 (None, 错误码 name_taken / full)。"""
    nk = _name_key(name)
    people = [p for p in ((r.get("state") or {}).get("participants") or []) if isinstance(p, dict)]
    for p in people:
        if p.get("rank") in ("host", "assistant") and _name_key(p.get("name")) == nk:
            return None, "name_taken"
    claims = r.setdefault("claims", {})
    h = claims.get(nk)
    if h is not None:
        if key and hmac.compare_digest(h, hashlib.sha256(key.encode()).hexdigest()):
            return key, None
        return None, "name_taken"
    # 认领过的名字不让出来 (让出来就能被人刷掉再顶替共同主持人): 总数有上限, 一个 IP 在一个房间里新认领的名字也有上限
    per_ip = r.setdefault("claim_ip", {})
    if len(claims) >= CLAIMS_MAX or (PUBLIC and per_ip.get(ip, 0) >= CLAIMS_PER_IP):   # 自己的中继: 一个实验室共用一个 IP 也不卡
        return None, "full"
    per_ip[ip] = per_ip.get(ip, 0) + 1
    key = secrets.token_urlsafe(18)
    claims[nk] = hashlib.sha256(key.encode()).hexdigest()
    DIRTY.set()
    return key, None


NAME_MAX = 32          # 访客的名字最多这么多字（和访客页输入框一样）

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
    """调用方持锁。create: 没有就建; 房间数到上限时丢最久没动的、没在开的那个 (它是谁的记进 OWNERS);
    全都在开就建不了, 返回 None。"""
    r = ROOMS.get(room)
    if r is None and create:
        if len(ROOMS) >= MAX_ROOMS:
            idle = [k for k, x in ROOMS.items() if not _live(x)]
            if not idle:
                return None
            oldest = min(idle, key=lambda k: ROOMS[k]["touched"])
            _owner_keep(oldest, ROOMS[oldest].get("owner"))
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
        claims = d.get("claims") if isinstance(d.get("claims"), dict) else {}
        ROOMS[room] = {"state": d.get("state"), "sv": int(d.get("sv") or 1), "paper": None, "pv": 0, "assets": av,
                       "touched": float(d.get("touched") or time.time()), "host_seen": float(d.get("host_seen") or 0),
                       "inbox": [], "results": {}, "uploads": {}, "owner": d.get("owner"),
                       "claims": {k: v for k, v in claims.items() if isinstance(k, str) and isinstance(v, str)},
                       "state_n": len(json.dumps(d.get("state"), ensure_ascii=False).encode("utf-8"))}


def _saver():
    """有变动就 2 秒内写一次盘 (原子替换)。"""
    while True:
        DIRTY.wait()
        time.sleep(2)
        DIRTY.clear()
        try:                                   # 出什么错都不能让存盘线程停掉
            with LOCK:
                data = {room: {"state": None if r["state"] is None else {k: v for k, v in r["state"].items() if k not in NO_DISK},   # 字幕不落盘
                               "sv": r["sv"], "touched": r["touched"], "host_seen": r.get("host_seen", 0),
                               "av": {k: [c, base64.b64encode(b).decode()] for k, (c, b) in r["assets"].items() if k.startswith("av-")},
                               "owner": r.get("owner"),       # 公共中继: 房间刚被领走、还没推状态时 state 是 None
                               "claims": dict(r.get("claims") or {})}   # 认领过的名字 (只存哈希): 中继重启后别人照样顶替不了
                        for room, r in ROOMS.items() if r["state"] is not None or r.get("owner")}
                owners = dict(OWNERS)
            tmp = STORE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, STORE)
            if owners or os.path.exists(OWNER_STORE):
                with open(OWNER_STORE + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(owners, f)
                os.replace(OWNER_STORE + ".tmp", OWNER_STORE)
        except Exception as ex:
            print(f"save rooms: {ex!r}", file=sys.stderr)


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


def _is_cohost(r, name):
    """这个访客在主持人推来的参会人名单里是不是共同主持人。调用方持锁。"""
    people = ((r or {}).get("state") or {}).get("participants") or []
    nk = _name_key(name)
    return any(isinstance(p, dict) and _name_key(p.get("name")) == nk and p.get("rank") == "cohost" for p in people)


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


def _live_count():
    """/healthz 里的 live: 在开的会有几场, 自动更新 (bloom_update.py) 看到大于 0 就不重启中继。调用方持锁。
    算: 主持人推的状态是在开 (state.live), 而且主持人那边 LIVE_WINDOW (15 分钟) 内联系过 (host_seen: 推送、have、inbox)。
    主持人那边重启、断网一会儿也还算在开; 主持人再也不回来的房间最多挡 15 分钟 (访客或别的请求碰到它时,
    _live() 过了 HOST_STALE 就把它标成结束, 那之后也不算)。
    不看 touched: 访客开着旧链接轮询、没开会但打开了邀请的 Bloom (每分钟问一次 have, 状态是"未开始") 都会刷新它。
    不调 _live(): 它会顺手把房间标成结束, 查健康不该改东西。"""
    now = time.time()
    return sum(1 for r in ROOMS.values()
               if (r.get("state") or {}).get("live") and now - r.get("host_seen", 0) <= LIVE_WINDOW)


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
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   {"Access-Control-Allow-Origin": "*"})

    def _authed(self, room):
        """主持人那边: LiveKit 密钥签的 JWT (自己的中继); 公共中继还认通行码, 房间谁先用就归谁。"""
        self._via_device = False
        self._auth_busy = False
        h = self.headers.get("Authorization") or ""
        if h.startswith("Device ") and PUBLIC:
            dev = self._device(h[7:])
            if not dev:
                return False
            with LOCK:
                r = ROOMS.get(room)
                owner = (r or {}).get("owner") or OWNERS.get(room)   # 挤出内存的房间也还是原来那台电脑的
                if owner != dev:
                    if owner is not None or (r is not None and r["state"] is not None):
                        return False                   # 别的电脑的房间 / 以前用密钥开的房间
                    mine = [k for k, x in ROOMS.items() if x.get("owner") == dev]
                    gone = [k for k, d in OWNERS.items() if d == dev and k not in ROOMS]
                    if len(mine) + len(gone) >= PUB_ROOMS_PER_DEV:   # 占得太多: 把最久没用的那个让出来
                        if gone:
                            OWNERS.pop(gone[0], None)
                        else:
                            old = min(mine, key=lambda k: ROOMS[k]["touched"])
                            if _live(ROOMS[old]):
                                return False
                            ROOMS.pop(old, None)
                    r = _room(room, create=True)
                    if r is None:                      # 房间数满了、都在开: 稍后再来
                        self._auth_busy = True
                        return False
                    r["owner"] = dev
                    OWNERS.pop(room, None)
                    DIRTY.set()
                elif r is None:                        # 自己的房间被挤出内存了: 重新建起来, 还是它的
                    r = _room(room, create=True)
                    if r is None:
                        self._auth_busy = True
                        return False
                    r["owner"] = dev
                    OWNERS.pop(room, None)
                    DIRTY.set()
            self._via_device = True
            return True
        if not h.startswith("Bearer "):
            return False
        try:
            c = jwt.decode(h[7:], API_SECRET, algorithms=["HS256"], options={"verify_aud": False})
        except Exception:
            return False
        v = c.get("video") or {}
        return c.get("iss") == API_KEY and bool(v.get("roomAdmin")) and v.get("room") == room

    @staticmethod
    def _device(cred):
        did, _, sec = str(cred).strip().partition(":")
        with DEV_LOCK:
            d = DEVICES.get(did)
        if not d or not sec:
            return None
        return did if hmac.compare_digest(d["h"], hashlib.sha256(sec.encode()).hexdigest()) else None

    def _ip(self):
        return (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip() or self.client_address[0]

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

    def _body(self, limit=None):
        """请求体; 太大 None (self._busy=False), 同时在收的太多 None (self._busy=True, 回 503 稍后再来)。"""
        self._busy = False
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if limit is None:
            limit = PUB_MAX_BODY if getattr(self, "_via_device", False) else MAX_BODY
        if n < 0 or n > limit:
            return None
        if not n:
            return b""
        if not _take(n):
            self._busy = True
            return None
        try:
            return self.rfile.read(n)
        finally:
            _give(n)

    def _deny(self):
        """_authed 没过: 房间数满了 503, 别的 401。"""
        if getattr(self, "_auth_busy", False):
            return self._json(503, {"error": "busy"})
        return self._json(401, {"error": "unauthorized"})

    # ---------- GET ----------
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        if u.path == "/healthz":
            with LOCK:
                live = _live_count()
            return self._json(200, {"ok": True, "public": PUBLIC, "live": live})
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
                return self._deny()
            with LOCK:
                r = _room(parts[1])
                acts = list((r or {}).get("inbox") or [])
                if r:
                    r["inbox"] = []
                    r["host_seen"] = time.time()
            return self._json(200, {"acts": acts})
        if len(parts) == 4 and parts[0] == "push" and parts[2] == "upload" and ROOM_RE.match(parts[1]):
            if not self._authed(parts[1]):
                return self._deny()
            with LOCK:
                r = _room(parts[1])
                up = (r or {}).get("uploads", {}).pop(parts[3], None)
            if not up:
                return self._json(404, {"error": "no such upload"})
            name, data, by, _t = up
            return self._send(200, data, "application/octet-stream",
                              {"X-Bloom-Name": urllib.parse.quote(name), "X-Bloom-By": urllib.parse.quote(by)})
        if len(parts) == 3 and parts[0] == "push" and parts[2] == "participants" and ROOM_RE.match(parts[1]):
            if not self._authed(parts[1]):
                return self._deny()
            try:
                return self._json(200, {"participants": _participants(parts[1])})
            except Exception as ex:
                return self._json(502, {"error": f"livekit: {ex}"[:160]})
        if len(parts) == 3 and parts[0] == "push" and parts[2] == "have" and ROOM_RE.match(parts[1]):
            if not self._authed(parts[1]):
                return self._deny()
            with LOCK:
                r = _room(parts[1])
                if r is not None:
                    r["host_seen"] = time.time()
                have = {"sv": r["sv"] if r else 0, "pv": r["pv"] if r else 0, "lines": bool(r and r.get("lines")),
                        "paper": bool(r and r["paper"]), "assets": sorted((r or {}).get("assets", {}).keys())}
            return self._json(200, have)
        return self._json(404, {"error": "not found"})

    def _token(self, q):
        room = q.get("room", [""])[0].strip()
        name = q.get("name", [""])[0].strip()
        if not room or not name:
            return self._json(400, {"error": "room 和 name 必填"})
        if len(room) > 64 or len(name) > NAME_MAX:
            return self._json(400, {"error": f"room 最多 64 个字、name 最多 {NAME_MAX} 个字"})
        if not ROOM_RE.match(room):
            return self._json(400, {"error": "房间号不对"})
        if _name_key(name) in HOST_IDS:            # 主持人那边语音桥的两路身份: 访客用了会把主持人的语音挤掉
            return self._json(409, {"error": "name_taken"})
        with LOCK:
            live = _live(_room(room))
        if not live:                               # 只有正在开的会能进: 旧链接 / 还没开的会都拿不到 token
            return self._json(403, {"error": "会议未开始或已结束"})
        if PUBLIC:                                 # 公共中继: 一场会最多这么多人
            try:
                if len(_participants(room)) >= PUB_MAX_GUESTS:
                    return self._json(429, {"error": "会议人数已满"})
            except Exception:
                pass
        with LOCK:                                 # 这个名字在这场会里是不是已经有人用了 (见 _claim)
            r = _room(room)
            key, err = _claim(r, name, q.get("claim", [""])[0].strip()[:64], self._ip()) if r else (None, "gone")
        if err:
            return self._json(409 if err == "name_taken" else 429 if err == "full" else 403, {"error": err})
        role = "guest"                             # 身份由主持人在 Bloom 里定, 链接里写什么都不算
        token = make_token(room, name, role)
        return self._json(200, {"token": token, "url": WS_URL, "room": room, "identity": name, "role": role, "ttl": TTL,
                                "claim": key})

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
            if what == "lines":
                if not r or not r.get("lines"):
                    return self._json(200, {})
                return self._send(200, r["lines"], "application/json; charset=utf-8")
            if what == "paper":
                if not r or not r["paper"]:
                    return self._send(404, "论文加载中…".encode("utf-8"), "text/plain; charset=utf-8")
                return self._send(200, r["paper"], "text/html; charset=utf-8")
            if what == "fig" and len(rest) == 2 and r:
                a = r["assets"].get(rest[1])
                if a:
                    ctype, data = a
                    # 网页和 SVG 直接打开时会跑里面的脚本: 一律关进沙盒（<img> 里显示不受影响），别人推的图碰不到中继这个网址
                    extra = {"Content-Security-Policy": "sandbox"} if ctype in ("text/html", "image/svg+xml") else None
                    # 名字里带内容哈希的图（桌面端 view._guest_asset_name：fig1-3f2a9c1b7d.png；头像 av-<哈希>）：同名就是同一份，
                    # 手机缓存一天，来回切论文、每次改稿换新版不用再下一遍；别的照旧不缓存
                    hashed = bool(HASHED_RE.search(rest[1]))
                    return self._send(200, data, ctype, extra, cache="private, max-age=86400, immutable" if hashed else "no-store")
        return self._json(404, {"error": "not found"})

    # ---------- POST ----------
    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        if u.path == "/dev/register":
            if not PUBLIC:
                return self._json(404, {"error": "not found"})
            if not _reg_allow(self._ip()):
                return self._json(429, {"error": "too many"})
            did, sec = "d" + secrets.token_hex(8), secrets.token_urlsafe(30)
            with DEV_LOCK:
                DEVICES[did] = {"h": hashlib.sha256(sec.encode()).hexdigest(), "at": int(time.time())}
                _dev_save()
            _log_line(f"register device={did}")
            return self._json(200, {"device": did, "secret": sec, "ws": WS_URL})
        if len(parts) == 3 and parts[0] == "push" and parts[2] == "join" and ROOM_RE.match(parts[1]):
            if not self._authed(parts[1]):
                return self._deny()
            try:
                obj = json.loads((self._body() or b"{}").decode("utf-8"))
            except ValueError:
                return self._json(400, {"error": "bad json"})
            ident, role = str(obj.get("identity") or ""), str(obj.get("role") or "")
            if ident not in HOST_IDS or role not in ("teacher", "student"):
                return self._json(400, {"error": "identity"})
            name = str(obj.get("name") or ident).strip()[:NAME_MAX] or ident
            now = int(time.time())
            tok = jwt.encode({"iss": API_KEY, "sub": ident, "nbf": now - 10, "exp": now + 8 * 3600, "jti": uuid.uuid4().hex,
                              "name": name, "metadata": json.dumps({"bloom_host": True, "role": role}, ensure_ascii=False),
                              "video": {"roomJoin": True, "room": parts[1], "canPublish": True, "canSubscribe": role == "teacher",
                                        "canPublishData": role == "teacher"}}, API_SECRET, algorithm="HS256")
            return self._json(200, {"token": tok, "url": WS_URL})
        if len(parts) == 3 and parts[0] == "view" and parts[2] == "diag" and ROOM_RE.match(parts[1]):
            return self._diag(parts[1])
        if len(parts) == 3 and parts[0] == "view" and parts[2] in ("act", "upload") and ROOM_RE.match(parts[1]):
            return self._guest_act(parts[1], parts[2], urllib.parse.parse_qs(u.query))
        if len(parts) == 4 and parts[0] == "push" and parts[2] == "result" and ROOM_RE.match(parts[1]):
            if not self._authed(parts[1]):
                return self._deny()
            body = self._body(limit=MAX_RESULT_BODY)
            if body is None:
                return self._json(503 if self._busy else 413, {"error": "busy" if self._busy else "too large"})
            try:
                obj = json.loads((body or b"{}").decode("utf-8"))
            except ValueError:
                return self._json(400, {"error": "bad json"})
            with LOCK:
                r = _room(parts[1])
                if r is None:
                    return self._json(404, {"error": "no room"})
                _sweep(r)
                rid = parts[3][:64]
                old = r["results"].pop(rid, None)
                while len(r["results"]) >= MAX_RESULTS:        # 留着的太多: 扔最早的
                    r["results"].pop(next(iter(r["results"])))
                err = _fits(r, 0, len(body))
                if err:
                    if old:
                        r["results"][rid] = old
                    return self._json(503 if err == "busy" else 413, {"error": err})
                r["results"][rid] = (obj, time.time(), len(body))
            return self._json(200, {"ok": True})
        if len(parts) < 3 or parts[0] != "push" or not ROOM_RE.match(parts[1]):
            return self._json(404, {"error": "not found"})
        room, what = parts[1], parts[2]
        if not self._authed(room):
            return self._deny()
        body = self._body()
        if body is None:
            return self._json(503 if self._busy else 413, {"error": "busy" if self._busy else "too large"})
        with LOCK:
            r = _room(room, create=True)
            if r is None:                          # 房间数满了、都在开
                return self._json(503, {"error": "busy"})
            r["host_seen"] = time.time()
            # 公共中继: 一个房间、所有房间加起来在内存里都有上限 (换掉的那份先减掉)
            old = {"state": r.get("state_n", 0), "lines": len(r.get("lines") or b""), "paper": len(r.get("paper") or b""),
                   "asset": len((r["assets"].get(parts[3]) or ("", b""))[1]) if len(parts) == 4 else 0}.get(what)
            if old is not None:
                err = _fits(r, old, len(body))
                if err:
                    return self._json(503 if err == "busy" else 413, {"error": err})
            if what == "state":
                try:
                    st = json.loads(body.decode("utf-8"))
                except ValueError:
                    return self._json(400, {"error": "bad json"})
                if not isinstance(st, dict):
                    return self._json(400, {"error": "bad json"})
                if PUBLIC and getattr(self, "_via_device", False) and st.get("live") and not _live(r):
                    if sum(1 for k, x in ROOMS.items() if k != room and _live(x)) >= PUB_MAX_LIVE:
                        return self._json(429, {"error": "busy"})
                st.pop("lines", None)                  # 旧字段；新版逐字稿在 transcript 里 (访客和主持人看到的一样)
                st["pushed_at"] = time.time()
                r["state"] = st
                r["state_n"] = len(body)
                r["sv"] += 1
                DIRTY.set()
                return self._json(200, {"ok": True, "sv": r["sv"], "pv": r["pv"]})
            if what == "lines":                    # 逐字稿 + 任务板（JSON），状态里的 lv 是它的版本
                r["lines"] = body
                return self._json(200, {"ok": True})
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
                r["lines"] = None
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
        with LOCK:                                 # 收 50 MB 之前先看收不收得下: 会没在开、排满了就不收
            r = _room(room)
            if not _live(r):
                return self._json(409, {"error": "会议已结束"})
            if what == "upload" and len(r["uploads"]) >= MAX_UPLOADS:
                return self._json(429, {"error": "上传过于频繁，请稍后重试"})
            err = _fits(r, 0, n) if what == "upload" else None
        if err:
            return self._json(503 if err == "busy" else 413, {"error": err})
        if n and not _take(n):
            return self._json(503, {"error": "busy"})
        try:
            body = self.rfile.read(n) if n else b""
        finally:
            _give(n)
        aid = uuid.uuid4().hex[:16]
        if what == "upload":
            name = os.path.basename(str(q.get("name", [""])[0]).replace("\\", "/")).strip()[:120]
            if not name or not name.lower().endswith(UP_EXT) or not body:
                return self._json(415, {"error": "仅支持 PDF、Word、PPT、Markdown、LaTeX、TXT"})
            act = {"id": aid, "by": by, "path": "upload", "body": {"upload": aid, "name": name, "size": len(body)}, "at": time.time()}
        else:
            try:
                obj = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            path = str(obj.get("path") or "")
            if path not in ACT_PATHS or not isinstance(obj.get("body", {}), dict):
                return self._json(403, {"error": "不支持此操作"})
            act = {"id": aid, "by": by, "path": path, "body": obj.get("body") or {}, "at": time.time()}
        with LOCK:
            r = _room(room)
            if not _live(r):
                return self._json(409, {"error": "会议已结束"})
            if act["path"] in COHOST_PATHS and not _is_cohost(r, by):
                return self._json(403, {"error": "cohost_only"})
            _sweep(r)
            if what == "upload":
                if len(r["uploads"]) >= MAX_UPLOADS:
                    return self._json(429, {"error": "上传过于频繁，请稍后重试"})
                err = _fits(r, 0, len(body))
                if err:
                    return self._json(503 if err == "busy" else 413, {"error": err})
                r["uploads"][aid] = (act["body"]["name"], body, by, time.time())
            if len(r["inbox"]) >= MAX_INBOX:
                return self._json(429, {"error": "操作过于频繁"})
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
    _dev_load()
    _owners_load()
    threading.Thread(target=_saver, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("BLOOM_PORT") or 8787)), Handler)
    srv.daemon_threads = True
    print("bloom relay on 127.0.0.1:8787", flush=True)
    srv.serve_forever()
