# -*- coding: utf-8 -*-
"""会议语音桥：开会时 Bloom 以两个身份进中继的 LiveKit 房间，让通过邀请链接进来的人听得到、也说得上话。

- "主持人"（bloom-host）：推你的麦克风。用 LiveKit 自带的 WebRTC 音频处理（回声消除 + 降噪 + 自动增益）：
  访客的声音从你的扬声器放出来，再被麦克风录到的那部分会被消掉，访客听不到自己的回声。
  学生正在出声的时候这一路推静音：学生的声音是 ffplay 直接放的，回声消除够不着它，不静音的话访客会听到两遍学生。
  你压着学生说话会打断它（耳朵的判停），它一停这一路就恢复。
- "Claude"（bloom-student）：推学生的声音。Mouth 每放一句就把同一个音频文件（同样的语速）解码成 48 kHz 推进来，
  打断时（ffplay 被杀）立刻停、清空。
- 访客的声音：订阅下来混在一起，从默认扬声器放出来；放出去的同一份 PCM 喂给回声消除当参考，
  也按到达时刻记进 ears.PLAYBACK（耳朵拿它认出扬声器漏进口喷鸡麦克风的回声，见 ears.py）。
- 你点了静音（ears.MIC）或者你正用另一台设备以访客身份参会（ears.PLAYBACK.host_elsewhere）：你这一路推静音。
- 访客说的话：每位访客自己那一路（16 kHz）直接送去转写（zuhui/guest_ears.py：按人分段、常驻的 Fun-ASR-Nano 进程），
  转出来的字交给 on_guest_text（app.py 按停顿并句、记成这位访客的话）。主持人戴不戴耳机都一样。
- 谁在说：按声音大小记下每个人"在说话"的时间段（访客：收到的那一路；你：回声消除之后的麦克风）。
  耳朵问某段转写是不是访客说的（guest_over）：是的话，那是访客的声音从扬声器漏进了你的麦克风，直接转写那一路已经有了，
  这段丢掉；直接转写起不来时才退回老办法，把这段记成访客的话。
- 访客连续出声一会儿（on_guest_barge）：学生正在出声就停下来听。

全部跑在自己的线程和 asyncio 循环里；开会时 start()，散会 stop()。中继或声卡出问题只记日志，不影响开会。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
from collections import deque

from . import share as _share
from .ears import MIC, PLAYBACK

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- which mic (the same rule as record.py's pick_mic)
_PK_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_PK_DESCS = ("{026e516e-b814-414b-83cd-856d6fef4822},2", "{b3f8fa53-0004-438e-9003-51a46e139bfc},26",
             "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")
_VIRTUAL = ("virtual", "blackhole", "loopback", "soundflower", "zoom", "teams", "oray", "vb-audio", "voicemeeter",
            "stereo mix", "立体声混音", "aggregate", "聚集", "cable")


def _endpoints() -> list[dict]:
    """Windows' active audio endpoints (HKLM MMDevices): flow, the names they are shown by, on Bluetooth or not."""
    import re
    import sys
    if sys.platform != "win32":
        return []
    import winreg
    out, base = [], r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
    for flow in ("Render", "Capture"):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{flow}")
        except OSError:
            continue
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                dev = winreg.OpenKey(root, guid)
                if winreg.QueryValueEx(dev, "DeviceState")[0] != 1:
                    continue
                props, vals, j = winreg.OpenKey(dev, "Properties"), {}, 0
                while True:
                    try:
                        n, v, _t = winreg.EnumValue(props, j)
                    except OSError:
                        break
                    j += 1
                    if isinstance(v, str):
                        vals[n] = v
            except OSError:
                continue
            name = vals.get(_PK_NAME) or ""
            out.append({"flow": flow, "names": [f"{name} ({vals[k]})" for k in _PK_DESCS if vals.get(k)] + ([name] if name else []),
                        "bt": any(re.search(r"BTHENUM|BTHHFENUM|BTHLE", v, re.I) for v in vals.values())})
    return out


def _endpoint(name: str, flow: str, eps: list[dict]) -> dict:
    n = (name or "").strip().lower()
    for e in eps if n else []:
        for full in e["names"] if e["flow"] == flow else []:
            if full.lower() == n or (len(n) >= 24 and "(" in n and full.lower().startswith(n)):
                return {**e, "name": full}
    return {}


def pick_input(sd) -> int | None:
    """The bridge's mic: record.mic in Blurt's config when set; else, while the sound goes to a Bluetooth headset, the
    computer's own mic (the headset's would switch it to hands-free: mono, narrowband, hollow); else the default.
    Writes the names in use to ~/.zuhui/devices.json (the meeting page's card). None = PortAudio's default."""
    from pathlib import Path as _P
    idx, why, eps = None, None, None
    try:
        devs, apis = sd.query_devices(), sd.query_hostapis()
        mics = [(i, d) for i, d in enumerate(devs) if d["max_input_channels"] > 0
                and apis[d["hostapi"]]["name"] in ("MME", "Windows WASAPI")]
        try:
            from .mic_prefs import chosen            # 设置 → 语音 → 麦克风（$BLURT_HOME/config.json 的 record.mic）
            want = chosen()
        except Exception:
            want = None
        if isinstance(want, str) and want not in ("", "default", "off", "none"):
            idx = next((i for i, d in mics if want.lower() in d["name"].lower()), None)
        if idx is None:
            eps = _endpoints()
            out = sd.query_devices(kind="output")["name"]
            if eps and _endpoint(out, "Render", eps).get("bt"):
                default_in = sd.default.device[0]
                order = sorted(mics, key=lambda m: (m[0] != default_in, "WASAPI" in apis[m[1]["hostapi"]]["name"]))
                idx = next((i for i, d in order if not any(h in d["name"].lower() for h in _VIRTUAL)
                            and _endpoint(d["name"], "Capture", eps).get("bt") is False), None)
                why = "bluetooth_output" if idx is not None else None
        try:
            eps = eps if eps is not None else _endpoints()
            mic = sd.query_devices(idx if idx is not None else sd.default.device[0])["name"]
            spk = sd.query_devices(kind="output")["name"]
            home = _P(os.environ.get("ZUHUI_HOME") or _P.home() / ".zuhui")
            d = {"mic": _endpoint(mic, "Capture", eps).get("name") or mic, "speaker": _endpoint(spk, "Render", eps).get("name") or spk,
                 "by": "bridge", "why": why, "at": round(time.time(), 1)}
            tmp = home / f"devices.json.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, home / "devices.json")
        except Exception:
            pass
    except Exception as e:
        log.info("语音桥：选麦克风时出错，用默认的: %r", e)
        return None
    if why:
        log.info("语音桥：声音走蓝牙耳机，麦克风改用 %s（耳机自己的麦会让它进免提模式）", sd.query_devices(idx)["name"])
    return idx

SR = 48000
FRAME = SR // 100                      # 10 ms
CREATE_NO_WINDOW = 0x08000000


class AudioBridge:
    def __init__(self, cfg: dict, mouth, me: str = "主持人", devices: bool = True):
        self.cfg = cfg                              # 和后端共用的那份：设置里换了中继，这里马上看得到
        sc = dict(cfg.get("share") or {})
        self.enabled = sc.get("audio", True) is not False
        self.mouth = mouth
        self.me = me or "主持人"
        from .config import assistant_name
        self.ai = assistant_name(cfg)               # 会上那位 AI 的名字：访客页上它说话时亮圈靠这个名字对上
        self.devices = devices                    # False：不开扬声器、麦克风，也不推学生（测试用：只听访客、转写）
        self.error: str | None = None
        self.connected = False
        self.on_guest_text = None                 # (名字, 文字, info)：访客自己那一路直接转写出来的一段
        self.on_guest_barge = None                # (名字)：访客连续出声了一会儿
        self.on_guest_draft = None                # (名字, 初稿 或 None=作废, info)：访客停顿了，快识别先出的初稿
        self.on_follow = None                     # (名字, 消息)：访客页滑到了你在看的地方（或者没找到 / 他自己在看）
        self._host_room = None
        self.guest_ears = None
        try:
            from .guest_ears import GuestEars
            self.guest_ears = GuestEars(cfg, on_text=self._guest_text, on_barge=self._guest_barge,
                                        audible=lambda: bool(getattr(self.mouth, "audible", False)))
        except Exception as ex:                   # 缺组件：退回老办法（麦克风录到的认成访客）
            log.warning("语音桥：访客直接转写起不来：%r", ex)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._lock = threading.Lock()
        self._spans: list[tuple[str, str, float, float]] = []     # (身份, 名字, 开始, 结束) 说过话的时间段
        self._now: dict[str, tuple[str, float]] = {}              # 身份 -> (名字, 开始) 正在说
        self._vad_state: dict[str, list] = {}                     # 身份 -> [连续响了几帧, 最后一次响的时间]
        self._stu_src = None
        self.app_audio = None                     # 会议软件（腾讯会议等）放出来的声音：当成一位访客转写（zuhui/app_audio.py）
        self.app_label: str | None = None         # 现在挂着的会议软件的显示名（"腾讯会议"）；也是逐字稿上那一行的名字
        from .components import which
        self._ffmpeg = which("ffmpeg")
        self._spare = None                        # 提前起好、等着的转码进程：((采样率, 语速), 进程)
        self._spare_lock = threading.Lock()
        if mouth is not None and self.enabled and devices:
            mouth.on_play = self._student_play
            mouth.on_play_stream = self._student_play_stream

    # ---------- 对外 ----------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def hears_guests(self) -> bool:
        """有访客的声音进来：通过邀请链接进了语音房间，或者挂上了会议软件（腾讯会议）的声音。"""
        return bool(self.connected or self.app_label)

    @property
    def direct_ok(self) -> bool:
        """访客的话在直接转写（他自己那一路）：麦克风里漏进来的访客声音就不用再记了。"""
        ge = self.guest_ears
        return bool(self.hears_guests and ge is not None and ge.healthy)

    def guest_talking(self, within: float = 0.5) -> list[str]:
        """这会儿正在出声的访客的名字（判停说他最后一个有声帧离现在不到 within 秒）。"""
        ge = self.guest_ears
        return ge.talking(within) if (ge is not None and self.hears_guests) else []

    # ---------- 会议软件的声音（腾讯会议等，zuhui/app_audio.py） ----------
    APP_IDENT = "app-audio"

    def app_on(self) -> None:
        """开会时：找正在跑的会议软件，单独录它放出来的声音，当成一位访客（名字 = 软件名）直接转写进逐字稿。
        不用中继、不用开邀请；不分是谁在说。config 的 app_audio.enabled = false 关掉。"""
        if self.app_audio is not None or self.guest_ears is None:
            return
        if (self.cfg.get("app_audio") or {}).get("enabled", True) is False:
            return
        try:
            from . import app_audio
            if not app_audio.supported():
                return
            self.app_audio = app_audio.AppListener(self._app_pcm, on_state=self._app_state)
            self.app_audio.start()
        except Exception as ex:
            self.app_audio = None
            log.warning("会议软件的声音录不了：%r", ex)

    def app_off(self) -> None:
        """散会：不再录会议软件的声音。语音房间没开时访客转写是这里起的，没说完的那段转完交出去、转写进程停掉。"""
        a, self.app_audio = self.app_audio, None
        if a is not None:
            try:
                a.stop()
            except Exception:
                log.exception("停会议软件的声音出错")
        if self.app_label:
            self._app_state(None)
        if not self.running and self.guest_ears is not None:
            try:
                self.guest_ears.stop()
            except Exception:
                log.exception("停访客转写出错")

    def _app_state(self, label: str | None) -> None:
        ge, old = self.guest_ears, self.app_label
        if label:
            self.app_label = label
            if ge is not None:
                ge.start()                        # 语音房间没开（没邀请人）时访客转写还没起
                ge.joined(self.APP_IDENT, label)
            log.info("会议软件：录 %s 放出来的声音", label)
        else:
            self.app_label = None
            if ge is not None and old:
                ge.left(self.APP_IDENT)
            log.info("会议软件：%s 断开了", old)

    def _app_pcm(self, label: str, pcm: bytes, t: float) -> None:
        """会议软件放出来的一块声音（16 kHz 单声道，t = 第一个采样的墙上时间）：判谁在说、回声参考、直接转写，和访客那一路一样。"""
        try:
            import numpy as np
            a16 = np.frombuffer(pcm, dtype=np.int16)
            ident, n = self.APP_IDENT, 160
            for i in range(0, len(a16) - n + 1, n):
                a = a16[i:i + n].astype(np.float32)
                self._level(ident, label, float(np.sqrt(np.mean(a * a))), 300.0, t + (i + n) / 16000)
            t_end = t + len(a16) / 16000
            PLAYBACK.put(ident, a16, t_end, kind="guest")
            ge = self.guest_ears
            if ge is not None:
                if self.app_audio is not None and not (ge._thread and ge._thread.is_alive()):
                    ge.start()                    # 语音房间断了会把访客转写一起停掉：会议软件还挂着就再起
                ge.feed(ident, label, pcm, t_end)
        except Exception:
            log.debug("会议软件的声音没接上", exc_info=True)

    def guest_texts(self, name: str, ws: float, we: float) -> list[str]:
        """这位访客在 [ws, we] 前后直接转出来的字。"""
        ge = self.guest_ears
        return ge.texts_near(name, ws, we) if ge is not None else []

    @staticmethod
    def mic_off() -> bool:
        """你这一路不推给访客：你点了静音（ears.MIC），或者你正用另一台设备以访客身份参会（ears.PLAYBACK.host_elsewhere：
        家里没人，这台电脑的麦克风只会把扬声器的回声和空屋子的声音推出去）。"""
        return bool(MIC.muted or PLAYBACK.host_elsewhere)

    def _guest_draft(self, name: str, text, info) -> None:
        cb = self.on_guest_draft
        if cb is not None:
            cb(name, text, info)

    def _guest_text(self, name: str, text: str, info: dict) -> None:
        cb = self.on_guest_text
        if cb is not None:
            cb(name, text, info)

    def _guest_barge(self, name: str) -> None:
        cb = self.on_guest_barge
        if cb is not None:
            cb(name)

    def start(self) -> None:
        if not self.enabled or self.running:
            return
        if not self._can_join():
            return
        try:
            import livekit.rtc  # noqa: F401
            import sounddevice  # noqa: F401
        except Exception as ex:                       # 没装 LiveKit SDK / sounddevice：开会照常，只是访客听不到
            self.error = f"语音桥缺组件：{ex!r}"
            log.warning(self.error)
            return
        self._thread = threading.Thread(target=self._run, name="audio-bridge", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 5.0) -> None:
        self.app_off()
        loop, ev = self._loop, self._stop
        if loop and ev:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=wait)

    def guest_over(self, ws: float, we: float) -> str | None:
        """[ws, we] 这段（墙上时间）主要是哪位访客在说：访客说话的时间占一半以上、你那一路（回声消除后）几乎没声，
        返回访客的名字；否则 None。"""
        if we <= ws:
            return None
        dur = we - ws
        now = time.time()
        best, best_ov, me_ov = None, 0.0, 0.0
        with self._lock:
            spans = list(self._spans) + [(i, n, s, now) for i, (n, s) in self._now.items()]
        per: dict[str, float] = {}
        names: dict[str, str] = {}
        for ident, name, s, e in spans:
            ov = max(0.0, min(we, e + 0.3) - max(ws, s - 0.3))   # 说话检测比声音晚一点，前后各放宽 0.3 秒
            if ov <= 0:
                continue
            if ident == "bloom-host":
                me_ov += ov
            elif ident != "bloom-student":
                per[ident] = per.get(ident, 0.0) + ov
                names[ident] = name
        for ident, ov in per.items():
            if ov > best_ov:
                best, best_ov = ident, ov
        if best and best_ov >= 0.5 * dur and me_ov < 0.3 * dur:
            return names[best]
        return None

    def _level(self, ident: str, name: str, rms: float, th: float, now: float) -> None:
        """10 毫秒一帧的音量 → 说话的时间段：连续 3 帧过线算开口，0.4 秒没过线算说完。
        线是"固定下限"和"这一路平时的底噪 × 2.5"里大的那个：房间里一直有声音（空调、别人说话、视频），
        不会被当成一直在说话，访客开口时也就认得出来。底噪往下跟得快、往上跟得慢。"""
        st = self._vad_state.setdefault(ident, [0, 0.0, None])
        floor = st[2]
        if floor is None:
            floor = rms
        elif rms < floor:
            floor = 0.9 * floor + 0.1 * rms
        else:
            floor = 0.998 * floor + 0.002 * rms
        st[2] = floor
        loud = rms >= max(th, 2.5 * floor)
        st[0] = st[0] + 1 if loud else 0
        if loud:
            st[1] = now
        with self._lock:
            talking = ident in self._now
            if not talking and st[0] >= 3:
                self._now[ident] = (name, now - 0.03)
            elif talking and not loud and now - st[1] > 0.4:
                nm, s0 = self._now.pop(ident)
                self._spans.append((ident, nm, s0, st[1]))
                if len(self._spans) > 2000:
                    self._spans = [x for x in self._spans if now - x[3] < 300]

    def _sent(self, fut, what: str) -> None:
        """数据通道发送的结果：失败记一行（以前 try/except 吞掉，权限不对也看不出来）；一分钟最多记一次。"""
        def done(f, what=what):
            try:
                f.result()
            except Exception as ex:
                now = time.time()
                if now - getattr(self, "_send_err_t", 0.0) > 60:
                    self._send_err_t = now
                    log.warning("语音桥：数据通道发%s失败：%r", what, ex)
        try:
            fut.add_done_callback(done)
        except Exception:
            pass

    def send_focus(self, focus: dict) -> None:
        """你屏幕上正看着哪儿：马上走房间的数据通道推给所有访客（几十毫秒），不等中继那一轮。"""
        room, loop = self._host_room, self._loop
        if not (self.connected and room is not None and loop is not None):
            return
        body = json.dumps(dict(focus, t="focus"), ensure_ascii=False).encode("utf-8")
        try:
            self._sent(asyncio.run_coroutine_threadsafe(
                room.local_participant.publish_data(body, reliable=True, topic="bloom"), loop), "翻页位置")
        except Exception:
            pass

    def send_scroll(self, pos: dict) -> None:
        """滚动中的一帧：不保证送达的那种通道（丢一帧无所谓，下一帧就到），最快。"""
        room, loop = self._host_room, self._loop
        if not (self.connected and room is not None and loop is not None):
            return
        now = time.time()
        if now - getattr(self, "_scroll_t", 0.0) < 0.012:         # 兜底上限约 80 帧/秒（页面自己按 60 帧/秒发；停在哪儿另有可靠的 send_focus）
            return
        self._scroll_t = now
        body = json.dumps(pos, separators=(",", ":")).encode("utf-8")
        try:
            self._sent(asyncio.run_coroutine_threadsafe(
                room.local_participant.publish_data(body, reliable=False, topic="bloom-s"), loop), "滚动帧")
        except Exception:
            pass

    # ---------- 线程 / 循环 ----------
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as ex:
            self.error = f"{type(ex).__name__}: {ex}"[:300]
            log.warning("语音桥停了：%s", self.error)
        finally:
            self.connected = False
            self._stu_src = None
            if self.guest_ears is not None:           # 散会：还没说完的那段转完交出去，转写进程停掉
                try:
                    self.guest_ears.stop()
                except Exception:
                    log.exception("语音桥：停访客转写出错")
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None

    def _base(self) -> str:
        return str(((self.cfg.get("share") or {}).get("base_url")) or "").strip()

    def _can_join(self) -> bool:
        """自己的中继（有 LiveKit 密钥）一直能进；公共中继要打开了邀请才进（没邀请人，声音不发出去）。"""
        if not self._base():
            return False
        if _share.has_secret(self._base()):
            return True
        try:
            from .relay_cfg import invite_on
            return invite_on()
        except Exception:
            return False

    def _token(self, identity: str, name: str, role: str) -> str:
        if not _share.has_secret(self._base()):        # 公共中继：中继代签
            tok, url = _share.host_token(self._base(), identity, name, role)
            self._pub_url = url
            return tok
        e = _share._env()
        room = _share.room()
        return _share._jwt(e["LIVEKIT_API_KEY"], e["LIVEKIT_API_SECRET"],
                           {"roomJoin": True, "room": room, "canPublish": True, "canSubscribe": role == "teacher",
                            "canPublishData": role == "teacher"},   # 主持人推翻页（以前 False：SFU 全丢，只能走中继）
                           ttl=8 * 3600, identity=identity,
                           extra={"name": name, "metadata": json.dumps({"bloom_host": True, "role": role}, ensure_ascii=False)})

    def _url(self) -> str:
        if not _share.has_secret(self._base()):        # 公共中继：领通行码时中继给的 wss 地址
            d = _share.device(self._base()) or {}
            return getattr(self, "_pub_url", "") or str(d.get("ws") or "")
        e = _share._env()
        u = e.get("LIVEKIT_URL") or ""
        if not u:
            base = _share.DEFAULT_BASE.rstrip("/")
            u = base.replace("https://", "wss://").replace("http://", "ws://")
        return u

    async def _main(self) -> None:
        import numpy as np
        import sounddevice as sd
        from livekit import rtc
        from livekit.rtc.media_devices import OutputPlayer, _APMDelayEstimator

        self._stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        url = self._url()

        # ---- 你：麦克风（回声消除）+ 访客的声音从扬声器放 ----
        host = rtc.Room(loop=loop)
        apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=True, high_pass_filter=True,
                                        auto_gain_control=True)
        est = _APMDelayEstimator()
        player = (OutputPlayer(sample_rate=SR, num_channels=1, blocksize=SR // 20, apm_for_reverse=apm, delay_estimator=est)
                  if self.devices else None)
        added: set[str] = set()
        ge = self.guest_ears
        if ge is not None:
            ge.start()

        def is_host(p) -> bool:
            try:
                return bool(json.loads(p.metadata or "{}").get("bloom_host"))
            except ValueError:
                return False

        async def listen(track, ident: str, name: str):
            # 16 kHz 单声道（LiveKit 自己重采样）：判谁在说 + 直接转写 + 记成回声的参考（同一份正从扬声器放出去）
            st = rtc.AudioStream(track, sample_rate=16000, num_channels=1, frame_size_ms=10)
            try:
                async for ev in st:
                    a16 = np.frombuffer(ev.frame.data, dtype=np.int16)
                    now = time.time()
                    a = a16.astype(np.float32)
                    self._level(ident, name, float(np.sqrt(np.mean(a * a))) if a.size else 0.0, 300.0, now)
                    if player is not None:
                        PLAYBACK.put(ident, a16, now, kind="guest")
                    if ge is not None:
                        ge.feed(ident, name, a16.tobytes(), now)
            finally:
                await st.aclose()

        def on_sub(track, pub, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO and not is_host(participant) and track.sid not in added:
                added.add(track.sid)
                name = participant.name or participant.identity
                if player is not None:
                    asyncio.ensure_future(player.add_track(track))
                if ge is not None:
                    ge.joined(participant.identity, name)
                asyncio.ensure_future(listen(track, participant.identity, name))
                log.info("语音桥：放 %s 的声音", name)

        def on_unsub(track, pub, participant):
            if track.sid in added:
                added.discard(track.sid)
                if player is not None:
                    asyncio.ensure_future(player.remove_track(track))
                if ge is not None:
                    ge.left(participant.identity)

        host.on("track_subscribed", on_sub)
        host.on("track_unsubscribed", on_unsub)

        def on_data(pkt):                          # 访客页回报：跟到了哪儿
            try:
                if getattr(pkt, "topic", "") != "bloom":
                    return
                m = json.loads(bytes(pkt.data).decode("utf-8"))
            except Exception:
                return
            p = getattr(pkt, "participant", None)
            name = getattr(p, "name", None) or getattr(p, "identity", None) or "访客"
            cb = self.on_follow
            if m.get("t") == "at" and cb is not None:
                try:
                    cb(name, m)
                except Exception:
                    pass

        host.on("data_received", on_data)
        await host.connect(url, self._token("bloom-host", self.me, "teacher"),
                           rtc.RoomOptions(auto_subscribe=True, connect_timeout=15))
        self._host_room = host
        if player is None:                         # 测试：只听访客
            self.connected, self.error = True, None
            log.info("语音桥：已进房间 %s（只听访客）", _share.room())
            try:
                await self._stop.wait()
            finally:
                self.connected = False
                try:
                    await asyncio.wait_for(host.disconnect(), 5)
                except Exception:
                    pass
            return
        await player.start()

        mic_src = rtc.AudioSource(SR, 1, queue_size_ms=500)
        mic_track = rtc.LocalAudioTrack.create_audio_track("mic", mic_src)
        await host.local_participant.publish_track(mic_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        silence = bytes(FRAME * 2)

        def student_audible() -> bool:
            try:
                return bool(getattr(self.mouth, "audible", False))
            except Exception:
                return False

        def on_mic(indata, frames, time_info, status):
            try:
                ind = float(time_info.currentTime - time_info.inputBufferAdcTime)
                apm.set_stream_delay_ms(int(max((ind + est.get_output_delay()) * 1000.0, 0.0)))
            except Exception:
                pass
            hush = student_audible()
            off = self.mic_off()                           # 你点了静音 / 你在用别的设备参会：这一路推静音，也不算你在说
            for i in range(frames // FRAME):
                f = rtc.AudioFrame(indata[i * FRAME:(i + 1) * FRAME, 0].tobytes(), SR, 1, FRAME)
                try:
                    apm.process_stream(f)
                except Exception:
                    pass
                try:                                       # 你说没说话：看回声消除之后的声音（访客的声音已经消掉了）
                    a = np.frombuffer(f.data, dtype=np.int16).astype(np.float32)
                    self._level("bloom-host", self.me, 0.0 if off else float(np.sqrt(np.mean(a * a))), 400.0, time.time())
                except Exception:
                    pass
                if hush or off:                            # 学生在出声：这一路推静音（见文件头）
                    f = rtc.AudioFrame(silence, SR, 1, FRAME)
                if not q.full():
                    loop.call_soon_threadsafe(q.put_nowait, f)

        mic = sd.InputStream(callback=on_mic, dtype="int16", channels=1, samplerate=SR, blocksize=SR // 20,
                             device=pick_input(sd))
        mic.start()

        async def pump():
            while True:
                f = await q.get()
                try:
                    await mic_src.capture_frame(f)
                except Exception:
                    pass

        pump_task = asyncio.create_task(pump())

        # ---- 学生：单独一个身份，推 Mouth 放的那句 ----
        stu = rtc.Room(loop=loop)
        await stu.connect(url, self._token("bloom-student", self.ai, "student"),
                          rtc.RoomOptions(auto_subscribe=False, connect_timeout=15))
        stu_src = rtc.AudioSource(SR, 1, queue_size_ms=1000)
        stu_track = rtc.LocalAudioTrack.create_audio_track("student", stu_src)
        await stu.local_participant.publish_track(stu_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        self._stu_src = stu_src
        self.connected, self.error = True, None
        threading.Thread(target=self._refill_spare, args=(32000, float(getattr(self.mouth, "rate", 1.0) or 1.0)),
                         name="bridge-spare", daemon=True).start()   # 第一句之前先起好一个转码进程
        log.info("语音桥：已进房间 %s（你 + 学生）", _share.room())

        try:
            await self._stop.wait()
        finally:
            self.connected = False
            self._stu_src = None
            with self._spare_lock:
                sp, self._spare = self._spare, None
            if sp is not None and sp[1].poll() is None:
                try:
                    sp[1].kill()
                except Exception:
                    pass
            pump_task.cancel()
            try:
                mic.stop()
                mic.close()
            except Exception:
                pass
            for coro in (player.aclose(), mic_src.aclose(), stu_src.aclose(), host.disconnect(), stu.disconnect()):
                try:
                    await asyncio.wait_for(coro, 5)
                except Exception:
                    pass
            with self._lock:
                now = time.time()
                for ident, (name, s) in self._now.items():
                    self._spans.append((ident, name, s, now))
                self._now.clear()
            log.info("语音桥：已离开房间")

    # ---------- 学生的声音 ----------
    def _student_play(self, path, rate: float, proc) -> None:
        """Mouth 刚开始用 ffplay 放一句：把同一个文件按同样的语速解码，推给房间；ffplay 停了（放完或被打断）就停。"""
        if not (self.connected and self._stu_src is not None and self._loop and self._ffmpeg):
            return
        threading.Thread(target=self._student_push, args=(str(path), rate, proc), name="bridge-student", daemon=True).start()

    def _student_play_stream(self, st, rate: float, proc) -> None:
        """Mouth 边合成边播的一句：从同一个流从头读，按同样的语速转成房间的采样率推出去。"""
        if not (self.connected and self._stu_src is not None and self._loop and self._ffmpeg):
            return
        threading.Thread(target=self._student_push, args=(None, rate, proc, st), name="bridge-student", daemon=True).start()

    def _dec_cmd(self, sr: int, rate: float) -> list:
        cmd = [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(sr), "-ch_layout", "mono",
               "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer", "-i", "pipe:0"]
        if abs(rate - 1.0) > 0.01:
            cmd += ["-af", f"atempo={min(2.0, max(0.5, rate)):.2f}"]
        return cmd + ["-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]

    def _spawn_dec(self, sr: int, rate: float):
        return subprocess.Popen(self._dec_cmd(sr, rate), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)

    def _refill_spare(self, sr: int, rate: float) -> None:
        if not self._ffmpeg:
            return
        try:
            p = self._spawn_dec(sr, rate)
        except Exception:
            return
        with self._spare_lock:
            old, self._spare = self._spare, ((sr, round(rate, 2)), p)
        if old is not None and old[1].poll() is None:
            try:
                old[1].kill()
            except Exception:
                pass

    def _take_dec(self, sr: int, rate: float):
        """流的转码进程：有提前起好的就用它（久没运行时冷起要一秒多），再在后台补一个。"""
        with self._spare_lock:
            sp, self._spare = self._spare, None
        p = sp[1] if (sp is not None and sp[0] == (sr, round(rate, 2)) and sp[1].poll() is None) else None
        if sp is not None and p is None and sp[1].poll() is None:
            try:
                sp[1].kill()
            except Exception:
                pass
        if p is None:
            p = self._spawn_dec(sr, rate)
        threading.Thread(target=self._refill_spare, args=(sr, rate), name="bridge-spare", daemon=True).start()
        return p

    def _student_hold(self, src, loop, recent, proc):
        """嘴先停一下（mouth.pause）：推给访客的这一路跟着停。访客那边还排着没放的（最多约 1 秒）先清掉，
        等嘴恢复后先补推这几帧再接着推，访客听到的和你这边一样从停的地方接上。→ 要补推的帧；这句被打断了 → None。"""
        dead = lambda: proc is not None and proc.poll() is not None and proc.returncode not in (0, None)   # noqa: E731
        try:
            q = float(src.queued_duration or 0.0)
        except Exception:
            q = 0.0
        try:
            loop.call_soon_threadsafe(src.clear_queue)
        except Exception:
            pass
        k = min(len(recent), int(round(q * SR / FRAME)))
        again = list(recent)[len(recent) - k:] if k > 0 else []
        recent.clear()
        while getattr(self.mouth, "paused", False) and self.connected:
            if dead():
                return None
            time.sleep(0.02)
        return None if dead() or not self.connected else again

    def _student_push(self, path, rate: float, proc, st=None) -> None:
        from livekit import rtc
        src, loop = self._stu_src, self._loop
        if src is None or loop is None:
            return
        cmd = None
        if st is None:
            cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path]
            if abs(rate - 1.0) > 0.01:
                cmd += ["-af", f"atempo={min(2.0, max(0.5, rate)):.2f}"]
            cmd += ["-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
        dec = None
        try:
            if st is not None:                                  # 流：裸 PCM 从管道进（用提前起好的转码进程）
                dec = self._take_dec(st.sr or 32000, rate)
            else:
                dec = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
            if st is not None:
                def feed(d=dec):
                    alive = lambda: proc is None or proc.poll() is None or proc.returncode == 0   # noqa: E731
                    try:
                        for b in st.iter(alive):
                            d.stdin.write(b)
                            d.stdin.flush()      # 写一块就送进去（不 flush 的话每块尾巴要等下一块，访客那边会一顿一顿）
                    except Exception:
                        pass
                    finally:
                        try:
                            d.stdin.close()
                        except Exception:
                            pass
                threading.Thread(target=feed, name="bridge-student-feed", daemon=True).start()
            n = FRAME * 2
            recent: deque = deque(maxlen=int(1.5 * SR / FRAME))    # 最近推出去的帧：嘴停一下时访客那边还排着没放的，恢复后补推
            while True:
                if proc is not None and proc.poll() is not None and proc.returncode not in (0, None):
                    break                                       # 被打断（ffplay 被杀）
                if getattr(self.mouth, "paused", False):        # 嘴先停一下（你开口了，还在听是不是打断）：访客这边也停
                    again = self._student_hold(src, loop, recent, proc)
                    if again is None:
                        break
                    for b in again:
                        asyncio.run_coroutine_threadsafe(src.capture_frame(rtc.AudioFrame(b, SR, 1, FRAME)), loop).result(timeout=3)
                        recent.append(b)
                    continue
                buf = dec.stdout.read(n)
                if not buf:
                    break
                if len(buf) < n:
                    buf += bytes(n - len(buf))
                fut = asyncio.run_coroutine_threadsafe(src.capture_frame(rtc.AudioFrame(buf, SR, 1, FRAME)), loop)
                fut.result(timeout=3)
                recent.append(buf)
        except Exception as ex:
            log.debug("语音桥：学生这句没推完：%r", ex)
        finally:
            if dec is not None:
                try:
                    dec.kill()
                except Exception:
                    pass
            if proc is not None and proc.poll() is not None and proc.returncode not in (0, None):
                try:
                    loop.call_soon_threadsafe(src.clear_queue)
                except Exception:
                    pass
