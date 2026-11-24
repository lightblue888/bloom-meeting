# -*- coding: utf-8 -*-
"""按程序录声音：只录某个程序（腾讯会议、Zoom……）放出来的声音，不混扬声器里别的声音，也不录麦克风。

为什么：线上组会时，别的参会人的话是从会议软件里放出来的。麦克风要么只听得到你自己（戴耳机），
要么混着回声和 AI 的声音（外放）。Windows 10 2004（build 19041）起 WASAPI 能按进程回环录音
（process loopback），只拿这个进程树放出来的声音，跟系统音量、默认设备、别的程序都无关。
这里用纯 ctypes 调，不加依赖（numpy 只用来下混和降采样）。

接口：
  APPS                          进程名（小写）→ 显示名
  supported()                   这台 Windows 能不能按程序录
  find_app(apps=None)           正在跑的会议软件 → (根进程 pid, 进程名, 显示名) | None
                                腾讯会议是一堆 wemeetapp.exe，取"父进程不是同名程序"的那个；录的时候带上整棵进程树
  tree_pids(pid)                这个进程和它所有的子孙
  activity(pids)                这些进程现在有没有活动的音频会话 → (麦克风, 扬声器) | None（查不了）
  AppCapture(pid, on_pcm)       on_pcm(pcm, t)：16 kHz 单声道 s16le，每块 10 ms 的整数倍，t = 这块第一个采样的墙上时间
  AppListener(on_pcm, on_state) 后台每 poll_s 秒找一次会议软件，看它是不是在开会：在开会就挂上录，开完了、关了就断开。
                                on_pcm(显示名, pcm, t)；on_state(显示名 | None) 开始开会、开完时各一次。
                                set_enabled(False) 只看不录（会议页上点了「停止收听」）

在不在开会：会议软件开机自启、挂在托盘里时进程一直在，但它的音频会话都是不活动的；开会时麦克风会话是活动的
（静音了也开着，用来提示"你在说话"），扬声器会话也一直开着。以前只看进程在不在，托盘里的腾讯会议也挂上录，
它放的一声提示音被转写成一句 "I'm not sure." 记在「腾讯会议」名下（2026-11-25 组会）。

怎么录：
  ActivateAudioInterfaceAsync("VAD\\Process_Loopback", IAudioClient, PROCESS_LOOPBACK + 进程树) 拿到 IAudioClient，
  按 16 kHz 单声道 16 位初始化（让 Windows 自己转格式）；不行就按 48 kHz 双声道录，自己下混、降到 16 kHz。
  事件驱动取包，抓取线程是 MTA。完成回调是手搓的 COM 对象（QueryInterface/AddRef/Release/ActivateCompleted）。

输出是不间断的帧流，下游按帧判停，断流会让"正在说话"一直挂着。实测（Windows 11 26200）不出声的进程
也每 10 ms 送一包（带 ±1 的抖动），但包也会断（丢包、系统忙、进程正在退出），所以照样按墙上时间补静音：
没包时只补到"现在 - LAG_S"，晚一点到的包还能接上；包的起点（按 QPC 时间戳）比流的末尾晚，就在中间补静音。

目标进程退出：抓取报错或者一直没包都照样补静音；确认进程没了（进程句柄已触发 / 退出码）才 alive=False，
AppListener 看到后断开、接着找。进程还在但抓取一直报错超过 ERR_GRACE_S，也算断了（error 里写原因），让监听器重挂。

测试：
  python -X utf8 -m unittest tests.test_app_audio -v              会放 2 秒 1 kHz 测试音
  python -X utf8 -m zuhui.app_audio --seconds 20 --wav out.wav    手动：录正在跑的会议软件，每秒打一行音量
  python -X utf8 -m zuhui.app_audio --pid 1234                    录指定进程
"""
from __future__ import annotations

import ctypes
import logging
import math
import sys
import threading
import time
import uuid
from collections import deque

import numpy as np

log = logging.getLogger("zuhui.app_audio")

APPS: dict[str, str] = {
    "wemeetapp.exe": "腾讯会议",
    "zoom.exe": "Zoom",
    "ms-teams.exe": "Teams",
    "teams.exe": "Teams",
    "feishu.exe": "飞书",
    "lark.exe": "Lark",
    "dingtalk.exe": "钉钉",
}

RATE = 16000                 # 往外送的采样率
FRAME = RATE // 100          # 10 ms = 160 个采样
FRAME_BYTES = FRAME * 2
MAX_FRAMES_PER_CALL = 4      # 一次 on_pcm 最多 40 ms（补静音、包攒多了时按这个切）
BUFFER_MS = 200              # WASAPI 缓冲
WAIT_MS = 20                 # 等包的超时：没包时也每 20 ms 醒一次补静音，帧流不至于一顿一顿的
LAG_S = 0.1                  # 补静音只补到"现在 - 0.1 秒"，晚到的包还能接在后面，不被静音顶掉
GAP_TOL_S = 0.02             # 包的起点比流的末尾晚过这个数，中间补静音
CHECK_S = 0.5                # 每隔这么久看一次目标进程还在不在
ERR_GRACE_S = 3.0            # 抓取报错但进程还在，这么久还没好就算断了
ACTIVATE_TIMEOUT_S = 5.0     # 等激活回调
MIN_BUILD = 19041            # Windows 10 2004
BUF_DISCONTINUITY = 0x1      # AUDCLNT_BUFFERFLAGS_*
BUF_SILENT = 0x2
BUF_TIMESTAMP_ERROR = 0x4
E_RENDER, E_CAPTURE = 0, 1   # 音频端点方向：扬声器、麦克风
AUDIO_SESSION_ACTIVE = 1     # AudioSessionState：0 不活动，1 活动，2 已失效


class AppAudioError(Exception):
    """激活 / 初始化失败。hr = HRESULT（无符号 32 位），没有就是 None。"""

    def __init__(self, msg: str, hr: int | None = None):
        super().__init__(msg)
        self.hr = None if hr is None else (hr & 0xFFFFFFFF)


def _hex(hr) -> str:
    return "0x%08X" % ((hr or 0) & 0xFFFFFFFF)


def supported() -> bool:
    """Windows 10 2004（build 19041）以上才有 process loopback。"""
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().build >= MIN_BUILD
    except Exception:
        return False


class _Down3:
    """48 kHz → 16 kHz：低通（7.2 kHz，95 阶加窗 sinc）后每 3 个取 1 个，跨包保留滤波器状态和抽取相位。"""

    TAPS = 95

    def __init__(self):
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2
        fc = 7200 / 48000
        h = 2 * fc * np.sinc(2 * fc * n) * np.blackman(self.TAPS)
        self.h = (h / h.sum()).astype(np.float32)
        self.hist = np.zeros(self.TAPS - 1, np.float32)
        self.phase = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.hist, x.astype(np.float32, copy=False)])
        y = np.convolve(buf, self.h, mode="valid")          # 长度 = len(x)
        out = y[self.phase::3]
        self.phase = (self.phase - len(y)) % 3
        self.hist = buf[len(buf) - (self.TAPS - 1):]
        return out


# ---------------------------------------------------------------- Win32 / COM 声明

if sys.platform == "win32":
    from ctypes import POINTER, Structure, byref, c_int, c_int64, c_long, c_uint32, c_uint64, c_ulong, c_ushort, c_void_p, c_wchar, c_wchar_p

    # 自己的 WinDLL 实例：设 argtypes 不影响别的模块用 ctypes.windll.kernel32
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32")
    _mmdev = None

    class _GUID(Structure):
        _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def _guid(s: str) -> "_GUID":
        return _GUID.from_buffer_copy(uuid.UUID(s).bytes_le)

    IID_IUnknown = _guid("00000000-0000-0000-C000-000000000046")
    IID_IAgileObject = _guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
    IID_IActivateCompletion = _guid("41D949AB-9862-444A-80F6-C261334DA5EB")
    IID_IAudioClient = _guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
    IID_IAudioCaptureClient = _guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317")
    _HANDLER_IIDS = {bytes(IID_IUnknown), bytes(IID_IAgileObject), bytes(IID_IActivateCompletion)}

    S_OK = 0
    E_NOINTERFACE = -0x7FFFBFFE          # 0x80004002
    E_POINTER = -0x7FFFBFFD              # 0x80004003
    AUDCLNT_S_BUFFER_EMPTY = 0x08890001
    AUDCLNT_SHAREMODE_SHARED = 0
    AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
    AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
    AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
    AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
    STREAM_FLAGS = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                    | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
    WAVE_FORMAT_PCM = 1
    VT_BLOB = 65
    ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
    PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
    COINIT_MULTITHREADED = 0
    TH32CS_SNAPPROCESS = 0x2
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    WAIT_OBJECT_0 = 0
    _INVALID_HANDLE = c_void_p(-1).value

    class _WAVEFORMATEX(Structure):
        _pack_ = 1
        _fields_ = [("wFormatTag", c_ushort), ("nChannels", c_ushort), ("nSamplesPerSec", c_ulong),
                    ("nAvgBytesPerSec", c_ulong), ("nBlockAlign", c_ushort), ("wBitsPerSample", c_ushort),
                    ("cbSize", c_ushort)]

    class _LOOPBACK_PARAMS(Structure):
        _fields_ = [("TargetProcessId", c_ulong), ("ProcessLoopbackMode", c_int)]

    class _ACTIVATION_PARAMS(Structure):
        _fields_ = [("ActivationType", c_int), ("ProcessLoopbackParams", _LOOPBACK_PARAMS)]

    class _BLOB(Structure):
        _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]

    class _PROPVARIANT(Structure):
        _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("blob", _BLOB)]

    class _PE32(Structure):            # PROCESSENTRY32W
        _fields_ = [("dwSize", c_ulong), ("cntUsage", c_ulong), ("th32ProcessID", c_ulong),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", c_ulong), ("cntThreads", c_ulong),
                    ("th32ParentProcessID", c_ulong), ("pcPriClassBase", c_long), ("dwFlags", c_ulong),
                    ("szExeFile", c_wchar * 260)]

    _k32.CreateEventW.argtypes = [c_void_p, c_int, c_int, c_wchar_p]
    _k32.CreateEventW.restype = c_void_p
    _k32.WaitForSingleObject.argtypes = [c_void_p, c_ulong]
    _k32.WaitForSingleObject.restype = c_ulong
    _k32.CloseHandle.argtypes = [c_void_p]
    _k32.CloseHandle.restype = c_int
    _k32.OpenProcess.argtypes = [c_ulong, c_int, c_ulong]
    _k32.OpenProcess.restype = c_void_p
    _k32.GetExitCodeProcess.argtypes = [c_void_p, POINTER(c_ulong)]
    _k32.GetExitCodeProcess.restype = c_int
    _k32.GetProcessTimes.argtypes = [c_void_p, POINTER(c_uint64), POINTER(c_uint64), POINTER(c_uint64), POINTER(c_uint64)]
    _k32.GetProcessTimes.restype = c_int
    _k32.CreateToolhelp32Snapshot.argtypes = [c_ulong, c_ulong]
    _k32.CreateToolhelp32Snapshot.restype = c_void_p
    _k32.Process32FirstW.argtypes = [c_void_p, POINTER(_PE32)]
    _k32.Process32FirstW.restype = c_int
    _k32.Process32NextW.argtypes = [c_void_p, POINTER(_PE32)]
    _k32.Process32NextW.restype = c_int
    _k32.QueryPerformanceCounter.argtypes = [POINTER(c_int64)]
    _k32.QueryPerformanceCounter.restype = c_int
    _k32.QueryPerformanceFrequency.argtypes = [POINTER(c_int64)]
    _k32.QueryPerformanceFrequency.restype = c_int
    _ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
    _ole32.CoInitializeEx.restype = c_long
    _ole32.CoUninitialize.argtypes = []
    _ole32.CoUninitialize.restype = None

    _QPF = c_int64()
    _k32.QueryPerformanceFrequency(byref(_QPF))

    def _qpc_100ns() -> int:
        c = c_int64()
        _k32.QueryPerformanceCounter(byref(c))
        return c.value * 10_000_000 // (_QPF.value or 1)

    # COM 方法原型（第一个参数都是 this）
    _P_QI = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(_GUID), POINTER(c_void_p))
    _P_RELEASE = ctypes.WINFUNCTYPE(c_ulong, c_void_p)
    _P_GET_RESULT = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_long), POINTER(c_void_p))
    _P_INIT = ctypes.WINFUNCTYPE(c_long, c_void_p, c_int, c_ulong, ctypes.c_longlong, ctypes.c_longlong,
                                 POINTER(_WAVEFORMATEX), c_void_p)
    _P_VOID = ctypes.WINFUNCTYPE(c_long, c_void_p)
    _P_SET_EVENT = ctypes.WINFUNCTYPE(c_long, c_void_p, c_void_p)
    _P_GET_SERVICE = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(_GUID), POINTER(c_void_p))
    _P_GET_BUFFER = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p), POINTER(c_uint32), POINTER(c_ulong),
                                       POINTER(c_uint64), POINTER(c_uint64))
    _P_RELEASE_BUFFER = ctypes.WINFUNCTYPE(c_long, c_void_p, c_uint32)
    _P_NEXT_PACKET = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_uint32))

    # vtable 下标
    _QI, _RELEASE = 0, 2
    _OP_GET_RESULT = 3
    _AC_INITIALIZE, _AC_START, _AC_STOP, _AC_SET_EVENT, _AC_GET_SERVICE = 3, 10, 11, 13, 14
    _CC_GET_BUFFER, _CC_RELEASE_BUFFER, _CC_NEXT_PACKET = 3, 4, 5

    class _Com:
        """一个 COM 接口指针：按 vtable 下标调方法（函数对象缓存起来，抓取循环里一秒调几百次）。"""

        __slots__ = ("ptr", "_fns")

        def __init__(self, ptr: int):
            self.ptr = ptr
            self._fns = {}

        def call(self, idx: int, proto, *args):
            fn = self._fns.get(idx)
            if fn is None:
                vtbl = ctypes.cast(self.ptr, POINTER(POINTER(c_void_p))).contents
                fn = self._fns[idx] = proto(vtbl[idx])
            return fn(self.ptr, *args)

        def release(self) -> None:
            if self.ptr:
                try:
                    self.call(_RELEASE, _P_RELEASE)
                except Exception:
                    pass
                self.ptr = None

    # ---- 激活完成回调：手搓的 IActivateAudioInterfaceCompletionHandler
    # vtable 和四个回调函数对象是模块级的，一直留着引用；每个回调对象只有一个 lpVtbl 指针，
    # 按地址在 _HANDLERS 里找到它的 Event。引用数归零后对象挪进 _RETIRED（有上限）再留一阵，
    # 系统万一晚一点再调，也打在有效内存上。
    _HANDLERS: dict[int, list] = {}      # 地址 → [对象, Event, 引用数]
    _RETIRED: deque = deque(maxlen=64)
    _HLOCK = threading.Lock()

    class _HandlerObj(Structure):
        _fields_ = [("lpVtbl", c_void_p)]

    def _h_addref(this):
        with _HLOCK:
            h = _HANDLERS.get(this)
            if h is None:
                return 1
            h[2] += 1
            return h[2]

    def _h_release(this):
        with _HLOCK:
            h = _HANDLERS.get(this)
            if h is None:
                return 0
            h[2] -= 1
            if h[2] > 0:
                return h[2]
            _RETIRED.append(_HANDLERS.pop(this))
            return 0

    def _h_qi(this, riid, ppv):
        try:
            if not ppv:
                return E_POINTER
            if riid and ctypes.string_at(riid, 16) in _HANDLER_IIDS:
                ppv[0] = this
                _h_addref(this)
                return S_OK
            ppv[0] = None
            return E_NOINTERFACE
        except Exception:
            return E_NOINTERFACE

    def _h_done(this, op):
        try:
            with _HLOCK:
                h = _HANDLERS.get(this)
            if h is not None:
                h[1].set()
        except Exception:
            pass
        return S_OK

    _CALLBACKS = (
        ctypes.WINFUNCTYPE(c_long, c_void_p, c_void_p, POINTER(c_void_p))(_h_qi),
        ctypes.WINFUNCTYPE(c_ulong, c_void_p)(_h_addref),
        ctypes.WINFUNCTYPE(c_ulong, c_void_p)(_h_release),
        ctypes.WINFUNCTYPE(c_long, c_void_p, c_void_p)(_h_done),
    )
    _VTBL = (c_void_p * 4)(*[ctypes.cast(f, c_void_p).value for f in _CALLBACKS])

    def _new_handler() -> tuple[int, threading.Event]:
        obj = _HandlerObj(ctypes.addressof(_VTBL))
        ev = threading.Event()
        addr = ctypes.addressof(obj)
        with _HLOCK:
            _HANDLERS[addr] = [obj, ev, 1]
        return addr, ev

    def _activate(pid: int) -> "_Com":
        """激活一个只录 pid 进程树的 IAudioClient（还没 Initialize）。在 MTA 线程里调。"""
        global _mmdev
        if _mmdev is None:
            try:
                dll = ctypes.WinDLL("Mmdevapi")
                fn = dll.ActivateAudioInterfaceAsync
            except (OSError, AttributeError) as e:
                raise AppAudioError(f"激活失败：{e}") from None
            fn.argtypes = [c_wchar_p, POINTER(_GUID), POINTER(_PROPVARIANT), c_void_p, POINTER(c_void_p)]
            fn.restype = c_long
            _mmdev = fn
        params = _ACTIVATION_PARAMS(ACTIVATION_TYPE_PROCESS_LOOPBACK,
                                    _LOOPBACK_PARAMS(pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE))
        pv = _PROPVARIANT()
        pv.vt = VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.addressof(params)
        addr, ev = _new_handler()
        op = c_void_p()
        try:
            hr = _mmdev("VAD\\Process_Loopback", byref(IID_IAudioClient), byref(pv), addr, byref(op))
            if hr < 0 or not op.value:
                raise AppAudioError(f"激活失败：{_hex(hr)}", hr)
            opc = _Com(op.value)
            try:
                if not ev.wait(ACTIVATE_TIMEOUT_S):
                    raise AppAudioError("激活失败：超时")
                ahr, unk = c_long(), c_void_p()
                hr = opc.call(_OP_GET_RESULT, _P_GET_RESULT, byref(ahr), byref(unk))
                if hr < 0:
                    raise AppAudioError(f"激活失败：{_hex(hr)}", hr)
                if ahr.value < 0 or not unk.value:
                    raise AppAudioError(f"激活失败：{_hex(ahr.value)}", ahr.value)
                u = _Com(unk.value)
                try:
                    client = c_void_p()
                    hr = u.call(_QI, _P_QI, byref(IID_IAudioClient), byref(client))
                    if hr < 0 or not client.value:
                        raise AppAudioError(f"激活失败：{_hex(hr)}", hr)
                    return _Com(client.value)
                finally:
                    u.release()
            finally:
                opc.release()
        finally:
            _h_release(addr)

    # ---- 音频会话：会议软件现在有没有在用麦克风、扬声器
    CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
    IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
    IID_IAudioSessionManager2 = _guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
    IID_IAudioSessionControl2 = _guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d")
    CLSCTX_ALL = 0x17
    DEVICE_STATE_ACTIVE = 1
    _ole32.CoCreateInstance.argtypes = [POINTER(_GUID), c_void_p, c_ulong, POINTER(_GUID), POINTER(c_void_p)]
    _ole32.CoCreateInstance.restype = c_long
    _P_ENUM_ENDPOINTS = ctypes.WINFUNCTYPE(c_long, c_void_p, c_int, c_ulong, POINTER(c_void_p))
    _P_COUNT_U = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_uint32))
    _P_ITEM = ctypes.WINFUNCTYPE(c_long, c_void_p, c_uint32, POINTER(c_void_p))
    _P_DEV_ACTIVATE = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(_GUID), c_ulong, c_void_p, POINTER(c_void_p))
    _P_OUT_PTR = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_void_p))
    _P_COUNT_I = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_int))
    _P_GET_SESSION = ctypes.WINFUNCTYPE(c_long, c_void_p, c_int, POINTER(c_void_p))
    _P_GET_PID = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(c_ulong))
    _EN_ENUM_ENDPOINTS = 3
    _COL_COUNT, _COL_ITEM = 3, 4
    _DEV_ACTIVATE = 3
    _SM2_GET_ENUMERATOR = 5
    _SE_COUNT, _SE_GET = 3, 4
    _SC_GET_STATE = 3
    _SC2_GET_PID = 14

    def _sessions(flow: int) -> list[tuple[int, int]]:
        """一个方向（E_RENDER 扬声器 / E_CAPTURE 麦克风）所有启用的设备上的音频会话：[(pid, 状态)]。
        调用的线程要已经 CoInitializeEx。"""
        en = c_void_p()
        hr = _ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL, byref(IID_IMMDeviceEnumerator), byref(en))
        if hr < 0 or not en.value:
            raise AppAudioError(f"查会话失败：{_hex(hr)}", hr)
        out: list[tuple[int, int]] = []
        held: list[_Com] = [_Com(en.value)]
        try:
            col = c_void_p()
            hr = held[0].call(_EN_ENUM_ENDPOINTS, _P_ENUM_ENDPOINTS, flow, DEVICE_STATE_ACTIVE, byref(col))
            if hr < 0 or not col.value:
                raise AppAudioError(f"查会话失败：{_hex(hr)}", hr)
            cc = _Com(col.value)
            held.append(cc)
            n = c_uint32()
            cc.call(_COL_COUNT, _P_COUNT_U, byref(n))
            for i in range(n.value):
                dev = c_void_p()
                if cc.call(_COL_ITEM, _P_ITEM, i, byref(dev)) < 0 or not dev.value:
                    continue
                d = _Com(dev.value)
                mgr = se = None
                try:
                    p = c_void_p()
                    if d.call(_DEV_ACTIVATE, _P_DEV_ACTIVATE, byref(IID_IAudioSessionManager2), CLSCTX_ALL, None, byref(p)) < 0 or not p.value:
                        continue
                    mgr = _Com(p.value)
                    p = c_void_p()
                    if mgr.call(_SM2_GET_ENUMERATOR, _P_OUT_PTR, byref(p)) < 0 or not p.value:
                        continue
                    se = _Com(p.value)
                    k = c_int()
                    se.call(_SE_COUNT, _P_COUNT_I, byref(k))
                    for j in range(k.value):
                        p = c_void_p()
                        if se.call(_SE_GET, _P_GET_SESSION, j, byref(p)) < 0 or not p.value:
                            continue
                        sc = _Com(p.value)
                        try:
                            st, q, pid = c_int(), c_void_p(), c_ulong()
                            sc.call(_SC_GET_STATE, _P_COUNT_I, byref(st))
                            if sc.call(_QI, _P_QI, byref(IID_IAudioSessionControl2), byref(q)) >= 0 and q.value:
                                sc2 = _Com(q.value)
                                try:
                                    sc2.call(_SC2_GET_PID, _P_GET_PID, byref(pid))
                                finally:
                                    sc2.release()
                            out.append((int(pid.value), int(st.value)))
                        finally:
                            sc.release()
                finally:
                    for c in (se, mgr, d):
                        if c is not None:
                            c.release()
        finally:
            for c in reversed(held):
                c.release()
        return out

    def _coinit() -> bool:
        return _ole32.CoInitializeEx(None, COINIT_MULTITHREADED) >= 0

    def _couninit() -> None:
        _ole32.CoUninitialize()

    def _snapshot() -> list[tuple[int, int, str]]:
        """所有进程的 (pid, 父 pid, exe 名)。"""
        h = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not h or h == _INVALID_HANDLE:
            return []
        out = []
        try:
            e = _PE32()
            e.dwSize = ctypes.sizeof(_PE32)
            ok = _k32.Process32FirstW(h, byref(e))
            while ok:
                out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
                ok = _k32.Process32NextW(h, byref(e))
        finally:
            _k32.CloseHandle(h)
        return out

    def _created(pid: int) -> int | None:
        """进程创建时间（FILETIME，100 ns）；打不开就 None。"""
        h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            c, x, k, u = c_uint64(), c_uint64(), c_uint64(), c_uint64()
            if _k32.GetProcessTimes(h, byref(c), byref(x), byref(k), byref(u)):
                return c.value
            return None
        finally:
            _k32.CloseHandle(h)

    class _Proc:
        """拿着目标进程的句柄判断它退没退（pid 被复用也不会认错）。"""

        def __init__(self, pid: int):
            self.pid = pid
            self.sync = True
            self.h = _k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not self.h:
                self.sync = False
                self.h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not self.h:
                err = ctypes.get_last_error()
                if err != ERROR_ACCESS_DENIED:
                    raise AppAudioError(f"录制失败：找不到进程 {pid}")
                self.h = None       # 打不开（别的用户 / 受保护进程）：退回看进程快照
            if self.exited():
                self.close()
                raise AppAudioError(f"录制失败：进程 {pid} 已退出")

        def exited(self) -> bool:
            h = self.h
            if h is None:
                return all(p != self.pid for p, _, _ in _snapshot())
            if self.sync:
                return _k32.WaitForSingleObject(h, 0) == WAIT_OBJECT_0
            code = c_ulong()
            if not _k32.GetExitCodeProcess(h, byref(code)):
                return True
            return code.value != STILL_ACTIVE

        def close(self) -> None:
            h, self.h = self.h, None
            if h:
                _k32.CloseHandle(h)
else:
    def _snapshot() -> list[tuple[int, int, str]]:
        return []

    def _sessions(flow: int) -> list[tuple[int, int]]:
        raise AppAudioError("查会话失败：不是 Windows")

    def _coinit() -> bool:
        return False

    def _couninit() -> None:
        pass


# ---------------------------------------------------------------- 找会议软件

def find_app(apps: dict | None = None) -> tuple[int, str, str] | None:
    """正在跑的会议软件：(根进程 pid, 进程名（小写）, 显示名)，没有就 None。

    同名的一堆进程里取"父进程不是同名程序"的那个（父进程同名但比它晚创建，说明 pid 被复用了，也算根）。
    有好几个根时：先按 apps 里的顺序，再挑同名子孙最多的，再挑最早创建的。
    """
    want = {str(k).lower(): v for k, v in (APPS if apps is None else apps).items()}
    if not want or sys.platform != "win32":
        return None
    try:
        procs = _snapshot()
    except Exception:
        log.debug("进程快照失败", exc_info=True)
        return None
    by_pid = {pid: (ppid, name.lower()) for pid, ppid, name in procs}
    cands = [(pid, ppid, name.lower()) for pid, ppid, name in procs if name.lower() in want]
    if not cands:
        return None
    created: dict[int, int | None] = {}

    def born(pid: int):
        if pid not in created:
            created[pid] = _created(pid)
        return created[pid]

    def is_root(pid: int, ppid: int, name: str) -> bool:
        p = by_pid.get(ppid)
        if p is None or ppid == pid or p[1] != name:
            return True
        cp, cc = born(ppid), born(pid)
        return cp is not None and cc is not None and cp > cc

    roots = [c for c in cands if is_root(*c)] or cands
    kids: dict[int, list[int]] = {}
    for pid, ppid, name in cands:
        kids.setdefault(ppid, []).append(pid)

    def tree(pid: int) -> int:
        seen, todo = {pid}, [pid]
        while todo:
            for k in kids.get(todo.pop(), ()):
                if k not in seen:
                    seen.add(k)
                    todo.append(k)
        return len(seen)

    order = {k: i for i, k in enumerate(want)}
    pid, _, name = min(roots, key=lambda c: (order[c[2]], -tree(c[0]), born(c[0]) or 0, c[0]))
    return pid, name, want[name]


def tree_pids(root: int) -> set[int]:
    """root 和它所有的子孙进程（不管叫什么：腾讯会议的声音可能是子进程放的）。"""
    kids: dict[int, list[int]] = {}
    try:
        for pid, ppid, _ in _snapshot():
            if pid != ppid:
                kids.setdefault(ppid, []).append(pid)
    except Exception:
        log.debug("进程快照失败", exc_info=True)
    seen, todo = {int(root)}, [int(root)]
    while todo:
        for k in kids.get(todo.pop(), ()):
            if k not in seen:
                seen.add(k)
                todo.append(k)
    return seen


def activity(pids) -> tuple[bool, bool] | None:
    """这些进程现在有没有活动的音频会话：(麦克风, 扬声器)。查不了（不是 Windows、系统出错）返回 None。
    调用的线程要已经 CoInitializeEx（AppListener 的监听线程是 MTA）。"""
    want = {int(p) for p in pids}
    try:
        mic = any(p in want and st == AUDIO_SESSION_ACTIVE for p, st in _sessions(E_CAPTURE))
        spk = any(p in want and st == AUDIO_SESSION_ACTIVE for p, st in _sessions(E_RENDER))
    except Exception:
        log.debug("查音频会话失败", exc_info=True)
        return None
    return mic, spk


# ---------------------------------------------------------------- 录一个进程树

class AppCapture:
    """只录 pid 这棵进程树放出来的声音。on_pcm(pcm, t) 在抓取线程里调：16 kHz 单声道 s16le，
    每块 10 ms 的整数倍（最多 40 ms），t = 这块第一个采样的墙上时间。帧流不间断，没声音时补静音。"""

    FORMATS = ((16000, 1), (48000, 2))    # 依次试：先让 Windows 直接给 16 kHz 单声道，不行再自己转

    def __init__(self, pid: int, on_pcm):
        self.pid = int(pid)
        self._on_pcm = on_pcm
        self._formats = self.FORMATS
        self.fmt: tuple[int, int] | None = None    # 实际初始化成的 (采样率, 声道)
        self.glitches = 0                          # WASAPI 报的丢包次数
        self._th: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._init_err: AppAudioError | None = None
        self._alive = False
        self._error: str | None = None
        self._proc = None
        self._client = None
        self._cap = None
        self._event = None
        self._down = None
        self._src_rate = RATE
        self._ch = 1
        self._block = 2
        self._buf = bytearray()
        self._t0 = 0.0
        self._total = 0          # 已进缓冲的采样数（含补的静音）
        self._emitted = 0        # 已送出去的采样数
        self._cb_err = False
        self._lv: deque = deque()
        self._lv_n = 0
        self._lv_sum = 0.0
        self._lv_lock = threading.Lock()

    # ---- 对外

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def level(self) -> float:
        """最近 1 秒的 RMS（int16 刻度）。"""
        with self._lv_lock:
            if self._lv_n <= 0:
                return 0.0
            return math.sqrt(max(self._lv_sum, 0.0) / self._lv_n)

    def start(self) -> None:
        if self._th is not None:
            return
        if not supported():
            raise AppAudioError("录制失败：需要 Windows 10 2004 以上")
        self._proc = _Proc(self.pid)
        self._stop.clear()
        self._ready.clear()
        self._init_err = None
        self._error = None
        th = threading.Thread(target=self._run, name=f"app-audio-{self.pid}", daemon=True)
        self._th = th
        th.start()
        if not self._ready.wait(ACTIVATE_TIMEOUT_S + 5):
            self.stop()
            raise AppAudioError("录制失败：启动超时")
        if self._init_err is not None:
            th.join(2)
            self._th = None
            raise self._init_err

    def stop(self) -> None:
        self._stop.set()
        th = self._th
        if th is not None and th is not threading.current_thread():
            th.join(3)

    # ---- 抓取线程

    def _run(self) -> None:
        co = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED) >= 0
        try:
            try:
                self._open()
            except AppAudioError as e:
                self._init_err = e
                return
            except Exception as e:
                log.exception("按程序录音：初始化出错")
                self._init_err = AppAudioError(f"录制失败：{e}")
                return
            self._alive = True
            self._ready.set()
            try:
                self._loop()
            except Exception as e:
                log.exception("按程序录音：抓取出错")
                self._error = f"录制失败：{e}"
        finally:
            self._alive = False
            self._close()
            if self._proc is not None:
                self._proc.close()
            if co:
                _ole32.CoUninitialize()
            self._ready.set()

    def _open(self) -> None:
        last = 0
        for rate, ch in self._formats:
            client = _activate(self.pid)
            wf = _WAVEFORMATEX(WAVE_FORMAT_PCM, ch, rate, rate * ch * 2, ch * 2, 16, 0)
            hr = client.call(_AC_INITIALIZE, _P_INIT, AUDCLNT_SHAREMODE_SHARED, STREAM_FLAGS,
                             BUFFER_MS * 10_000, 0, byref(wf), None)
            if hr >= 0:
                self._client = client
                self.fmt = (rate, ch)
                break
            client.release()
            last = hr
            log.info("按程序录音：%d Hz %d 声道初始化失败 %s", rate, ch, _hex(hr))
        else:
            raise AppAudioError(f"初始化失败：{_hex(last)}", last)
        rate, ch = self.fmt
        if rate not in (RATE, RATE * 3):
            raise AppAudioError(f"初始化失败：不支持 {rate} Hz")
        self._src_rate, self._ch, self._block = rate, ch, ch * 2
        self._down = _Down3() if rate != RATE else None
        cap = c_void_p()
        hr = self._client.call(_AC_GET_SERVICE, _P_GET_SERVICE, byref(IID_IAudioCaptureClient), byref(cap))
        if hr < 0 or not cap.value:
            raise AppAudioError(f"初始化失败：{_hex(hr)}", hr)
        self._cap = _Com(cap.value)
        self._event = _k32.CreateEventW(None, False, False, None)
        if not self._event:
            raise AppAudioError("初始化失败：CreateEvent")
        hr = self._client.call(_AC_SET_EVENT, _P_SET_EVENT, self._event)
        if hr < 0:
            raise AppAudioError(f"初始化失败：{_hex(hr)}", hr)
        self._buf = bytearray()
        self._total = self._emitted = 0
        self._t0 = time.time()
        hr = self._client.call(_AC_START, _P_VOID)
        if hr < 0:
            raise AppAudioError(f"启动失败：{_hex(hr)}", hr)
        log.info("按程序录音：pid %d，%d Hz %d 声道", self.pid, rate, ch)

    def _close(self) -> None:
        if self._client is not None:
            try:
                self._client.call(_AC_STOP, _P_VOID)
            except Exception:
                pass
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._client is not None:
            self._client.release()
            self._client = None
        if self._event:
            _k32.CloseHandle(self._event)
            self._event = None

    def _loop(self) -> None:
        next_check = 0.0
        err_since = None
        err_hr = 0
        while not self._stop.is_set():
            _k32.WaitForSingleObject(self._event, WAIT_MS)
            if err_since is None:
                hr = self._drain()
                if hr < 0:
                    err_since, err_hr = time.time(), hr
                    log.info("按程序录音：抓取出错 %s（pid %d），先补静音", _hex(hr), self.pid)
            now = time.time()
            if now >= next_check or err_since is not None:
                next_check = now + CHECK_S
                if self._proc.exited():
                    log.info("按程序录音：进程 %d 已退出", self.pid)
                    break
                if err_since is not None and now - err_since > ERR_GRACE_S:
                    self._error = f"录制失败：{_hex(err_hr)}"
                    break
            self._fill(now)
            self._emit()

    def _drain(self) -> int:
        """把攒着的包都取出来。返回 HRESULT（出错 < 0）。"""
        n_next, data, frames = c_uint32(), c_void_p(), c_uint32()
        flags, dpos, qpos = c_ulong(), c_uint64(), c_uint64()
        cap = self._cap
        while True:
            hr = cap.call(_CC_NEXT_PACKET, _P_NEXT_PACKET, byref(n_next))
            if hr < 0:
                return hr
            if n_next.value == 0:
                return 0
            hr = cap.call(_CC_GET_BUFFER, _P_GET_BUFFER, byref(data), byref(frames), byref(flags),
                          byref(dpos), byref(qpos))
            if hr == AUDCLNT_S_BUFFER_EMPTY:
                return 0
            if hr < 0:
                return hr
            n, fl = frames.value, flags.value
            raw = None
            if n and data.value and not (fl & BUF_SILENT):
                raw = ctypes.string_at(data.value, n * self._block)
            hr = cap.call(_CC_RELEASE_BUFFER, _P_RELEASE_BUFFER, n)
            self._feed(raw, n, fl, qpos.value)
            if hr < 0:
                return hr

    def _feed(self, raw: bytes | None, n: int, flags: int, qpc: int) -> None:
        if n <= 0:
            return
        if flags & BUF_DISCONTINUITY:
            self.glitches += 1
        now = time.time()
        start = None
        if qpc and not (flags & BUF_TIMESTAMP_ERROR):
            age = (_qpc_100ns() - qpc) / 1e7
            if -0.05 <= age <= 2.0:
                start = now - age
        if start is None:
            start = now - n / self._src_rate
        x = self._convert(raw, n)
        gap = int(round((start - self._end()) * RATE))
        if gap > GAP_TOL_S * RATE:
            self._zeros(gap)
        self._buf += x.tobytes()
        self._total += len(x)

    def _convert(self, raw: bytes | None, n: int) -> np.ndarray:
        """一个包 → 16 kHz 单声道 int16。"""
        if self._src_rate == RATE and self._ch == 1:
            if raw is None:
                return np.zeros(n, np.int16)
            return np.frombuffer(raw, dtype="<i2")
        if raw is None:
            x = np.zeros(n, np.float32)
        else:
            x = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            if self._ch > 1:
                x = x.reshape(-1, self._ch).mean(axis=1)
        if self._down is not None:
            x = self._down(x)
        return np.clip(np.round(x), -32768, 32767).astype(np.int16)

    def _end(self) -> float:
        """缓冲里最后一个采样之后的墙上时间。"""
        return self._t0 + self._total / RATE

    def _zeros(self, n: int) -> None:
        self._buf += bytes(2 * n)
        self._total += n

    def _fill(self, now: float) -> None:
        behind = int((now - LAG_S - self._end()) * RATE)
        if behind > 0:
            self._zeros(behind)

    def _emit(self) -> None:
        buf = self._buf
        while len(buf) >= FRAME_BYTES:
            k = min(len(buf) // FRAME_BYTES, MAX_FRAMES_PER_CALL)
            nb = k * FRAME_BYTES
            chunk = bytes(buf[:nb])
            del buf[:nb]
            t = self._t0 + self._emitted / RATE
            self._emitted += k * FRAME
            self._meter(chunk)
            try:
                self._on_pcm(chunk, t)
            except Exception:
                if not self._cb_err:
                    self._cb_err = True
                    log.exception("按程序录音：on_pcm 出错")

    def _meter(self, chunk: bytes) -> None:
        a = np.frombuffer(chunk, dtype="<i2").astype(np.float64)
        s, n = float(np.dot(a, a)), len(a)
        with self._lv_lock:
            self._lv.append((n, s))
            self._lv_n += n
            self._lv_sum += s
            while self._lv and self._lv_n - self._lv[0][0] >= RATE:
                n0, s0 = self._lv.popleft()
                self._lv_n -= n0
                self._lv_sum -= s0


# ---------------------------------------------------------------- 盯着会议软件

class AppListener:
    """后台每 poll_s 秒找一次会议软件，看它是不是在开会：麦克风会话活动，或者扬声器会话连着活动了 SPK_HOLD_S 秒
    （提示音、来电铃声不算）。在开会就挂上录；麦克风、扬声器都停了 IDLE_S 秒（会开完了）或者程序退出就断开，接着找。
    查不了音频会话（activity 返回 None）时照老办法：进程在就算在开会。

    on_state(显示名 | None) 在监听线程里调：开始开会、开完时各一次，不管录不录（会议页上据此显示、收起这位参会人；
    stop() 时在开会的也会回调一次 None）。enabled=False 只看在不在开会、不录；set_enabled 随时切换。
    on_pcm(显示名, pcm, t) 在抓取线程里调；开始开会那次 on_state 一定先回调完，才挂上录。
    """

    RETRY_S = 30.0       # 同一个进程挂不上，隔这么久再试
    SPK_HOLD_S = 6.0     # 麦克风没开、只有扬声器在响：连着响这么久才算在开会
    IDLE_S = 10.0        # 在开会的软件麦克风、扬声器都不活动这么久，算会开完了

    def __init__(self, on_pcm, on_state=None, poll_s: float = 2.0, apps: dict | None = None, enabled: bool = True,
                 activity_fn=None):
        self._on_pcm = on_pcm
        self._on_state = on_state
        self._poll_s = max(0.05, float(poll_s))
        self._apps = apps
        self._activity = activity_fn or activity
        self._enabled = bool(enabled)
        self._cap: AppCapture | None = None
        self._call: tuple[int, str] | None = None      # 在开会的那个：(根进程 pid, 显示名)
        self._spk_since: float | None = None
        self._idle_since: float | None = None
        self._failed: dict[int, float] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._th: threading.Thread | None = None

    @property
    def label(self) -> str | None:
        """在开会的会议软件的显示名（不管录不录）。"""
        c = self._call
        return c[1] if c else None

    @property
    def capturing(self) -> bool:
        return self._cap is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        """录不录（在开会时马上挂上 / 断开）。"""
        self._enabled = bool(on)
        self._wake.set()

    def start(self) -> None:
        if self._th is not None and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._run, name="app-audio-listen", daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        th = self._th
        if th is not None and th is not threading.current_thread():
            th.join(6)
        self._th = None

    def _run(self) -> None:
        co = _coinit()                    # 查音频会话要 COM（MTA）
        try:
            next_poll = 0.0
            while not self._stop.is_set():
                cap = self._cap
                if cap is not None and not cap.alive:
                    if cap.error:
                        log.info("按程序录音：%s 断开（%s）", self.label, cap.error)
                    self._drop()          # 抓取断了：马上再看一次，还在开会就重挂
                    next_poll = 0.0
                now = time.monotonic()
                if self._wake.is_set() or now >= next_poll:
                    self._wake.clear()
                    next_poll = now + self._poll_s
                    self._poll(now)
                wait = max(0.0, next_poll - time.monotonic())
                self._wake.wait(min(wait, 0.2) if self._cap is not None else wait)
        except Exception:
            log.exception("按程序录音：监听出错")
        finally:
            self._end_call()
            if co:
                _couninit()

    def _poll(self, now: float) -> None:
        try:
            found = find_app(self._apps)
        except Exception:
            log.debug("找会议软件失败", exc_info=True)
            found = None
        if not found:
            self._end_call()
            return
        pid, exe, name = found
        if self._call is not None and self._call[0] != pid:
            self._end_call()                  # 软件重开了：换了一个进程
        live = self._live(pid, now)
        if live and self._call is None:
            self._call = (pid, name)
            log.info("按程序录音：%s 在开会（%s，pid %d）", name, exe, pid)
            self._call_state(name)
        elif not live and self._call is not None:
            self._end_call()
        if self._call is not None and self._enabled and self._cap is None:
            self._attach(pid, name, now)
        elif self._cap is not None and not self._enabled:
            self._drop()

    def _live(self, pid: int, now: float) -> bool:
        """这个会议软件现在算不算在开会（见类的说明）。"""
        try:
            act = self._activity(tree_pids(pid))
        except Exception:
            log.debug("查音频会话失败", exc_info=True)
            act = None
        if act is None:
            return True
        mic, spk = act
        if not spk:
            self._spk_since = None
        elif self._spk_since is None:
            self._spk_since = now
        if mic or spk:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = now
        if self._call is None:
            return bool(mic or (spk and now - self._spk_since >= self.SPK_HOLD_S))
        return self._idle_since is None or now - self._idle_since < self.IDLE_S

    def _attach(self, pid: int, name: str, now: float) -> None:
        self._failed = {p: t for p, t in self._failed.items() if t > now}
        if pid in self._failed:
            return
        cap = AppCapture(pid, lambda pcm, t, _name=name: self._on_pcm(_name, pcm, t))
        try:
            cap.start()
        except AppAudioError as e:
            log.warning("按程序录音：%s（pid %d）挂不上：%s", name, pid, e)
            self._failed[pid] = now + self.RETRY_S
            return
        self._cap = cap
        log.info("按程序录音：开始录 %s（pid %d）", name, pid)

    def _drop(self) -> None:
        """不录了（会议软件还可能在开会）。"""
        cap, self._cap = self._cap, None
        if cap is None:
            return
        cap.stop()
        log.info("按程序录音：停止录 %s", self.label)

    def _end_call(self) -> None:
        self._drop()
        call, self._call = self._call, None
        self._spk_since = self._idle_since = None
        if call is None:
            return
        log.info("按程序录音：%s 不在开会了", call[1])
        self._call_state(None)

    def _call_state(self, label) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(label)
        except Exception:
            log.exception("按程序录音：on_state 出错")


# ---------------------------------------------------------------- 手动测试

def _main(argv=None) -> int:
    import argparse
    import wave

    ap = argparse.ArgumentParser(description="按程序录声音：录正在跑的会议软件（或指定进程），每秒打一行音量")
    ap.add_argument("--pid", type=int, help="录这个进程（默认找会议软件）")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--wav", help="录到的声音存成 16 kHz 单声道 wav")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not supported():
        print("需要 Windows 10 2004 以上")
        return 2
    if args.pid:
        pid, label = args.pid, f"pid {args.pid}"
    else:
        found = find_app()
        if not found:
            print("没找到会议软件")
            return 1
        pid, exe, label = found
        label = f"{label}（{exe}，pid {pid}）"
    chunks: list[bytes] = []
    cap = AppCapture(pid, lambda pcm, t: chunks.append(pcm))
    try:
        cap.start()
    except AppAudioError as e:
        print(e)
        return 1
    print(f"{label}：{cap.fmt[0]} Hz {cap.fmt[1]} 声道")
    end = time.time() + args.seconds
    try:
        while time.time() < end and cap.alive:
            time.sleep(1.0)
            print(f"音量 {cap.level:7.0f}")
    except KeyboardInterrupt:
        pass
    cap.stop()
    if cap.error:
        print(cap.error)
    if args.wav:
        with wave.open(args.wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(chunks))
        print(f"存到 {args.wav}（{sum(map(len, chunks)) / (RATE * 2):.1f} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
