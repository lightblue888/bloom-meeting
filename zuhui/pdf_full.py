# -*- coding: utf-8 -*-
"""新 PDF 先出文字、后台补全（2026-11-20，用户："让读取文档的速度加快一些 对那些新文件来说"）。

装了 MinerU 的电脑上，一篇新 PDF 认公式和表格要 30 到 60 秒。papers.import_file 先用 PyMuPDF 抽纯文本（0.1–0.2 秒）
装成文档（meta：engine=pymupdf、pending_full=true），大厅、会议页马上能用；MinerU 全量转换排进这里的后台队列：
一次只转一篇，同一篇不重复排。转完自动换成完整版：
    · 换之前核对：这份 .md 从纯文本版装上以后没被改过（md_sha1 对得上），也没有已完成的 AI 修订（guard）。
      对不上就不换，留着现在的，meta 记 full_skipped；
    · 换的时候（拿着对比页的 _DOC_LOCK，和手改、打勾不同时写）：MinerU 的 .md 和 figures/ 写进 <名字>-bloom/，
      meta 改成新引擎、新指纹、去掉 pending_full；这篇有对比页基线的（摊开过、带进会的），基线和"开会那一刻的原文"
      一起换成完整版：原文和现稿都是完整版，不留"修订"；
    · MinerU 失败：留着纯文本版，去掉 pending_full，meta 记 full_error。
换完 / 不换 / 失败都叫 listeners（app 在那里重新生成对比页、换掉"开会前"的版本快照、叫醒等着的改稿工人）。
换之前这篇上的改稿任务在任务板上排着队、不开始（hands._loop 问 pending()）。
后端重启时队列没了：resume() 把 meta 里还写着 pending_full 的重新排上。"""
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from . import mineru_svc, pdfconv

CONV_META = ".bloom-convert.json"          # 和 papers.CONV_META 一样
listeners: list = []                        # fn(dst: Path, outcome: "swapped"|"kept"|"failed", info: dict)
guard = None                                # fn(dst: Path) -> 不换的原因 or None（app 设：这篇有已完成的 AI 修订）
log = lambda text: None                     # noqa: E731  app 设

_cv = threading.Condition()
_jobs: list[dict] = []                      # 排着的和正在转的（running=True）
_worker: threading.Thread | None = None


def _key(p) -> str:
    try:
        return os.path.normcase(str(Path(str(p)).resolve()))
    except OSError:
        return os.path.normcase(str(p))


def _sha1(p: Path) -> str:
    try:
        return hashlib.sha1(Path(p).read_bytes()).hexdigest()
    except OSError:
        return ""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_meta(dst) -> dict:
    try:
        d = json.loads((Path(str(dst)).parent / CONV_META).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(dst: Path, meta: dict) -> None:
    p = Path(dst).parent / CONV_META
    try:
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _update_meta(dst: Path, drop=(), **kw) -> dict:
    meta = read_meta(dst)
    for k in drop:
        meta.pop(k, None)
    meta.update(kw)
    _write_meta(dst, meta)
    return meta


# ---------- 队列 ----------
def enqueue(src, dst, keep=None) -> bool:
    """把 src（PDF）的 MinerU 全量转换排上，转完换掉 dst（纯文本版 .md）。同一份 dst 已经排着 / 正在转：不重复排，返回 False。"""
    global _worker
    k = _key(dst)
    with _cv:
        if any(j["key"] == k and not j.get("done") for j in _jobs):
            return False
        _jobs.append({"key": k, "src": str(src), "dst": str(dst), "keep": str(keep) if keep else None,
                      "sha": read_meta(dst).get("md_sha1") or _sha1(Path(dst)), "queued": time.time()})
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, name="pdf-full", daemon=True)
            _worker.start()
        _cv.notify_all()
    return True


def pending(path) -> bool:
    """这份 .md 正等着换成完整版（排着或正在转）。"""
    if not path:
        return False
    k = _key(path)
    with _cv:
        return any(j["key"] == k and not j.get("done") for j in _jobs)


def waiting_on_disk(path) -> bool:
    """别的进程（CCD 工人：python -m zuhui worker watch / claim）看这篇是不是还在等完整版：后端的队列它看不到，
    读 .md 旁边 meta 里的 pending_full（换完、不换、失败都会去掉；后端重启时 resume() 重新排上或去掉）。"""
    if not path:
        return False
    p = Path(str(path))
    if p.suffix.lower() != ".md" or not p.is_file():
        return False
    meta = read_meta(p)
    return bool(meta.get("pending_full")) and Path(str(meta.get("source") or "")).stem == p.stem


def pending_paths() -> list[str]:
    with _cv:
        return [j["dst"] for j in _jobs if not j.get("done")]


def wait_idle(timeout: float = 30.0) -> bool:
    """测试用：等队列空。"""
    end = time.time() + timeout
    with _cv:
        while _jobs:
            left = end - time.time()
            if left <= 0:
                return False
            _cv.wait(left)
    return True


def resume(paths) -> int:
    """后端重启：这些 .md 的 meta 里还写着 pending_full、也没被改过的，重新排上。返回排上几篇。"""
    if not mineru_svc.available():
        return 0
    n = 0
    for p in paths or []:
        try:
            dst = Path(str(p))
            meta = read_meta(dst)
            if not meta.get("pending_full") or not dst.is_file():
                continue
            src = Path(str(meta.get("source") or ""))
            if not src.is_file() or (meta.get("md_sha1") and meta["md_sha1"] != _sha1(dst)):
                _update_meta(dst, drop=("pending_full",), full_skipped="edited" if src.is_file() else "source_missing",
                             full_at=_now())
                continue
            n += bool(enqueue(src, dst))
        except Exception:
            continue
    return n


def _loop() -> None:
    global _worker
    while True:
        with _cv:
            job = next((j for j in _jobs if not j.get("running") and not j.get("done")), None)
            if job is None:
                _worker = None
                return
            job["running"] = True
        try:
            outcome, info = _run(job)
        except Exception as e:                       # 怎么炸都不能让这篇一直挂着"识别中"
            outcome, info = "failed", {"error": f"{type(e).__name__}: {e}"}
            try:
                _update_meta(Path(job["dst"]), drop=("pending_full",), full_error=info["error"][:300], full_at=_now())
            except Exception:
                pass
        with _cv:
            job["done"] = True                       # pending() 不再算它：叫醒的工人马上能领这篇的任务
            _cv.notify_all()
        name = Path(job["dst"]).name
        if outcome == "swapped":
            log(f"已换成完整版：{name}（{info.get('secs')} 秒，图 {info.get('figures', 0)}、表 {info.get('tables', 0)}、"
                f"公式 {info.get('equations', 0)}）")
        elif outcome == "kept":
            log(f"没有换成完整版（{info.get('why')}），保留现在的：{name}")
        else:
            log(f"识别公式和表格失败，保留纯文本：{name}：{info.get('error')}")
        for fn in list(listeners):
            try:
                fn(Path(job["dst"]), outcome, info)
            except Exception as e:
                log(f"换完整版之后的收尾出错：{e!r}")
        with _cv:                                    # 收尾也做完了才出队（wait_idle 等到这里）
            if job in _jobs:
                _jobs.remove(job)
            _cv.notify_all()


def _run(job: dict) -> tuple[str, dict]:
    src, dst = Path(job["src"]), Path(job["dst"])
    sha0 = job.get("sha") or ""
    if not src.is_file():
        _update_meta(dst, drop=("pending_full",), full_error="源文件不存在", full_at=_now())
        return "failed", {"error": "源文件不存在"}
    if not dst.is_file() or _sha1(dst) != sha0:     # 还没轮到就被改过（或删了）：不用转了
        _update_meta(dst, drop=("pending_full",), full_skipped="edited", full_at=_now())
        return "kept", {"why": "edited"}
    stage = Path(tempfile.mkdtemp(prefix="bloom-full-"))
    t0 = time.time()
    try:
        with mineru_svc.use() as url:
            try:
                md, stats = pdfconv.convert_pdf(src, stage, remote_url=url)
            except pdfconv.ConvertError as e:
                if not url:
                    raise
                log(f"MinerU 服务转换失败，改为本地转换：{str(e)[:200]}")
                shutil.rmtree(stage / "figures", ignore_errors=True)
                md, stats = pdfconv.convert_pdf(src, stage)
        stats["secs"] = round(time.time() - t0, 1)
        from .view import _DOC_LOCK, _copy_doc, base_for
        with _DOC_LOCK:
            why = None
            if not dst.is_file() or _sha1(dst) != sha0:
                why = "edited"
            elif guard is not None:
                try:
                    why = guard(dst) or None
                except Exception:
                    why = None
            if why:
                _update_meta(dst, drop=("pending_full",), full_skipped=why, full_at=_now())
                return "kept", {"why": why}
            pdfconv.install(stage, md, dst)
            sha = _sha1(dst)
            _update_meta(dst, drop=("pending_full", "full_error", "full_skipped"), engine=stats.get("engine"),
                         md_sha1=sha, at=_now(), full_at=_now(),
                         **{k: v for k, v in stats.items() if k in ("figures", "tables", "equations", "tier", "secs")})
            b = base_for(dst)                        # 摊开过 / 带进会的：原文基线一起换成完整版，不留"修订"
            if b.exists():
                _copy_doc(dst, b)
                _copy_doc(b, Path(str(b) + ".orig"))
            if job.get("keep"):
                orig = Path(job["keep"]) / ("original" + dst.suffix)
                if orig.is_file():
                    try:
                        shutil.copyfile(dst, orig)
                    except OSError:
                        pass
        return "swapped", dict({k: v for k, v in stats.items() if k in ("figures", "tables", "equations", "engine", "secs")},
                               sha=sha, old_sha=sha0)
    except pdfconv.ConvertError as e:
        err = str(e)[:300]
        _update_meta(dst, drop=("pending_full",), full_error=err, full_at=_now())
        return "failed", {"error": err}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
