# -*- coding: utf-8 -*-
"""设置 → 语音 → 麦克风：会上录哪个麦克风。

存在 Blurt 的 config.json（$BLURT_HOME，默认 ~/.blurt）的 record.mic，录音那边就读这一个键：
- Tauri 的 capture.rs mic_from_config：没有 / "default" = 系统默认，"off" / false = 不录，别的字符串 = 设备名，
  作为 `record.py start --mic <名字>` 传过去；
- record.py pick_mic 按"设备名里含这一段"（不分大小写）在 PortAudio 的 WASAPI、MME 输入设备里找，找不到就报错；
- 语音桥（bridge.pick_input）也这样找。
所以设备表列的是 PortAudio 里 WASAPI 的输入设备名（全名；MME 的名字会被截到 31 个字，同一个设备也能被这个全名找到），
选了哪个就原样存它的名字。选"系统默认"时把这个键删掉：record.py 不认 "default" 这个词，会当成设备名去找。

只改 record.mic，别的键原样留着：和 Blurt 自己写这个文件一样，在 $BLURT_HOME/.config.lock 下读、合并、写临时文件
再换上；文件读不出来（手改坏了）就不写。开着会不让改（409）：这一场的录音已经按原来的设备开了。

    GET  /api/mic/device → {mic: ""（系统默认）| 设备名 | "off", devices: [名字], default: 系统默认设备的名字,
                            missing: 选的设备现在找不到, live: 开着会, max}
    POST /api/mic/device {mic: "" | 设备名} → 同上；开着会 409 {error: live}，名字不对 400 {error: bad | too_long}，
                            写不了 500 {error: unreadable | failed}
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

MAX = 200                     # 设备名最长多少字（Windows 的设备名一般几十个字）
_LOCK = threading.Lock()      # 本进程里两个请求同时写：排队


def home() -> Path:
    return Path(os.environ.get("BLURT_HOME") or Path.home() / ".blurt")


def config_path() -> Path:
    return home() / "config.json"


# ---------------------------------------------------------------- 读：和 Blurt 的 _common.parse_config_text 一样宽容
def _drop_trailing_commas(text: str) -> str:
    """字符串外面、紧挨着 } 或 ] 的逗号去掉（手改最常见的错）。"""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] not in "}]":
                out.append(c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _read() -> tuple[dict, bool]:
    """(设置, 读得出来)。没有这个文件算读得出来（{}）。"""
    try:
        text = config_path().read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}, True
    except (OSError, UnicodeDecodeError):
        return {}, False
    text = text.lstrip("﻿")
    for t in (text, _drop_trailing_commas(text)):
        try:
            v = json.loads(t)
        except ValueError:
            continue
        return (v, True) if isinstance(v, dict) else ({}, False)
    return {}, False


def chosen() -> str:
    """现在选的：""（系统默认）、"off"（不录，只会是手改的）或设备名（和 capture.rs 一样，原样不去空格）。"""
    rec = _read()[0].get("record")
    v = rec.get("mic") if isinstance(rec, dict) else None
    if v is False or (isinstance(v, str) and v in ("off", "none")):
        return "off"
    if isinstance(v, str) and v and v != "default":
        return v
    return ""


def present(name: str, inputs: list) -> bool:
    """record.py 能不能找到它：设备名里含这一段（不分大小写）。"""
    n = name.lower()
    return any(n in str(x).lower() for x in inputs)


# ---------------------------------------------------------------- 写：$BLURT_HOME/.config.lock 下读、合并、换上
@contextmanager
def _file_lock(path: Path, timeout: float = 15.0):
    """和 _common.file_lock、Tauri 的 settings.rs 同一把锁（Windows：锁文件的第 0 个字节；别处 flock）。
    等了 timeout 秒还拿不到就不等了照写（和 Tauri 那边一样：宁可不锁，也别丢了用户的设置）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    locked = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def clean(value) -> str:
    """界面交上来的 → 要存的：""（系统默认）或设备名。不合格抛 ValueError（bad / too_long）。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("bad")
    v = value.strip()
    if v == "default":
        return ""
    if v in ("off", "none") or any(ord(c) < 32 for c in v):
        raise ValueError("bad")
    if len(v) > MAX:
        raise ValueError("too_long")
    return v


def save(value) -> str:
    """改 record.mic（"" = 删掉这个键 = 系统默认），返回存下的。名字不对 ValueError；文件读不出来 OSError("unreadable")。"""
    v = clean(value)
    path = config_path()
    with _LOCK, _file_lock(path.with_name(".config.lock")):
        cfg, ok = _read()
        if not ok:
            raise OSError("unreadable")
        rec = cfg.get("record")
        if not isinstance(rec, dict):
            if not v:
                return ""                                  # 本来就是系统默认：不用写
            rec = {}
        if v:
            rec["mic"] = v
        elif "mic" in rec:
            del rec["mic"]
        else:
            return ""
        cfg["record"] = rec
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(20):                          # Windows：别人正读着时 os.replace 会被拒
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    raise
                time.sleep(0.05)
    return v


# ---------------------------------------------------------------- 接口
def _live(viewer) -> bool:
    ms = getattr(viewer, "meetings", None)
    try:
        return bool(ms and ms.current())
    except Exception:
        return False


def _probe(viewer) -> dict:
    """系统现在的输入设备（view.py 的 _DEVICE_PROBE，另起小进程问；2 秒内问过的就用那一次的）。"""
    at, val = getattr(viewer, "_dev", (0.0, None))
    if val is None or not isinstance(val.get("inputs"), list) or time.time() - at > 2:
        try:
            viewer._probe_devices()
        except Exception:
            pass
        at, val = getattr(viewer, "_dev", (0.0, None))
    return val if isinstance(val, dict) else {}


def public(viewer) -> dict:
    p = _probe(viewer)
    inputs = [str(x) for x in (p.get("inputs") or []) if str(x).strip() and not str(x).endswith("[Loopback]")]
    cur = chosen()
    return {"mic": cur, "devices": inputs, "default": p.get("mic"),
            "missing": bool(cur and cur != "off" and isinstance(p.get("inputs"), list) and not present(cur, inputs)),
            "live": _live(viewer), "max": MAX}


def http_get(viewer) -> tuple[dict, int]:
    return public(viewer), 200


def http_post(viewer, body: dict | None) -> tuple[dict, int]:
    if _live(viewer):
        return {"ok": False, "error": "live"}, 409
    try:
        save((body or {}).get("mic"))
    except ValueError as e:
        return {"ok": False, "error": str(e) or "bad"}, 400
    except OSError as e:
        return {"ok": False, "error": "unreadable" if str(e) == "unreadable" else "failed"}, 500
    # 上一场录音记下的"实际用的设备"（~/.zuhui/devices.json）不作数了：下一场按新选的开
    try:
        from .config import STATE
        (Path(STATE) / "devices.json").unlink()
    except (OSError, ImportError):
        pass
    try:
        viewer._dev = (0.0, None)
        viewer._ready_cache = (0.0, None)
    except Exception:
        pass
    return public(viewer), 200
