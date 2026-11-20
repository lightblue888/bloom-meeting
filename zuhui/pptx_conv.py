# -*- coding: utf-8 -*-
"""PPT（.pptx）→ 能读的讲稿 Markdown：每页一节（"第 N 页 · 标题"），下面是正文（项目符号按级别缩进）、表格、图、备注。
只读：会上 AI 不改 PPT（有人让改时说"PPT 只能看不能改"），手改一段、写回也都不做。
只用 zipfile + XML（lxml），不加新依赖。图（png / jpg / gif / svg / webp）存进 figures/figN.*，figures.json 记一笔，
正文原位写 [image: 图 N] 和图注（和 PDF / Word 转出来的同一套，对比页按图号把图放回去）；emf / wmf 这类浏览器画不了的跳过。
隐藏的页不进讲稿。"""
from __future__ import annotations

import json
import posixpath
import re
import zipfile
from pathlib import Path

from lxml import etree

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
R_ID = f"{{{NS['r']}}}id"
R_EMBED = f"{{{NS['r']}}}embed"
IMG_OK = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
MAX_IMG = 8_000_000
CONV_META = ".bloom-convert.json"


class PptxError(ValueError):
    pass


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """一个部件的关系：{rId: (类型尾巴, 目标部件的完整路径)}。"""
    d, name = posixpath.split(part)
    rp = posixpath.join(d, "_rels", name + ".rels")
    try:
        root = etree.fromstring(z.read(rp))
    except (KeyError, etree.XMLSyntaxError):
        return {}
    out = {}
    for r in root:
        t = str(r.get("Target") or "")
        if r.get("TargetMode") == "External":
            continue
        full = posixpath.normpath(posixpath.join(d, t)) if not t.startswith("/") else t.lstrip("/")
        out[str(r.get("Id"))] = (str(r.get("Type") or "").rsplit("/", 1)[-1], full)
    return out


def _slides(z: zipfile.ZipFile) -> list[str]:
    """按放映顺序的幻灯片部件。"""
    try:
        pres = etree.fromstring(z.read("ppt/presentation.xml"))
    except KeyError:
        raise PptxError("不是 PPT 文件（里面没有 ppt/presentation.xml）")
    rels = _rels(z, "ppt/presentation.xml")
    out = []
    for s in pres.iterfind("p:sldIdLst/p:sldId", NS):
        rid = s.get(R_ID)
        if rid in rels and rels[rid][1] in z.namelist():
            out.append(rels[rid][1])
    return out


def _md_esc(s: str) -> str:
    s = s.replace("\u000b", " ").replace("\t", " ")
    s = re.sub(r"[  ]+", " ", s).strip()
    return re.sub(r"^([#>*+-]|\d+[.)])(\s)", r"\\\1\2", s)


def _para_text(p) -> str:
    out = []
    for el in p:
        tag = etree.QName(el).localname
        if tag in ("r", "fld"):
            t = el.find("a:t", NS)
            out.append((t.text or "") if t is not None else "")
        elif tag == "br":
            out.append(" ")
    return "".join(out)


def _paras(tx) -> list[tuple[int, str]]:
    """一个文字框里的段落：[(级别, 文字)]，空段去掉。"""
    out = []
    for p in tx.iterfind("a:p", NS):
        ppr = p.find("a:pPr", NS)
        try:
            lvl = int(ppr.get("lvl") or 0) if ppr is not None else 0
        except ValueError:
            lvl = 0
        t = _para_text(p).strip()
        if t:
            out.append((min(lvl, 6), t))
    return out


def _table(tbl) -> list[str]:
    rows = []
    for tr in tbl.iterfind("a:tr", NS):
        cells = []
        for tc in tr.iterfind("a:tc", NS):
            if tc.get("hMerge") == "1" or tc.get("vMerge") == "1":
                cells.append("")
                continue
            txt = " ".join(t for _, t in _paras(tc.find("a:txBody", NS))) if tc.find("a:txBody", NS) is not None else ""
            cells.append(_md_esc(txt).replace("|", "｜"))
        if cells:
            rows.append(cells)
    if not rows:
        return []
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join(["---"] * n) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return out


def _pos(el) -> tuple[int, int]:
    off = el.find(".//a:xfrm/a:off", NS)
    if off is None:
        off = el.find(".//p:xfrm/a:off", NS)
    try:
        return (int(off.get("y")), int(off.get("x"))) if off is not None else (10 ** 12, 0)
    except (TypeError, ValueError):
        return (10 ** 12, 0)


def _shapes(tree) -> list:
    """一页上的东西按版面从上到下、从左到右排（组合里的摊开）。"""
    items = []

    def walk(parent):
        for el in parent:
            tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
            if tag == "grpSp":
                walk(el)
            elif tag in ("sp", "pic", "graphicFrame"):
                items.append(el)
    walk(tree)
    return sorted(items, key=_pos)


def _ph(el) -> str:
    ph = el.find("p:nvSpPr/p:nvPr/p:ph", NS)
    if ph is None:
        ph = el.find("p:nvPicPr/p:nvPr/p:ph", NS)
    return "" if ph is None else str(ph.get("type") or "body")


def _notes(z: zipfile.ZipFile, rels: dict) -> list[str]:
    part = next((t for k, (typ, t) in rels.items() if typ == "notesSlide"), None)
    if not part or part not in z.namelist():
        return []
    try:
        root = etree.fromstring(z.read(part))
    except etree.XMLSyntaxError:
        return []
    out = []
    for sp in root.iterfind(".//p:sp", NS):
        if _ph(sp) not in ("body", ""):
            continue                             # 页码、缩略图占位这些不要
        tx = sp.find("p:txBody", NS)
        if tx is not None:
            out += [t for _, t in _paras(tx)]
    return out


def convert_pptx(src: Path, stage: Path) -> tuple[str, dict]:
    """转成讲稿 Markdown；图写进 stage/figures/。返回 (Markdown, {"engine": "pptx", "slides", "figures", "tables"})。"""
    try:
        z = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError):
        raise PptxError(f"PPT 文件已损坏：{src.name}")
    with z:
        slides = _slides(z)
        if not slides:
            raise PptxError(f"PPT 中没有幻灯片：{src.name}")
        names = set(z.namelist())
        raw_all = " ".join(z.read(s).decode("utf-8", "replace") for s in slides[:40])
        cjk = len(re.findall(r"[一-鿿]", raw_all)) >= 4
        figs_dir = stage / "figures"
        fig_meta: dict = {}
        out = [f"# {src.stem}", ""]
        nfig = ntab = shown = 0
        for k, part in enumerate(slides, 1):
            try:
                root = etree.fromstring(z.read(part))
            except etree.XMLSyntaxError:
                continue
            if root.get("show") == "0":
                continue                         # 隐藏的页
            shown += 1
            rels = _rels(z, part)
            tree = root.find("p:cSld/p:spTree", NS)
            title, body = "", []
            for el in _shapes(tree) if tree is not None else []:
                tag = etree.QName(el).localname
                if tag == "sp":
                    tx = el.find("p:txBody", NS)
                    if tx is None:
                        continue
                    ph = _ph(el)
                    ps = _paras(tx)
                    if ph in ("title", "ctrTitle") and not title:
                        title = _md_esc(" ".join(t for _, t in ps))
                        continue
                    if ph in ("sldNum", "dt", "ftr"):
                        continue                 # 页码、日期、页脚
                    bullets = ph in ("body", "obj") or len(ps) > 1
                    for lvl, t in ps:
                        body.append(("  " * lvl + "- " + _md_esc(t)) if bullets else _md_esc(t))
                    body.append("")
                elif tag == "pic":
                    blip = el.find(".//a:blip", NS)
                    rid = blip.get(R_EMBED) if blip is not None else None
                    if not rid or rid not in rels:
                        continue
                    target = rels[rid][1]
                    ext = Path(target).suffix.lower()
                    if ext not in IMG_OK or target not in names:
                        continue
                    data = z.read(target)
                    if len(data) > MAX_IMG:
                        continue
                    nfig += 1
                    figs_dir.mkdir(parents=True, exist_ok=True)
                    fname = f"fig{nfig}{'.jpg' if ext == '.jpeg' else ext}"
                    (figs_dir / fname).write_bytes(data)
                    fig_meta[str(nfig)] = {"file": fname, "source": "pptx", "slide": k}
                    pr = el.find("p:nvPicPr/p:cNvPr", NS)
                    alt = _md_esc(str((pr.get("descr") if pr is not None else "") or ""))[:120]
                    if re.search(r"\.[A-Za-z0-9]{2,4}$", alt) or re.match(r"(?i)^(picture|图片|image)\s*\d*$", alt):
                        alt = ""                 # 文件名、"图片 3" 这种默认的替代文字不当图注
                    body.append(f"[image: 图 {nfig}]")
                    cap = alt or (f"第 {k} 页的图" if cjk else f"Image on slide {k}")
                    body.append((f"图 {nfig}：" if cjk else f"Figure {nfig}: ") + cap)
                    body.append("")
                elif tag == "graphicFrame":
                    tbl = el.find(".//a:tbl", NS)
                    if tbl is not None:
                        rows = _table(tbl)
                        if rows:
                            ntab += 1
                            body += rows + [""]
            head = (f"第 {k} 页" if cjk else f"Slide {k}") + (f" · {title}" if title else "")
            out += [f"## {head}", ""]
            out += body if any(x.strip() for x in body) else [("（这一页没有文字）" if cjk else "(No text on this slide)"), ""]
            notes = _notes(z, rels)
            if notes:
                out.append(("备注：" if cjk else "Notes: ") + " ".join(_md_esc(n) for n in notes))
                out.append("")
        if not shown:
            raise PptxError(f"PPT 中所有页均已隐藏：{src.name}")
        if fig_meta:
            (figs_dir / "figures.json").write_text(json.dumps(fig_meta, ensure_ascii=False, indent=1), encoding="utf-8")
    md = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip() + "\n"
    return md, {"engine": "pptx", "slides": shown, "figures": nfig, "tables": ntab}


def readonly(md_path) -> bool:
    """这篇是不是从 PPT 转来的讲稿（只能看不能改）：看它旁边 .bloom-convert.json 里记的原文件。"""
    try:
        p = Path(str(md_path))
        if p.suffix.lower() == ".pptx":
            return True
        meta = json.loads((p.parent / CONV_META).read_text(encoding="utf-8"))
        src = str(meta.get("source") or "")
        return src.lower().endswith(".pptx") and Path(src).stem == p.stem
    except (OSError, ValueError, AttributeError):
        return False


READONLY_SAY = ("PPT 不支持编辑", "Slides are view-only. I can't edit them.")
