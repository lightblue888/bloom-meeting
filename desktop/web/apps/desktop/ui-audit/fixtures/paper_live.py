# -*- coding: utf-8 -*-
"""ui-audit 的真论文页：zuhui/view.py 生成的那一页（不是 paper.html 那个替身），给论文区的几个屏幕（paper-live-*：
选中后的「引用」浮层、写批注的框、改动落地后停在哪）用。一篇编出来的论文，两个版本（版本 2 在第 2 节第 3 段后面补了
一句：一处改动落地，那一段变长），中英文各一份，写到 <输出目录>/paper-live-<zh|en>-<1|2>.html。

页面里 fetch('/version')、fetch('/api/…') 这些绝对路径前面补上 /__zuhui：审计的假后端在那下面应答（lib/mock-backend.ts）。
全在临时 ZUHUI_HOME 里，不碰 ~/.zuhui，不起服务、不连网。lib/global-setup.ts 开跑前调它：
    python -X utf8 ui-audit/fixtures/paper_live.py ui-audit/out/paper-live"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "paper-live").resolve()
OUT.mkdir(parents=True, exist_ok=True)
HOME = Path(tempfile.mkdtemp(prefix="ui-audit-paper-"))
os.environ["ZUHUI_HOME"] = str(HOME)
os.environ["BLURT_HOME"] = str(HOME / "blurt")
ROOT = next(p for p in Path(__file__).resolve().parents if (p / "zuhui" / "view.py").is_file())
sys.path.insert(0, str(ROOT))

from zuhui import share as _share  # noqa: E402
_share.PUBLIC_RELAY = "https://127.0.0.1:1/"          # 不碰真的中继
from zuhui import view  # noqa: E402
from zuhui.meetings import Meetings  # noqa: E402

SECTIONS = {"zh": ["引言", "方法", "数据与设置", "实验结果", "讨论"],
            "en": ["Introduction", "Method", "Data and setup", "Results", "Discussion"]}


def para(lang, s, i):
    if lang == "zh":
        return (f"第{s + 1}节第{i + 1}段：这里描述一个编出来的步骤，样本量为 {120 + s * 7 + i} 条，窗口长度取 {8 + i} 秒，"
                f"阈值设为 0.{s + 3}{i}。这一段只是占位，用来检查页面在改动落地后能不能停在原来读到的地方，写得长一些，好让每段都占两三行。")
    return (f"Section {s + 1}, paragraph {i + 1}: a made-up step with {120 + s * 7 + i} samples, a window of {8 + i} seconds and a "
            f"threshold of 0.{s + 3}{i}. It only holds the place, so the page can be checked for staying where you were reading "
            "after a change lands; it is long enough to take two or three lines.")


EXTRA = {"zh": "补一句：窗口长度按前一周的波动率选，波动大时取短一些。",
         "en": "Added: the window follows last week's volatility and is shorter when the market moves more."}


def paper(lang, version):
    title = "一篇编出来的论文：会上实时改稿" if lang == "zh" else "A made-up paper: revising it live in a meeting"
    out = [f"# {title}\n"]
    for s, name in enumerate(SECTIONS[lang]):
        out.append(f"## {s + 1} {name}\n")
        for i in range(6):
            t = para(lang, s, i)
            if version == 2 and (s, i) == (1, 2):
                t += EXTRA[lang]
            out.append(t + "\n")
    return "\n".join(out)


# 页面里的绝对路径（/version、/api/focus、/api/scroll、/api/comment）走假后端：前面补 /__zuhui
SHIM = ("<script>(function(){var f=window.fetch;window.fetch=function(u,o){if(typeof u==='string'&&u.charAt(0)==='/'"
        "&&u.indexOf('/__zuhui/')!==0)u='/__zuhui'+u;return f.call(window,u,o);};})();</script>")

try:
    for lang in ("zh", "en"):
        p = HOME / f"paper-{lang}.md"
        p.write_text(paper(lang, 1), encoding="utf-8")
        v = view.Viewer(port=0, meetings=Meetings(root=HOME / f"meetings-{lang}"))
        v.cfg.setdefault("meeting", {})["me"] = "Maya"
        v.mirror = p
        v.base = view.base_for(p)
        v.ensure_base(rebase=True)
        v.ui_lang = lang
        for version in (1, 2):
            p.write_text(paper(lang, version), encoding="utf-8")
            if not v.rebuild():
                raise SystemExit(f"paper_live: the page generator failed ({lang} v{version})")
            page = view._embed(v.html, local_fonts=True)
            page = page.replace("<head>", "<head>" + SHIM, 1) if "<head>" in page else SHIM + page
            (OUT / f"paper-live-{lang}-{version}.html").write_text(page, encoding="utf-8")
    print(f"paper_live: wrote {OUT}")
finally:
    shutil.rmtree(HOME, ignore_errors=True)
