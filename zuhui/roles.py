# -*- coding: utf-8 -*-
"""参会人的身份：主持人（开 Bloom 的你）、共同主持人（你在这场会里点过的人）、参会者（其余通过邀请链接进来的人）。

- 主持人和共同主持人说的话算数：Claude 照办、排改稿任务、能确认 / 撤回改动。参会者的话只记进逐字稿，Claude 不照办。
  模型不去猜谁是老师，只看说话的人是不是主持人或共同主持人。
- 共同主持人每场会重新设（顶栏参会人卡片里「设为共同主持人」）。没有登录，进会的名字谁都能填，所以不跨场自动沿用：
  上一场是共同主持人的名字这场又进会了，顶栏提示「某某 上次是共同主持人，恢复？」，点一下才算（点「不是」这场不再提）。
- 同一个房间里名字不会重（同名进会会把先进的挤掉），所以这场会里按名字认人。
- 存在 ~/.zuhui/roles.json：{"meeting": 这场会的编号, "cohosts": {key: {name, at}}, "dismissed": [key],
  "known": {key: {name, at}}}。后端中途重启、同一场会接着开，共同主持人还在；开下一场时这场的人挪进 known。
"""
from __future__ import annotations

import json
import threading
import time

from .config import STATE

PATH = STATE / "roles.json"
ROLES = ("participant", "cohost")
_ALIAS = {"teacher": "cohost"}          # 旧叫法（老师）和模拟会、考卷脚本里的 set_role(.., "teacher")：都当共同主持人
_lock = threading.Lock()


def _key(name: str) -> str:
    return " ".join(str(name or "").split()).lower()


def _load() -> dict:
    try:
        d = json.loads(PATH.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    if "people" in d:                   # 旧格式（按名字记的老师）：老师都当"上次是共同主持人"，提示一次恢复，不自动沿用
        known = d.setdefault("known", {})
        for k, v in (d.pop("people") or {}).items():
            if (v or {}).get("role") == "teacher":
                known[k] = {"name": v.get("name") or k, "at": v.get("at")}
        d.pop("asked", None)
        d.pop("suggest", None)
    d.setdefault("cohosts", {})
    d.setdefault("dismissed", [])
    d.setdefault("known", {})
    return d


def _save(d: dict) -> None:
    try:
        PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _retire(d: dict) -> None:
    """这场的共同主持人挪进 known（下一场进会时提示恢复），这场清空。"""
    known = d["known"]
    for k, v in d["cohosts"].items():
        known[k] = {"name": v.get("name") or k, "at": v.get("at")}
    d.update({"cohosts": {}, "dismissed": []})


def begin(meeting_id: str) -> None:
    """开了一场会。同一场（后端重启后接着开）再调一次不动。
    上一场散会时已经清过（end）：会前设的（模拟会、考卷脚本开会前就点好）带进这场。
    上一场没散就开了新的（崩了、录屏被顶掉）：上一场的人先挪进 known。"""
    with _lock:
        d = _load()
        if d.get("meeting") == meeting_id:
            return
        if d.get("meeting"):
            _retire(d)
        d["meeting"] = meeting_id
        _save(d)


def end() -> None:
    """散会：这场的共同主持人挪进 known，之后没有进行中的会。"""
    with _lock:
        d = _load()
        _retire(d)
        d["meeting"] = None
        _save(d)


def get(name: str) -> str:
    """这场会里的身份：cohost / participant。"""
    with _lock:
        return "cohost" if _key(name) in _load()["cohosts"] else "participant"


def is_cohost(name: str | None) -> bool:
    return bool(name) and get(name) == "cohost"


is_teacher = is_cohost                  # 旧名字：模拟会、考卷脚本还在用


def set_role(name: str, role: str) -> dict:
    """你（或共同主持人）在 Bloom 里点的：设为共同主持人 / 改回参会者。只管这场会。"""
    role = _ALIAS.get(role, role)
    if role not in ROLES:
        raise ValueError(f"身份只能是 {ROLES}")
    k = _key(name)
    if not k:
        raise ValueError("没有名字")
    with _lock:
        d = _load()
        rec = {"name": str(name).strip(), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if role == "cohost":
            d["cohosts"][k] = rec
        else:
            d["cohosts"].pop(k, None)
        if k not in d["dismissed"]:
            d["dismissed"].append(k)    # 设过了（不管设成什么）就不再提示恢复
        _save(d)
        return {"name": rec["name"], "role": role, "at": rec["at"]}


def suggest(name: str, why: str = "") -> bool:
    """旧接口（学生猜谁像老师）：不再猜，什么都不做。"""
    return False


def suggestions() -> list[dict]:
    """上一场（或更早）是共同主持人、这场还没设的人：顶栏提示「上次是共同主持人，恢复？」。调用方再挑在场的。"""
    with _lock:
        d = _load()
        skip = set(d["cohosts"]) | set(d["dismissed"])
        return [{"name": v.get("name") or k, "why": "上次是共同主持人"} for k, v in d["known"].items() if k not in skip]


def dismiss(name: str) -> None:
    """你点了「不是」：这场会不再提示恢复这个人。"""
    k = _key(name)
    with _lock:
        d = _load()
        if k and k not in d["dismissed"]:
            d["dismissed"].append(k)
        _save(d)


def all_roles() -> dict[str, str]:
    """{名字: 身份}：这场会的共同主持人，给前端标参会人。"""
    with _lock:
        return {v.get("name") or k: "cohost" for k, v in _load()["cohosts"].items()}
