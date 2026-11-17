# -*- coding: utf-8 -*-
"""组会 · 命令行入口

  python -m zuhui start [--dry] [--mute]            开会：等 Bloom 录屏（Alt+Shift+R），老师说话 → 秒回 → 排队改稿 → 汇报
  python -m zuhui replay <录屏会话目录> [--speed 2] [--dry] [--mute]
                                                   用一段旧录屏的转写回放整条链路（测试）
  python -m zuhui say "要念的话"                    用学生的嗓子念一句
  python -m zuhui board [--reset]                   看/清任务板
  python -m zuhui tts                               确保克隆音色服务在跑（没在跑就拉起）

  --dry  工人不真调 Claude，睡 3 秒回假结果（CLI 没登录时用）
  --mute 不出声，只在控制台打印要念的话
"""
import argparse
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def worker(a):
    """CCD 工人会话经这些命令跟任务板打交道（任务板多进程安全）。"""
    import json
    import time
    from .board import Board
    b = Board()
    if a.wcmd == "watch":
        seen = set()
        try:
            while True:
                for t in b.tasks():
                    if t["status"] == "queued" and t["id"] not in seen:
                        seen.add(t["id"])
                        print(json.dumps({"event": "task", "id": t["id"], "idx": t["idx"], "title": t["title"],
                                          "remark": t["remark"], "shot": t.get("shot"), "section": t.get("section"),
                                          "created": t.get("created"),
                                          # 会中能摊开几篇论文：这条改哪一篇（旧任务没有，改当前那篇）
                                          "paper_path": t.get("paper_path")}, ensure_ascii=False), flush=True)
                time.sleep(1.0)
        except KeyboardInterrupt:
            return
    elif a.wcmd == "claim":
        t = b.claim(a.id)
        print(json.dumps({"id": t["id"], "status": t["status"], "title": t["title"], "remark": t["remark"],
                          "shot": t.get("shot"), "section": t.get("section"), "paper_path": t.get("paper_path")},
                         ensure_ascii=False))
    elif a.wcmd == "finish":
        if not (a.result or a.error):
            raise SystemExit("finish 需要 --result 或 --error")
        t = b.finish(a.id, result=a.result, error=a.error)
        print(json.dumps({"id": t["id"], "status": t["status"]}, ensure_ascii=False))
    elif a.wcmd == "add":
        remark = " ".join(a.remark)
        t = b.add(remark=remark, title=a.title or remark[:30])
        print(json.dumps({"id": t["id"], "status": t["status"], "title": t["title"]}, ensure_ascii=False))
    elif a.wcmd == "list":
        for t in b.tasks():
            print(f"{t['id']:<4} {t['status']:<8} {t['title']}")
        print(b.counts())
    else:
        print("worker 子命令：watch | claim <id> | finish <id> --result/--error | add <话> | list")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="zuhui", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("start"); s.add_argument("--dry", action="store_true"); s.add_argument("--mute", action="store_true")
    s.add_argument("--no-view", action="store_true", help="不起文稿对比页"); s.add_argument("--no-open", action="store_true", help="起对比页但不自动开浏览器")
    s = sub.add_parser("replay"); s.add_argument("session"); s.add_argument("--speed", type=float, default=1.0)
    s.add_argument("--dry", action="store_true"); s.add_argument("--mute", action="store_true")
    s.add_argument("--no-view", action="store_true"); s.add_argument("--no-open", action="store_true")
    s = sub.add_parser("serve", help="给 Bloom 当后端：等于 start --no-open（对比页和 /api 在 127.0.0.1:47330）")
    s.add_argument("--dry", action="store_true"); s.add_argument("--mute", action="store_true")
    v = sub.add_parser("view", help="只起文稿对比页（不开会）"); v.add_argument("--rebase", action="store_true", help="把当前镜像设为新的原文快照")
    v.add_argument("--no-open", action="store_true"); v.add_argument("--port", type=int, default=None)
    s = sub.add_parser("say"); s.add_argument("text", nargs="+")
    s = sub.add_parser("board"); s.add_argument("--reset", action="store_true")
    sub.add_parser("tts")
    w = sub.add_parser("worker", help="CCD 工人会话用的任务板命令")
    ws = w.add_subparsers(dest="wcmd")
    ws.add_parser("watch", help="常驻：每出现一条新排队任务就打印一行 JSON（给 Monitor 用）")
    x = ws.add_parser("claim"); x.add_argument("id")
    x = ws.add_parser("finish"); x.add_argument("id"); x.add_argument("--result", default=None); x.add_argument("--error", default=None)
    x = ws.add_parser("add"); x.add_argument("remark", nargs="+"); x.add_argument("--title", default=None)
    ws.add_parser("list")
    a = ap.parse_args(argv)

    if a.cmd == "worker":
        return worker(a)

    if a.cmd in ("start", "replay", "serve"):
        # 只许一个后端（两个会同时听、同时答、同一条意见记两遍）。先抢一把文件锁：两个几乎同时启动时，
        # 端口检查都会放行，锁只有一个抢得到。锁跟着进程走，进程一退出（包括被杀）就自动释放。
        import socket
        from .config import STATE
        global _BACKEND_LOCK
        _BACKEND_LOCK = open(STATE / "backend.lock", "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                _BACKEND_LOCK.seek(0)
                msvcrt.locking(_BACKEND_LOCK.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(_BACKEND_LOCK.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("后端已经在运行（另一个进程拿着 backend.lock），这一份不启动。", flush=True)
            return 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            port = int(os.environ.get("ZUHUI_VIEW_PORT", "47330"))
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                print(f"后端已经在运行（127.0.0.1:{port}），这一份不启动。", flush=True)
                return 0
        from . import netenv
        took = netenv.strip_proxy()          # 会议连接不走本机代理（见 netenv.py）；claude / edge-tts 子进程再加回去
        if took:
            print(f"会议连接不走代理：已从后端环境拿掉 {', '.join(sorted(took))}（改稿的 claude、备用嗓子照样走代理）", flush=True)
        from .app import Meeting
        m = Meeting(dry=a.dry, mute=a.mute)
        m.run(replay_session=getattr(a, "session", None), speed=getattr(a, "speed", 1.0),
              view=not getattr(a, "no_view", False), open_browser=(a.cmd != "serve" and not getattr(a, "no_open", False)))
    elif a.cmd == "view":
        import time
        from .config import load
        from .view import PORT, Viewer
        v = Viewer(load(), port=a.port or PORT)
        if a.rebase:
            v.ensure_base(rebase=True)
            print("原文快照已重置为当前镜像")
        v.serve(open_browser=not a.no_open)
        print(f"文稿对比页：{v.url}  （Ctrl+C 退出）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            v.stop()
    elif a.cmd == "say":
        import time
        from .config import load
        from .mouth import Mouth
        m = Mouth(load())
        m.ensure_server()
        m.say(" ".join(a.text))
        while m.speaking:
            time.sleep(0.2)
        time.sleep(0.5)
    elif a.cmd == "board":
        from .board import Board
        b = Board()
        if a.reset:
            b.reset(); print("任务板已清空")
        print(b.summary(max_items=50)); print(b.counts())
    elif a.cmd == "tts":
        from .config import load
        from .mouth import Mouth
        up = Mouth(load()).ensure_server()
        print("克隆音色服务：在线" if up else "克隆音色服务：正在后台拉起（首次加载 1-3 分钟）")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
