# -*- coding: utf-8 -*-
"""把拖进来的论文文件接到学生手上。学生直接改用户自己文件夹里的那份，不改副本：
    .md / .markdown / .txt  就地改这个文件
    .tex                    就地改这个文件（当纯文本比对）
    .pdf                    MinerU 转成 Markdown（公式是 LaTeX、表格是 Markdown 表、图截进 figures/），
                            写到同目录下的 <名字>-bloom/<名字>.md，学生改这份（不回写 PDF）；
                            MinerU 没装 / 失败 / 超时就退回 PyMuPDF 按版面块抽纯文本。
                            装了 MinerU、又没有转好的：先用 PyMuPDF 纯文本装上（不到 1 秒），MinerU 排进后台，
                            转完自动换成完整版（zuhui/pdf_full.py；meta 里 pending_full=true 表示还在等）
    .docx                   pandoc 转 gfm（公式 TeX，图抽进 figures/），同样写到 <名字>-bloom/<名字>.md
    .pptx                   转成讲稿（每页标题、正文、表格、图、备注，zuhui/pptx_conv.py），写到 <名字>-bloom/<名字>.md；
                            只读：AI 不改 PPT，手改一段、写回也不做
转换出来的放在单独的 <名字>-bloom/ 文件夹：同一目录里几篇论文的图、你自己的 figures/ 都不会被覆盖。
以前导入时放在 PDF 旁边的 <名字>.md 如果开会时改过，就接着用它。
开会时的原样另存一份到 ~/.zuhui/papers/<时间戳>-<文件名>/original.<ext>，对比页拿它当"原文"。
源文件所在目录写不进去（光盘、只读共享）时，退回到 ~/.zuhui/papers/… 下的副本。
返回 {"path","name","kind","chars","source","dir","folder","in_place"}，PDF / Word 另有 "convert"（引擎、用时、图表公式数）。
转得慢（MinerU 一篇几十秒）：import_file(progress=回调) 或 import_status() 看进度，也写在 ~/.zuhui/paper_import.json。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import pdfconv
from .config import STATE

PAPERS = STATE / "papers"
TEXT_EXT = {".md", ".markdown", ".txt"}
STATUS_FILE = STATE / "paper_import.json"
CONV_META = ".bloom-convert.json"
# 新 PDF 先出文字、后台补全（pdf_full.py）。ZUHUI_PDF_QUICK=0 退回以前：导入时就等 MinerU 转完
QUICK = os.environ.get("ZUHUI_PDF_QUICK", "1").strip().lower() not in ("0", "off", "no")


def _slug(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", s).strip("-")
    return s[:60] or "paper"


KEEP_MAX = 40          # ~/.zuhui/papers 下最多留这么多份导入时的原样，多了删最旧的


def _writable_dir(d: Path) -> bool:
    try:   # 探测文件名每次都不同：两个导入同时跑时，一个删掉的探测文件不会让另一个误判"写不进去"
        probe = d / f".bloom-probe-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# ---------- 进度 ----------
_status: dict = {"busy": False}
_status_lock = threading.Lock()


def import_status() -> dict:
    """正在导入的论文转到哪了：{"busy","name","stage","done","total","text","elapsed"}（前端可以轮询）。"""
    with _status_lock:
        d = dict(_status)
    t0 = d.pop("t0", None)
    if d.get("busy") and t0:
        d["elapsed"] = round(time.time() - t0, 1)
    return d


def _report(cb, **kw) -> None:
    with _status_lock:
        _status.update(kw)
        snap = dict(_status)
    t0 = snap.pop("t0", None)
    if snap.get("busy") and t0:
        snap["elapsed"] = round(time.time() - t0, 1)
    try:
        STATUS_FILE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    if cb:
        try:
            cb(snap)
        except Exception:
            pass


# ---------- PyMuPDF（MinerU 不在时的退路） ----------
def _pdf_text(src: Path) -> str:
    """按版面块抽纯文本；去掉每页重复的页眉页脚、页码、行号。"""
    import pymupdf as fitz  # PyMuPDF（新版不再用 fitz 这个名字）

    doc = fitz.open(str(src))
    pages = []
    for page in doc:
        r = page.rect
        keep = []
        for b in sorted(page.get_text("blocks"), key=lambda b: (round(b[1] / 5), b[0])):  # (x0,y0,x1,y1,text,no,type)
            if len(b) > 6 and b[6] != 0:
                continue  # 图片块
            t = " ".join(ln.strip() for ln in (b[4] or "").splitlines() if ln.strip())
            t = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", t)  # 中文行尾拼接不要空格
            t = re.sub(r"-\s+(?=[a-z])", "", t)                                 # 英文断字连回去
            if len(t) < 2 and not t.isdigit():
                continue
            band = "top" if b[3] < r.y0 + 0.08 * r.height else "bottom" if b[1] > r.y0 + 0.92 * r.height else ""
            narrow = (b[2] - b[0]) < 0.05 * r.width
            if narrow and (b[3] - b[1]) > 0.25 * r.height:
                continue                                  # 页边竖排的字：arXiv 编号、审稿版行号
            keep.append((t, band, narrow))
        pages.append(keep)
    n = len(pages)
    seen: dict = {}
    for keep in pages:
        for t, band, _ in keep:
            if band:
                k = (band, re.sub(r"\d+", "#", t))
                seen[k] = seen.get(k, 0) + 1
    out = [f"# {src.stem}", ""]
    for pno, keep in enumerate(pages, 1):
        paras = []
        for t, band, narrow in keep:
            if re.fullmatch(r"[\d\s\-–—/.|第页共Page of]{1,16}", t) and (band or narrow or len(t) <= 4):
                continue                                         # 页码、行号
            if band and n >= 3 and seen.get((band, re.sub(r"\d+", "#", t)), 0) >= max(3, n * 0.3):
                continue                                         # 每页都有的页眉页脚
            if len(t) >= 2:
                paras.append(t)
        if paras:
            out.append(f"<!-- 第 {pno} 页 -->")
            out.append("")
            for p in paras:
                out.append(p)
                out.append("")
    doc.close()
    return "\n".join(out).rstrip() + "\n"


def _pdf_precheck(src: Path) -> None:
    """转之前先挡掉加密的和扫描件（MinerU 跑一遍要几十秒，别白跑）。"""
    import pymupdf as fitz  # PyMuPDF（新版不再用 fitz 这个名字）
    try:
        doc = fitz.open(str(src))
    except Exception:
        raise ValueError(f"PDF 文件已损坏：{src.name}")
    try:
        if doc.needs_pass:
            raise ValueError(f"PDF 已加密：{src.name}")
        chars = 0
        for page in doc:
            chars += len(re.sub(r"\s", "", page.get_text("text") or ""))
            if chars >= 200:
                return
        if chars < 20:
            raise ValueError(f"PDF 中没有文字（可能是扫描件）：{src.name}")
    finally:
        doc.close()


def _decode(raw: bytes) -> tuple[str, str]:
    """文本论文的编码：先 UTF-8，不行再 GB18030（Windows 记事本存的中文常是 GBK）。返回 (文字, 编码名)。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8?"


def _prune_keep() -> None:
    try:
        dirs = sorted((d for d in PAPERS.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
        for d in dirs[:-KEEP_MAX]:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


# ---------- PDF / Word → Markdown ----------
def _sha1(p: Path) -> str:
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def _conv_version(meta: dict) -> int:
    """.bloom-convert.json 里记的转换器版本；没记的是加版本号之前转的，算 1。"""
    try:
        return int(meta.get("conv") or 1)
    except (TypeError, ValueError):
        return 1


def _pandoc_plain(src: Path, pandoc: str) -> str:
    r = subprocess.run([pandoc, str(src), "-t", "gfm", "--wrap=none"], capture_output=True, timeout=120,
                       creationflags=pdfconv.NO_WINDOW)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def _legacy_edited(src: Path, legacy: Path, ext: str, pandoc: str | None) -> bool:
    """以前导入时写在源文件旁边的 <名字>.md：开会时改过就接着用；还是当时自动转出来的原样就重新转。"""
    try:
        old = legacy.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return True
    try:
        if ext == ".pdf":
            if "<!-- 第 1 页 -->" not in old:
                return True                    # 不是我们抽的（你自己的 Markdown）
            import pymupdf as fitz  # PyMuPDF（新版不再用 fitz 这个名字）
            doc = fitz.open(str(src))
            fresh = [f"# {src.stem}", ""]   # 最早那版 PDF 抽字的输出，原样重算一遍对比（现在的 _pdf_text 在下面另比）
            for pno, page in enumerate(doc, 1):
                paras = []
                for b in sorted(page.get_text("blocks"), key=lambda b: (round(b[1] / 5), b[0])):
                    if len(b) > 6 and b[6] != 0:
                        continue
                    t = " ".join(ln.strip() for ln in (b[4] or "").splitlines() if ln.strip())
                    t = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", t)
                    t = re.sub(r"-\s+(?=[a-z])", "", t)
                    if len(t) >= 2:
                        paras.append(t)
                if paras:
                    fresh += [f"<!-- 第 {pno} 页 -->", ""]
                    for p in paras:
                        fresh += [p, ""]
            doc.close()
            return old.strip() not in ("\n".join(fresh).strip(), _pdf_text(src).strip())
        if pandoc:
            return old.strip() != _pandoc_plain(src, pandoc).replace("\r\n", "\n").strip()
    except Exception:
        return True
    return True


def _convert(src: Path, ext: str, folder: Path, work: Path, in_place: bool, cb, quick: bool = False) -> tuple[Path, dict]:
    """PDF / docx → 学生改的 Markdown。返回 (路径, 转换信息)。
    quick（PDF，装了 MinerU）：没有转好的就先抽纯文本，转换信息带 pending_full=True，调用方把 MinerU 排进后台。"""
    from .components import pandoc as _find_pandoc         # 随后端装的 pypandoc-binary 优先，没有再找系统的
    pandoc = _find_pandoc() if ext == ".docx" else None
    if ext == ".docx" and not pandoc:
        raise RuntimeError("缺少 pandoc，无法导入 Word")
    # 1) 以前放在源文件旁边、开会改过的那份（PPT 没有这回事：旁边同名的 .md 是你自己的）
    legacy = folder / (src.stem + ".md")
    if ext != ".pptx" and in_place and legacy.exists() and legacy.stat().st_mtime >= src.stat().st_mtime \
            and _legacy_edited(src, legacy, ext, pandoc):
        return legacy, {"engine": "existing", "note": "沿用旁边已有的、改过的 " + legacy.name}
    # 2) 转换结果放进自己的文件夹
    out_dir = work
    if in_place:
        out_dir = work / f"{src.stem}-bloom"
        try:
            out_dir.mkdir(exist_ok=True)
        except OSError:
            raise PermissionError(out_dir)
    dst = out_dir / (src.stem + ".md")
    meta_p = out_dir / CONV_META
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        edited = meta.get("md_sha1") and meta.get("md_sha1") != _sha1(dst)
        better = ext == ".pdf" and meta.get("engine") == "pymupdf" and pdfconv.mineru_kit() is not None
        # Word 的转换规则升级过（pdfconv.DOCX_VERSION）：上次是旧版本转的、Markdown 也没人改过（指纹对得上）就重转；
        # 改过的（会上的修订）一律不动；没有指纹、说不清改没改过的，也不动
        stale = (ext == ".docx" and meta.get("engine") == "pandoc" and bool(meta.get("md_sha1"))
                 and _conv_version(meta) < pdfconv.DOCX_VERSION)
        if edited or not (better or stale):
            return dst, {"engine": meta.get("engine") or "existing", "note": "沿用上次转好的 " + dst.name,
                         **{k: v for k, v in meta.items() if k in ("figures", "tables", "equations")}}
        if better and quick:
            # 上次的纯文本版（还在等、或者上次 MinerU 没转成），没人改过：接着用它，MinerU 再排进后台
            from . import pdf_full
            pdf_full._update_meta(dst, drop=("full_error", "full_skipped"), pending_full=True)
            return dst, {"engine": "pymupdf", "note": "沿用纯文本版，后台识别公式和表格", "pending_full": True}
    stage = Path(tempfile.mkdtemp(prefix="bloom-conv-"))
    t0 = time.time()
    try:
        if ext == ".pdf" and quick:
            _report(cb, stage="PyMuPDF", text="正在提取文字")
            md, stats = _pdf_text(src), {"engine": "pymupdf", "pending_full": True}
        elif ext == ".pdf":
            try:
                from . import mineru_svc                    # 常驻服务起着就交给它（模型已经载好）
                md, stats = pdfconv.convert_pdf(src, stage, progress=lambda **kw: _report(cb, **kw), remote_url=mineru_svc.url())
            except pdfconv.ConvertError as e:
                _report(cb, stage="PyMuPDF", text="识别失败，改为提取纯文本")
                shutil.rmtree(stage / "figures", ignore_errors=True)
                md, stats = _pdf_text(src), {"engine": "pymupdf", "warning": str(e)[:300]}
        elif ext == ".pptx":
            from . import pptx_conv
            _report(cb, stage="pptx", text="正在转换 PPT")
            md, stats = pptx_conv.convert_pptx(src, stage)
        else:
            _report(cb, stage="pandoc", text="正在转换 Word")
            md, stats = pdfconv.convert_docx(src, stage, pandoc)
        stats["secs"] = round(time.time() - t0, 1)
        pdfconv.install(stage, md, dst)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    try:
        meta_p.write_text(json.dumps({"engine": stats.get("engine"), "source": str(src), "src_mtime": src.stat().st_mtime,
                                      "md_sha1": _sha1(dst), "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                      **({"conv": pdfconv.DOCX_VERSION} if ext == ".docx" else {}),
                                      **({"pending_full": True} if stats.get("pending_full") else {}),
                                      **{k: v for k, v in stats.items() if k in ("figures", "tables", "equations", "tier", "secs", "slides")}},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return dst, stats


ZIP_PART_MAX = 300 * 1024 * 1024     # Word / PPT 里一个部件解开最多这么大（正文 XML 一般几 MB，大的是视频）
ZIP_XML_MAX = 256 * 1024 * 1024      # 所有 XML 部件解开加起来
ZIP_ALL_MAX = 1024 * 1024 * 1024     # 整份解开
ZIP_PARTS_MAX = 20000


def _zip_precheck(src: Path) -> None:
    """Word / PPT 是 zip：转换前先一块块解一遍数大小（不留在内存里，顺带校验 CRC，头里写小了的也逃不掉）。
    压缩炸弹（几百 KB 解开几十 GB，访客能传进来）直接挡掉，不让转换把内存吃光。不是 zip 的交给后面照旧报错。"""
    import zipfile
    try:
        z = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError):
        return
    bad = ValueError(f"文件解压后过大：{src.name}")
    with z:
        infos = z.infolist()
        if len(infos) > ZIP_PARTS_MAX:
            raise bad
        total = xml = 0
        for info in infos:
            if info.is_dir():
                continue
            is_xml = info.filename.lower().endswith((".xml", ".rels"))
            got = 0
            try:
                with z.open(info) as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        got += len(chunk)
                        if (got > ZIP_PART_MAX or total + got > ZIP_ALL_MAX
                                or (is_xml and xml + got > ZIP_XML_MAX)):
                            raise bad
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError) as e:
                raise ValueError(f"文件已损坏：{src.name}") from e
            total += got
            xml += got if is_xml else 0


def import_file(src: str | Path, progress=None, quick: bool | None = None) -> dict:
    """progress：可选回调，收到 {"busy","name","stage","done","total","text","elapsed"}（PDF 转换时每半秒左右一次）。
    quick（默认 QUICK）：装了 MinerU 的 PDF 没有转好的，先出纯文本、MinerU 排进后台（返回的 info 带 pending_full）。"""
    src = Path(src).resolve()
    # 先把拿不来当论文的挡掉（报给用户看的话要说人话），再动任何文件
    if not src.exists():
        raise FileNotFoundError(f"文件不存在：{src.name}")
    if src.is_dir():
        raise ValueError(f"不支持导入文件夹：{src.name}")
    ext = src.suffix.lower()
    if ext not in TEXT_EXT | {".tex", ".pdf", ".docx", ".pptx"}:
        raise ValueError(f"不支持的文件类型：{ext or '（没有扩展名）'}（支持 pdf / md / tex / docx / pptx / txt）")
    if src.stat().st_size == 0:
        raise ValueError(f"文件为空：{src.name}")
    if ext == ".pdf":
        _pdf_precheck(src)
    if ext in (".docx", ".pptx"):
        _zip_precheck(src)
    _report(progress, busy=True, name=src.stem, stage="开始", done=0, total=0, text=f"正在导入 {src.name}",
            t0=time.time(), error=None)
    quick = (QUICK if quick is None else bool(quick)) and ext == ".pdf" and pdfconv.mineru_kit() is not None
    try:
        return _import(src, ext, progress, quick)
    except Exception as e:
        _report(progress, error=str(e)[:300])
        raise
    finally:
        _report(progress, busy=False, stage="完成")


def _import(src: Path, ext: str, progress, quick: bool = False) -> dict:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    keep = PAPERS / f"{stamp}-{_slug(src.stem)}-{uuid.uuid4().hex[:4]}"   # 原样快照放这里，不碰用户的文件夹
    keep.mkdir(parents=True, exist_ok=True)
    folder = src.parent
    in_place = _writable_dir(folder)
    work = folder if in_place else keep           # 学生要改的文件放哪
    conv = None

    if ext in TEXT_EXT or ext == ".tex":
        text, enc = _decode(src.read_bytes())
        if not text.strip():
            shutil.rmtree(keep, ignore_errors=True)
            raise ValueError(f"文件中没有文字：{src.name}")
        if enc != "utf-8-sig":
            # 不是 UTF-8（多半是 GBK）：转成 UTF-8 存一份，学生改这份，原文件不动
            dst = work / (src.stem + ".utf8" + (".tex" if ext == ".tex" else ".md"))
            dst.write_text(text, encoding="utf-8")
        elif in_place:
            dst = src                              # 就改这一份
        else:
            dst = work / (src.name if ext == ".tex" else src.stem + ".md")
            shutil.copyfile(src, dst)
        kind = "latex" if ext == ".tex" else "markdown"
    elif ext in (".pdf", ".docx", ".pptx"):
        try:
            try:
                dst, conv = _convert(src, ext, folder, work, in_place, progress, quick)
            except PermissionError:               # 源目录能写探测文件、却建不了子文件夹：退回副本
                in_place, work = False, keep
                dst, conv = _convert(src, ext, folder, work, in_place, progress, quick)
        except Exception:
            shutil.rmtree(keep, ignore_errors=True)
            raise
        if ext == ".pdf":
            body = re.sub(r"<!--.*?-->|^#.*$|^\[image: [^\]]*\]$", "", dst.read_text(encoding="utf-8", errors="replace"), flags=re.M)
            if len(re.sub(r"\s", "", body)) < 20:
                shutil.rmtree(keep, ignore_errors=True)
                raise ValueError(f"PDF 中没有文字（可能是扫描件）：{src.name}")
        kind = "pdf" if ext == ".pdf" else "pptx" if ext == ".pptx" else "docx"
    else:
        raise ValueError(f"不支持的文件类型：{ext}（支持 pdf / md / tex / docx / pptx / txt）")
    try:
        chars = len(dst.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError:
        chars = dst.stat().st_size
    shutil.copyfile(dst, keep / ("original" + dst.suffix))  # 导入时的原样，留着做"原文"
    _prune_keep()
    info = {"path": str(dst), "name": src.stem, "kind": kind, "chars": chars, "source": str(src),
            "dir": str(keep), "folder": str(dst.parent),
            "in_place": bool(in_place and (dst.parent == folder or dst.parent.parent == folder))}
    if conv is not None:
        info["convert"] = conv
    if conv and conv.get("pending_full"):          # 先出的纯文本：MinerU 排进后台，转完换成完整版
        from . import pdf_full
        pdf_full.enqueue(src, dst, keep)
        info["pending_full"] = True
    return info
