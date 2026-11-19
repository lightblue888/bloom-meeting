#!/usr/bin/env python3
"""Bloom 中继自动更新 (setup.sh 装的 bloom-update.timer 每小时跑一次, 只用标准库):

1. 查 GitHub 上最新的发布 (releases/latest) 的版本号, 和 /opt/bloom/VERSION 一样就退出。
2. 本机中继 /healthz 的 live > 0 (有会在开) 就这次不换, 下个小时再看。中继连不上也不换。
3. 下载这个版本的 relay/token_server.py、relay/bloom_update.py 和发布里的 bloom-guest.zip, 先校验
   (py_compile, token_server.py 里得有 /healthz; zip 里要有 g/guest.html), 不过就放弃。
4. 当前的 token_server.py、bloom_update.py、访客页 www 备份到 /opt/bloom/backup/<旧版本>/ (只留最近 2 份), 换上新的,
   systemctl restart bloom-token; 10 秒内 /healthz 不通就换回旧的再重启, 这个版本 24 小时内不再试 (--force 现在就试)。
5. 成功了写 VERSION, 最后换掉自己。

    sudo python3 /opt/bloom/bloom_update.py              手动跑一次
    sudo journalctl -u bloom-update --since today        看日志
    sudo systemctl disable --now bloom-update.timer      关掉自动更新

设置 (命令行参数 > 环境变量 > /opt/bloom/update.env > 默认值):
    --repo       BLOOM_REPO          lightblue888/bloom-meeting
    --dir        BLOOM_DIR           /opt/bloom    token_server.py、VERSION、backup/、bloom_update.py 都在这里
    --www        BLOOM_WWW           <dir>/www     访客页, 要和 <dir> 在同一个分区 (换的时候整个目录改名)
    --env-file   BLOOM_UPDATE_ENV    <dir>/update.env
    --api        BLOOM_GITHUB_API    https://api.github.com
    --raw        BLOOM_GITHUB_RAW    https://raw.githubusercontent.com
    --healthz    BLOOM_HEALTHZ       http://127.0.0.1:8787/healthz
    --systemctl  BLOOM_SYSTEMCTL     systemctl     (测试换成记录调用的假命令)
    --service    BLOOM_SERVICE       bloom-token
    --wait       BLOOM_HEALTH_WAIT   10            重启后等 /healthz 通的秒数
                 GITHUB_TOKEN                      私有仓库才要
"""
import argparse, json, os, py_compile, re, shlex, shutil, subprocess, sys, tempfile, time, types
import urllib.error, urllib.parse, urllib.request, zipfile

OPTS = (   # (参数名, 环境变量, 默认值); 空的默认值按 --dir 算
    ("repo", "BLOOM_REPO", "lightblue888/bloom-meeting"),
    ("dir", "BLOOM_DIR", "/opt/bloom"),
    ("www", "BLOOM_WWW", ""),
    ("env_file", "BLOOM_UPDATE_ENV", ""),
    ("api", "BLOOM_GITHUB_API", "https://api.github.com"),
    ("raw", "BLOOM_GITHUB_RAW", "https://raw.githubusercontent.com"),
    ("healthz", "BLOOM_HEALTHZ", "http://127.0.0.1:8787/healthz"),
    ("systemctl", "BLOOM_SYSTEMCTL", "systemctl"),
    ("service", "BLOOM_SERVICE", "bloom-token"),
    ("wait", "BLOOM_HEALTH_WAIT", "10"),
)
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
KEEP = 2                              # 备份留几份
FAIL_HOLD = 24 * 3600                 # 换上后起不来的版本这么久内不再试
MAX_PY = 5 * 1024 * 1024              # 下载的 .py 最多这么大
MAX_ZIP = 100 * 1024 * 1024           # 访客页的包最多这么大
MAX_UNZIP = 300 * 1024 * 1024         # 解开以后最多这么大
UA = "bloom-relay-update"


def log(msg):
    """一行日志 (journal 里看)。stdout 一律按 UTF-8 写, 系统是 C locale 也不出错。"""
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))
        sys.stdout.flush()
    except Exception:
        pass


def _err(ex):
    return (str(ex) or type(ex).__name__)[:200]


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_env(path):
    out = {}
    for line in _read(path).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _config(argv):
    ap = argparse.ArgumentParser(description="Bloom 中继自动更新")
    for name, var, dflt in OPTS:
        ap.add_argument("--" + name.replace("_", "-"), dest=name, metavar=var, help=f"默认 {dflt or '按 --dir 算'}")
    ap.add_argument("--force", action="store_true", help="上次换上后起不来的版本也现在就再试")
    a = ap.parse_args(argv)

    def pick(name, var, dflt, file_env=None):
        for v in (getattr(a, name), os.environ.get(var), (file_env or {}).get(var)):
            if v:
                return v
        return dflt

    d = pick("dir", "BLOOM_DIR", "/opt/bloom")
    env_file = pick("env_file", "BLOOM_UPDATE_ENV", os.path.join(d, "update.env"))
    fe = _read_env(env_file)
    c = types.SimpleNamespace(**{name: pick(name, var, dflt, fe) for name, var, dflt in OPTS})
    c.dir, c.env_file = d, env_file
    c.www = c.www or os.path.join(d, "www")
    c.token = os.environ.get("GITHUB_TOKEN") or fe.get("GITHUB_TOKEN") or ""
    c.wait = float(c.wait)
    c.force = a.force
    c.token_server = os.path.join(d, "token_server.py")
    c.self_path = os.path.join(d, "bloom_update.py")
    c.version_file = os.path.join(d, "VERSION")
    c.failed_file = os.path.join(d, "update-failed")
    c.backup_root = os.path.join(d, "backup")
    return c


# ---------- 网络 ----------
class _Redirect(urllib.request.HTTPRedirectHandler):
    """跳到别的域名 (发布里的附件会跳到 GitHub 的下载服务器) 时不带 GITHUB_TOKEN。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers.pop("Authorization", None)
        return new


_WEB = urllib.request.build_opener(_Redirect)                              # GitHub: 按系统的代理设置走
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # 本机中继: 不走代理


def _get(c, url, limit, accept=None):
    h = {"User-Agent": UA}
    if accept:
        h["Accept"] = accept
    if c.token:
        h["Authorization"] = "Bearer " + c.token
    out = bytearray()
    with _WEB.open(urllib.request.Request(url, headers=h), timeout=60) as r:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            out += chunk
            if len(out) > limit:
                raise ValueError(f"{url} 太大 (超过 {limit >> 20} MB)")
    return bytes(out)


def _save(path, data):
    with open(path, "wb") as f:
        f.write(data)


def _health(c):
    """本机中继的 /healthz: 通了 (ok) 返回那份 JSON, 不通 None。"""
    try:
        with _LOCAL.open(urllib.request.Request(c.healthz, headers={"User-Agent": UA}), timeout=3) as r:
            d = json.loads(r.read().decode("utf-8") or "{}")
        return d if isinstance(d, dict) and d.get("ok") else None
    except Exception:
        return None


def _busy(c, cur, tag):
    """现在不能换: 返回退出码 (有会在开 0, 中继连不上 1); 能换返回 None。"""
    h = _health(c)
    if h is None:
        log(f"本机中继连不上 ({c.healthz}), 这次不更新")
        return 1
    live = h.get("live")
    if not isinstance(live, int) or isinstance(live, bool):
        live = 0                                  # 旧版中继不报 live: 只能当没在开会
    if live > 0:
        log(f"有 {live} 场会在开, 这次不更新 ({cur or '?'} -> {tag})")
        return 0
    return None


def _systemctl(c, verb):
    cmd = shlex.split(c.systemctl) + [verb, c.service]
    try:
        rc = subprocess.run(cmd, timeout=120).returncode
    except (OSError, subprocess.SubprocessError) as ex:
        log(f"systemctl {verb} {c.service} 失败: {_err(ex)}")
        return False
    if rc:
        log(f"systemctl {verb} {c.service} 失败: 退出码 {rc}")
    return rc == 0


def _came_up(c):
    """重启后 c.wait 秒内 /healthz 通了没有。"""
    end = time.time() + c.wait
    while True:
        if _health(c) is not None:
            return True
        if time.time() >= end:
            return False
        time.sleep(0.5)


# ---------- 校验 ----------
def _check_py(path, work, must=None):
    with open(path, "rb") as f:
        data = f.read()
    name = os.path.basename(path)
    if must and must not in data:
        raise ValueError(f"{name} 里没有 {must.decode()}")
    try:
        py_compile.compile(path, cfile=os.path.join(work, "check.pyc"), doraise=True)
    except py_compile.PyCompileError as ex:
        raise ValueError(f"{name} 编译不过: {ex.msg.strip().splitlines()[-1]}")


def _check_zip(path):
    """访客页的包: 打得开、CRC 对、没有往外跳的路径、不太大、有 g/guest.html。"""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            for n in names:
                p = n.replace("\\", "/")
                if p.startswith("/") or ":" in p or ".." in p.split("/"):
                    raise ValueError(f"bloom-guest.zip 里的路径不对: {n}")
            if "g/guest.html" not in names:
                raise ValueError("bloom-guest.zip 里没有 g/guest.html")
            if sum(i.file_size for i in z.infolist()) > MAX_UNZIP:
                raise ValueError("bloom-guest.zip 解开太大")
            bad = z.testzip()
            if bad:
                raise ValueError(f"bloom-guest.zip 里的 {bad} 坏了")
    except ValueError:
        raise
    except Exception as ex:
        raise ValueError(f"bloom-guest.zip 打不开: {_err(ex)}")


# ---------- 备份 ----------
def _backup(c, old_tag):
    """当前的 token_server.py、bloom_update.py、www 复制到 backup/<旧版本>/, 只留最近 KEEP 份。返回备份目录。"""
    os.makedirs(c.backup_root, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", old_tag)[:64].lstrip(".") or "unknown"
    dst = os.path.join(c.backup_root, name)
    tmp = os.path.join(c.backup_root, "." + name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for f in (c.token_server, c.self_path):
        if os.path.isfile(f):
            shutil.copy2(f, os.path.join(tmp, os.path.basename(f)))
    if os.path.isdir(c.www):
        shutil.copytree(c.www, os.path.join(tmp, "www"), symlinks=True)
    shutil.rmtree(dst, ignore_errors=True)          # 同一个旧版本备份过 (上次换回来了): 换成这次的
    os.rename(tmp, dst)
    now = time.time()
    os.utime(dst, (now, now))
    olds = [os.path.join(c.backup_root, n) for n in os.listdir(c.backup_root) if not n.startswith(".")]
    olds = sorted((p for p in olds if os.path.isdir(p)), key=os.path.getmtime, reverse=True)
    for p in olds[KEEP:]:
        shutil.rmtree(p, ignore_errors=True)
    return dst


def _leftovers(c):
    """上次跑到一半被打断留下的临时目录: 访客页正好换到一半就先换回来, 再删掉。"""
    for n in os.listdir(c.dir):
        p = os.path.join(c.dir, n)
        if n.startswith(".update-") and os.path.isdir(p):
            old = os.path.join(p, "www-old")
            if not os.path.isdir(c.www) and os.path.isdir(old):
                os.rename(old, c.www)
                log("上次没换完的访客页已换回")
            shutil.rmtree(p, ignore_errors=True)


def _lock(path):
    """拿到锁返回打开的文件 (进程退出时自动放开); 别的进程拿着就返回 None。"""
    f = open(path, "a+")
    try:
        if os.name == "nt":                        # 本机测试 (Windows)
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


# ---------- 主流程 ----------
def main(argv=None):
    c = _config(argv)
    if not REPO_RE.match(c.repo):
        log(f"仓库名不对: {c.repo}")
        return 1
    os.makedirs(c.dir, exist_ok=True)
    lock = _lock(os.path.join(c.dir, ".update.lock"))
    if lock is None:
        log("已经有一份在跑, 这次不跑")
        return 0
    try:
        _leftovers(c)
        cur = _read(c.version_file)
        try:
            rel = json.loads(_get(c, f"{c.api.rstrip('/')}/repos/{c.repo}/releases/latest", 2 << 20,
                                  "application/vnd.github+json").decode("utf-8"))
        except Exception as ex:
            log(f"查最新版本失败: {_err(ex)}")
            return 1
        tag = str((rel if isinstance(rel, dict) else {}).get("tag_name") or "")
        if not TAG_RE.match(tag):
            log(f"最新发布的版本号不对: {tag!r}")
            return 1
        if tag == cur:
            log(f"已是最新: {tag}")
            return 0
        if (not c.force and _read(c.failed_file) == tag
                and time.time() - os.path.getmtime(c.failed_file) < FAIL_HOLD):
            log(f"{tag} 上次换上后中继起不来, 已换回 {cur or '原来的版本'}; 24 小时内不再试 (--force 现在就试)")
            return 0
        code = _busy(c, cur, tag)
        if code is not None:
            return code
        work = tempfile.mkdtemp(prefix=".update-", dir=c.dir)
        try:
            return _update(c, cur, tag, rel, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        lock.close()


def _update(c, cur, tag, rel, work):
    log(f"有新版本: {cur or '?'} -> {tag}")
    raw = f"{c.raw.rstrip('/')}/{c.repo}/{tag}/relay/"
    new_ts = os.path.join(work, "token_server.py")
    new_self = os.path.join(work, "bloom_update.py")
    zpath = os.path.join(work, "guest.zip")
    try:
        asset = next((a for a in rel.get("assets") or [] if isinstance(a, dict) and a.get("name") == "bloom-guest.zip"), None)
        if not asset:
            raise ValueError("发布里没有 bloom-guest.zip")
        _save(new_ts, _get(c, raw + "token_server.py", MAX_PY))
        _check_py(new_ts, work, b"/healthz")              # 换上以后靠 /healthz 确认起来了
        url = asset.get("url") if c.token else (asset.get("browser_download_url") or asset.get("url"))   # 私有仓库走 API 下附件
        _save(zpath, _get(c, str(url or ""), MAX_ZIP, "application/octet-stream"))
        _check_zip(zpath)
        try:
            _save(new_self, _get(c, raw + "bloom_update.py", MAX_PY))
            _check_py(new_self, work)
        except urllib.error.HTTPError as ex:
            if ex.code != 404:
                raise
            new_self = None                                # 这个版本里没有 bloom_update.py: 自己不换
    except Exception as ex:
        log(f"下载或校验失败, 这次不更新: {_err(ex)}")
        return 1

    # 新的访客页先在旁边摆好: 现在的 www 复制一份, g/ 整个换掉 (带哈希的旧文件不留), 别的文件覆盖 (和 setup.sh 一样)
    stage = os.path.join(work, "www")
    try:
        if os.path.isdir(c.www):
            shutil.copytree(c.www, stage, symlinks=True)
        else:
            os.makedirs(stage)
        shutil.rmtree(os.path.join(stage, "g"), ignore_errors=True)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(stage)
    except Exception as ex:
        log(f"解开访客页失败, 这次不更新: {_err(ex)}")
        return 1

    code = _busy(c, cur, tag)                              # 下载这一会儿可能开了会: 换之前再看一眼
    if code is not None:
        return code
    try:
        bak = _backup(c, cur or "unknown")
    except Exception as ex:
        log(f"备份失败, 这次不更新: {_err(ex)}")
        return 1

    old_www = os.path.join(work, "www-old")
    had_www = os.path.isdir(c.www)
    try:
        if had_www:
            os.rename(c.www, old_www)
        try:
            os.rename(stage, c.www)
        except OSError:
            if had_www:
                os.rename(old_www, c.www)
            raise
        os.chmod(new_ts, 0o644)
        os.replace(new_ts, c.token_server)
    except Exception as ex:
        log(f"换文件失败: {_err(ex)}")
        _restore(c, bak, old_www, had_www, work)
        return 1
    log(f"已换上 {tag}, 重启 {c.service}")
    if _systemctl(c, "restart") and _came_up(c):
        _write(c.version_file, tag + "\n")
        if new_self:
            os.chmod(new_self, 0o644)
            os.replace(new_self, c.self_path)
        try:
            os.remove(c.failed_file)
        except OSError:
            pass
        log(f"更新完成: {cur or '?'} -> {tag}, 旧的在 {bak}")
        return 0

    log(f"{tag} 起不来 ({c.wait:g} 秒内 /healthz 不通), 换回 {cur or '原来的版本'}")
    _restore(c, bak, old_www, had_www, work)
    _systemctl(c, "reset-failed")                          # 新版连着崩几次会被 systemd 挡住, 先清掉再重启
    ok = _systemctl(c, "restart") and _came_up(c)
    _write(c.failed_file, tag + "\n")
    log("已换回" if ok else f"换回以后中继还是起不来, 看 journalctl -u {c.service}")
    return 1


def _restore(c, bak, old_www, had_www, work):
    """换回旧的: www 改名换回来, token_server.py 从备份里拿。"""
    try:
        if had_www and os.path.isdir(old_www):
            if os.path.isdir(c.www):
                os.rename(c.www, os.path.join(work, "www-bad"))
            os.rename(old_www, c.www)
        src = os.path.join(bak, "token_server.py")
        if os.path.isfile(src):
            tmp = c.token_server + ".restore"
            shutil.copy2(src, tmp)
            os.replace(tmp, c.token_server)
    except Exception as ex:
        log(f"换回失败: {_err(ex)}, 备份在 {bak}")


if __name__ == "__main__":
    sys.exit(main())
