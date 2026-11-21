# -*- coding: utf-8 -*-
"""组会主界面：一页实时刷新的「文稿修订对比」。

老师就对着这一页读论文（口喷鸡录的也是这个窗口）。工人每改一处，页面上立刻长出修订标记
（删红增绿），右栏挂上老师原话和学生汇报，J/K 跳改动。用的是 ~/.claude/skills/html-diff 的生成器：
    原文 = 开会时的镜像快照（~/.zuhui/base/<文件名>）      现稿 = paper/ 里的镜像（工人改这里）
    批注 = 任务板（编号就是任务号）  规则 = 每个任务改稿前后抓到的「独有新增文字」（~/.zuhui/changes/<任务>.json）

HTTP（127.0.0.1:47330）：
    GET /          对比页（注入了自动刷新 + 控制条）
    GET /version   {"build": n, "counts": {...}, "mirror": 时间}
    GET /record    让口喷鸡开始录屏（blurt://meeting）
    GET /rebuild   强制重算
    GET /rebase    把当前镜像设为新的原文快照（新一场会）
"""
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .board import Board, task_paper, note_tags
from .board import is_app_task as _is_app_task
from .config import STATE, load, paper_path
from .meetings import paper_id

# 修订对比页的生成器：仓库里带的那份（vendor/html-diff，开源版）优先，没有就用本机 Claude 技能里的
HTMLDIFF = next((p for p in (Path(__file__).resolve().parent.parent / "vendor" / "html-diff" / "scripts" / "htmldiff.py",
                             Path.home() / ".claude" / "skills" / "html-diff" / "scripts" / "htmldiff.py") if p.is_file()),
                Path.home() / ".claude" / "skills" / "html-diff" / "scripts" / "htmldiff.py")
VIEW_DIR = STATE / "view"
BASE_DIR = STATE / "base"
HOST_COLOR, CLAUDE_COLOR = "#60a5fa", "#e3906f"
GUEST_PALETTE = ['#4ade80', '#facc15', '#f472b6', '#22d3ee', '#c084fc', '#f87171', '#2dd4bf', '#a3e635', '#e879f9', '#fbbf24']
AVATARS = STATE / "avatars"   # 头像：<用户名>.jpg / claude.svg / bloom.png（链接进来的人默认用它）
AVATAR_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp"}
FIG_TYPES = dict(AVATAR_TYPES, **{".gif": "image/gif", ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8"})
# 公式排版用本机那份 MathJax，不联网：项目里的完整版（带全部 TeX 扩展，boldsymbol / mathtools / physics 这些不用再去拉），
# 没有就退回 live-doc 那份精简版
MATHJAX = next((p for p in (Path(__file__).resolve().parent.parent / "assets" / "mathjax-tex-svg-full.js",
                             Path.home() / ".claude" / "tools" / "live-doc" / "mathjax-tex-svg.js") if p.is_file()),
               Path.home() / ".claude" / "tools" / "live-doc" / "mathjax-tex-svg.js")
CHANGES_DIR = STATE / "changes"
PORT = int(os.environ.get("ZUHUI_VIEW_PORT", "47330"))


def base_for(path) -> Path:
    """这篇论文"还没看过的改动"的基线（加 .orig 是开会 / 加进会那一刻的原文）。按论文编号存：
    会中摊开几篇时各有各的；两个文件夹里都叫 main.tex 的两篇也不互相盖掉。"""
    p = Path(str(path))
    return BASE_DIR / f"{paper_id(p)}-{p.name}"


def _same_file(a, b) -> bool:
    try:
        return os.path.normcase(str(Path(str(a)).resolve())) == os.path.normcase(str(Path(str(b)).resolve()))
    except OSError:
        return str(a) == str(b)


# ---------- 稿子和基线的读写（2026-11-15） ----------
# 2026-11-14 组会：同一处的三条一起打勾，对比页同时发了三个请求，三个线程同时读基线、各改各的、各写各的，
# 后写完的那份（短）盖在先写完的那份（长）的前半截上，文件尾巴留下前一份的最后 42 个字节，从"导"字中间断开：
# 基线解不开 UTF-8，对比页"对比生成失败"，另外一条打过勾的改动也丢了。现在：读改写一律拿 _DOC_LOCK；
# 写先写临时文件再整份换上去；读到写了一半的文件（解不开、读的时候大小在变）稍等再读；换行和 BOM 照原样写回。
_DOC_LOCK = threading.RLock()


class DocReadError(Exception):
    """稿子读不出来（一直解不开 UTF-8 / 一直在变）。"""


def _read_raw(path, tries: int = 8) -> bytes:
    """读一份正在被别人写的文件：读到的字节能按 UTF-8 解开、读的前后大小和修改时间都没变才算读到；
    不然稍等再读（最多约 1.8 秒）。一直不行抛 DocReadError（带最后一次读到的字节，调用方可以将就着用）。"""
    p = Path(path)
    raw = b""
    for i in range(tries):
        try:
            s1 = p.stat()
            raw = p.read_bytes()
            s2 = p.stat()
        except OSError as e:
            if i == tries - 1:
                raise
            time.sleep(0.04 * (i + 1))
            continue
        if (s1.st_size, s1.st_mtime_ns) == (s2.st_size, s2.st_mtime_ns) and len(raw) == s2.st_size:
            try:
                raw.decode("utf-8")
                return raw
            except UnicodeDecodeError:
                pass
        time.sleep(0.04 * (i + 1))
    err = DocReadError(f"{p.name} 读不出来（不是完整的 UTF-8）")
    err.raw = raw
    raise err


def _split_doc(raw: bytes) -> tuple[str, bool, str]:
    """字节 → (换行统一成 \\n 的文字, 有没有 BOM, 原来的换行符)。CRLF 和 LF 混着的按多的那种写回。"""
    bom = raw.startswith(b"\xef\xbb\xbf")
    s = (raw[3:] if bom else raw).decode("utf-8")
    crlf = s.count("\r\n")
    nl = "\r\n" if crlf and crlf >= s.count("\n") - crlf else "\n"
    return s.replace("\r\n", "\n"), bom, nl


def _read_doc(path) -> tuple[str, bool, str]:
    """读稿子 / 基线：(\\n 换行的文字, 有没有 BOM, 原来的换行符)。"""
    return _split_doc(_read_raw(path))


def _join_doc(text: str, bom: bool, nl: str) -> bytes:
    t = str(text).replace("\r\n", "\n")
    if nl != "\n":
        t = t.replace("\n", nl)
    return (b"\xef\xbb\xbf" if bom else b"") + t.encode("utf-8")


def _write_bytes_atomic(path, data: bytes) -> None:
    """先写同目录的临时文件再整份换上去：谁在这时读都读到完整的旧版或新版。Windows 上目标正被别的进程开着时
    os.replace 会被拒：稍等再换，一直换不上才直接写（一次写完整份）。"""
    p = Path(path)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    for i in range(12):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.03 * (i + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    with open(p, "wb") as f:
        f.write(data)


def _write_doc(path, text: str, bom: bool, nl: str) -> None:
    """写回稿子 / 基线：\\n 换回原来的换行符，BOM 照原样。"""
    _write_bytes_atomic(path, _join_doc(text, bom, nl))


def _copy_doc(src, dst) -> None:
    """整份拷过去（开会 / 加进会时定原文）：读到完整的一份再写，字节不变。"""
    try:
        raw = _read_raw(src)
    except DocReadError as e:
        raw = e.raw
    _write_bytes_atomic(dst, raw)


INJECT = r"""
<style>
#zh-bar{position:fixed;right:14px;bottom:14px;z-index:9999;font:13px/1.4 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
 background:rgba(30,30,34,.92);color:#eee;border-radius:12px;padding:10px 12px;box-shadow:0 6px 24px rgba(0,0,0,.25);display:flex;gap:10px;align-items:center;max-width:min(92vw,520px)}
#zh-bar button{background:#3b82f6;color:#fff;border:0;border-radius:8px;padding:6px 12px;font:inherit;cursor:pointer}
#zh-bar button.ghost{background:transparent;border:1px solid #666;color:#ddd}
#zh-bar .st{opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:260px}
#zh-bar .dot{width:9px;height:9px;border-radius:50%;background:#888;display:inline-block;margin-right:6px}
#zh-bar .dot.on{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.25)}
@media (max-width:640px){#zh-bar{left:14px;right:14px;max-width:none}}
</style>
<div id="zh-bar"><span class="dot" id="zh-dot"></span><span class="st" id="zh-st">{{T:connecting}}</span>
<button id="zh-rec" aria-label="{{T:start}}">{{T:start}}</button>
<button class="ghost" id="zh-unfold" aria-label="{{T:unfold_aria}}">{{T:unfold}}</button></div>
<script>
(function(){
  var BUILD = __BUILD__;
  var st = document.getElementById('zh-st'), dot = document.getElementById('zh-dot');
  /* 读到哪儿：屏幕最上面那一段（开头几个字、第几段、滑过了多少），按"那一段"回去（不按像素：公式和图一排版就跳）。
     2026-11-21 改动一落地论文就回到最上面：外壳连不上后端几秒时整页重来，新页面没人告诉它读到哪。现在屏幕上的这一份
     边读边记（zh-read@论文），怎么重新加载都回到那一段；换新版本时外壳先问旧的一份读到哪、让新的一份停到同一段再换上来，
     你正在滚、在选字、在写批注时等一等再换（zhReadAsk → zhRead → zhGo → zhGone，见 MeetingLayout / paperSwap.ts） */
  function zhNorm(s){ return (s || '').replace(/\s+/g, ' ').trim(); }
  function zhTop(){ var b = document.querySelector('.bar'); return (b && getComputedStyle(b).display !== 'none' ? b.getBoundingClientRect().bottom : 0) + 8; }
  function zhText(el){          /* 一段的字：不算段头"第 N 段 · 已修改"、删掉的字、批注、图钉（这一段改了状态照样认得出） */
    var c = el.cloneNode(true);
    Array.prototype.forEach.call(c.querySelectorAll('.rowhead, del, .notes, .zh-pins, .zh-note, .zh-cm-pin, #zh-cm-add, .zh-editor'), function(x){ x.parentNode.removeChild(x); });
    return zhNorm(c.textContent);
  }
  function zhReading(){
    var rows = document.querySelectorAll('.docs .row'), top = zhTop();
    for (var i = 0; i < rows.length; i++) { var r = rows[i].getBoundingClientRect();
      if (r.bottom > top && r.height > 0) return {i: i, text: zhText(rows[i]).slice(0, 40), off: (top - r.top) / r.height, y: Math.round(window.scrollY)}; }
    return {i: -1, y: Math.round(window.scrollY)};
  }
  function zhFind(at){          /* 开头的字对得上的那段（几段一样时取离原来位置最近的）；对不上按第几段 */
    var rows = document.querySelectorAll('.docs .row'), key = zhNorm(at.text).slice(0, 24), best = null, gap = 1e9, i0 = at.i || 0;
    if (key.length >= 6) for (var i = 0; i < rows.length; i++) { if (Math.abs(i - i0) < gap && zhText(rows[i]).indexOf(key) === 0) { best = rows[i]; gap = Math.abs(i - i0); } }
    return best || (at.i >= 0 ? rows[at.i] || null : null);
  }
  /* 回到那一段之后的几秒里（你没动过）一直按它对齐：公式、图、字体排好以后上面的段落会变高 */
  var pin = null;
  function zhPlace(){
    if (!pin || !pin.el.isConnected) return;
    var r = pin.el.getBoundingClientRect(), y = Math.max(0, Math.round(r.top + window.scrollY - zhTop() + pin.off * r.height));
    if (Math.abs(y - window.scrollY) > 1) window.scrollTo(0, y);
  }
  function zhBackTo(at, ms){
    if (!at) return;
    var el = zhFind(at);
    if (!el) { pin = null; if (at.y) window.scrollTo(0, at.y); return; }
    pin = {el: el, off: Math.min(1, Math.max(0, at.off || 0)), until: performance.now() + (ms || 0)};
    zhPlace();
  }
  try { new ResizeObserver(function(){ if (pin && performance.now() < pin.until) zhPlace(); }).observe(document.body); } catch (e) {}
  /* 你在这一页上动了（滚轮、按键、点、拖）：不再替你对齐；刚动过、按着没放，外壳先别换版本 */
  var lastIn = -1e9, downAt = 0, selAt = 0;
  function zhInput(){ lastIn = performance.now(); pin = null; shown = true; }
  ['wheel', 'keydown', 'touchstart'].forEach(function(k){ window.addEventListener(k, zhInput, {capture: true, passive: true}); });
  window.addEventListener('pointerdown', function(){ zhInput(); downAt = performance.now(); }, true);
  ['pointerup', 'pointercancel', 'blur'].forEach(function(k){ window.addEventListener(k, function(){ if (downAt) lastIn = performance.now(); downAt = 0; }, true); });
  document.addEventListener('selectionchange', function(){ var s = window.getSelection(); if (s && !s.isCollapsed) { if (!selAt) selAt = performance.now(); } else selAt = 0; });
  function zhBusy(){
    var now = performance.now();
    if (now - lastIn < 700 || (downAt && now - downAt < 8000)) return 'input';
    if (document.querySelector('.zh-editor')) return 'edit';
    var cm = document.querySelector('#zh-cm-card textarea');
    if (cm && (cm.value.trim() || document.activeElement === cm)) return 'comment';
    if (selAt && now - selAt < 15000) return 'select';            /* 选了字（可能正要点「引用」）：最多等 15 秒 */
    return '';
  }
  var PAPER = __PAPER__;   /* 这一页是哪篇论文（会中能来回切）：读到哪按论文各记各的 */
  /* 新旧两份是不是同一篇（访客那份没有论文编号：认第一个标题） */
  var DOC = PAPER || zhNorm((document.querySelector('.docs .sect') || {}).textContent).slice(0, 60);
  /* 在屏幕上的这一份才记（外壳的 zhHello、你动过它）：后面那个槽里还没换上来的那份不记 */
  var shown = window.parent === window, keepT = 0;
  function zhKeep(){ if (!PAPER || !shown) return; try { var rd = zhReading(); rd.p = PAPER; sessionStorage.setItem('zh-read@' + PAPER, JSON.stringify(rd)); } catch (e) {} }
  window.addEventListener('scroll', function(){ if (!keepT) keepT = setTimeout(function(){ keepT = 0; zhKeep(); }, 250); }, {passive: true});
  try {
    var one = JSON.parse(sessionStorage.getItem('zh-read') || 'null');   /* 上一份换版本前记下的 */
    sessionStorage.removeItem('zh-read'); sessionStorage.removeItem('zh-scroll');
    if (one && one.p !== undefined && one.p !== PAPER) one = null;       /* 换了一篇：上一篇读到哪不往这篇上套 */
    /* 这篇最近读到哪（边读边记的，比换版本那一刻的新；以前切走时读到哪也在这里；没读过就从头） */
    var mine = PAPER ? JSON.parse(sessionStorage.getItem('zh-read@' + PAPER) || 'null') : null;
    zhBackTo(mine || one, 3000);
  } catch (e) {}
  window.addEventListener('message', function(e){
    var d = e.data || {};
    if (d.zhReloadAck) window.__zhHold = true;   /* 外壳接手换版本：这一份不再轮询 */
    if (d.zhHello) shown = true;
    if (d.zhCmd || d.zhFind || d.zhGoto || d.zhZoom != null) pin = null;   /* 外壳那一行的上一处 / 下一处 / 查找 / 缩放：你在动 */
    if (d.zhReadAsk && e.source) {               /* 外壳要换新版本了：读到哪，你是不是正在滚 / 选字 / 写批注 */
      var rd = zhReading(); rd.d = DOC; zhKeep();
      try { e.source.postMessage({zhRead: {id: d.zhReadAsk, at: rd, busy: zhBusy()}}, '*'); } catch (x) {}
    }
    if (d.zhGo && e.source) {                    /* 换上来之前：停到旧的那一份读到的那一段（同一篇才停） */
      if (d.zhGo.d === DOC) zhBackTo(d.zhGo, 3000);
      try { e.source.postMessage({zhGone: d.id}, '*'); } catch (x) {}
    }
  });
  function unfoldAll(open){ document.querySelectorAll('details.fold').forEach(function(d){ d.open = open; }); try{ localStorage.setItem('zh-unfold', open ? '1' : '0'); }catch(e){} }
  var pref = '1'; try { pref = localStorage.getItem('zh-unfold') || '1'; } catch (e) {}
  if (pref === '1') unfoldAll(true);
  document.getElementById('zh-unfold').onclick = function(){ var anyClosed = Array.prototype.some.call(document.querySelectorAll('details.fold'), function(d){ return !d.open; }); unfoldAll(anyClosed); };
  document.getElementById('zh-rec').onclick = function(){ fetch('/record').then(function(){ st.textContent = '{{T:rec_ok}}'; }).catch(function(){ st.textContent = '{{T:rec_fail}}'; }); };
  function tick(){
    fetch('/version', {cache:'no-store'}).then(function(r){ return r.json(); }).then(function(v){
      dot.className = 'dot on';
      var c = v.counts || {}; st.textContent = '{{T:st_queued}}' + (c.queued||0) + '{{T:st_working}}' + (c.working||0) + '{{T:st_done}}' + (c.done||0) + (c.notes ? '{{T:st_notes}}' + c.notes : '') + (c.failed ? '{{T:st_failed}}' + c.failed : '') + (v.building ? '{{T:st_rebuilding}}' : '');
      if (v.build !== BUILD && !window.__zhHold) {
        /* 先确认页面本身拿得到再刷新：后端正好在重启时刷新，会停在一张"无法访问此页面"上再也回不来 */
        fetch(location.href, {cache: 'no-store'}).then(function(r){
          if (!r.ok || window.__zhHold) return;
          try { var rd = zhReading(); rd.p = PAPER; sessionStorage.setItem('zh-read', JSON.stringify(rd)); zhKeep(); } catch (e) {}
          if (window.parent !== window && document.documentElement.classList.contains('zh-hosted')) {
            /* 嵌在 Bloom 里：请外壳在后面那个槽里加载新版本、准备好再换上来；外壳 1.5 秒没回应才自己刷新 */
            window.parent.postMessage({zhReload: BUILD}, '*');
            setTimeout(function(){ if (!window.__zhHold) location.reload(); }, 1500);
            return;
          }
          location.reload();
        }).catch(function(){});
      }
    }).catch(function(){ dot.className = 'dot'; st.textContent = '{{T:down}}'; });
  }
  if (BUILD !== null) { setInterval(tick, 1000); tick(); }   /* 每秒问一次有没有新版本（切论文、改完一处后一秒内换上） */
})();
/* 嵌在 Bloom 里时：照「文稿对比」做的章节栏（全文所有小节，可收起），并把"屏幕上正显示哪一节哪一段"报给后端，
   学生据此知道老师讲到哪了 */
(function(){
  var root = document.documentElement;
  if (!root.classList.contains('zh-embed')) return;
  var LIVE = (__BUILD__) !== null;   /* 以往会议的快照：不上报位置，批注只读 */
  var docs = document.querySelector('.docs'); if (!docs) return;
  var sects = Array.prototype.slice.call(docs.querySelectorAll('.sect.lv2, .sect.lv3'));
  function titleOf(el){
    var c = el.cloneNode(true);
    Array.prototype.forEach.call(c.querySelectorAll('.notes, del'), function(x){ x.parentNode.removeChild(x); });
    return (c.textContent || '').replace(/\s+/g, ' ').trim();
  }
  var chg = {}, cur = -1;
  Array.prototype.forEach.call(docs.querySelectorAll('.sect, .row.chg'), function(el){
    if (el.classList.contains('sect')) {
      var k = sects.indexOf(el); if (k >= 0) cur = k;
      if (el.classList.contains('chg') && cur >= 0) chg[cur] = 1;
    } else if (cur >= 0) chg[cur] = 1;
  });
  var aside = null, list = null, items = [], curIdx = -1, lock = 0;
  if (sects.length) {
    aside = document.createElement('aside'); aside.id = 'zh-toc';
    var nChg = Object.keys(chg).length;
    aside.innerHTML = '<div class="zh-tochead">{{T:toc_head}} <span>' + sects.length + '{{T:toc_n}}' + (nChg ? ' · ' + nChg + '{{T:toc_chg}}' : '') + '</span></div><div class="zh-toclist"></div>';
    list = aside.querySelector('.zh-toclist');
    sects.forEach(function(el, i){
      var d = document.createElement('div'), tt = titleOf(el);
      d.className = 'zh-ti' + (el.classList.contains('lv3') ? ' lv3' : '') + (chg[i] ? ' chg' : '');
      d.innerHTML = '<span class="zh-tx"></span><span class="zh-dot"></span>';
      d.firstChild.textContent = tt; d.title = tt;
      d.onclick = function(){ lock = Date.now() + 900; setCur(i); var y = el.getBoundingClientRect().top + window.scrollY - barBottom() - 6; window.scrollTo({top: y, behavior: 'smooth'}); if (!wide()) setOpen(false, true); };
      list.appendChild(d); items.push(d);
    });
    var btn = document.createElement('button'); btn.id = 'zh-tocbtn'; btn.type = 'button'; btn.setAttribute('aria-label', '{{T:toc_aria}}');
    document.body.appendChild(aside); document.body.appendChild(btn);
    var open = null; try { open = localStorage.getItem('zh-toc'); } catch (e) {}
    open = open === null ? window.innerWidth >= 900 : open === '1';
    function setOpen(v, transient){ open = v; if (!transient) { try { localStorage.setItem('zh-toc', v ? '1' : '0'); } catch (e) {} } apply(); }
    function apply(){ root.classList.toggle('zh-toc-open', open); root.classList.toggle('zh-toc-push', open && wide()); btn.textContent = open ? '‹' : '›'; }
    btn.onclick = function(){ setOpen(!open); };
    window.addEventListener('resize', apply); apply();
  }
  /* 够宽就把正文推开（照文稿对比）；太窄才盖在正文上，点了条目自动收起 */
  function wide(){ return window.innerWidth >= 760; }
  function barBottom(){ var b = document.querySelector('.bar'); return b ? Math.max(0, b.getBoundingClientRect().bottom) : 0; }
  function setCur(i){
    if (i === curIdx) return; curIdx = i;
    items.forEach(function(x, k){ x.classList.toggle('cur', k === i); });
    var a = items[i]; if (!a || !aside) return;
    var r = a.getBoundingClientRect(), b = aside.getBoundingClientRect();
    if (r.top < b.top + 36 || r.bottom > b.bottom - 36) aside.scrollTop += r.top - b.top - b.height / 2;
  }
  var lastSent = '';
  function focusNow(){
    var top = barBottom() + 8, idx = -1;
    for (var i = 0; i < sects.length; i++) { if (sects[i].getBoundingClientRect().top <= top + 28) idx = i; else break; }
    if (Date.now() > lock) setCur(idx);
    var rows = docs.querySelectorAll('.row'), first = null, ri = -1, off = 0;
    for (var k = 0; k < rows.length; k++) { var rr = rows[k].getBoundingClientRect(); if (rr.bottom > top + 12 && rr.top < window.innerHeight) { first = rows[k]; ri = k; if (rr.height > 0) off = Math.min(1, Math.max(0, (top - rr.top) / rr.height)); break; } }
    var p = first && (first.querySelector('.para') || first.querySelector('.text'));
    var payload = { section: idx >= 0 ? titleOf(sects[idx]) : null,
                    text: p ? (p.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120) : null,
                    no: first ? first.getAttribute('data-no') : null,
                    ri: ri, off: Math.round(off * 20) / 20 };   /* 这一段滑过了多少：访客按比例对齐 */
    var key = JSON.stringify(payload);
    if (!LIVE || key === lastSent) return; lastSent = key;
    try { fetch('/api/focus', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: key}).catch(function(){}); } catch (e) {}
  }
  var tk = 0;
  window.addEventListener('scroll', function(){ if (tk) return; tk = setTimeout(function(){ tk = 0; focusNow(); }, 80); }, {passive: true});
  /* 连续流：你滚动时每秒 60 次把"第几段、这一段滑过了多少"推给访客，访客那边逐帧跟着画 */
  var sq = 0, sLast = 0, sRaf = 0, sRows = null, sGuests = -1, sBusy = 0;
  function streamNow(){
    sRaf = 0; if (!LIVE) return;
    /* 有访客 60 次/秒（停在哪儿另有每 3 秒和停下时的 /api/focus 兜底）；后端说没访客就每秒最多 1 次（只为发现有人进来）。
       上一条还在路上就先不发（后端忙时不越堆越多），回来后发当时最新的位置；超过 1 秒没回算丢了 */
    var now = performance.now(), gap = sGuests === 0 ? 1000 : 16;
    if (now - sLast < gap || (sBusy && now - sBusy < 1000)) { sRaf = requestAnimationFrame(streamNow); return; }
    sLast = now; sBusy = now;
    if (!sRows || !sRows.length || !sRows[0].isConnected) sRows = docs.querySelectorAll('.row');
    var top = barBottom() + 8, lo = 0, hi = sRows.length - 1, ri = -1;
    if (window.scrollY > 2) {
      while (lo <= hi) { var mid = (lo + hi) >> 1; if (sRows[mid].getBoundingClientRect().bottom > top) { ri = mid; hi = mid - 1; } else lo = mid + 1; }
    }
    var off = 0; if (ri >= 0) { var rr = sRows[ri].getBoundingClientRect(); if (rr.height > 0) off = Math.min(1, Math.max(0, (top - rr.top) / rr.height)); }
    try { fetch('/api/scroll', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({s: ++sq, ri: ri, off: Math.round(off * 1000) / 1000}), keepalive: true})
            .then(function(r){ return r.json(); }).then(function(j){ if (j && typeof j.g === 'number') sGuests = j.g; })
            .catch(function(){}).then(function(){ sBusy = 0; }); } catch (e) { sBusy = 0; }
  }
  window.addEventListener('scroll', function(){ if (!sRaf) sRaf = requestAnimationFrame(streamNow); }, {passive: true});
  setTimeout(focusNow, 400); setInterval(focusNow, 3000);
  /* 先选中再说：在正文里选中一段，松开鼠标（抬起按键、手指）后在选区结尾旁边弹出「引用」，点了才把选中的字和它在
     哪一节报给外壳（2026-11-21：以前一选中就算引用，顺手勾一下也算）。外壳给主持人、共同主持人显示一个能清掉的小条，
     下一句对 AI 说的话以它为准（zuhui/inmeeting.py Anchor）。删掉的字、批注、按钮不算。
     能不能引用由外壳说（{zhQuote: {can}}：会上、能对 AI 说话的人）；点别处、按 Esc、选区没了就收起，浮着不占版面 */
  var QUOTE = false, qsel = null;
  var qbox = document.createElement('div'); qbox.id = 'zh-quote'; qbox.setAttribute('role', 'toolbar');
  qbox.innerHTML = '<button type="button" class="zh-quote-b" data-testid="paper-quote">{{T:quote}}</button>';
  document.body.appendChild(qbox);
  function selPick(){
    var sel = window.getSelection ? window.getSelection() : null;
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    var rg = sel.getRangeAt(0), anc = rg.commonAncestorContainer;
    if (!docs.contains(anc)) return null;
    if (anc.nodeType !== 1) anc = anc.parentNode;
    if (anc && anc.closest && anc.closest('.zh-editor, .zh-note, textarea, input')) return null;
    var box = document.createElement('div'); box.appendChild(rg.cloneContents());
    Array.prototype.forEach.call(box.querySelectorAll('del, .notes, button, .rowhead, .zh-pins, .zh-editor, .zh-thumb'), function(x){ x.parentNode.removeChild(x); });
    var text = (box.textContent || '').replace(/\s+/g, ' ').trim();
    if (text.length < 2 || text.length > 1000) return null;
    var idx = -1;
    for (var i = 0; i < sects.length; i++) { if (sects[i].compareDocumentPosition(rg.startContainer) & 4) idx = i; else break; }
    /* 选区结尾：往后拖的是最后一行的末尾，往前拖的是第一行的开头（松手的地方） */
    var back = false;
    try { back = sel.anchorNode === sel.focusNode ? sel.focusOffset < sel.anchorOffset : !!(sel.anchorNode.compareDocumentPosition(sel.focusNode) & 2); } catch (e) {}
    var rect = null;
    if (sel.focusNode && sel.focusNode.nodeType === 3) {
      try { var cr = document.createRange(); cr.setStart(sel.focusNode, sel.focusOffset); cr.collapse(true); var b = cr.getBoundingClientRect(); if (b.height > 0) rect = b; } catch (e) {}
    }
    if (!rect) {
      var rs = Array.prototype.filter.call(rg.getClientRects(), function(r){ return r.width > 1 && r.height > 0; });
      if (!rs.length) return null;
      var r0 = back ? rs[0] : rs[rs.length - 1];
      rect = {left: back ? r0.left : r0.right, right: back ? r0.left : r0.right, top: r0.top, bottom: r0.bottom};
    }
    return {text: text, section: idx >= 0 ? titleOf(sects[idx]) : null, rect: rect, back: back};
  }
  function qHide(){ qsel = null; qbox.classList.remove('show'); }
  function qShow(){
    var p = QUOTE && window.parent !== window ? selPick() : null;
    if (!p) return qHide();
    qsel = p; qbox.classList.add('show');
    var w = qbox.offsetWidth, h = qbox.offsetHeight, vw = document.documentElement.clientWidth, vh = window.innerHeight, top = barBottom() + 4;
    var x = Math.max(8, Math.min(p.rect.left - w / 2, vw - w - 14));
    var y = p.back ? p.rect.top - h - 8 : p.rect.bottom + 8;           /* 往后拖放在这一行下面，往前拖放在上面 */
    if (!p.back && y + h > vh - 8) y = p.rect.top - h - 8;              /* 放不下就换到另一边 */
    if (p.back && y < top) y = p.rect.bottom + 8;
    qbox.style.left = Math.round(x + window.scrollX) + 'px'; qbox.style.top = Math.round(y + window.scrollY) + 'px';
  }
  /* 点它不丢选区 */
  qbox.addEventListener('mousedown', function(e){ e.preventDefault(); e.stopPropagation(); });
  qbox.addEventListener('mouseup', function(e){ e.stopPropagation(); });
  qbox.addEventListener('click', function(e){ e.stopPropagation(); });
  qbox.querySelector('button').onclick = function(){
    var p = qsel; qHide(); if (!p) return;
    try { window.parent.postMessage({zhSel: {text: p.text, section: p.section}}, '*'); } catch (e) {}
    try { window.getSelection().removeAllRanges(); } catch (e) {}
  };
  document.addEventListener('mouseup', function(){ setTimeout(qShow, 0); });
  document.addEventListener('keyup', function(e){ if (e.shiftKey || e.key === 'Shift') setTimeout(qShow, 0); });
  document.addEventListener('touchend', function(){ setTimeout(qShow, 300); });
  document.addEventListener('mousedown', function(e){ if (qsel && !qbox.contains(e.target)) qHide(); }, true);
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && qsel) qHide(); }, true);
  document.addEventListener('selectionchange', function(){ if (!qsel) return; var s = window.getSelection(); if (!s || s.isCollapsed) qHide(); });
  window.addEventListener('resize', function(){ if (qsel) qHide(); });
  window.addEventListener('message', function(e){
    var d = e.data || {}; if (d.zhQuote === undefined) return;
    QUOTE = !!(d.zhQuote && d.zhQuote.can); if (!QUOTE) qHide();
  });
  try { if (window.parent !== window) window.parent.postMessage({zhQuoteAsk: 1}, '*'); } catch (e) {}
  /* 批注：一条任务一个小圆标，贴在它改的那一段右边（还没改完的贴在你说话时看着的那一段）；点开才看内容；
     打勾 = 看过了，这条连同它的修订标记一起消掉 */
  var TASKS = __TASKS__, ME = __ME__, AI = __AI__;      /* AI：{name, color}，卡片上"结果"前面署它的名字（_ai_tag） */
  var STATUS = {queued: '{{T:s_queued}}', working: '{{T:s_working}}', done: '{{T:s_done}}', failed: '{{T:s_failed}}'};
  /* 标注（会上改不了、钉在这里的，2026-11-14）：图钉不写编号，改过的是勾，标注是空心小旗。
     标注的状态（会后改 / 已处理）和标签（要重新跑实验……）画成卡片底下一排胶囊，头上不再写状态（2026-11-19） */
  function stText(t){ return t.note ? (t.resolved ? '{{T:s_note_ok}}' : '{{T:s_note}}') : (STATUS[t.status] || ''); }
  var TAGS = {rerun: '{{T:tag_rerun}}', data: '{{T:tag_data}}', figure: '{{T:tag_figure}}', lit: '{{T:tag_lit}}', discuss: '{{T:tag_discuss}}'};
  function tagRow(t){
    if (!t.note) return '';
    var s = '<span class="zh-tag ' + (t.resolved ? 'is-ok' : 'is-st') + '">' + stText(t) + '</span>';
    (t.tags || []).forEach(function(k){ if (TAGS[k]) s += '<span class="zh-tag">' + TAGS[k] + '</span>'; });
    return '<div class="zh-tags">' + s + '</div>';
  }
  var ICON = {
    done: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3.2 3L13 4.5"/></svg>',
    note: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14V2.5M4 3h7.5l-1.8 2.7L11.5 8.5H4"/></svg>',
    failed: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M8 3.5v6M8 12.5v.1"/></svg>',
    working: '<svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><circle cx="3.5" cy="8" r="1.4"/><circle cx="8" cy="8" r="1.4"/><circle cx="12.5" cy="8" r="1.4"/></svg>'
  };
  ICON.queued = ICON.working;
  function pinIcon(st){ return ICON[st] || ICON.done; }
  function norm(s){ return (s || '').replace(/\s+/g, ' ').trim(); }
  /* 引号也要转：拼进 title="…"、style="…" 里的是访客能写的字（批注、名字），只转尖括号会被 " 断开属性塞进 onmouseover */
  function esc(s){ return (s == null ? '' : String(s)).replace(/[&<>"']/g, function(c){ return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  /* 比对用的"字"：只留字母、数字、汉字（Markdown 记号、标点、空白都不算），和后端 Viewer._key 一样 */
  function kOf(s){ return (s || '').replace(/[^0-9A-Za-zÀ-ɏͰ-Ͽ一-鿿]+/g, ''); }
  var ROWS = Array.prototype.slice.call(docs.querySelectorAll('.row, .sect'));
  function rowKeys(el){                                  /* 一段改后的字（去掉删掉的、批注、图钉）和整段的字，各算一次 */
    if (el.__zhK) return el.__zhK;
    var c = el.cloneNode(true);
    Array.prototype.forEach.call(c.querySelectorAll('del, .notes, .zh-pins, .zh-note, .rowhead'), function(x){ x.parentNode.removeChild(x); });
    return (el.__zhK = {after: kOf(c.textContent), all: kOf(el.textContent)});
  }
  function findByHint(h, chgOnly){                       /* 这条改出来的字、改后那几段的开头，在哪一段里 */
    var ins = (h.ins || []).filter(function(s){ return s.length >= 4; }), heads = (h.heads || []).filter(function(s){ return s.length >= 4; });
    if (!ins.length && !heads.length) return null;
    for (var i = 0; i < ROWS.length; i++) {
      var el = ROWS[i]; if (chgOnly && !el.classList.contains('chg')) continue;
      var k = rowKeys(el).after; if (!k) continue;
      if (heads.some(function(s){ return k.indexOf(s) === 0; }) || ins.some(function(s){ return k.indexOf(s) >= 0; })) return el;
    }
    return null;
  }
  function findByText(text){                             /* 说话时屏幕上那一段 / 工人写的"钉在哪一段"的开头 */
    var key = kOf(text).slice(0, 18); if (key.length < 6) return null;
    for (var i = 0; i < ROWS.length; i++) { var rk = rowKeys(ROWS[i]); if (rk.all.indexOf(key) === 0 || rk.after.indexOf(key) === 0) return ROWS[i]; }
    return null;
  }
  /* 一条任务的图钉贴在哪一段：改完的先认它的修订标记（生成器挂上的批注号），再认它改出来的字在哪一段（同一段几条改动
     挤在一起时字会被别条拆开，批注号挂不上）；都没有就退回说话时屏幕上那一段、那一节。还找不到返回 null：钉在文档开头 */
  function anchorOf(t){
    var el = null, f = t.focus || {}, h = t.hint || {};
    if (t.status === 'done' && !t.note) {
      el = docs.querySelector('[data-notes~="' + t.idx + '"]') || findByHint(h, true) || findByHint(h, false);
    }
    if (!el && f.text) el = findByText(f.text);
    if (!el) {
      var want = norm(t.section || f.section);
      if (want) for (var k = 0; k < sects.length; k++) { if (titleOf(sects[k]) === want) { el = sects[k]; break; } }
    }
    return el;
  }
  function docStart(){                                   /* 文档开头那一段（正文里，跟着正文滚，不是浮在边上的框）；不挑折起来的没改动段落 */
    for (var i = 0; i < ROWS.length; i++) { var d = ROWS[i].closest('details.fold'); if (!d || d.open) return ROWS[i]; }
    return ROWS[0] || null;
  }
  var floatBox = null, openCard = null;
  function closeCard(){
    if (openCard && openCard.parentNode) openCard.parentNode.removeChild(openCard);
    openCard = null;
    Array.prototype.forEach.call(document.querySelectorAll('.zh-pin.on'), function(x){ x.classList.remove('on'); });
  }
  function postId(url, id){
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: id})}).then(function(r){ return r.json(); });
  }
  function showCard(g, pin, host){
    var same = openCard && openCard.zhPin === pin;
    closeCard(); if (same) return;
    var c = document.createElement('div'); c.className = 'zh-note'; c.zhPin = pin;
    var first = g.tasks[0], many = g.tasks.length > 1;
    var canOk = LIVE && g.tasks.some(function(t){ return t.status === 'done' || t.status === 'failed'; });
    var canX = function(t){ return LIVE && t.status === 'done' && !t.note; };
    /* 每一行同一个排法：标题 · 状态 · 撤回（×）· 确认（✓）。× 在每一条任务自己那一行，✓ 只在卡片头上（确认这一处的全部）；
       这张卡用得到几列按钮就留几列，没有按钮的行留着空位：状态字、按钮上下对齐。状态只写字，不再画勾
       （2026-11-21：头上左边一个状态勾、右边一个确认勾，下面每条左边又一个勾，看着乱） */
    var cols = (g.tasks.some(canX) ? 1 : 0) + (canOk ? 1 : 0);
    c.style.setProperty('--zh-cols', String(cols));
    var XSVG = '<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
    function acts(x, ok){
      return cols ? '<span class="zh-acts">'
        + (x ? '<button type="button" class="zh-note-x" aria-label="{{T:reject_aria}}" title="{{T:reject_aria}}">' + XSVG + '</button>' : '')
        + (ok ? '<button type="button" class="zh-note-ok" aria-label="{{T:ok_aria}}" title="{{T:ok_aria}}">' + ICON.done + '</button>' : '') + '</span>' : '';
    }
    function st(t){ return t.note ? '' : '<span class="zh-note-st is-' + esc(t.status) + '">' + stText(t) + '</span>'; }
    /* 一个人的名字：他的颜色（浅色主题下和黑色对半混，.zh-who），太长省略号，悬停看全名 */
    function whoTag(name, color){ return '<b class="zh-who"' + (color ? ' style="--zh-who:' + esc(color) + '"' : '') + ' title="' + esc(name) + '">' + esc(name) + '</b>'; }
    var headTitle = many ? (first.section || (first.focus || {}).section || first.title) : first.title;
    var html = '<div class="zh-note-head"><span class="zh-note-title" title="' + esc(headTitle) + '">' + esc(headTitle) + '</span>'
      + (many ? '' : st(first)) + acts(!many && canX(first), canOk) + '</div><div class="zh-note-body">';
    g.tasks.forEach(function(t){
      var who = t.who || ME, said = t.said != null ? t.said : t.remark;
      html += '<div class="zh-item is-' + t.status + '" data-id="' + esc(t.id) + '">'
        + (many ? '<div class="zh-item-head"><span class="zh-item-title" title="' + esc(t.title) + '">' + esc(t.title) + '</span>' + st(t) + acts(canX(t), false) + '</div>' : '')
        + (t.thread ? (function(th){
              var line = function(x){ return x.who ? '<p class="zh-note-said">' + whoTag(x.who, x.color) + esc(x.text) + '</p>'
                                                   : '<p class="zh-note-said' + (x.sys ? ' zh-note-sys' : '') + '">' + esc(x.text) + '</p>'; };
              var pick = t.discussion_rows;      /* 后台按语境挑出的相关几句：只摆这几句，别的折起来 */
              if (pick && pick.length && pick.length < th.length) {
                var rest = th.filter(function(x, i){ return pick.indexOf(i) < 0; });
                return '<details class="zh-more"><summary>{{T:other_a}}' + rest.length + '{{T:other_b}}</summary>' + rest.map(line).join('') + '</details>'
                  + th.filter(function(x, i){ return pick.indexOf(i) >= 0; }).map(line).join('');
              }
              if (th.length <= 10) return th.map(line).join('');
              return '<details class="zh-more"><summary>{{T:earlier_a}}' + (th.length - 10) + '{{T:earlier_b}}</summary>' + th.slice(0, -10).map(line).join('') + '</details>'
                + th.slice(-10).map(line).join('');
            })(t.thread)
                    : (said ? '<p class="zh-note-said">' + whoTag(who, t.who_color) + esc(said) + '</p>' : ''))
        + (t.note
            ? (t.plan ? '<p class="zh-note-did"><b>{{T:plan}}</b>' + esc(t.plan) + '</p>' : '')
              + (t.need ? '<p class="zh-note-did"><b>{{T:need}}</b>' + esc(t.need) + '</p>' : '')
              + (t.plan ? '' : '<p class="zh-note-did">' + whoTag(AI.name, AI.color) + esc(t.status !== 'done' ? '{{T:plan_wait}}' : (t.result || '')) + '</p>')
              + tagRow(t)
            : (t.result ? '<p class="zh-note-did">' + whoTag(AI.name, AI.color) + esc(t.result) + '</p>' : ''))
        + (t.error ? '<p class="zh-note-bad">' + esc(t.error) + '</p>' : '')
        + '</div>';
    });
    c.innerHTML = html + '</div>';
    c.addEventListener('click', function(e){ e.stopPropagation(); });
    function gone(ids){                                  /* 这几条收起了：卡片里划掉，整处都收起了就连圆标一起去掉 */
      g.tasks = g.tasks.filter(function(t){ return ids.indexOf(t.id) < 0; });
      (TASKS || []).forEach(function(t){ if (ids.indexOf(t.id) >= 0) t.resolved = true; });
      if (!g.tasks.length) { closeCard(); if (pin.parentNode) pin.parentNode.removeChild(pin); paintBars(); return; }
      pin.innerHTML = pinIcon(statusOf(g.tasks)); pin.className = 'zh-pin is-' + statusOf(g.tasks) + ' on';
      closeCard(); paintBars();
    }
    Array.prototype.forEach.call(c.querySelectorAll('.zh-note-x'), function(x){
      var item = x.closest('.zh-item') || c.querySelector('.zh-item');
      var id = item.getAttribute('data-id');
      x.onclick = function(){
        x.disabled = true; x.classList.add('is-busy');
        postId('/api/board/reject', id).then(function(d){
          if (d && d.ok) { var ids = d.ids || [id];
            ids.forEach(function(x){ var it = c.querySelector('.zh-item[data-id="' + x + '"]'); if (it) it.classList.add('is-gone'); });
            setTimeout(function(){ gone(ids); }, 160); return; }
          x.disabled = false; x.classList.remove('is-busy');
          var bad = item.querySelector('.zh-note-bad') || item.appendChild(document.createElement('p'));
          bad.className = 'zh-note-bad'; bad.textContent = (d && d.error) || '{{T:undo_fail}}';
        }).catch(function(){ x.disabled = false; x.classList.remove('is-busy'); });
      };
    });
    var ok = c.querySelector('.zh-note-ok');
    if (ok) ok.onclick = function(){
      ok.disabled = true;
      var ids = g.tasks.filter(function(t){ return t.status === 'done' || t.status === 'failed'; }).map(function(t){ return t.id; });
      /* 一次发过去，后端按改稿先后一条条并进原文（以前每条一个请求同时发，三个线程同时改原文快照，把它写坏了） */
      fetch('/api/board/resolve', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: ids})})
        .then(function(r){ return r.json(); })
        .then(function(d){ if (d && d.ok) gone(ids); else ok.disabled = false; })
        .catch(function(){ ok.disabled = false; });
    };
    host.appendChild(c); pin.classList.add('on'); openCard = c;
    var r = c.getBoundingClientRect();
    if (r.bottom > window.innerHeight - 8) window.scrollBy({top: r.bottom - window.innerHeight + 16, behavior: 'smooth'});
  }
  function statusOf(ts){
    var ed = ts.filter(function(t){ return !t.note; });
    if (!ed.length) return 'note';
    var has = function(s){ return ed.some(function(t){ return t.status === s; }); };
    return has('working') ? 'working' : has('queued') ? 'queued' : has('failed') ? 'failed' : 'done';
  }
  function label(ts){ var n = ts.map(function(t){ return t.idx; }); return n.length > 3 ? n.slice(0, 2).join('·') + '…' : n.join('·'); }
  document.addEventListener('click', closeCard);
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') closeCard(); });
  /* 一处一个圆标：同一个地方（spot 一样，或者落在同一段上）的几条合成一组，圆标贴在最近改的那一条所在的段落 */
  var groups = [];
  (TASKS || []).forEach(function(t){
    if (t.resolved || t.app) return;                     /* 说的是软件本身的（界面、声音、功能）：不进论文页 */
    var g = null, key = t.spot || t.id;
    for (var i = 0; i < groups.length; i++) { if (groups[i].key === key) { g = groups[i]; break; } }
    if (!g) { g = {key: key, tasks: []}; groups.push(g); }
    g.tasks.push(t);
  });
  groups.forEach(function(g){
    var at = null;
    for (var k = g.tasks.length - 1; k >= 0 && !at; k--) at = anchorOf(g.tasks[k]);
    /* 正文里哪儿都对不上（没改正文、读不到稿子、说的地方找不到）：钉在文档开头那一段上，照样能点开、打勾（2026-11-15） */
    g.host = at || docStart();
  });
  /* 标注和改过的分开成组：同一段上可以既有一个勾、又有一面小旗（并成一组的话小旗就被勾盖住了） */
  var allNote = function(g){ return g.tasks.every(function(t){ return t.note; }); };
  for (var gi = 0; gi < groups.length; gi++) {         /* spot 不同但落在同一段上的，也并成一组 */
    for (var gj = groups.length - 1; gj > gi; gj--) {
      if (groups[gi].host && groups[gj].host === groups[gi].host && allNote(groups[gi]) === allNote(groups[gj])) { groups[gi].tasks = groups[gi].tasks.concat(groups[gj].tasks); groups.splice(gj, 1); }
    }
  }
  groups.forEach(function(g){
    g.tasks.sort(function(a, b){ return a.idx - b.idx; });
    var host = g.host;
    if (!host) return;                                   /* 页面上一段都没有（空稿子） */
    var stack = null;
    for (var i = 0; i < host.children.length; i++) { if (host.children[i].classList.contains('zh-pins')) { stack = host.children[i]; break; } }
    if (!stack) { stack = document.createElement('div'); stack.className = 'zh-pins'; host.appendChild(stack); }
    var pin = document.createElement('button'); pin.type = 'button'; pin.className = 'zh-pin is-' + statusOf(g.tasks); pin.innerHTML = pinIcon(statusOf(g.tasks));
    pin.setAttribute('aria-label', g.tasks.map(function(t){ return stText(t) + ' ' + (t.title || ''); }).join('，'));
    pin.zhGroup = g;
    /* 同一段上既有勾又有小旗：勾在上、旗在下，每次都是这个顺序 */
    if (allNote(g)) stack.appendChild(pin); else stack.insertBefore(pin, stack.querySelector('.zh-pin.is-note'));
    pin.onclick = function(e){ e.stopPropagation(); showCard(g, pin, host); };
  });
  /* 左边那道线：一段一种颜色，按固定的先后定，重算、滚动、点开卡片都不变（2026-11-15：同一段既有改动又有标注时，
     以前黄、蓝、灰来回跳：标注的黄线、生成器给"当前这一处"的蓝线、改过的灰线谁写在后面谁赢）。
     还没处理的标注（黄）> 还没打勾的改动（强调色）> 其余改过的（灰） */
  function paintBars(){
    var open = {};
    (TASKS || []).forEach(function(t){ if (!t.resolved && !t.app) open[String(t.idx)] = t; });
    ROWS.forEach(function(el){
      var note = false, edit = false;
      Array.prototype.forEach.call(el.querySelectorAll(':scope > .zh-pins > .zh-pin'), function(p){
        (p.zhGroup ? p.zhGroup.tasks : []).forEach(function(t){ if (t.resolved) return; if (t.note) note = true; else edit = true; });
      });
      if (!edit && el.classList.contains('chg'))
        edit = (el.getAttribute('data-notes') || '').split(/\s+/).some(function(n){ var t = open[n]; return t && !t.note; });
      var v = note ? 'note' : edit ? 'edit' : el.classList.contains('chg') ? 'done' : '';
      if (v) el.setAttribute('data-zh-bar', v); else el.removeAttribute('data-zh-bar');
    });
  }
  paintBars();
  /* 图：导出的 Markdown 里图只剩占位符，这里把论文文件夹 figures/ 下的图放回原位 */
  var FIGS = __FIGS__ || {};
  function putFig(row, no){
    var f = FIGS[no], p = row && row.querySelector('.para'); if (!f || !p || row.classList.contains('zh-figrow')) return;
    row.classList.add('zh-figrow');
    p.innerHTML = f.kind === 'html' ? '<iframe class="zh-fig" loading="lazy"></iframe>' : '<img class="zh-fig" alt="">';
    p.firstChild.src = f.src;
  }
  Array.prototype.forEach.call(docs.querySelectorAll('.row'), function(row){
    var lab = row.querySelector('.rowhead .label'); if (!lab) return;
    var name = (lab.textContent || '').replace(/\s+/g, ' ').trim(), m = /^图\s*(\d+)$/.exec(name);
    if (m) { putFig(row, m[1]); return; }
    m = /^图\s*(\d+)\s*注$/.exec(name);      /* 占位行没标成"图 N"的：图注上面那一行就是图 */
    if (m) { var prev = row.previousElementSibling; if (prev && prev.classList.contains('row') && prev.querySelector('.para.fixed')) putFig(prev, m[1]); }
  });
  /* 公式：稿子里是 LaTeX 源码，这里排成公式（MathJax，本机文件，不联网）。带修订标记的公式保持源码，好看清改了哪里 */
  var maths = [], inl = [];
  Array.prototype.forEach.call(docs.querySelectorAll('.para.mono'), function(p){
    if (p.querySelector('ins, del')) return;
    var lab = p.closest('.row') && p.closest('.row').querySelector('.rowhead .label');
    if (lab && /^(代码|Code)$/.test((lab.textContent || '').trim())) return;   /* ```text 代码块不是公式 */
    var tex = (p.textContent || '').trim();
    if (!/\\[a-zA-Z]+|[_^]/.test(tex)) return;
    p.setAttribute('data-tex', tex); p.classList.add('zh-math'); p.textContent = '\\[' + tex + '\\]'; maths.push(p);
  });
  /* 行内公式 $...$（PDF / Word 转来的稿子里复杂的行内公式保留 LaTeX）；带修订标记的段落保持源码 */
  Array.prototype.forEach.call(docs.querySelectorAll('.para:not(.mono), .sect, table.dtable td, table.dtable th'), function(p){
    if (p.querySelector('ins, del') || !/(^|[^\\$])\$[^$\n]+\$/.test(p.textContent || '')) return;
    inl.push(p);
  });
  if (maths.length || inl.length) {
    /* 公式是稿子里的字（访客传的论文、批注、共同主持人改的段都可能进来）：\href{javascript:…} 会变成整块能点的链接、
       \style \class 能改页面样式。这几个只留里面的内容，html、require 扩展不加载（\require{html} 也拉不回来） */
    window.MathJax = { tex: { tags: 'ams', displayMath: [['\\[', '\\]']], inlineMath: [['\\(', '\\)'], ['$', '$']], processEscapes: true,
        packages: {'[-]': ['html', 'require']},
        macros: { href: ['{#2}', 2], class: ['{#2}', 2], style: ['{#2}', 2], cssId: ['{#2}', 2], data: ['{#2}', 2] } }, svg: { fontCache: 'global' },
      startup: { typeset: false, ready: function(){ MathJax.startup.defaultReady();
        var all = maths.concat(inl);   /* 先一次排完（快）；有一个出错再退回一块一块排，免得连累整页 */
        MathJax.typesetPromise(all).catch(function(){
          var q = Promise.resolve();
          all.forEach(function(el){ q = q.then(function(){ return MathJax.typesetPromise([el]); }).catch(function(){}); });
        }); } } };
    var mj = document.createElement('script'); mj.src = '/assets/mathjax.js?v=__MJV__'; mj.async = true; document.head.appendChild(mj);
  }
  /* 行内的 p_{t,1}、B_t、m_{s,k}^{*}（导出时丢了公式标记，只剩这种写法）排成上下标 */
  var SCRIPT = /(?<![A-Za-z0-9_\\])([A-Za-z\u00C0-\u024F\u0370-\u03FF]|\uD835[\uDC00-\uDFFF])((?:[_^](?:\{[^{}]{1,16}\}|[A-Za-z0-9*](?![A-Za-z0-9])))+)/g;
  function scriptify(node){
    var t = node.nodeValue; if (!t || (t.indexOf('_') < 0 && t.indexOf('^') < 0)) return;
    if (t.indexOf('$') >= 0) {                       /* $...$ 里的留给 MathJax，只排外面的 */
      var bits = t.split(/(\$[^$\n]+\$)/);
      if (bits.length > 1) {
        var fr = document.createDocumentFragment(), outs = [];
        bits.forEach(function(s, i){ var tn = document.createTextNode(s); fr.appendChild(tn); if (i % 2 === 0) outs.push(tn); });
        node.parentNode.replaceChild(fr, node); outs.forEach(scriptify); return;
      }
    }
    SCRIPT.lastIndex = 0; if (!SCRIPT.test(t)) return; SCRIPT.lastIndex = 0;
    var frag = document.createDocumentFragment(), last = 0, m;
    while ((m = SCRIPT.exec(t))) {
      frag.appendChild(document.createTextNode(t.slice(last, m.index)));
      var v = document.createElement('span'); v.className = 'zh-var'; v.textContent = m[1]; frag.appendChild(v);
      (m[2].match(/[_^](?:\{[^{}]{1,16}\}|[A-Za-z0-9*])/g) || []).forEach(function(p){
        var e = document.createElement(p.charAt(0) === '_' ? 'sub' : 'sup'), body = p.slice(1);
        if (body.charAt(0) === '{') body = body.slice(1, -1);
        e.textContent = body; frag.appendChild(e);
      });
      last = m.index + m[0].length;
    }
    frag.appendChild(document.createTextNode(t.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }
  (function(){
    var w = document.createTreeWalker(docs, NodeFilter.SHOW_TEXT, null), list = [], n;
    while ((n = w.nextNode())) { var pe = n.parentNode; if (pe && pe.closest && pe.closest('.para, .sect') && !pe.closest('.mono')) list.push(n); }
    list.forEach(scriptify);
  })();
  /* 编辑：点工具栏的「编辑」，再点任意一段（标题、公式、表格也行），就地改这一段的原文；Ctrl+Enter 保存，Esc 取消 */
  if (LIVE) {
    var bar = document.querySelector('.bar'), views = bar && bar.querySelector('.views');
    var eb = document.createElement('button'); eb.type = 'button'; eb.id = 'zh-editbtn'; eb.textContent = '{{T:edit}}'; eb.setAttribute('aria-pressed', 'false');
    if (bar) bar.insertBefore(eb, views || null);
    var editing = false, ed = null;
    /* 正在改的这段存一份草稿（sessionStorage，外壳前后两个槽的页面共用）：外壳正好在这时换上新版本，
       换上来的页面接着打开这段，打了一半的字不丢。保存、取消、关掉编辑就删 */
    var DRAFT = 'zh-draft';
    var blocks = function(){
      return Array.prototype.filter.call(docs.querySelectorAll('table, .para, .sect'), function(el){
        return !el.closest('.zh-figrow') && (el.tagName === 'TABLE' || !el.parentNode.closest('table'));
      });
    };
    var keepDraft = function(){
      if (!ed) return;
      try { sessionStorage.setItem(DRAFT, JSON.stringify({p: __PAPER__, at: blocks().indexOf(ed.block), key: ed.key, old: ed.old,
                                                           index: ed.index, text: ed.ta.value, t: Date.now()})); } catch (e) {}
    };
    var dropDraft = function(){ try { sessionStorage.removeItem(DRAFT); } catch (e) {} };
    var closeEditor = function(){
      if (!ed) return;
      if (ed.holder.parentNode) ed.holder.parentNode.removeChild(ed.holder);
      ed.block.style.display = ''; ed = null; window.__zhHold = false;
      dropDraft();
    };
    /* 存好了：编辑框马上收起，这一段先按纯文字显示改后的样子，新版本（带格式、公式）一两秒后换上来。
       以前存好后编辑框一直开着、写"已保存"、保存按钮点不动，要等新版本换上来才收起：看着像没存上 */
    var showSaved = function(block, text){
      if (!text.trim()) { block.style.display = 'none'; return; }
      if (block.tagName === 'TABLE' || block.getAttribute('data-tex') || block.querySelector('img')) return;
      block.textContent = text.replace(/^\s*#{1,6}\s+/, '').replace(/\*\*|__|`/g, '').replace(/\s*\n\s*/g, ' ').trim();
    };
    var setEditing = function(v){
      editing = v; root.classList.toggle('zh-editing', v);
      eb.setAttribute('aria-pressed', v ? 'true' : 'false'); eb.textContent = v ? '{{T:edit_done}}' : '{{T:edit}}';
      try { sessionStorage.setItem('zh-edit', v ? '1' : '0'); } catch (e) {}
      if (!v) closeEditor();
    };
    var flash = function(block, msg){
      var t = document.createElement('div'); t.className = 'zh-editmsg'; t.textContent = msg;
      block.parentNode.insertBefore(t, block.nextSibling);
      setTimeout(function(){ if (t.parentNode) t.parentNode.removeChild(t); }, 2600);
    };
    var afterText = function(block){
      if (block.getAttribute('data-tex')) return block.getAttribute('data-tex');
      var c = block.cloneNode(true);
      Array.prototype.forEach.call(c.querySelectorAll('del, .notes, .zh-pins, .zh-note, .rowhead'), function(x){ x.parentNode.removeChild(x); });
      return (c.textContent || '').trim();
    };
    var post = function(url, body){
      return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(function(r){ return r.json(); });
    };
    /* d = {source, index}（/api/para/source 给的）；draft：草稿里接着打的字（换上来的页面接着开，见 DRAFT） */
    var mount = function(block, d, draft){
      closeEditor();
      var holder = document.createElement('div'); holder.className = 'zh-editor';
      holder.innerHTML = '<textarea class="zh-edit" spellcheck="false"></textarea><div class="zh-editbar"><span class="zh-edithint">{{T:edit_hint}}</span>'
        + '<button type="button" class="zh-cancel">{{T:cancel}}</button><button type="button" class="zh-save">{{T:save}}</button></div>';
      var ta = holder.querySelector('textarea'), hint = holder.querySelector('.zh-edithint'), btn = holder.querySelector('.zh-save');
      ta.value = draft != null ? draft : d.source;
      block.parentNode.insertBefore(holder, block.nextSibling); block.style.display = 'none';
      var mine = ed = {block: block, holder: holder, ta: ta, old: d.source, index: d.index, key: afterText(block).slice(0, 200),
                       restored: draft != null, touched: false};
      window.__zhHold = true;
      var fit = function(){ ta.style.height = 'auto'; ta.style.height = Math.min(Math.max(ta.scrollHeight + 4, 60), Math.round(window.innerHeight * 0.7)) + 'px'; };
      mine.fit = fit;
      var save = function(){
        if (ed !== mine || btn.disabled) return;
        btn.disabled = true;
        var text = ta.value;
        dropDraft();                                  /* 发出去了：存没存上都以回来的结果为准，换页时别再拿草稿重开 */
        post('/api/para/save', {index: mine.index, old: mine.old, text: text}).then(function(x){
          if (!x.ok) { btn.disabled = false; hint.textContent = x.error || '{{T:not_saved}}'; keepDraft(); return; }
          if (ed === mine) closeEditor();
          if (x.changed) showSaved(block, text);
        }).catch(function(){ btn.disabled = false; hint.textContent = '{{T:no_backend}}'; keepDraft(); });
      };
      ta.addEventListener('input', function(){ mine.touched = true; fit(); keepDraft(); });
      fit(); keepDraft();
      if (draft == null || window.parent === window) ta.focus();
      holder.addEventListener('click', function(e){ e.stopPropagation(); });
      btn.onclick = save; holder.querySelector('.zh-cancel').onclick = closeEditor;
      ta.addEventListener('keydown', function(e){
        if (e.key === 'Escape') { e.preventDefault(); closeEditor(); }
        else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
        e.stopPropagation();
      });
    };
    var openEditor = function(block){
      closeEditor();
      post('/api/para/source', {text: afterText(block)}).then(function(d){
        if (!d.ok) { flash(block, d.error || '{{T:not_found}}'); return; }
        mount(block, d, null);
      }).catch(function(){ flash(block, '{{T:no_backend}}'); });
    };
    /* 换上来的页面：上一份页面里正改着一段（草稿还在）就接着打开它。在后面那个槽里先开好：外壳换上来以后（zhHello）再给焦点；
       换上来之前前一份页面里又打的字，跟着草稿（storage 事件）补上 */
    var restoreDraft = function(){
      var dr = null;
      try { dr = JSON.parse(sessionStorage.getItem(DRAFT) || 'null'); } catch (e) {}
      if (!dr || dr.p !== __PAPER__ || typeof dr.text !== 'string' || Date.now() - (dr.t || 0) > 30 * 60 * 1000) return;
      var all = blocks(), block = null;
      for (var i = 0; i < all.length && !block; i++) if (afterText(all[i]).slice(0, 200) === dr.key) block = all[i];
      if (!block && dr.at >= 0 && dr.at < all.length) block = all[dr.at];
      if (block) mount(block, {source: dr.old, index: dr.index}, dr.text);
    };
    window.addEventListener('storage', function(e){
      if (e.key !== DRAFT || !ed || !ed.restored || ed.touched) return;
      if (!e.newValue) { closeEditor(); return; }   /* 前一份页面里存了、取消了：这边接着开的也收起 */
      try { var dr = JSON.parse(e.newValue); if (dr.old === ed.old && typeof dr.text === 'string') { ed.ta.value = dr.text; ed.fit(); } } catch (x) {}
    });
    window.addEventListener('message', function(e){
      if (e.data && e.data.zhHello && ed && ed.restored && !ed.touched) { ed.fit(); ed.ta.focus(); }
    });
    eb.onclick = function(){ setEditing(!editing); };
    docs.addEventListener('click', function(e){
      if (!editing || !e.target.closest) return;
      if (e.target.closest('.zh-editor, .zh-pins, .zh-note, a, button, summary')) return;
      var block = e.target.closest('table') || e.target.closest('.para, .sect');
      if (!block || !docs.contains(block) || block.closest('.zh-figrow')) return;
      e.preventDefault(); e.stopPropagation(); openEditor(block);
    }, true);
    try { if (sessionStorage.getItem('zh-edit') === '1') { setEditing(true); restoreDraft(); } } catch (e) {}
  }
  /* Ctrl+F：在正文里查找。输入即高亮，回车下一个，Shift+回车上一个，Esc 关 */
  (function(){
    if (!(window.CSS && CSS.highlights && window.Highlight)) return;
    var box = null, input = null, count = null, ranges = [], cur = -1, timer = 0;
    function search(q){
      ranges = []; cur = -1;
      CSS.highlights.delete('zh-find'); CSS.highlights.delete('zh-find-cur');
      q = (q || '').toLowerCase();
      if (!q) { count.textContent = ''; return; }
      Array.prototype.forEach.call(docs.querySelectorAll('.para, .sect, .dtable td, .dtable th'), function(b){
        if (b.offsetParent === null || (b.parentNode.closest && b.parentNode.closest('.para, .sect'))) return;
        var w = document.createTreeWalker(b, NodeFilter.SHOW_TEXT, null), nodes = [], starts = [], text = '', n;
        while ((n = w.nextNode())) {
          if (n.parentNode.closest('.zh-pins, .zh-note, .rowhead, .notes, mjx-container, .zh-editor')) continue;
          nodes.push(n); starts.push(text.length); text += n.nodeValue;
        }
        function locate(off){
          for (var k = nodes.length - 1; k >= 0; k--) { if (starts[k] <= off) return [nodes[k], Math.min(off - starts[k], nodes[k].nodeValue.length)]; }
          return null;
        }
        var low = text.toLowerCase(), at = 0, i;
        while ((i = low.indexOf(q, at)) >= 0) {
          var a = locate(i), z = locate(i + q.length);
          if (a && z) { var r = document.createRange(); r.setStart(a[0], a[1]); r.setEnd(z[0], z[1]); ranges.push(r); }
          at = i + q.length;
        }
      });
      if (!ranges.length) { count.textContent = '0'; return; }
      var h = new Highlight(); ranges.forEach(function(r){ h.add(r); }); CSS.highlights.set('zh-find', h);
      var top = barBottom() + 4, first = 0;
      for (var k = 0; k < ranges.length; k++) { if (ranges[k].getBoundingClientRect().bottom >= top) { first = k; break; } }
      go(first);
    }
    function go(i){
      if (!ranges.length) return;
      cur = (i + ranges.length) % ranges.length;
      CSS.highlights.set('zh-find-cur', new Highlight(ranges[cur]));
      count.textContent = (cur + 1) + ' / ' + ranges.length;
      var r = ranges[cur].getBoundingClientRect();
      if (r.top < barBottom() + 50 || r.bottom > window.innerHeight - 40) window.scrollTo({top: r.top + window.scrollY - window.innerHeight * 0.35, behavior: 'smooth'});
    }
    function shut(){
      if (!box) return;
      box.classList.remove('open'); CSS.highlights.delete('zh-find'); CSS.highlights.delete('zh-find-cur'); ranges = []; cur = -1;
    }
    function open(){
      if (!box) {
        box = document.createElement('div'); box.id = 'zh-find';
        box.innerHTML = '<input type="text" placeholder="{{T:find}}" spellcheck="false"><span class="zh-find-n"></span>'
          + '<button type="button" data-a="prev" aria-label="{{T:prev}}">‹</button><button type="button" data-a="next" aria-label="{{T:next}}">›</button>'
          + '<button type="button" data-a="close" aria-label="{{T:close}}">×</button>';
        document.body.appendChild(box);
        input = box.querySelector('input'); count = box.querySelector('.zh-find-n');
        input.addEventListener('input', function(){ clearTimeout(timer); timer = setTimeout(function(){ search(input.value); }, 120); });
        input.addEventListener('keydown', function(e){
          if (e.key === 'Enter') { e.preventDefault(); go(cur + (e.shiftKey ? -1 : 1)); }
          else if (e.key === 'Escape') { e.preventDefault(); shut(); }
          if (!((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F'))) e.stopPropagation();
        });
        box.addEventListener('click', function(e){
          e.stopPropagation();
          var a = e.target.getAttribute ? e.target.getAttribute('data-a') : null;
          if (a === 'prev') go(cur - 1); else if (a === 'next') go(cur + 1); else if (a === 'close') shut();
        });
      }
      box.style.top = (barBottom() + 8) + 'px'; box.classList.add('open');
      var sel = String(window.getSelection() || '').trim();
      if (sel && sel.length < 60 && sel.indexOf('\n') < 0) input.value = sel;
      input.focus(); input.select();
      if (input.value) search(input.value);
    }
    document.addEventListener('keydown', function(e){
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'f' || e.key === 'F')) { e.preventDefault(); e.stopPropagation(); open(); }
      else if (e.key === 'F3' && box && box.classList.contains('open')) { e.preventDefault(); go(cur + (e.shiftKey ? -1 : 1)); }
    }, true);
    window.addEventListener('message', function(e){ if (e.data && e.data.zhFind) open(); });
  })();
  /* 视图切换：选中的那一格是一块会滑过去的浮片 */
  (function(){
    var v = document.querySelector('.bar .views'); if (!v) return;
    var thumb = document.createElement('span'); thumb.className = 'zh-thumb'; v.insertBefore(thumb, v.firstChild);
    function place(){
      var b = v.querySelector('button[aria-pressed="true"]'); if (!b) return;
      thumb.style.width = b.offsetWidth + 'px'; thumb.style.transform = 'translateX(' + b.offsetLeft + 'px)';
    }
    v.addEventListener('click', function(){ setTimeout(place, 0); });
    window.addEventListener('resize', place);
    place(); setTimeout(function(){ place(); thumb.classList.add('ready'); }, 80);
  })();
  /* Bloom 的批注栏点了第 N 条 → 滚到那一处改动 */
  window.addEventListener('message', function(e){
    var d = e.data || {}; if (!d.zhGoto) return;
    var el = document.querySelector('[data-notes~="' + String(d.zhGoto).replace(/[^\w-]/g, '') + '"]'); if (!el) return;
    var y = el.getBoundingClientRect().top + window.scrollY - barBottom() - 12;
    window.scrollTo({top: y, behavior: 'smooth'});
    el.classList.add('current'); setTimeout(function(){ el.classList.remove('current'); }, 2400);
  });
  /* 滚动条只在工具栏下面：整页的原生滚动条藏起来，画一条从工具栏底边开始的（拖得动，滚轮照常） */
  (function(){
    var root = document.documentElement;
    var sb = document.createElement('div'); sb.id = 'zh-sb';
    var th = document.createElement('div'); th.className = 'zh-sb-t'; sb.appendChild(th); document.body.appendChild(sb);
    var drag = null, raf = 0;
    function metrics(){
      var top = barBottom(), H = root.scrollHeight, vh = window.innerHeight, track = vh - top - 4;
      var h = Math.max(32, track * vh / Math.max(H, 1));
      return {top: top, H: H, vh: vh, track: track, h: h};
    }
    function place(){
      raf = 0;
      var m = metrics();
      root.style.setProperty('--zh-bar-h', m.top + 'px');
      if (m.H <= m.vh + 2 || m.track < 40) { sb.style.display = 'none'; return; }
      sb.style.display = '';
      var y = (m.track - m.h) * (window.scrollY / (m.H - m.vh));
      th.style.height = m.h + 'px'; th.style.transform = 'translateY(' + Math.max(0, y) + 'px)';
    }
    function later(){ if (!raf) raf = requestAnimationFrame(place); }
    th.addEventListener('pointerdown', function(e){
      e.preventDefault(); th.setPointerCapture(e.pointerId);
      drag = {y: e.clientY, s: window.scrollY}; sb.classList.add('drag');
    });
    th.addEventListener('pointermove', function(e){
      if (!drag) return;
      var m = metrics(), k = (m.H - m.vh) / Math.max(1, m.track - m.h);
      window.scrollTo(0, drag.s + (e.clientY - drag.y) * k);
    });
    function end(){ drag = null; sb.classList.remove('drag'); }
    th.addEventListener('pointerup', end); th.addEventListener('pointercancel', end);
    window.addEventListener('scroll', later, {passive: true});
    window.addEventListener('resize', later);
    try { new ResizeObserver(later).observe(document.body); } catch (e) { setInterval(later, 1000); }
    place();
  })();
  /* 宽表放不下：右上角一个放大按钮，点开在大窗里看全表（照 Claude 的做法），Esc 或点外面关 */
  (function(){
    var ICON = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9"/></svg>';
    var CLOSE = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
    var shade = null;
    function close(){ if (shade) { shade.remove(); shade = null; document.documentElement.classList.remove('zh-tbl-open'); } }
    function open(wrap){
      close();
      var t = wrap.querySelector('table'); if (!t) return;
      shade = document.createElement('div'); shade.id = 'zh-tbl-shade';
      var box = document.createElement('div'); box.className = 'zh-tbl-box'; box.setAttribute('role', 'dialog');
      var x = document.createElement('button'); x.type = 'button'; x.className = 'zh-tbl-x'; x.setAttribute('aria-label', '{{T:close}}'); x.innerHTML = CLOSE;
      var body = document.createElement('div'); body.className = 'zh-tbl-body';
      body.appendChild(t.cloneNode(true));
      box.appendChild(x); box.appendChild(body); shade.appendChild(box); document.body.appendChild(shade);
      document.documentElement.classList.add('zh-tbl-open');
      x.onclick = close;
      shade.addEventListener('mousedown', function(e){ if (e.target === shade) close(); });
      x.focus();
    }
    document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && shade) { e.preventDefault(); close(); } }, true);
    function wrapOf(t){
      var w = t.parentElement;
      while (w && w !== document.body) {
        var cs = getComputedStyle(w);
        if (/(auto|scroll|hidden)/.test(cs.overflowX) || w.classList.contains('tablewrap')) return w;
        w = w.parentElement;
      }
      return t.parentElement;
    }
    function scan(){
      Array.prototype.forEach.call(document.querySelectorAll('.docs table'), function(t){
        if (t.closest('#zh-tbl-shade')) return;
        var w = wrapOf(t); if (!w) return;
        var over = t.scrollWidth > w.clientWidth + 2 || t.getBoundingClientRect().width > w.getBoundingClientRect().width + 2;
        // 按钮放在表格外面（表格上方右端，不跟着表格横向滚走）
        var hold = w.previousElementSibling && w.previousElementSibling.classList.contains('zh-tbl-hold') ? w.previousElementSibling : null;
        if (over && !hold) {
          hold = document.createElement('div'); hold.className = 'zh-tbl-hold';
          var b = document.createElement('button'); b.type = 'button'; b.className = 'zh-tbl-btn'; b.setAttribute('aria-label', '{{T:zoom_tbl}}');
          b.innerHTML = ICON; b.onclick = function(e){ e.preventDefault(); e.stopPropagation(); open(w); };
          hold.appendChild(b); w.parentNode.insertBefore(hold, w);
        } else if (!over && hold) hold.remove();
      });
    }
    var tm = 0; function later(){ clearTimeout(tm); tm = setTimeout(scan, 150); }
    window.addEventListener('resize', later);
    try { new ResizeObserver(later).observe(document.body); } catch (e) {}
    scan(); setTimeout(scan, 600);
  })();
  /* 论文缩放：Ctrl + 滚轮、Ctrl + 加号 / 减号放大缩小，Ctrl + 0 复原；大小记在这一页自己的 localStorage 里（重启也在）。
     只缩放正文（章节栏、查找条不动），缩放前后屏幕顶上那一段留在原处。外壳里按的键由外壳转过来（zhZoom） */
  var ZOOM = (function(){
    var KEY = 'zh-zoom', STEPS = [0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5];
    var z = 1, saved = false, acc = 0, accT = 0;
    try { var v0 = parseFloat(localStorage.getItem(KEY)); if (v0 >= 0.5 && v0 <= 2.5) { z = v0; saved = true; } } catch (e) {}
    function put(v){ docs.style.zoom = v === 1 ? '' : String(v); }
    function anchor(){
      var top = barBottom() + 4, list = docs.querySelectorAll('.row, .sect');
      for (var i = 0; i < list.length; i++) { var r = list[i].getBoundingClientRect(); if (r.height && r.bottom > top) return {el: list[i], top: r.top}; }
      return null;
    }
    function set(v, store){
      v = Math.round(Math.min(2.5, Math.max(0.5, Number(v) || 1)) * 100) / 100;
      var a = v !== z ? anchor() : null;
      z = v; put(v);
      if (a) { var nr = a.el.getBoundingClientRect(); window.scrollBy(0, nr.top - a.top); }
      if (store !== false) { saved = true; try { localStorage.setItem(KEY, String(v)); } catch (e) {} }
      try { window.dispatchEvent(new Event('resize')); } catch (e) {}
      if (window.__zhSend) window.__zhSend();
    }
    function step(d){
      var i, n = STEPS.length;
      if (d > 0) { for (i = 0; i < n; i++) { if (STEPS[i] > z + 0.001) return set(STEPS[i]); } return set(STEPS[n - 1]); }
      for (i = n - 1; i >= 0; i--) { if (STEPS[i] < z - 0.001) return set(STEPS[i]); }
      return set(STEPS[0]);
    }
    put(z);
    document.addEventListener('keydown', function(e){
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      var k = e.key, c = e.code;
      if (k === '=' || k === '+' || c === 'Equal' || c === 'NumpadAdd') step(1);
      else if (k === '-' || k === '_' || c === 'Minus' || c === 'NumpadSubtract') step(-1);
      else if (k === '0' || c === 'Digit0' || c === 'Numpad0') set(1);
      else return;
      e.preventDefault(); e.stopPropagation();
    }, true);
    window.addEventListener('wheel', function(e){
      if (!e.ctrlKey) return;
      e.preventDefault();
      var now = Date.now(); if (now - accT > 400) acc = 0; accT = now;
      acc += e.deltaMode ? e.deltaY * 40 : e.deltaY;          /* 鼠标一格 = 一档；触控板捏合是一串小步，攒够了才跳 */
      if (Math.abs(acc) >= 50) { step(acc < 0 ? 1 : -1); acc = 0; }
    }, {passive: false});
    return {get: function(){ return z; }, saved: function(){ return saved; }, set: set, step: step};
  })();
  /* 嵌在 Bloom 里（地址带 host=bloom）：这页自己的工具栏藏起来，上一处 / 下一处 / 编辑 / 三种视图画在外壳那一行。
     外壳 → 这里：{zhCmd: 'prev' | 'next' | 'edit' | 'view', view: 'mark' | 'new' | 'old'}、{zhZoom: 'in' | 'out' | 'reset' | 数字}、{zhHello: 1}；
     这里 → 外壳：{zhState: {cur, total, view, views, canEdit, editing, zoom, zoomSaved, live}}，一变就发 */
  (function(){
    if (window.parent === window) return;
    var pos = document.getElementById('pos');
    var views = Array.prototype.slice.call(document.querySelectorAll('.bar .views button[data-v]'));
    function state(){
      var m = pos ? /([0-9]+|\S)\s*\/\s*([0-9]+)/.exec(pos.textContent || '') : null;
      var on = views.filter(function(b){ return b.getAttribute('aria-pressed') === 'true'; })[0];
      var eb = document.getElementById('zh-editbtn');
      return {cur: m && /^[0-9]+$/.test(m[1]) ? parseInt(m[1], 10) : 0, total: m ? parseInt(m[2], 10) : 0,
              view: on ? on.getAttribute('data-v') : null, views: views.map(function(b){ return b.getAttribute('data-v'); }),
              canEdit: !!eb, editing: !!eb && eb.getAttribute('aria-pressed') === 'true',
              zoom: ZOOM.get(), zoomSaved: ZOOM.saved(), live: LIVE};
    }
    var last = '';
    function send(force){
      var s = state(), k = JSON.stringify(s);
      if (!force && k === last) return;
      last = k;
      try { window.parent.postMessage({zhState: s}, '*'); } catch (e) {}
    }
    window.__zhSend = function(){ send(false); };
    /* 点在论文里、按 Esc：外壳那边开着的弹层（邀请、参会人卡片、保存菜单）要关掉，可这些事件到不了外壳的页面，转过去 */
    document.addEventListener('pointerdown', function(){ try { window.parent.postMessage({zhPointer: 1}, '*'); } catch (e) {} }, true);
    document.addEventListener('keydown', function(e){ if (e.key === 'Escape') { try { window.parent.postMessage({zhKey: 'Escape'}, '*'); } catch (e2) {} } }, true);
    /* 指针在论文上移动：外壳里靠悬停打开的参会人卡片该收起了（外壳自己收不到这些移动），最多 0.4 秒说一次 */
    var lastMv = 0;
    document.addEventListener('mousemove', function(){ var n = Date.now(); if (n - lastMv > 400) { lastMv = n; try { window.parent.postMessage({zhHover: 1}, '*'); } catch (e) {} } }, {passive: true});
    try {
      var mo = new MutationObserver(function(){ send(false); });
      if (pos) mo.observe(pos, {childList: true, characterData: true, subtree: true});
      views.forEach(function(b){ mo.observe(b, {attributes: true, attributeFilter: ['aria-pressed']}); });
      var eb0 = document.getElementById('zh-editbtn');
      if (eb0) mo.observe(eb0, {attributes: true, attributeFilter: ['aria-pressed']});
    } catch (e) { setInterval(function(){ send(false); }, 700); }
    window.addEventListener('message', function(e){
      var d = e.data || {};
      if (d.zhCmd) {
        var c = d.zhCmd, b = null;
        if (c === 'prev') b = document.getElementById('prev');
        else if (c === 'next') b = document.getElementById('next');
        else if (c === 'edit') b = document.getElementById('zh-editbtn');
        else if (c === 'view') b = views.filter(function(x){ return x.getAttribute('data-v') === d.view; })[0];
        if (b) b.click();
        send(true);
      }
      if (d.zhZoom != null) {
        if (d.zhZoom === 'in') ZOOM.step(1);
        else if (d.zhZoom === 'out') ZOOM.step(-1);
        else if (d.zhZoom === 'reset') ZOOM.set(1);
        else if (typeof d.zhZoom === 'number') ZOOM.set(d.zhZoom, d.store !== false);
      }
      if (d.zhHello) send(true);
    });
    send(true);
  })();
})();
</script>
"""

PLACEHOLDER_MARK = '<meta name="bloom-placeholder" content="1">'   # 占位页的记号：给访客的换成不带细节的那句
PLACEHOLDER = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>Bloom</title>
<meta name="bloom-placeholder" content="1">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>html{{background:#0b0b0b}}</style></head>
<body style="font-family:system-ui,'Microsoft YaHei',sans-serif;background:#0b0b0b;color:#a09e96;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center">
<p style="font-size:14px">{msg}</p></body></html>"""


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


EMBED_CSS = """
<style id="zh-embed">
/* 嵌在 Bloom 窗口里：只留工具栏和正文。标题、说明、改动量表、页尾、浮条、快捷键提示、改动统计、图例都不要；
   批注在 Bloom 自己的那一栏里，这里不再占正文的右半边；改动目录换成下面照「文稿对比」做的章节栏 */
header, section.summary, section.rest, #zh-bar, .side, .kbd, .bar .stats, .bar .legend, .bar .tool,
.row .notes, .sect .notes.inline { display: none !important; }
:root { --zh-accent: #60a5fa; }
body { padding: 0 0 48px !important; }
.wrap { max-width: none !important; gap: 0 !important; }
.layout { grid-template-columns: minmax(0, 1fr) !important; gap: 0 !important; }
.doc { border-left: 0 !important; border-right: 0 !important; border-radius: 0 !important; }
.row { grid-template-columns: minmax(0, 1fr) !important; gap: 0 !important; padding: 14px 28px !important; }
.sect { padding-left: 28px !important; padding-right: 28px !important; }
.para { max-width: none !important; }
.bar { padding: 6px 12px !important; border-radius: 0 !important; border-width: 0 0 1px !important; gap: 8px 12px !important; }
/* 没改动的段落像正常文档一样连着读：不要"N 段未改动"的分隔条，也不要每段头上的"第 N 段 · 未改动"标签 */
details.fold > summary { display: none !important; }
.row:not(.chg) .rowhead { display: none !important; }
.row:not(.chg) { padding-top: 4px !important; padding-bottom: 4px !important; }
/* 整页只有这一根滚动条，细的 */
html { scrollbar-width: thin; scrollbar-color: var(--rule) transparent; background: var(--paper); }
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-thumb { background: var(--rule); border-radius: 5px; }
::-webkit-scrollbar-track { background: transparent; }
/* 右缘的改动刻度贴着滚动条，只留刻线，不要那个像第二根滚动条的视口框 */
.rail { right: 1px !important; width: 5px !important; background: none !important; border: 0 !important; }
.rail .view { display: none !important; }
.rail a { left: 0 !important; right: 0 !important; }
/* 章节栏：全文所有小节，有改动的带一个点；左缘的小把手收起/展开 */
#zh-toc { position: fixed; left: 0; top: 0; bottom: 0; width: 214px; z-index: 30; box-sizing: border-box;
  background: var(--sheet); border-right: 1px solid var(--rule); overflow-y: auto; padding: 10px 7px 40px;
  transform: translateX(-100%); transition: transform .22s ease; scrollbar-width: thin; font-family: var(--sans); }
:root.zh-toc-open #zh-toc { transform: none; box-shadow: 10px 0 28px rgba(0, 0, 0, .22); }
:root.zh-toc-push #zh-toc { box-shadow: none; }
:root.zh-toc-push body { padding-left: 214px !important; }
.zh-tochead { font-size: 11px; color: var(--muted); padding: 3px 9px 7px; letter-spacing: .04em; }
.zh-tochead span { opacity: .7; }
.zh-ti { position: relative; display: flex; align-items: center; gap: 6px; padding: 5px 9px; margin-bottom: 1px;
  border-radius: 6px; cursor: pointer; font-size: 12.5px; line-height: 1.35; color: var(--muted);
  transition: background .18s, color .18s; }
.zh-ti::before { content: ""; position: absolute; left: 2px; top: 50%; width: 2px; height: 0; transform: translateY(-50%);
  border-radius: 2px; background: var(--zh-accent); transition: height .2s cubic-bezier(.22, 1, .36, 1); }
.zh-ti:hover::before { height: 52%; }
.zh-ti.cur::before { height: 66%; }
.zh-ti:hover { background: color-mix(in srgb, var(--ink) 6%, transparent); color: var(--ink); }
.zh-ti.cur { color: var(--ink); font-weight: 600;
  background: linear-gradient(90deg, color-mix(in srgb, var(--zh-accent) 20%, transparent),
    color-mix(in srgb, var(--zh-accent) 4%, transparent) 72%, transparent); }
.zh-ti .zh-tx { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zh-ti .zh-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--zh-accent); flex: none; opacity: 0; }
.zh-ti.chg .zh-dot { opacity: .9; }
.zh-ti.lv3 { padding-left: 21px; font-size: 11.5px; }
#zh-tocbtn { position: fixed; left: 0; top: 50%; z-index: 31; width: 15px; height: 46px; transform: translateY(-50%);
  border: 1px solid var(--rule); border-left: 0; border-radius: 0 7px 7px 0; background: var(--sheet); color: var(--muted);
  cursor: pointer; font-size: 11px; line-height: 1; padding: 0; transition: left .22s ease; }
:root.zh-toc-open #zh-tocbtn { left: 214px; }
#zh-tocbtn:hover { color: var(--zh-accent); }
/* 批注：贴在被改的那一段右边的小圆标，点开才看内容；打勾 = 看过了，消掉 */
.row, .sect { position: relative; }
.row { padding-right: 56px !important; }
.zh-pins { position: absolute; right: 16px; top: 13px; display: flex; flex-direction: column; gap: 5px; z-index: 3; }
.row:not(.chg) > .zh-pins { top: 5px; }
.sect > .zh-pins { top: 50%; transform: translateY(-50%); }
.zh-pin { width: 22px; height: 22px; border-radius: 50%; border: 1.5px solid var(--rule); background: var(--sheet); color: var(--muted);
  font: 600 11px/1 var(--sans); font-variant-numeric: tabular-nums; cursor: pointer; padding: 0; transition: box-shadow .12s, transform .12s; }
.zh-pin:hover, .zh-pin.on { transform: scale(1.08); box-shadow: 0 0 0 3px color-mix(in srgb, var(--zh-accent) 24%, transparent); }
.zh-pin.is-done { background: var(--zh-accent); border-color: var(--zh-accent); color: #fff; }
.zh-pin.is-working { border-color: #d9a406; color: #d9a406; animation: zh-pulse 1.4s ease-in-out infinite; }
.zh-pin.is-failed { border-color: var(--del); color: var(--del); }
@keyframes zh-pulse { 50% { box-shadow: 0 0 0 4px color-mix(in srgb, #d9a406 26%, transparent); } }
/* 批注卡片（2026-11-21 用户："这玩意太长了 固定大小 加上滚动"）：最高 400px（窗口矮时更矮），头一行不动，
   下面的原话、结果在卡片里滚，滚到头不带着论文滚 */
.zh-note { position: absolute; right: 48px; top: 6px; z-index: 6; box-sizing: border-box; width: min(340px, calc(100% - 64px));
  display: flex; flex-direction: column; max-height: min(400px, calc(100vh - 80px));
  background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; box-shadow: 0 12px 32px rgba(0, 0, 0, .3);
  padding: 10px 12px 0; font: 400 13px/1.55 var(--sans); color: var(--ink); cursor: default; text-align: left; }
.zh-note-body { flex: 0 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; margin-right: -8px; padding: 0 8px 12px 0;
  scrollbar-width: thin; scrollbar-color: color-mix(in srgb, var(--ink) 24%, transparent) transparent;
  /* 上下边淡出：没滚到头时看得出还有字；滚到头时淡出的只是上面 8px 的空白和下面的留白 */
  mask-image: linear-gradient(to bottom, transparent 0, #000 8px, #000 calc(100% - 12px), transparent 100%); }
/* 头一行和每条任务那一行同一个排法：标题 · 状态字 · 按钮列（× 撤回在第一列，✓ 确认在最后一列，见 showCard） */
.zh-note-head, .zh-item-head { display: flex; align-items: center; gap: 8px; min-height: 24px; }
.zh-note-head { flex: none; }
.zh-note-title { flex: 1; min-width: 0; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zh-item-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zh-note-st { flex: none; font-size: 12px; color: var(--muted); }
.zh-note-st.is-working { color: color-mix(in srgb, #d9a406 var(--zh-name-mix, 100%), #000); }
.zh-note-st.is-failed { color: var(--del); }
.zh-acts { flex: none; display: grid; grid-template-columns: repeat(var(--zh-cols, 1), 24px); gap: 6px; }
.zh-acts .zh-note-x { grid-column: 1; }
.zh-acts .zh-note-ok { grid-column: -2 / -1; }
.zh-note-ok, .zh-note-x { width: 24px; height: 24px; border-radius: 50%; border: 1px solid var(--rule); background: transparent; color: var(--muted);
  cursor: pointer; padding: 0; display: inline-grid; place-items: center; }
.zh-note-ok svg, .zh-note-x svg { display: block; }
.zh-note-ok:hover { background: #16a34a; border-color: #16a34a; color: #fff; }
.zh-note-x:hover { background: #dc2626; border-color: #dc2626; color: #fff; }
.zh-note-x:disabled, .zh-note-ok:disabled { opacity: .5; cursor: default; }
.zh-note-x.is-busy { animation: zh-pulse 1s ease-in-out infinite; }
.zh-item { margin-top: 8px; transition: opacity .16s; }
.zh-item + .zh-item { padding-top: 8px; border-top: 1px solid var(--rule); }
.zh-item.is-gone { opacity: 0; }
.zh-item p:first-of-type { margin-top: 4px; }
/* 名字（你、访客、AI）：各人的颜色，浅色主题下和黑色对半混（同外壳的 nameColor）；太长省略号，悬停看全名 */
html:root { --zh-name-mix: 50%; }
@media (prefers-color-scheme: dark) { html:root:not([data-theme="light"]) { --zh-name-mix: 100%; } }
html:root[data-theme="dark"] { --zh-name-mix: 100%; }
.zh-note b.zh-who { display: inline-block; max-width: 10em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  vertical-align: bottom; line-height: 20px; color: color-mix(in srgb, var(--zh-who, var(--muted)) var(--zh-name-mix, 100%), #000); }
.zh-pin { width: auto; min-width: 22px; padding: 0 5px; }
.zh-more summary { cursor: pointer; font-size: 11px; color: var(--muted); margin-top: 6px; list-style: none; }
.zh-more summary::-webkit-details-marker { display: none; }
.zh-more[open] summary { display: none; }
.zh-note p { margin: 8px 0 0; color: var(--muted); overflow-wrap: anywhere; font-family: var(--sans); font-size: 13px; line-height: 1.55; }
.zh-note p b { margin-right: 7px; font-size: 11px; font-weight: 600; }
.zh-note-said b { color: #7cc0ff; }
.zh-note p.zh-note-did { color: var(--ink); }
/* 标注卡片底下那排：状态（会后改 / 已处理）+ 标签（要重新跑实验……），会自动换行，卡片宽度不变 */
.zh-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.zh-tag { font-size: 11px; line-height: 18px; padding: 1px 9px; border-radius: 10px; white-space: nowrap;
  color: color-mix(in srgb, var(--muted) 60%, var(--ink)); background: color-mix(in srgb, var(--ink) 8%, transparent); }
.zh-tag.is-st { color: #7a4f05; background: rgba(217,154,43,.16); }
.zh-tag.is-ok { color: #166534; background: rgba(22,163,74,.14); }
@media (prefers-color-scheme: dark) {
  html:root:not([data-theme="light"]) .zh-tag.is-st { color: #e0a43a; }
  html:root:not([data-theme="light"]) .zh-tag.is-ok { color: #6cc483; }
}
html:root[data-theme="dark"] .zh-tag.is-st { color: #e0a43a; }
html:root[data-theme="dark"] .zh-tag.is-ok { color: #6cc483; }
.zh-note-did b { color: #d97757; }
.zh-note p.zh-note-bad { color: var(--del); }
/* 图放回原位；公式排版；行内上下标 */
.zh-figrow .para { text-align: center; color: var(--ink) !important; }
img.zh-fig { max-width: min(100%, 880px); height: auto; border-radius: 4px; background: #fff; }
iframe.zh-fig { width: 100%; height: 440px; border: 0; background: transparent; }
.para.zh-math { font-family: var(--serif) !important; font-size: 1rem !important; color: var(--ink) !important; text-align: center;
  overflow-x: auto; overflow-y: hidden; padding: 6px 0; white-space: normal; }
.para.zh-math mjx-container { margin: 0 !important; }
.para.zh-math mjx-container > svg { max-width: 100%; height: auto; }
img.zh-fig { padding: 10px; box-sizing: border-box; }
.zh-var { font-style: italic; }
.para sub, .para sup { font-size: .72em; line-height: 0; }
/* 编辑：工具栏的「编辑」，再点任意一段就地改原文 */
#zh-editbtn { font: inherit; font-size: .85rem; padding: 4px 12px; border: 1px solid var(--rule); border-radius: 5px; background: var(--sheet);
  color: var(--ink); cursor: pointer; margin-left: auto; }
#zh-editbtn[aria-pressed="true"] { background: var(--zh-accent); border-color: var(--zh-accent); color: #fff; }
#zh-editbtn + .views { margin-left: 0 !important; }
:root.zh-editing .docs .para, :root.zh-editing .docs .sect { cursor: text; }
:root.zh-editing .docs .row:hover .para, :root.zh-editing .docs .sect:hover { outline: 1px dashed color-mix(in srgb, var(--zh-accent) 65%, transparent);
  outline-offset: 5px; border-radius: 3px; }
.zh-editor { margin: 2px 0 4px; }
.doc > .zh-editor { padding: 8px 28px; }
textarea.zh-edit { display: block; width: 100%; box-sizing: border-box; min-height: 60px; font: 400 1rem/1.8 var(--serif); color: var(--ink);
  background: color-mix(in srgb, var(--zh-accent) 7%, var(--sheet)); border: 1px solid var(--zh-accent); border-radius: 6px; padding: 9px 11px;
  resize: vertical; outline: none; }
.zh-editbar { display: flex; align-items: center; gap: 8px; margin-top: 7px; font: 12px/1 var(--sans); }
.zh-edithint { margin-right: auto; color: var(--muted); }
.zh-editbar button { font: inherit; padding: 6px 14px; border-radius: 5px; border: 1px solid var(--rule); background: var(--sheet); color: var(--ink); cursor: pointer; }
.zh-editbar .zh-save { background: var(--zh-accent); border-color: var(--zh-accent); color: #fff; font-weight: 600; }
.zh-editmsg { margin: 4px 0; font: 12px var(--sans); color: var(--del); }
/* 配色取自 Claude Docs：暖灰底、象牙白字、赤陶色强调；整页一个平面，段落之间不画线 */
html:root {
  --paper: #faf9f5; --sheet: #faf9f5; --band: transparent; --ink: #1f1e1d; --muted: #73726c; --rule: #e3e1d7;
  --ins: #2f7d4f; --ins-bg: rgba(47, 125, 79, .12); --del: #b4483a; --del-bg: rgba(180, 72, 58, .10);
  --move: #2f7d4f; --note-bg: #f0eee6; --note-rule: #e3e1d7; --zh-accent: #c96442;
}
@media (prefers-color-scheme: dark) {
  html:root:not([data-theme="light"]) {
    --paper: #0b0b0b; --sheet: #0b0b0b; --band: transparent; --ink: #ecebe4; --muted: #a09e96; --rule: #2a2a28;
    --ins: #8ec48a; --ins-bg: rgba(126, 184, 120, .16); --del: #e58a78; --del-bg: rgba(229, 138, 120, .14);
    --move: #8ec48a; --note-bg: #1a1a19; --note-rule: #2a2a28; --zh-accent: #60a5fa;
  }
}
html:root[data-theme="dark"] {
  --paper: #0b0b0b; --sheet: #0b0b0b; --band: transparent; --ink: #ecebe4; --muted: #a09e96; --rule: #2a2a28;
  --ins: #8ec48a; --ins-bg: rgba(126, 184, 120, .16); --del: #e58a78; --del-bg: rgba(229, 138, 120, .14);
  --move: #8ec48a; --note-bg: #1a1a19; --note-rule: #2a2a28; --zh-accent: #60a5fa;
}
body { background: var(--paper) !important; color: var(--ink) !important; }
.doc { background: transparent !important; border: 0 !important; }
.row, .sect, .fold, .sect.lv3 + .row { border-top: 0 !important; }
.sect { background: transparent !important; padding-top: 22px !important; padding-bottom: 6px !important; }
.sect.lv1 { border-bottom: 0 !important; font-size: 1.55rem !important; padding-top: 26px !important; }
.sect.lv2 { font-size: 1.22rem !important; }
.sect.lv3 { color: var(--ink) !important; font-weight: 600 !important; font-size: 1.04rem !important; padding-top: 14px !important; }
/* 没改动的段落就是正文，不发灰 */
.same, .para.same, .fixed, .tablewrap.same .dtable { color: var(--ink) !important; }
.row:not(.chg) { padding-top: 7px !important; padding-bottom: 7px !important; }
.row.chg { box-shadow: inset 2px 0 0 color-mix(in srgb, var(--zh-accent) 75%, transparent); background: transparent !important; }
.row.current, .sect.current { box-shadow: inset 3px 0 0 var(--zh-accent) !important; background: color-mix(in srgb, var(--zh-accent) 7%, transparent) !important; }
.bar { background: var(--paper) !important; }
.views button[aria-pressed="true"] { background: color-mix(in srgb, var(--ink) 14%, transparent) !important; color: var(--ink) !important; font-weight: 600; }
.zh-pin.is-done { background: var(--zh-accent); border-color: var(--zh-accent); color: #fff; }
/* 纸面是近黑的：浮起来的东西（批注卡、章节栏、编辑框、图）用稍亮一档的底，才分得出层次 */
@media (prefers-color-scheme: dark) {
  html:root:not([data-theme="light"]) .zh-note { background: #1c1c1a; border-color: #33332f; }
  html:root:not([data-theme="light"]) #zh-toc, html:root:not([data-theme="light"]) #zh-tocbtn { background: #131312; }
  html:root:not([data-theme="light"]) textarea.zh-edit { background: #161615; }
  html:root:not([data-theme="light"]) .navctl button, html:root:not([data-theme="light"]) #zh-editbtn,
  html:root:not([data-theme="light"]) .zh-editbar button:not(.zh-save), html:root:not([data-theme="light"]) .zh-pin:not(.is-done) { background: #161615; }
}
/* Ctrl+F 查找条 */
#zh-find { position: fixed; right: 18px; top: 52px; z-index: 40; display: none; align-items: center; gap: 4px; padding: 5px 6px 5px 11px;
  border-radius: 8px; background: var(--sheet); border: 1px solid var(--rule); box-shadow: 0 10px 28px rgba(0, 0, 0, .35);
  font: 13px var(--sans); color: var(--ink); }
#zh-find.open { display: flex; }
#zh-find input { width: 200px; border: 0; outline: 0; background: transparent; color: var(--ink); font: inherit; padding: 4px 0; }
.zh-find-n { min-width: 48px; text-align: right; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; margin-right: 4px; }
#zh-find button { width: 26px; height: 26px; border: 0; border-radius: 5px; background: transparent; color: var(--muted); cursor: pointer;
  font-size: 15px; line-height: 1; padding: 0; }
#zh-find button:hover { background: color-mix(in srgb, var(--ink) 10%, transparent); color: var(--ink); }
::highlight(zh-find) { background-color: rgba(96, 165, 250, .38); }
::highlight(zh-find-cur) { background-color: #3b82f6; color: #ffffff; }
@media (prefers-color-scheme: dark) { html:root:not([data-theme="light"]) #zh-find { background: #1c1c1a; border-color: #33332f; } }
/* 选中一段后弹出的「引用」（2026-11-21）：浮在选区结尾旁边，不占版面；点了才算引用 */
#zh-quote { position: absolute; left: 0; top: 0; z-index: 45; display: none; padding: 3px; border-radius: 9px; --zh-q-hi: #efede6;
  background: #ffffff; border: 1px solid rgba(0, 0, 0, .12); box-shadow: 0 6px 18px rgba(0, 0, 0, .14); font: 500 13px/1 var(--sans); }
#zh-quote.show { display: block; animation: zhPop .12s ease-out; }
.zh-quote-b { display: block; height: 28px; padding: 0 12px; border: 0; border-radius: 6px;
  background: transparent; color: var(--ink); font: inherit; line-height: 28px; cursor: pointer; white-space: nowrap; }
.zh-quote-b:hover, .zh-quote-b:focus-visible { background: var(--zh-q-hi); outline: none; }
@media (prefers-color-scheme: dark) { html:root:not([data-theme="light"]) #zh-quote { --zh-q-hi: #33332f; background: #232321; border-color: #3a3a36;
  box-shadow: 0 10px 28px rgba(0, 0, 0, .55); } }
html:root[data-theme="dark"] #zh-quote { --zh-q-hi: #33332f; background: #232321; border-color: #3a3a36; box-shadow: 0 10px 28px rgba(0, 0, 0, .55); }
/* 调研第 1 条：用字号去填宽度；段落之间靠段距，不靠线 */
.para { font-size: 16px !important; line-height: 1.8 !important; }
.row:not(.chg) { padding-top: 0 !important; padding-bottom: 0 !important; }
.row:not(.chg) .para { margin-bottom: .75em; }
.row.chg { padding-top: 10px !important; padding-bottom: 10px !important; }
@media (min-width: 900px) { .para { font-size: 17px !important; } .row, .sect { padding-left: 56px !important; padding-right: 64px !important; } }
@media (min-width: 1200px) { .para { font-size: 18px !important; } .row, .sect { padding-left: 80px !important; padding-right: 88px !important; } }
.sect.lv1 { font-size: 24px !important; padding-top: 28px !important; padding-bottom: 8px !important; }
.sect.lv2 { font-size: 20px !important; padding-top: 28px !important; padding-bottom: 6px !important; }
.sect.lv3 { font-size: 17px !important; padding-top: 18px !important; padding-bottom: 2px !important; }
/* 第 9 条：圆标安静下来，强调色只给正在看的那一条；改过的段落页边一条细线 */
.zh-pin { border: 1px solid var(--rule); background: transparent; color: var(--muted); font: 600 12px/20px var(--sans); transition: none; }
.zh-pin:hover, .zh-pin.on { transform: none; box-shadow: none; border-color: var(--zh-accent); color: var(--ink); }
.zh-pin.is-done { background: color-mix(in srgb, var(--ink) 14%, transparent) !important; border-color: transparent !important; color: var(--ink) !important; }
.zh-pin.is-working { border-style: dashed; border-color: var(--muted); color: var(--muted); animation: none; }
.zh-pin.is-failed { border-color: var(--del); color: var(--del); }
.row.chg { box-shadow: inset 2px 0 0 var(--rule) !important; }
.row.chg.current, .row.chg:has(.zh-pin.on) { box-shadow: inset 2px 0 0 var(--zh-accent) !important; background: none !important; }
.zh-note { width: min(320px, calc(100% - 64px)); border-radius: 6px; box-shadow: 0 0 0 1px rgba(255, 255, 255, .08), 0 8px 24px rgba(0, 0, 0, .5); font-size: 13px; }
.zh-note-ok:hover { background: color-mix(in srgb, var(--ink) 10%, transparent); border-color: var(--rule); color: var(--ink); }
.zh-tochead, .zh-note-st { font-size: 12px; }
.zh-ti { font-size: 13px; }
.zh-ti.lv3 { font-size: 12px; }
#zh-toc { width: 220px; }
:root.zh-toc-push body { padding-left: 220px !important; }
:root.zh-toc-open #zh-tocbtn { left: 220px; }
/* 第 12 条：滚动条和外壳同一种画法；章节栏的藏掉 */
html { scrollbar-width: auto; scrollbar-color: auto; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-button { display: none; }
::-webkit-scrollbar-track, ::-webkit-scrollbar-corner { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, .14); border: 3px solid transparent; border-radius: 10px; background-clip: content-box; }
::-webkit-scrollbar-thumb:hover { background-color: rgba(255, 255, 255, .5); }
#zh-toc { scrollbar-width: none; }
#zh-toc::-webkit-scrollbar { display: none; }
/* 视图切换：凹下去的轨道 + 一块会滑动的浮片；工具栏按钮有轻微的悬停和按下反馈 */
.views { position: relative; display: inline-flex; padding: 3px !important; border: 0 !important; border-radius: 9px !important;
  overflow: visible !important; background: color-mix(in srgb, var(--ink) 8%, transparent);
  box-shadow: inset 0 1px 2px rgba(0, 0, 0, .45), inset 0 0 0 1px rgba(255, 255, 255, .03); }
.views button { position: relative; z-index: 1; border: 0 !important; background: transparent !important; color: var(--muted) !important;
  padding: 4px 14px !important; border-radius: 6px; font-weight: 400 !important; transition: color .18s ease; }
.views button + button { border-left: 0 !important; }
.views button:hover { color: var(--ink) !important; }
.views button[aria-pressed="true"] { background: transparent !important; color: var(--ink) !important; font-weight: 600 !important; }
.zh-thumb { position: absolute; left: 0; top: 3px; bottom: 3px; width: 0; border-radius: 6px; pointer-events: none;
  background: color-mix(in srgb, var(--ink) 15%, var(--sheet));
  box-shadow: 0 1px 3px rgba(0, 0, 0, .55), 0 0 0 1px rgba(255, 255, 255, .05), inset 0 1px 0 rgba(255, 255, 255, .08); }
.zh-thumb.ready { transition: transform .26s cubic-bezier(.3, 1.2, .4, 1), width .26s cubic-bezier(.3, 1.2, .4, 1); }
.navctl button, #zh-editbtn { transition: background .15s ease, border-color .15s ease, transform .08s ease; }
.navctl button:hover, #zh-editbtn:hover { background: color-mix(in srgb, var(--ink) 9%, var(--sheet)) !important;
  border-color: color-mix(in srgb, var(--ink) 30%, transparent) !important; }
.navctl button:active, #zh-editbtn:active, .views button:active { transform: translateY(1px); }
/* 控件的灰统一成一套：底（--zh-ctl）、线和浮起的那一档（--zh-ctl-line）、悬停（--zh-ctl-hi），都从纸面色和字色算出来 */
html:root { --zh-ctl: color-mix(in srgb, var(--ink) 6%, var(--sheet)); --zh-ctl-hi: color-mix(in srgb, var(--ink) 11%, var(--sheet));
  --zh-ctl-line: color-mix(in srgb, var(--ink) 17%, var(--sheet)); }
.navctl button, #zh-editbtn, .zh-editbar button:not(.zh-save) { background: var(--zh-ctl) !important; border: 1px solid var(--zh-ctl-line) !important;
  color: var(--ink) !important; box-shadow: none !important; outline: none; }
.navctl button:hover, #zh-editbtn:hover, .zh-editbar button:not(.zh-save):hover { background: var(--zh-ctl-hi) !important; border-color: var(--zh-ctl-line) !important; }
.navctl button:focus-visible, #zh-editbtn:focus-visible { border-color: var(--zh-accent) !important; }
#zh-editbtn[aria-pressed="true"] { background: var(--zh-accent) !important; border-color: var(--zh-accent) !important; color: #fff !important; }
.views { background: var(--zh-ctl) !important; border: 1px solid var(--zh-ctl-line) !important; box-shadow: none !important; padding: 2px !important; }
.zh-thumb { background: var(--zh-ctl-line) !important; box-shadow: 0 1px 2px rgba(0, 0, 0, .35) !important; top: 2px !important; bottom: 2px !important; }
#zh-find, .zh-note { background: var(--zh-ctl) !important; border-color: var(--zh-ctl-line) !important; }
#zh-toc, #zh-tocbtn { background: color-mix(in srgb, var(--ink) 3%, var(--sheet)) !important; border-color: var(--zh-ctl-line) !important; }
textarea.zh-edit { background: var(--zh-ctl) !important; }
.bar { border-color: var(--zh-ctl-line) !important; }
/* 滚动条只画在工具栏下面（整页原生的那条藏起来）；右侧改动刻度也从工具栏底边开始 */
html { scrollbar-width: none !important; }
html::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none; }
#zh-sb { position: fixed; right: 1px; width: 11px; top: calc(var(--zh-bar-h, 44px) + 2px); bottom: 2px; z-index: 30; pointer-events: none; }
.zh-sb-t { position: absolute; left: 2px; right: 2px; top: 0; border-radius: 6px; pointer-events: auto; cursor: default;
  background: rgba(255, 255, 255, .14); transition: background .15s ease; }
.zh-sb-t:hover, #zh-sb.drag .zh-sb-t { background: rgba(255, 255, 255, .45); }
.rail { top: calc(var(--zh-bar-h, 44px) + 2px) !important; }
/* 宽表：右上角放大按钮 + 大窗 */
.zh-tbl-hold { position: relative; height: 0; }
.zh-tbl-btn { position: absolute; bottom: 4px; right: 2px; z-index: 3; width: 26px; height: 26px; display: inline-flex; align-items: center;
  justify-content: center; padding: 0; border-radius: 7px; background: var(--zh-ctl) !important; border: 1px solid var(--zh-ctl-line) !important;
  color: var(--muted) !important; cursor: pointer; opacity: .85; transition: background .15s ease, color .15s ease, opacity .15s ease; }
.zh-tbl-btn:hover { background: var(--zh-ctl-hi) !important; color: var(--ink) !important; opacity: 1; }
.zh-tbl-btn:active { transform: translateY(1px); }
#zh-tbl-shade { position: fixed; inset: 0; z-index: 200; background: rgba(0, 0, 0, .55); display: flex; align-items: center; justify-content: center;
  padding: 3vh 3vw; animation: zhFade .14s ease; }
.zh-tbl-box { position: relative; max-width: 94vw; max-height: 92vh; display: flex; flex-direction: column; background: var(--sheet);
  border: 1px solid var(--zh-ctl-line); border-radius: 12px; box-shadow: 0 18px 60px rgba(0, 0, 0, .6); animation: zhPop .16s cubic-bezier(.3, 1.2, .4, 1); }
.zh-tbl-body { overflow: auto; padding: 46px 22px 22px; scrollbar-width: thin; scrollbar-color: rgba(255, 255, 255, .2) transparent; }
.zh-tbl-body table { margin: 0 !important; width: auto !important; max-width: none !important; }
.zh-tbl-x { position: absolute; top: 8px; right: 8px; z-index: 2; width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center;
  padding: 0; border-radius: 7px; background: var(--zh-ctl); border: 1px solid var(--zh-ctl-line); color: var(--muted); cursor: pointer; }
.zh-tbl-x:hover { background: var(--zh-ctl-hi); color: var(--ink); }
html.zh-tbl-open, html.zh-tbl-open body { overflow: hidden; }
@keyframes zhFade { from { opacity: 0; } to { opacity: 1; } }
@keyframes zhPop { from { opacity: 0; transform: scale(.97); } to { opacity: 1; transform: none; } }
/* 嵌在 Bloom 里（host=bloom）：上一处 / 下一处 / 编辑 / 视图都在外壳那一行，这页自己的工具栏不要 */
:root.zh-hosted .bar { display: none !important; }
/* 标注模式（2026-11-14）：图钉不写编号，改过的是勾，会上改不了的标注是空心小旗，那一段左边一道橙线 */
.zh-pin { width: 22px !important; height: 22px !important; padding: 0 !important; display: inline-grid !important; place-items: center; }
.zh-pin svg { display: block; }
.zh-pin.is-note { background: transparent !important; border: 1.5px solid #d99a2b !important; color: #d99a2b !important; }
.zh-pin.is-note:hover, .zh-pin.is-note.on { box-shadow: 0 0 0 3px rgba(217,154,43,.22) !important; }
.zh-note p.zh-note-sys { font-size: 12px; font-style: italic; opacity: .75; }
/* 左边那道线（2026-11-15）：一段一种颜色，由批注脚本按固定先后写在 data-zh-bar 上（标注 > 没打勾的改动 > 其余改过的），
   生成器的"当前这一处"、点开卡片都不再改它的颜色（以前同一段黄、蓝、灰来回跳） */
html:root body .docs .row.row[data-zh-bar], html:root body .docs .sect.sect[data-zh-bar] { box-shadow: inset 2px 0 0 var(--zh-bar-c) !important; }
html:root body .docs [data-zh-bar="note"] { --zh-bar-c: #d99a2b; }
html:root body .docs [data-zh-bar="edit"] { --zh-bar-c: color-mix(in srgb, var(--zh-accent) 80%, transparent); }
html:root body .docs [data-zh-bar="done"] { --zh-bar-c: var(--rule); }
</style>
<script>document.documentElement.classList.add('zh-embed');if(/[?&]host=bloom(&|$)/.test(location.search))document.documentElement.classList.add('zh-hosted');</script>
"""


TASKS_RE = re.compile(r"/\*zh-tasks\*/.*?/\*zh-tasks-end\*/", re.S)
# ?demo=1 时拿这几条假任务看批注标记长什么样（不碰任务板）
DEMO_TASKS = [
    {"id": "D1", "idx": 1, "title": "摘要 一亿用户挪到引言", "status": "done", "remark": "摘要里那句一亿用户，放到引言去，摘要不要出现具体公司。",
     "result": "摘要第一段里“仅 TradingView 一家就服务逾一亿用户”一句挪到了引言第一段末尾，摘要不再点名公司。拿不准的点：无。",
     "focus": {"section": "摘要", "text": "量化交易策略的回测通常跑在 OHLC bar 上"}},
    {"id": "D2", "idx": 2, "title": "3.1 概念段拆成几个小点", "status": "working", "remark": "这段概念堆在一起，给我拆成几个小点。",
     "section": "3.1 数据与回测", "focus": {"section": "3.1 数据与回测", "text": ""}},
    {"id": "D3", "idx": 3, "title": "引理证明写清楚", "status": "failed", "remark": "这个引理的证明太糊了。", "error": "没定位到你指的引理，没有改。"},
    {"id": "D4", "idx": 4, "title": "第 4 节消融实验", "status": "done", "note": True, "remark": "消融实验不够，要补一组去掉注意力模块的对照。",
     "plan": "加一组去掉注意力模块的对照，重跑表 3。", "need": "重跑消融实验", "tags": ["rerun", "figure"]},
]


def _pretty_model(m) -> str:
    """claude-opus-5-5 -> Claude Opus 5.5; deepseek-v4-pro -> DeepSeek V4 Pro; anything else as is."""
    m = str(m or "").strip()
    low = m.lower()
    alias = {"opus": "Claude Opus", "sonnet": "Claude Sonnet", "haiku": "Claude Haiku"}
    if low in alias:
        return alias[low]
    parts = low.split("-")
    if len(parts) >= 4 and parts[0] == "claude" and parts[2].isdigit() and parts[3].isdigit():
        return "Claude %s %s.%s" % (parts[1].capitalize(), parts[2], parts[3])
    if parts[0] == "deepseek" and len(parts) > 1:
        return "DeepSeek " + " ".join(p.upper() if (p[:1] == "v" and p[1:].isdigit()) else p.capitalize() for p in parts[1:])
    return m


def _js(v) -> str:
    return json.dumps(v, ensure_ascii=False).replace("</", "<\\/")


def _profile(viewer) -> dict:
    """设置里"个人资料"那一组：{"name": 昵称, "avatar": 你自己的头像地址（没有是 None，界面上画花）,
    "custom": 头像是不是在设置里选的（能移除）, "assistant": AI 现在的名字, "assistant_default": 不起名字时叫什么,
    "assistant_custom": 是不是起过名字}。"""
    from . import host_profile
    from .config import default_assistant_name
    me, ai = viewer.participants(guests=False)[:2]
    av = me.get("avatar")
    own = av if av and not str(av).split("?")[0].endswith("/bloom.png") else None
    return {"name": me["name"], "avatar": own, "custom": host_profile.avatar_file() is not None,
            "assistant": ai["name"], "assistant_default": default_assistant_name(viewer.cfg),
            "assistant_custom": host_profile.saved_assistant() is not None,
            "color": me["color"], "ai_color": ai["color"], "palette": host_profile.PALETTE}


def _mic_state(viewer) -> dict:
    """主持人麦克风静音没有、此刻谁在出声（/api/mic/stream、/api/mic/mute 用）：{"muted": bool, "talk": [...]}。
    talk 里 "me" 是主持人、"claude" 是 Claude，其余是访客的名字；会议页上这些人的头像外亮一圈。"""
    try:
        m = viewer.mic_mute() if callable(getattr(viewer, "mic_mute", None)) else False
    except Exception:
        m = False
    try:
        talk = viewer.talking() if callable(getattr(viewer, "talking", None)) else []
    except Exception:
        talk = []
    return {"muted": bool(m), "talk": sorted({str(x) for x in talk or [] if x})}


_WHO = re.compile(r"^(?:【这句是)?(参会的老师|参会者)(.+?)说(?:的】|[:：])\s*")
# 逐字稿上软件自己写的行（切论文时的"切到 某篇"）：以前被当成人说的话混进多人讨论（2026-11-14 组会的卡片上署了名）
_SYS_ROW = re.compile(r"^(?:已切换到 \S|切到 \S|Switched to \S)")   # "切到"：以前的会议
# 谁能跨源调这个后端：Bloom 自己的窗口（tauri）和本机的页面（localhost / 127.0.0.1，开发时的 vite 也算）。
# 不带 Origin 的（本机脚本、语音桥转来的访客操作、/bloom 技能）照常。以前是 *：你浏览器里随便哪个网站都能悄悄调本机
# 47330 切论文、清任务板、改名字（2026-11-17 收紧）
_LOCAL_ORIGIN = re.compile(r"^(?:tauri://localhost|https?://tauri\.localhost|https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?)$", re.I)
_LOCAL_HOST = re.compile(r"^(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?$", re.I)
_GUEST_UP = [0]                # 访客传的论文：正在取、导入、排队的份数（Viewer.guest_act）
_GUEST_UP_LOCK = threading.Lock()
_GCOLOR: dict = {}            # 访客 → 颜色（Viewer.guest_colors 定的，批注卡片里的名字也用它）


_NO_DOUBT = re.compile(r"(?:\s*拿不准的(?:点|地方)[：:]\s*无|(?<=[。.\n])\s*无)[。.]?\s*$")


def _task_brief(t: dict, me: str = "") -> dict:
    b = {k: t.get(k) for k in ("id", "idx", "title", "remark", "status", "result", "error", "section", "focus", "resolved",
                               "discussion_rows", "note", "plan", "need", "mode")}
    b["tags"] = note_tags(t)                       # 标注的标签：小旗卡片底下那排（会后改 + 要重新跑实验……）
    # 工人记录的第二句"拿不准的点：无。"是写给快嘴的格式，卡片上只是噪音（2026-11-21 晚上那场每张卡都带着）
    if b.get("result"):
        b["result"] = _NO_DOUBT.sub("", str(b["result"])).strip() or b["result"]
    # 工人找不到地方时会写"无（文件读不到…）"这种占位：当成没有位置，别拿它去对小节标题、段落开头
    if str(b.get("section") or "").startswith("无"):
        b["section"] = None
    if isinstance(b.get("focus"), dict) and str(b["focus"].get("text") or "").startswith("无"):
        b["focus"] = {**b["focus"], "text": None}
    b["app"] = _is_app_task(t)                     # 说的是软件本身：论文页上不画
    rows = [r for r in str(t.get("remark") or "").split("\n") if r.strip()]
    if len(rows) > 1 and all(re.match(r"^[^：:\s]{1,20}[：:]", r) or r.startswith("（前面还有") for r in rows):   # 多人讨论：一行一个人
        b["thread"] = []
        for r in rows:
            if r.startswith("（前面还有"):
                b["thread"].append({"who": "", "text": r, "color": ""})
                continue
            name, said = re.split(r"[：:]", r, maxsplit=1)
            if _SYS_ROW.match(said.strip()):       # 逐字稿上的系统行（"切到 某篇"）：不是谁说的，不署名
                b["thread"].append({"who": "", "text": said.strip(), "color": "", "sys": True})
                continue
            b["thread"].append({"who": name, "text": _drop_said_back(said.strip()), "color": _color_of(name, me)})
        return b
    m = _WHO.match(str(t.get("remark") or ""))     # 任务原话前面带着"参会的老师X说："：卡片上署他的名字，不署你的
    if m:
        name = m.group(2).strip()
        b["who"], b["said"], b["who_color"] = name, _drop_said_back(str(t.get("remark") or "")[m.end():]), _color_of(name, me)
    else:
        from . import host_profile
        b["said"], b["who_color"] = _drop_said_back(t.get("remark")), host_profile.colors()[0]
    return b


def _drop_said_back(s) -> str:
    """卡片上的原话：去掉后面附的"（当场复述的改法：…）""（当场复述的补充：…）"（旧任务写的是"（Claude 当场复述…"），
    那是给改稿的人看的，卡片上有标题和结果就够了（2026-11-21）。复述里自己也带括号（"5.1 第（4）段"）：按括号配对找结尾，
    配不上（被截断了）就去到末尾。别的附注（"（又补充了：…）"、选中的那一段）照留。"""
    from .board import _SAID_BACK
    s = str(s or "")
    out, i = [], 0
    while True:
        m = _SAID_BACK.search(s, i)
        if not m:
            out.append(s[i:])
            break
        out.append(s[i:m.start()])
        depth, j = 0, m.start()
        while j < len(s):
            depth += {"（": 1, "）": -1}.get(s[j], 0)
            j += 1
            if depth == 0:
                break
        i = j
    return "".join(out).strip()


def _guest_palette() -> list:
    """访客的配色：调色板里去掉你和 AI 选中的颜色（设置 → 个人资料），免得撞色。"""
    from . import host_profile
    taken = set(host_profile.colors())
    return [c for c in GUEST_PALETTE if c not in taken] or GUEST_PALETTE


_AI_NAME = ["Claude"]          # 会上那位 AI 现在叫什么（Viewer 起来时、设置里改名时更新；批注卡片上认它用）


def _color_of(name: str, me: str = "") -> str:
    """批注卡片上名字的颜色：你蓝、AI 橙、访客用会上定的颜色（后端重启过、表里没有的，按先后从调色板取）。
    以前的会里记的 AI 名字是 Claude，也认。"""
    from . import host_profile
    host_c, ai_c = host_profile.colors()
    if name in ("Claude", _AI_NAME[0]):
        return ai_c
    if not name or name == me:
        return host_c
    if name not in _GCOLOR:
        used = set(_GCOLOR.values()) | {host_c, ai_c}
        pal = _guest_palette()
        _GCOLOR[name] = next((c for c in pal if c not in used), pal[len(_GCOLOR) % len(pal)])
    return _GCOLOR[name]


def _load_changes(tid) -> dict:
    try:
        return json.loads((CHANGES_DIR / f"{tid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _spots(tasks: list, changes: dict | None = None) -> dict:
    """哪几条改的是同一处：改动里新旧段落有一段对得上（后一条改的正是前一条改出来的那段，或者两条改同一段），
    就算一处。返回 {任务 id: 这一处的 id}。changes：已经读好的改动记录（{任务 id: 记录}），没给就自己读。"""
    parent: dict = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: dict = {}
    for t in sorted(tasks, key=lambda x: x.get("idx") or 0):
        tid = str(t.get("id"))
        parent.setdefault(tid, tid)
        ch = changes.get(tid) if changes is not None and tid in changes else _load_changes(tid)
        ops = ch.get("ops") or []
        for op in ops:
            for para in (op.get("old") or []) + (op.get("new") or []):
                k = re.sub(r"\s+", "", str(para))[:40]
                if len(k) < 8:
                    continue
                if k in owner:
                    parent[find(tid)] = find(owner[k])
                else:
                    owner[k] = tid
    return {tid: find(tid) for tid in parent}


_KEY_RE = re.compile(r"[^0-9A-Za-zÀ-ɏͰ-Ͽ一-鿿]+")


def _hint(ch: dict) -> dict:
    """对比页找"这条改在哪一段"用的线索（只留字母、数字、汉字，和页面上 kOf 一样）：这条新加的字、改后那几段的开头。
    生成器的批注号挂不上时（同一段好几条改动挤在一起，这条的字被别条拆开了）靠它把勾贴到那一段上。"""
    k = lambda s: _KEY_RE.sub("", html.unescape(str(s or "")))   # noqa: E731
    ins = [k(s)[:24] for s in (ch.get("ins") or [])]
    heads = [k(p)[:20] for op in (ch.get("ops") or []) for p in (op.get("new") or [])]
    return {"ins": [s for s in ins if len(s) >= 4][:6], "heads": [s for s in heads if len(s) >= 4][:6]}


def _hand_heads(changes: dict) -> dict:
    """手改把哪几条的改动整个盖掉了（changes/hand.json，见 Viewer._hand_into_bases）：{任务 id: [手改后那一段的开头]}。
    这几条的修订标记没了、改出来的字也可能被删了，图钉按它贴到手改后的那一段上。只认改动记录还是当时那一份的（seq 对得上）。"""
    try:
        edits = json.loads((CHANGES_DIR / "hand.json").read_text(encoding="utf-8")).get("edits") or []
    except (OSError, ValueError, AttributeError):
        return {}
    out: dict = {}
    for r in edits:
        if not isinstance(r, dict) or not r.get("new") or not isinstance(r.get("tasks"), dict):
            continue
        h = _KEY_RE.sub("", html.unescape(str(r["new"])))[:20]
        for tid, seq in r["tasks"].items():
            if len(h) >= 4 and (changes.get(str(tid)) or {}).get("seq") == seq:
                out[str(tid)] = [h]                # 同一段又手改过：按最后一次的
    return out


def _briefs(tasks: list, me: str = "") -> list:
    changes = {str(t.get("id")): _load_changes(t.get("id")) for t in tasks}
    spots = _spots(tasks, changes)
    hand = _hand_heads(changes)
    out = []
    for t in tasks:
        if t.get("already") and t.get("status") == "done":
            continue                       # 正文本来就是要的样子、这条没动：论文页上不打勾（2026-11-21 组会 T6、T8）
        b = _task_brief(t, me)
        b["spot"] = spots.get(str(t.get("id")))
        if t.get("status") == "done" and not t.get("note"):
            b["hint"] = _hint(changes.get(str(t.get("id"))) or {})
            if hand.get(str(t.get("id"))):
                b["hint"]["heads"] = hand[str(t.get("id"))] + b["hint"]["heads"]
        out.append(b)
    return out


INJECT_TEXT = {'connecting': ('组会 · 连接中', 'Meeting · connecting'), 'start': ('开始组会', 'Start meeting'), 'unfold_aria': ('展开/收起未修改段落', 'Show or hide unchanged paragraphs'), 'unfold': ('全文', 'Full text'), 'rec_ok': ('已开始录屏', 'Recording started'), 'rec_fail': ('录屏启动失败：请按 Alt+Shift+R', 'Could not start recording. Press Alt+Shift+R'), 'st_queued': ('排队 ', 'Queued '), 'st_working': (' · 修改中 ', ' · editing '), 'st_done': (' · 已修改 ', ' · done '), 'st_failed': (' · 失败 ', ' · failed '), 'st_rebuilding': (' · 更新中…', ' · updating…'), 'down': ('连接已断开', 'Disconnected'), 'toc_head': ('章节', 'Sections'), 'toc_n': (' 节', ''), 'toc_chg': (' 节已修改', ' changed'), 'toc_aria': ('收起/展开章节', 'Collapse or expand sections'), 's_queued': ('排队中', 'Queued'), 's_working': ('修改中', 'Editing'), 's_done': ('已修改', 'Done'), 's_failed': ('修改失败', 'Failed'), 's_note': ('会后改', 'After the meeting'), 's_note_ok': ('已处理', 'Handled'), 'tag_rerun': ('需重跑实验', 'Rerun experiments'), 'tag_data': ('需补数据', 'Needs new data'), 'tag_figure': ('需重画图表', 'Redo figures or tables'), 'tag_lit': ('需查文献', 'Check the literature'), 'tag_discuss': ('待讨论', 'Needs discussion'), 'plan': ('修改方案', 'Plan'), 'need': ('需补充', 'Needs'), 'plan_wait': ('正在生成修改方案…', 'Writing the plan…'), 'st_notes': (' · 标注 ', ' · notes '), 'reject_aria': ('撤回修改', 'Undo change'), 'ok_aria': ('确认修改', 'Accept change'), 'other_a': ('其他 ', ''), 'other_b': (' 句', ' more lines'), 'earlier_a': ('前面 ', ''), 'earlier_b': (' 句', ' earlier lines'), 'undo_fail': ('撤回失败', 'Undo failed'), 'edit': ('编辑', 'Edit'), 'edit_done': ('完成编辑', 'Done editing'), 'not_found': ('文档中未找到此段', 'Paragraph not found in the paper'), 'edit_hint': ('Ctrl+Enter 保存 · Esc 取消', 'Ctrl+Enter to save · Esc to cancel'), 'cancel': ('取消', 'Cancel'), 'save': ('保存', 'Save'), 'not_saved': ('保存失败', 'Save failed'), 'saved': ('已保存', 'Saved'), 'no_backend': ('Bloom 无响应', 'Bloom is not responding'), 'find': ('查找', 'Find'), 'prev': ('上一个', 'Previous'), 'next': ('下一个', 'Next'), 'close': ('关闭', 'Close'), 'zoom_tbl': ('放大表格', 'Enlarge table')}   # 键 → (中文, English)
INJECT_TEXT["quote"] = ("引用", "Quote")     # 选中一段后弹出的按钮（2026-11-21）


def _inject(build: int | None, tasks: list, me: str, figs: dict | None = None, lang: str = "zh",
            paper: str | None = None, readonly: bool = False) -> str:
    """页尾注入：自动刷新 + 章节栏 + 批注标记。build=None 是以往会议的快照：不轮询、不上报、批注只读。
    lang：界面语言（zh / en），注入部分的界面字照它填。paper：这一页是哪篇论文（换版本时读到哪按论文各记各的）。"""
    s = INJECT
    for k, (zh, en) in INJECT_TEXT.items():
        s = s.replace("{{T:" + k + "}}", en if lang == "en" else zh)
    return (s.replace("__BUILD__", "null" if build is None else str(build))
            .replace("__PAPER__", _js(paper or ""))
            .replace("__TASKS__", "/*zh-tasks*/" + _js(_briefs(tasks, me)) + "/*zh-tasks-end*/")
            .replace("__ME__", _js(me or "你"))
            .replace("__AI__", _js(_ai_tag()))
            .replace("__FIGS__", _js(figs or {}))
            .replace("__MJV__", _mathjax_version())) + _comments_inject(lang, readonly)


def _ai_tag() -> dict:
    """批注卡片上"结果"前面署的名：AI 现在的名字（设置里起的，没起按模型来源叫；Viewer.participants 更新 _AI_NAME）和它的颜色。"""
    from . import host_profile
    return {"name": _AI_NAME[0], "color": host_profile.colors()[1]}


def _comments_inject(lang: str, readonly: bool) -> str:
    """第 6 批：论文上打字批注的气泡和卡片（zuhui/comments.py）；readonly = PPT 讲稿，"编辑"按钮去掉。"""
    try:
        from .comments import inject
        return inject(lang, readonly)
    except Exception:
        return ""


def _mathjax_version() -> str:
    """MathJax 的版本号（文件修改时间）：地址带上它，浏览器就能长期缓存那 2 MB。"""
    try:
        return str(int(MATHJAX.stat().st_mtime))
    except OSError:
        return "0"


_GFONTS = re.compile(r'<link[^>]+(?:fonts\.googleapis\.com|fonts\.gstatic\.com)[^>]*>\s*', re.I)
_LOCAL_FONTS: list = []


def _noto_installed() -> bool:
    """这台电脑装了思源宋体 / 黑体（Noto Serif SC / Noto Sans SC）没有。"""
    if not _LOCAL_FONTS:
        dirs = [Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts",
                Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "Microsoft" / "Windows" / "Fonts"]
        found = set()
        for d in dirs:
            try:
                for f in d.iterdir():
                    n = f.name.lower()
                    if n.startswith("notoserifsc"):
                        found.add("serif")
                    elif n.startswith("notosanssc"):
                        found.add("sans")
            except OSError:
                continue
        _LOCAL_FONTS.append(found >= {"serif", "sans"})
    return _LOCAL_FONTS[0]


# 没装 Noto 时的字体：Windows 自带的（雅黑比宋体清楚，等宽字体用 Consolas）
_SYS_FONTS = ("<style>:root{--serif:'Source Han Serif SC','Noto Serif SC','Microsoft YaHei UI','Microsoft YaHei',serif;"
              "--sans:'Microsoft YaHei UI','Microsoft YaHei','Segoe UI',sans-serif}</style>")


def _embed(html_text: str, local_fonts: bool = False) -> str:
    """给 Bloom 的 iframe 用的精简版页面。

    local_fonts：Bloom 窗口里那份（host=bloom）不去 Google Fonts 拿字体，用本机装好的 Noto Serif SC / Noto Sans SC。
    对比页的头里链着 Google Fonts 的 Noto（切成两百来片按字下载，样式表里的同名字体会盖过本机装的那份）：网慢、
    代理时通时不通时，字体片没下完之前正文先用后备的宋体（SimSun）画，字形小一圈、笔画细、英文是等宽的，看着就是
    "字变小了"；后端每次重算页面都整页刷新，又要再等一轮。本机没装这两个字体就照旧用 Google 的（访客那份也照旧）。"""
    if local_fonts:
        # Bloom 窗口里那份一律不链 Google Fonts（国内连不上时整页卡住等字体）：装了 Noto 用 Noto，没装用系统字体
        html_text = _GFONTS.sub("", html_text)
        if not _noto_installed():
            html_text = html_text.replace("</head>", _SYS_FONTS + "</head>", 1)
    if "</head>" in html_text:
        return html_text.replace("</head>", EMBED_CSS + "</head>", 1)
    return EMBED_CSS + html_text


# 访客（手机）那份论文快照：访客页把它放进 iframe，等 iframe 的 onload 才露出来（切论文、每次改稿换新版也一样）。
# onload 要等页面里所有东西：头里渲染阻塞的 Google Fonts 样式表和它按字引出的几十上百个字体分片、公式用的 MathJax（2 MB）、
# 所有插图。这些都挪到 onload 之后再拿：先用手机自带的中文字体把论文显示出来，网好时 Noto 字体换上来（display=swap），
# 公式随后排好，插图滚到附近才拿（tests/guest_paper_bench.py 量过前后）。
_GUEST_FONTS_LATE = ("<script>(function(){function f(){var h=%s;h.forEach(function(u){var l=document.createElement('link');"
                     "l.rel='stylesheet';l.href=u;document.head.appendChild(l);});}"
                     "if(document.readyState==='complete')f();else window.addEventListener('load',function(){setTimeout(f,0);});})();</script>")


def _guest_lite(page: str) -> str:
    """访客快照：字体样式表、MathJax 都等 onload 之后再拿，插图懒加载（见上）。"""
    css = re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="(https://fonts\.googleapis\.com/[^"]+)"[^>]*>', page)
    page = _GFONTS.sub("", page)
    if css:
        late = _GUEST_FONTS_LATE % json.dumps([html.unescape(u) for u in css])
        page = page.replace("</head>", late + "</head>", 1) if "</head>" in page else late + page
    page = page.replace("document.head.appendChild(mj);",
                        "if(document.readyState==='complete')document.head.appendChild(mj);"
                        "else window.addEventListener('load',function(){document.head.appendChild(mj);});/*__GUEST_DEFER_MJ__*/")
    # 访客页（和主持人同一套会议界面）里的这份：共同主持人能在批注上确认 / 撤回、就地改一段，和主持人一样（外壳 bloomCo()）；
    # 页面里调后端的 /api/… 交给外壳 bloomAct 经中继转给主持人那边办（谁能办由主持人那边定）。旧访客页没有这两个函数：照旧只读
    page = page.replace("var LIVE = (null) !== null;", "var LIVE = !!window.__zhCo;", 1)
    if "<head>" in page:
        page = page.replace("<head>", "<head>" + _GUEST_SHIM, 1)
    else:
        page = _GUEST_SHIM + page
    return page.replace("'<img class=\"zh-fig\" alt=\"\">'", "'<img class=\"zh-fig\" alt=\"\" loading=\"lazy\" decoding=\"async\">'")


_GUEST_SHIM = ("<script>(function(){var P=window.parent!==window?window.parent:null;"
               "try{window.__zhCo=!!(P&&P.bloomCo&&P.bloomCo());}catch(e){window.__zhCo=false;}"
               "var of=window.fetch.bind(window);"
               "window.fetch=function(u,o){var s=String((u&&u.url)||u||'');"
               "if(s.indexOf('/api/')!==0)return of(u,o);"
               "var b={};try{b=o&&o.body?JSON.parse(o.body):{};}catch(e){}"
               "var p;try{p=(P&&P.bloomAct)?P.bloomAct(s,b):Promise.resolve({ok:false,error:'read only'});}catch(e){p=Promise.resolve({ok:false});}"
               "return Promise.resolve(p).then(function(d){return new Response(JSON.stringify(d||{}),"
               "{status:200,headers:{'Content-Type':'application/json'}});});};})();</script>")


def _guest_asset_name(name: str, data: bytes) -> str:
    """推到中继的图带上内容的短哈希（fig1.png → fig1-3f2a9c1b7d.png）：两篇论文都有 fig1.png 也不会互相盖掉；
    同一张图名字不变、换了内容名字就变，中继可以让手机长期缓存（relay/token_server.py 给 fig/ 下这种名字发了长缓存头），
    来回切论文、每次改稿换新版都不用再下一遍图。"""
    import hashlib
    p = Path(name)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", p.stem)[:50] or "fig"
    return f"{stem}-{hashlib.sha1(data).hexdigest()[:10]}{p.suffix.lower()}"


def _deep_link(url: str) -> bool:
    """让 Bloom 开始 / 停止录屏（blurt://meeting、blurt://stop）。
    直接把链接交给装好的那个 exe（它是单实例：已经开着就转给开着的那个），不经过注册表里的 blurt:// 关联：
    那个关联曾被一份调试版改成指向它自己，结果每次开会都弹出连不上开发服务器的全屏错误页。"""
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    # 装在哪：安装包装的在 Bloom 文件夹（开源版），早先手装的在 Blurt 文件夹
    # 0.4.0 起程序叫 bloom.exe；更早的安装和手装的还叫 blurt.exe
    exe = next((p for p in (local / "Bloom" / "bloom.exe", local / "Bloom" / "blurt.exe", local / "Blurt" / "blurt.exe") if p.exists()),
               local / "Bloom" / "bloom.exe")
    try:
        if exe.exists():
            flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP
            subprocess.Popen([str(exe), url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
            return True
        os.startfile(url)
        return True
    except Exception:
        return False



def _tts_name() -> str:
    """语音合成：会上实际用的（onboard.tts_label：设置 → 语音 选的 + 这台电脑有的），状态面板上显示。"""
    from .onboard import tts_label
    return tts_label()


# ---------- 回顾：录屏 + 逐字稿对时间 ----------
VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska"}
VIDEO_CAP = 8 * 1024 * 1024     # 开放式 Range（bytes=N-）一次最多给 8 MB，浏览器自己接着要下一段；整份从不读进内存
VIDEO_CHUNK = 256 * 1024


def _read_json(p: Path) -> dict:
    try:
        d = json.loads(Path(p).read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso_epoch(s) -> float | None:
    """ISO 时间 → 秒。不带时区的按本机时间（会议 meta 就是这么存的），带时区的按它自己的。"""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def _session_of(meeting: dict | None) -> Path | None:
    """这场会的录屏文件夹（meta.json 的 session）。"""
    s = (meeting or {}).get("session")
    if not s:
        return None
    p = Path(str(s))
    return p if p.is_dir() else None


def _video_of(sd: Path | None) -> Path | None:
    """录屏文件：会话 meta.json 的 video，没写就找 recording.*。只认会话文件夹里直接放着的那一个。"""
    if not sd:
        return None
    meta = _read_json(sd / "meta.json")
    for n in (str(meta.get("video") or ""), "recording.mp4", "recording.webm", "recording.mov", "recording.mkv"):
        if not n:
            continue
        f = sd / Path(n).name
        if f.suffix.lower() in VIDEO_TYPES and f.is_file():
            return f
    return None


def _tkey(s) -> str:
    """对文字用：只留字母、数字、汉字、假名，小写（标点、空格、大小写、润色加的符号都不算）。"""
    return re.sub(r"[^0-9a-z぀-ヿ一-鿿]+", "", str(s or "").lower())


def _asr_segments(sd: Path) -> list[tuple[float, float, str]]:
    """录屏那一路的实时转写（逐字稿里你说的话就是从它来的），[(开始秒, 结束秒, 归一化文字)]，按结束时间排；
    时间是录屏里的时间。没有实时那份就用散会后整场重转的那份。"""
    out: list[tuple[float, float, str]] = []
    try:
        for raw in (sd / "live" / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(raw)
                out.append((float(x["start"]), float(x["end"]), _tkey(x.get("text"))))
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    if not out:
        for x in _read_json(sd / "transcript.json").get("segments") or []:
            try:
                out.append((float(x["start"]), float(x["end"]), _tkey(x.get("text"))))
            except (ValueError, KeyError, TypeError):
                continue
    out.sort(key=lambda s: s[1])
    return out


def _covers(key: str, seg: str) -> bool:
    """这一段转写是不是这句话的一部分：原样包含，或（润色、纠错改过几个字时）七成的相邻两字都在。"""
    if not seg:
        return False
    if seg in key:
        return True
    if len(seg) < 4:
        return False
    grams = {seg[i:i + 2] for i in range(len(seg) - 1)}
    return sum(1 for g in grams if g in key) >= 0.7 * len(grams)


def align_video_times(lines: list[dict], segs: list[tuple[float, float, str]], offset: float) -> dict:
    """给逐字稿每一句标上它在录屏里从哪一秒开始说（line["vt"]，原地改）。

    逐字稿的 sec 是这句话「写进逐字稿」那一刻（离开会多少秒），比开口晚：要等你说完、停顿判定说完了才落笔，
    一般晚 3~5 秒。offset 是开会时刻减录屏开始时刻（会议 meta 的 started 对会话 meta 的 started_at），
    sec + offset 就是落笔那一刻在录屏里的位置。你说的话再拿文字去对录屏那一路的实时转写，取这句话第一段的开头：
    点一句话，录屏就从你开口那一刻放。对不上的（访客、对不上字的）按对上的那些句子的中位数延迟和语速往回推；
    Claude 的话和系统行就用落笔时刻（Claude 的话是开口时落笔的）。返回对齐统计（测试和报告用）。"""
    import bisect
    import statistics
    ends = [e for _, e, _ in segs]
    used = -1                       # 已经分给前面句子的转写段（按结束时间排的下标），后面的句子不再用
    lags: list[float] = []
    rates: list[float] = []
    later: list[tuple[dict, float]] = []
    matched = 0
    for l in lines:
        if not isinstance(l, dict):
            continue
        try:
            T = float(l.get("in", l.get("sec")) or 0.0) + offset      # in = 落笔时刻（sec 现在是开口时刻）；旧会议没有 in
        except (TypeError, ValueError):
            T = offset
        who = l.get("who")
        if who in ("student", "system"):
            l["vt"] = round(max(0.0, T), 2)
            continue
        key = _tkey(l.get("text"))
        first = last = None
        if who == "teacher" and segs and key:
            i, skipped = bisect.bisect_right(ends, T + 1.0) - 1, 0
            while i > used and i >= 0 and segs[i][1] >= T - 90:
                s = segs[i]
                if last is None:
                    ok = _covers(key, s[2])
                else:
                    # 往前接同一句话的前几段：一句话里两段之间的停顿实测最多 3.5 秒；原样包含才接，
                    # 只是大致对得上的（重复说过的话很容易"对得上"）只接紧挨着的
                    gap = segs[first][0] - s[1]
                    ok = (gap <= 4.0 and bool(s[2]) and s[2] in key) or (gap <= 1.5 and _covers(key, s[2]))
                if ok:
                    last = i if last is None else last
                    first = i
                elif last is not None or skipped >= 2:
                    break
                else:
                    skipped += 1
                i -= 1
        if first is None:
            later.append((l, T))
            continue
        used = last
        st, en = segs[first][0], segs[last][1]
        l["vt"] = round(max(0.0, st), 2)
        matched += 1
        lags.append(T - en)
        if en - st > 0.3:
            rates.append(len(key) / (en - st))
    lag = statistics.median(lags) if lags else 3.5
    cps = statistics.median(rates) if rates else 4.0
    for l, T in later:
        l["vt"] = round(max(0.0, T - lag - len(_tkey(l.get("text"))) / max(cps, 1.0)), 2)
    return {"matched": matched, "estimated": len(later), "lag": round(lag, 2), "cps": round(cps, 2)}


# 本机现在的默认麦克风 / 扬声器（参会人卡片"自己"那一张）：另起一个小进程问 PortAudio（后端进程里的 PortAudio
# 在给访客放声音，不能为了刷新设备表去重启它）；WASAPI 的名字是全名，MME 的会截断。
# inputs：同一个接口下所有的输入设备（设置 → 语音 → 麦克风的下拉，zuhui/mic_prefs.py）
_DEVICE_PROBE = r"""
import json
out = {"mic": None, "speaker": None, "inputs": None}
try:
    import sounddevice as sd
    apis = sd.query_hostapis()
    h = next((a for a in apis if 'WASAPI' in a['name']), None) or apis[sd.default.hostapi]
    for k, i in (("mic", h['default_input_device']), ("speaker", h['default_output_device'])):
        if i is not None and i >= 0:
            out[k] = sd.query_devices(i)['name']
    names = []
    for i in h['devices']:
        d = sd.query_devices(i)
        if d['max_input_channels'] > 0 and d['name'] not in names:
            names.append(d['name'])
    out["inputs"] = names
except Exception:
    pass
print(json.dumps(out, ensure_ascii=False))
"""

class Viewer:
    def __init__(self, cfg: dict | None = None, port: int = PORT, board=None, meetings=None):
        self.cfg = cfg or load()
        self.port = port
        self.mirror = paper_path(self.cfg)
        self.base = base_for(self.mirror)
        self.papers: list[dict] = []        # 这场会摊开过的论文（app 开会、加论文时设；散会后留着给散会快照用）
        self.board = board or Board()
        if meetings is None:
            from .meetings import Meetings
            meetings = Meetings()
        self.meetings = meetings
        self.lock = threading.Lock()
        self._gen_lock = threading.RLock()   # 生成器一次只跑一份（实时页和散会快照共用输出文件）
        self.build = 0
        self.building = False
        # 占位页也带轮询脚本：第一版生成好会自己刷新过去
        self.html = PLACEHOLDER.format(msg="正在准备文档…").replace("</body>", _inject(0, [], "") + "</body>", 1)
        self._sig = None
        self._stop = threading.Event()
        self.ui_lang = "zh"                  # Bloom 窗口的界面语言（iframe 地址带 ui=zh / en）：对比页的界面字跟着它
        self.status: dict = {}              # app 填：fast_ok（对话模型有没有应答）、fast_ms（上一句首字延迟）
        self.focus: dict | None = None      # 对比页报上来的：屏幕上正显示哪一节、哪一段开头
        self.guests: list[dict] = []        # 通过邀请链接进来的人（中继接上后填）
        from .share import Share
        self.share = Share(self.cfg)        # 邀请链接 + 中继上谁进来了
        for d in (VIEW_DIR, BASE_DIR, CHANGES_DIR, AVATARS):
            d.mkdir(parents=True, exist_ok=True)
        self._adopt_legacy_base()

    # ---------- 原文快照 ----------
    @property
    def base0(self) -> Path:
        """开会那一刻的原文，整场不动：散会快照拿它比，打过勾的改动也都在。self.base 是"还没看过的改动"的基线，打勾会往前挪。"""
        return Path(str(self.base) + ".orig")

    def _adopt_legacy_base(self) -> None:
        """基线以前按文件名存（base/<文件名>），现在按论文编号存（base/<编号>-<文件名>）。升级后第一次起来：
        当前这篇还没有新名字的基线、旧的在，就把旧的接过来（会开到一半升级，原文基线不丢）。"""
        old = BASE_DIR / self.mirror.name
        try:
            if not self.base.exists() and old.is_file():
                shutil.copyfile(old, self.base)
                if Path(str(old) + ".orig").is_file() and not self.base0.exists():
                    shutil.copyfile(Path(str(old) + ".orig"), self.base0)
        except OSError:
            pass

    # ---------- 会中几篇论文：每条任务只在它那一篇里算 ----------
    def _first_paper(self) -> str | None:
        """这场会第一篇论文的编号（旧任务没记是哪篇：算它的）。"""
        ps = getattr(self, "papers", None) or []
        return ps[0].get("id") if ps else None

    def _paper_tasks(self, tid: str) -> list[dict]:
        """和 tid 同一篇论文上的任务。撤回、重放、打勾只在这一篇里算：别篇的改动记录（整篇的改前改后）
        混进来，会拿别篇的全文去重放，把这篇整个冲掉。"""
        tasks = self.board.tasks()
        me = next((t for t in tasks if t.get("id") == tid), None)
        if me is None:
            return tasks
        first = self._first_paper()
        k = task_paper(me, first)
        return [t for t in tasks if task_paper(t, first) == k]

    def _files_of(self, t: dict | None) -> tuple[Path, Path]:
        """这条任务改的那篇：(稿子, "还没看过的改动"的基线)。旧任务没记是哪篇：算这场会第一篇（升级前开的会开到一半
        加了论文时，屏幕上可能已经是别篇：按屏幕上那篇撤，会把这条记成"已撤回"、它自己那篇却没动）；
        连第一篇都不知道（只有一篇的老样子），或者就是屏幕上这篇：用当前镜像。"""
        pp = (t or {}).get("paper_path")
        if not pp:
            ps = getattr(self, "papers", None) or []
            pp = ps[0].get("path") if ps else None
        if pp and not _same_file(pp, self.mirror):
            return Path(pp), base_for(pp)
        return self.mirror, self.base

    def _active_tasks(self) -> list[dict]:
        """屏幕上这篇论文的任务：对比页的批注、修订标记、"还没落到正文里的"只画这一篇的。"""
        return self._tasks_of(self.mirror)

    def _tasks_of(self, mirror) -> list[dict]:
        """某一篇论文上的任务（_active_tasks 是屏幕上那篇；预先做别篇的页面时用别篇）。"""
        tasks = self.board.tasks()
        try:
            me = paper_id(mirror)
        except Exception:
            return tasks
        first = self._first_paper()
        return [t for t in tasks if task_paper(t, first) in (None, me)]

    def _name_of(self, mirror) -> str:
        """别的一篇在页面上的名字：会上记的名字（papers），没有就用文件名。"""
        for p in getattr(self, "papers", None) or []:
            if p.get("path") and _same_file(p["path"], mirror):
                return str(p.get("name") or Path(mirror).stem)
        return Path(mirror).stem

    def _doc_name(self) -> str:
        """对比页上这篇的名字：导入时起的名字（current_paper.json），没有就用文件名。
        以前一直取 config 里那篇的标题，换了论文对比页上还写着"FooBench 论文合稿"。"""
        try:
            return str(self.paper_info().get("name") or self.mirror.stem)
        except Exception:
            return self.mirror.stem

    def rebase_paper(self, path) -> None:
        """别的一篇（不在屏幕上）以现在的稿子为原文：开会时单子上的每一篇都从这一刻比起。"""
        p = Path(str(path))
        if not p.is_file():
            return
        b = base_for(p)
        with _DOC_LOCK:
            _copy_doc(p, b)
            _copy_doc(b, Path(str(b) + ".orig"))
            try:
                self._own_of(b).unlink(missing_ok=True)  # 上一场的"原文 + 手改"作废
            except OSError:
                pass

    def ensure_base(self, rebase: bool = False) -> None:
        with _DOC_LOCK:                         # 别和打勾、撤回同时改基线；稿子正被工人写到一半时读到完整的再拷
            if self.mirror.exists() and (rebase or not self.base.exists()):
                _copy_doc(self.mirror, self.base)
                _copy_doc(self.base, self.base0)
                try:
                    self._own_of(self.base).unlink(missing_ok=True)
                except OSError:
                    pass
            elif self.base.exists() and not self.base0.exists():
                _copy_doc(self.base, self.base0)

    def me_name(self) -> str:
        try:
            return str(self.participants()[0]["name"])
        except Exception:
            return "你"

    # ---------- 打勾：看过了 ----------
    @staticmethod
    def _spans(s: str) -> list[tuple[int, int, str]]:
        """按空行切段，留着每段在原文里的位置（改的时候别的字节一个都不动）。"""
        out, pos = [], 0

        def add(chunk: str, at: int) -> None:
            k = chunk.strip()
            if k:
                a = at + (len(chunk) - len(chunk.lstrip()))
                out.append((a, a + len(k), k))

        for m in re.finditer(r"\n\s*\n", s):
            add(s[pos:m.start()], pos)
            pos = m.end()
        add(s[pos:], pos)
        return out

    @classmethod
    def _apply_para_ops(cls, text: str, ops: list) -> str | None:
        """按段落把一条任务的改动（ops：old → new，hands._capture_changes 记的）落到 text（\\n 换行）上。
        有一处对不上就整条不落（返回 None），不留半截。同样的几段出现好几次（"签名：""时间："）：挑前一段也对得上的那一处。"""
        nl = lambda s: str(s).replace("\r\n", "\n")    # noqa: E731
        for op in ops:
            old = [nl(x) for x in (op.get("old") or [])]
            new = [nl(x) for x in (op.get("new") or [])]
            prev = nl(op["prev"]) if op.get("prev") is not None else None
            spans = cls._spans(text)
            keys = [k for _, _, k in spans]
            if old:
                hits = [i for i in range(len(keys) - len(old) + 1) if keys[i:i + len(old)] == old]
                if not hits:
                    return None
                at = next((i for i in hits if (keys[i - 1] if i > 0 else None) == prev), hits[0])
                a, b = spans[at][0], spans[at + len(old) - 1][1]
                if new:
                    text = text[:a] + "\n\n".join(new) + text[b:]
                else:                                   # 这几段删掉了：连带多出来的空行
                    text = (text[:a].rstrip("\n") + "\n\n" + text[b:].lstrip("\n")) if text[:a].strip() else text[b:].lstrip("\n")
            elif new:
                if prev is None:
                    text = "\n\n".join(new) + "\n\n" + text.lstrip("\n")
                elif prev in keys:
                    hits = [i for i, k in enumerate(keys) if k == prev]
                    b = spans[hits[0]][1]
                    text = text[:b] + "\n\n" + "\n\n".join(new) + text[b:]
                else:
                    return None
        return text

    def _apply_tok_change(self, text: str, ch: dict) -> str | None:
        """段落对不上时（同一段前面还压着没打勾的改动，或者前一条被撤了）：按词把这条的"改前 → 改后"落到基线上
        （撤回用的同一套按词对齐）。要换掉的词都得原样连着在，插入点两边的词都得在、而且挨着；有一处不满足就不落。"""
        if ch.get("before") is None or ch.get("after") is None:
            return None
        nl = lambda s: str(s or "").replace("\r\n", "\n")    # noqa: E731
        b, a, st = self._toks(nl(ch["before"])), self._toks(nl(ch["after"])), self._toks(text)
        diffs = [op for op in self._tok_align(b, a) if op[0] != "equal"]
        if not diffs:
            return None
        m = self._tok_map(b, st)
        for tag, i1, i2, j1, j2 in diffs:
            if i1 == i2:
                L = m.get(i1 - 1) if i1 > 0 else -1
                R = m.get(i1) if i1 < len(b) else len(st)
                if L is None or R is None or R != L + 1:
                    return None
        plan = self._tok_plan_with(m, b, a, st, True, ops=diffs)
        return None if plan is None else "".join(self._tok_apply(st, plan))

    def _accept_into_base(self, tid: str) -> bool:
        """把这条任务的改动并进"还没看过"的基线：对比页上它的修订标记随之消失。先按段落找原来那几段换成改后的；
        段落对不上就按词并；都不行就不动（返回 False，等它前面那条也打了勾再并，见 _retry_accepts）。
        读改写拿着 _DOC_LOCK（同一处几条一起打勾时请求是同时来的）；换行（CRLF / LF）和 BOM 照原样写回，先写临时文件再换上去。"""
        with _DOC_LOCK:
            ch = self._changes(tid)
            ops = ch.get("ops") or []
            mirror, base = self._files_of(self.board.get(tid))     # 这条改的那一篇和它的基线（不一定是屏幕上这篇）
            if not ops or not base.exists():
                return False
            try:
                text, bom, nl = _read_doc(base)
            except DocReadError:                     # 基线坏了：按原文和打过勾的重建一份再并
                if not self._heal_base(mirror):
                    return False
                text, bom, nl = _read_doc(base)
            keep, gone = self._hand_guard(mirror, tid)
            out = self._apply_para_ops(text, ops)
            if out is None and (keep or gone):
                # 这条改的段落有的后来被手改过（底稿里已经是作者打的字）：那几处不用再并，只并别处
                mine = list(keep) + gone
                rest = [op for op in ops if not any(self._ratio(str(p), k) >= 0.5 for k in mine
                                                    for p in (op.get("old") or []) + (op.get("new") or []))]
                if len(rest) < len(ops):
                    out = self._apply_para_ops(text, rest) if rest else text
            if out is None:
                out = self._apply_tok_change(text, ch)
            if out is None:
                return False
            # 手改过的段落：别把这条的字再塞回去（不然作者的段落上又冒出删除线）
            out = self._keep_paras(text, out, keep, gone)
            if out != text:
                _write_doc(base, out, bom, nl)
            return True

    # ---------- 图 ----------
    def figures(self, mirror=None) -> dict:
        """论文文件夹 figures/ 下的图：{"1": {"src": "/figures/fig1.png", "kind": "img"|"html"}}。"""
        d = Path(mirror if mirror is not None else self.mirror).parent / "figures"
        out: dict = {}
        if not d.is_dir():
            return out

        def put(no, f: Path) -> None:
            if f.is_file() and f.suffix.lower() in FIG_TYPES and str(no) not in out:
                # ?v=修改时间：浏览器可以一直缓存这张图（论文页每次刷新不用重拿），图换了地址就变
                out[str(no)] = {"src": "/figures/" + f.name + f"?v={int(f.stat().st_mtime)}",
                                "kind": "html" if f.suffix.lower() in (".html", ".htm") else "img"}

        try:
            meta = json.loads((d / "figures.json").read_text(encoding="utf-8"))
            for k, v in (meta or {}).items():
                put(k, d / str((v or {}).get("file") or ""))
        except (OSError, ValueError, AttributeError):
            pass
        for f in sorted(d.glob("*")):
            m = re.match(r"(?:fig|figure|图)[\s_-]*(\d+)\.[A-Za-z]+$", f.name, re.I)
            if m:
                put(int(m.group(1)), f)
        return out

    # ---------- 你自己改一段 ----------
    on_edit = None  # app 设置：稿子被手改之后刷新它脑子里的论文
    on_hand_edit = None  # app 设置：(文件, 原段, 新段)，手改时告诉改稿工人（hands.note_hand_edit）

    @staticmethod
    def _key(s: str) -> str:
        """比对用：只留字母、数字、汉字（Markdown 记号、转义、空白、标点都不算）。"""
        return re.sub(r"[^0-9A-Za-z\u00C0-\u024F\u0370-\u03FF\u4e00-\u9fff]+", "", html.unescape(s or ""))

    def para_source(self, text: str) -> dict:
        """页面上的一段 → 它在稿子里的原文（Markdown / LaTeX 源码）和段落序号。"""
        from .pptx_conv import READONLY_SAY, readonly
        if readonly(self.mirror):                     # 第 6 批：PPT 讲稿只能看
            return {"ok": False, "error": READONLY_SAY[1 if getattr(self, "ui_lang", "zh") == "en" else 0]}
        want = self._key(text)
        if len(want) < 2:
            return {"ok": False, "error": "段落为空"}
        try:
            src = _read_doc(self.mirror)[0]
        except (OSError, DocReadError, UnicodeDecodeError) as e:
            return {"ok": False, "error": f"读取文档失败：{e}"}
        spans = self._spans(src)
        keys = [self._key(k) for _, _, k in spans]
        idx = next((i for i, k in enumerate(keys) if k == want), -1)
        if idx < 0:
            import difflib
            best, score = -1, 0.0
            for i, k in enumerate(keys):
                if not k or abs(len(k) - len(want)) > max(16, int(len(want) * 0.3)):
                    continue
                sm = difflib.SequenceMatcher(None, k, want, autojunk=False)
                if sm.quick_ratio() < 0.82:
                    continue
                sc = sm.ratio()
                if sc > score:
                    best, score = i, sc
            if score >= 0.82:
                idx = best
        if idx < 0:
            return {"ok": False, "error": "文档中未找到此段"}
        return {"ok": True, "index": idx, "source": spans[idx][2].replace("\r\n", "\n")}

    def para_save(self, index, old: str, new: str, by: str | None = None) -> dict:
        """把第 index 段从 old 改成 new（new 为空 = 删掉这一段）。别的字节一个不动（换行统一成 \\n 改，写回时 CRLF / LF、BOM 照原样）。
        存好了在逐字稿上记一行"谁改了哪一段"（doc_edits）；by：共同主持人（访客）改的时是他的名字，不给就是主持人自己。"""
        norm = lambda s: (s or "").replace("\r\n", "\n").strip()    # noqa: E731
        old_n, new_n = norm(old), norm(new)
        from .pptx_conv import READONLY_SAY, readonly
        if readonly(self.mirror):                     # 第 6 批：PPT 讲稿只能看
            return {"ok": False, "error": READONLY_SAY[1 if getattr(self, "ui_lang", "zh") == "en" else 0]}
        with _DOC_LOCK:
            try:
                src, bom, nl = _read_doc(self.mirror)
            except (OSError, DocReadError, UnicodeDecodeError) as e:
                return {"ok": False, "error": f"读取文档失败：{e}"}
            spans = self._spans(src)
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = -1
            if not (0 <= index < len(spans)) or norm(spans[index][2]) != old_n:
                index = next((i for i, (_, _, k) in enumerate(spans) if norm(k) == old_n), -1)   # 稿子在你编辑时被改过：按原文再找
                if index < 0:
                    return {"ok": False, "error": "此段已被修改，请重新打开"}
            a, b, cur = spans[index]
            if new_n == old_n:
                return {"ok": True, "changed": False}
            try:
                _write_doc(self.mirror, src[:a] + new_n + src[b:], bom, nl)
            except OSError as e:
                return {"ok": False, "error": f"保存失败：{e}"}
            if self.on_hand_edit:                     # 改稿工人正在改这篇：这处手改不算进它的改动（撤回那条时不会连带撤掉）
                try:
                    self.on_hand_edit(self.mirror, cur, new_n)
                except Exception:
                    pass
            # 你自己打的字不算待看的修订：底稿里对应的那段换成同样的新文字，这一段不再有修订标记（见 _hand_into_bases）
            self._hand_into_bases(src.replace("\r\n", "\n"), index, cur, new_n, by)
        self._sig = None
        try:                                          # 逐字稿上记一行：AI（快嘴、改稿工人、/bloom）都看得到改了哪里
            from . import doc_edits
            doc_edits.from_viewer(self, src.replace("\r\n", "\n"), index, cur, new_n, by=by)
        except Exception:
            pass
        if self.on_edit:
            try:
                self.on_edit()
            except Exception:
                pass
        return {"ok": True, "changed": True}

    # ---------- 手改并进底稿（2026-11-21） ----------
    # 第二场组会：5.2 第（1）段先被两条任务改过（都没打勾），作者又在编辑框里把这一段整段重写了，保存后整段标成修订
    # （"这是我自己打进去的字"）。以前只有底稿里原样找得到这一段才跟着改，被 AI 改过的段落底稿里还是改前的样子，找不到。
    # 现在按段落对齐找底稿里对应的那段（几段被并成一段的：那几段），换成同样的新文字；AI 新加的段插到对齐的位置。
    # 开会时的原文（.orig）不动，另存一份"原文 + 手改"（.own）给散会快照比：散会的修订对比里只有 AI 的改动。
    # 每次手改记进 changes/hand.json：之后撤回、打勾更早的任务时，手改过的段落原样留着（不撤掉作者的字，也不把 AI 的字塞回底稿）。
    PAIR_MIN = 0.35        # 两段算"同一段改过"的相似度下限（和对比页生成器配对段落的门槛一样）

    @staticmethod
    def _hand_file() -> Path:
        return CHANGES_DIR / "hand.json"

    @staticmethod
    def _own_of(base) -> Path:
        """"开会时的原文 + 会上手改"那一份：散会快照从它比起。"""
        return Path(str(base) + ".own")

    @staticmethod
    def _own_ok(base) -> bool:
        """.own 能不能用：在，而且不比开会时的原文旧（原文后来整份换过，比如 PDF 换成完整版：旧的作废）。"""
        own, orig = Path(str(base) + ".own"), Path(str(base) + ".orig")
        try:
            return own.is_file() and (not orig.is_file() or own.stat().st_mtime_ns >= orig.stat().st_mtime_ns)
        except OSError:
            return False

    @classmethod
    def _words(cls, s: str) -> list[str]:
        return [t for t in cls._toks(s) if not t.isspace()]

    @classmethod
    def _ratio(cls, a: str, b: str) -> float:
        """两段有多像（按词比，汉字一个字算一个词，不算空白）。低于 PAIR_MIN 的一律算 0。"""
        import difflib
        ta, tb = cls._words(a), cls._words(b)
        if not ta or not tb:
            return 0.0
        sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
        if sm.real_quick_ratio() < cls.PAIR_MIN or sm.quick_ratio() < cls.PAIR_MIN:
            return 0.0
        return sm.ratio()

    @classmethod
    def _covered(cls, part: str, whole: str) -> float:
        """part 的字有多少成片地（连着三个词以上）在 whole 里：几段被并成一段时，并进去的那几段这个比例高。太短的段不算。"""
        import difflib
        tp, tw = cls._words(part), cls._words(whole)
        if len(tp) < 6:
            return 0.0
        blocks = difflib.SequenceMatcher(None, tp, tw, autojunk=False).get_matching_blocks()
        return sum(b.size for b in blocks if b.size >= 3) / len(tp)

    @classmethod
    def _para_pairs(cls, a: list[str], b: list[str]) -> dict:
        """a 的第几段对应 b 的第几段：一模一样的先整段对上，剩下的在同一处里按相似度配（不交叉），认出改过的段落。"""
        import difflib
        pairs: dict = {}
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "equal":
                pairs.update({i1 + k: j1 + k for k in range(i2 - i1)})
                continue
            if tag != "replace":
                continue
            wide = (i2 - i1) * (j2 - j1) > 1600              # 一大片都改了：只比位置相近的
            cand = []
            for i in range(i1, i2):
                for j in range(j1, j2):
                    if wide and abs((i - i1) - (j - j1)) > 20:
                        continue
                    r = cls._ratio(a[i], b[j])
                    if r >= cls.PAIR_MIN:
                        cand.append((r, i, j))
            got: list = []
            for r, i, j in sorted(cand, key=lambda x: (-x[0], x[1], x[2])):
                if i in pairs or any(j == y or (i < x) != (j < y) for x, y in got):
                    continue
                pairs[i] = j
                got.append((i, j))
        return pairs

    @staticmethod
    def _cut_para(text: str, a: int, b: int) -> str:
        """删掉 text[a:b] 这一段，连带多出来的空行。"""
        if not text[:a].strip():
            return text[b:].lstrip("\n")
        if not text[b:].strip():
            return text[:a].rstrip("\n") + text[b:]
        return text[:a].rstrip("\n") + "\n\n" + text[b:].lstrip("\n")

    @classmethod
    def _sync_para(cls, btext: str, mtext: str, index: int, new: str) -> str:
        """mtext（稿子）的第 index 段在 btext（底稿）里对应的那段（几段被 AI 并成这一段的：那几段）换成 new，new 为空就删掉。
        底稿里没有对应的（AI 新加的段）：插在前一段对应的那段后面。都是 \\n 换行的文字。"""
        bs, ms = cls._spans(btext), cls._spans(mtext)
        if not (0 <= index < len(ms)):
            return btext
        bk, mk = [k for _, _, k in bs], [k for _, _, k in ms]
        pairs = cls._para_pairs(bk, mk)
        inv = {j: i for i, j in pairs.items()}
        if index in inv:
            lo = hi = inv[index]
            while lo > 0 and lo - 1 not in pairs and cls._covered(bk[lo - 1], mk[index]) >= 0.6:
                lo -= 1
            while hi + 1 < len(bk) and hi + 1 not in pairs and cls._covered(bk[hi + 1], mk[index]) >= 0.6:
                hi += 1
            a, b = bs[lo][0], bs[hi][1]
            return btext[:a] + new + btext[b:] if new else cls._cut_para(btext, a, b)
        if not new:
            return btext                              # 删掉的是底稿里本来就没有的段（AI 加的）：底稿不用动
        prev = next((inv[j] for j in range(index - 1, -1, -1) if j in inv), None)
        if prev is None:
            at = bs[0][0] if bs else len(btext)
            return btext[:at] + new + ("\n\n" if bs else "\n") + btext[at:]
        b = bs[prev][1]
        return btext[:b] + "\n\n" + new + btext[b:]

    @classmethod
    def _keep_paras(cls, old: str, new: str, keep, gone=()) -> str:
        """new 是 old 被撤回 / 并进改动之后的样子。old 里手改过的段落（keep）在 new 里被动了的，原样放回去
        （被拆出去的碎段一起拿掉）；手删掉的段落（gone）被放回来的，再拿掉。别处照 new。"""
        keep = {k for k in keep or () if k}
        gone = [g for g in gone or () if g]
        if old == new or not (keep or gone):
            return new
        os_, ns = cls._spans(old), cls._spans(new)
        ok_, nk = [k for _, _, k in os_], [k for _, _, k in ns]
        pairs = cls._para_pairs(ok_, nk)
        inv = {j: i for i, j in pairs.items()}
        # 手改后又被后面的任务改过的（还是那一段，只是不再一字不差）也算
        held = {i for i, k in enumerate(ok_) if k in keep or any(cls._ratio(k, x) >= 0.6 for x in keep)}
        ops = []                                        # (起, 止, 换成的字 / None = 删掉这一段)，落在 new 上
        for i in sorted(held):
            j = pairs.get(i)
            if j is not None:
                if nk[j] != ok_[i]:
                    ops.append((ns[j][0], ns[j][1], ok_[i]))
                continue
            prev = next((pairs[x] for x in range(i - 1, -1, -1) if x in pairs), None)     # 被拿掉了：放回原处
            ops.append((0, 0, ok_[i] + "\n\n") if prev is None else (ns[prev][1], ns[prev][1], "\n\n" + ok_[i]))
        for j, k in enumerate(nk):
            if j in inv:
                continue
            side = [next((inv[x] for x in range(j - 1, -1, -1) if x in inv), None),
                    next((inv[x] for x in range(j + 1, len(nk)) if x in inv), None)]
            if (any(i in held and cls._covered(k, ok_[i]) >= 0.6 for i in side if i is not None)
                    or any(cls._ratio(k, g) >= 0.5 for g in gone)):
                ops.append((ns[j][0], ns[j][1], None))
        for a, b, rep in sorted(ops, key=lambda o: (o[0], o[1]), reverse=True):
            new = cls._cut_para(new, a, b) if rep is None else new[:a] + rep + new[b:]
        return new

    def _hand_edits(self) -> list[dict]:
        try:
            d = json.loads(self._hand_file().read_text(encoding="utf-8"))
            return [r for r in (d.get("edits") or []) if isinstance(r, dict)]
        except (OSError, ValueError, AttributeError):
            return []

    def _hand_guard(self, paper, tid: str) -> tuple[set, list]:
        """tid 改完之后才手改过的段落：(手改后的段落, 手删掉的段落)。撤回、打勾 tid 时这些段落原样留着。"""
        try:
            seq = float(self._changes(tid).get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0.0
        try:
            pid = paper_id(paper)
        except Exception:
            pid = None
        keep, gone = set(), []
        for r in self._hand_edits():
            if (pid and r.get("paper") and r.get("paper") != pid) or float(r.get("seq") or 0) <= seq:
                continue
            if r.get("new"):
                keep.add(str(r["new"]))
            elif r.get("old"):
                gone.append(str(r["old"]))
        return keep, gone

    def _hand_swallowed(self, cur: str) -> dict:
        """改过这一段、还没打勾的任务（这次手改把它们在这一段的改动盖掉了）：{任务: 它改动记录的 seq}。
        对比页拿它把这几条的图钉贴到手改后的这一段上（它们的修订标记没了，按改出来的字找不到）。"""
        out = {}
        for t in self._active_tasks():
            if t.get("status") != "done" or t.get("note") or t.get("resolved"):
                continue
            ch = self._changes(t["id"])
            news = [str(p).replace("\r\n", "\n").strip() for op in ch.get("ops") or [] for p in op.get("new") or []]
            if (any(p == cur or self._ratio(p, cur) >= 0.6 for p in news if p)
                    or any(len(str(s)) >= 4 and str(s) in cur for s in ch.get("ins") or [])):
                out[t["id"]] = ch.get("seq")
        return out

    def _hand_into_bases(self, src: str, index: int, cur: str, new: str, by: str | None) -> None:
        """para_save 存好一段之后调（拿着 _DOC_LOCK）：底稿和 .own 里对应的那段换成 new，记一笔手改。
        src：保存之前的整篇（\\n 换行）；index、cur：改的是它的第几段、原来的字。读写都按原来的换行和 BOM。"""
        own = self._own_of(self.base)
        try:
            if not self._own_ok(self.base) and self.base0.is_file():
                _copy_doc(self.base0, own)              # 这场会第一次手改：从开会时的原文起
        except (OSError, DocReadError):
            pass
        for path in (self.base, own):
            try:
                text, bom, nl = _read_doc(path)
                out = self._sync_para(text, src, index, new)
                if out != text:
                    _write_doc(path, out, bom, nl)
            except (OSError, DocReadError, UnicodeDecodeError):
                continue
        try:
            pid, edits = paper_id(self.mirror), self._hand_edits()[-199:]
            tasks = {}
            for r in edits:                             # 同一段接着手改：上一次盖掉的那几条照样算这一段的
                if r.get("paper") == pid and r.get("new") == cur and isinstance(r.get("tasks"), dict):
                    tasks.update(r["tasks"])
            tasks.update(self._hand_swallowed(cur))
            edits.append({"paper": pid, "seq": time.time(), "old": cur, "new": new,
                          "by": " ".join(str(by or "").split())[:32], "tasks": tasks})
            CHANGES_DIR.mkdir(parents=True, exist_ok=True)
            _write_bytes_atomic(self._hand_file(), json.dumps({"edits": edits}, ensure_ascii=False, indent=1).encode("utf-8"))
        except Exception:
            pass

    @staticmethod
    def _revert_text(text: str, ops: list) -> tuple[str, int]:
        """把一条任务的段落级改动（ops：old → new）从 text 里撤掉。那几段还是改完的样子：整段换回去；
        后来又被别的任务改过：按字只把这条改的那几处换回去，别人改的留着（对不上的那一处不动）。返回 (新文本, 撤成了几处)。"""
        import difflib

        def spans(s):
            return Viewer._spans(s)

        def three_way(cur: str, new: str, old: str) -> str | None:
            """cur 是 new 后来又被改过的样子：把 new → old 的那几处改动挪到 cur 上。有一处挪不过去就返回 None。"""
            al = difflib.SequenceMatcher(None, new, cur, autojunk=False).get_matching_blocks()

            def where(i: int, end: bool = False) -> int | None:
                for b in al:
                    if b.size and (b.a <= i < b.a + b.size or (end and b.a < i <= b.a + b.size)):
                        return b.b + (i - b.a)
                    if b.size and i == b.a + b.size and not end:
                        return b.b + b.size                 # 插入点在一段没变的字的末尾
                return None

            edits = []
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, new, old, autojunk=False).get_opcodes():
                if tag == "equal":
                    continue
                a = where(i1)
                b = a if i1 == i2 else where(i2, end=True)
                if a is None or b is None or b < a:
                    return None
                edits.append((a, b, old[j1:j2]))
            for a, b, rep in sorted(edits, reverse=True):
                cur = cur[:a] + rep + cur[b:]
            return cur

        done = 0
        for op in reversed(ops or []):
            old = [x.replace("\r\n", "\n") for x in (op.get("old") or [])]
            new = [x.replace("\r\n", "\n") for x in (op.get("new") or [])]
            prev = (op.get("prev") or "").replace("\r\n", "\n") or None
            sp = spans(text)
            keys = [k for _, _, k in sp]
            if new:
                n = len(new)
                at = next((i for i in range(len(keys) - n + 1) if keys[i:i + n] == new), -1)
                rep = "\n\n".join(old)
                if at < 0:                                   # 后来又被改过：找最像的那几段，按字撤
                    nj = "\n\n".join(new)
                    best, score = -1, 0.0
                    for i in range(len(keys) - n + 1):
                        r = difflib.SequenceMatcher(None, nj, "\n\n".join(keys[i:i + n]), autojunk=False).ratio()
                        if r > score:
                            best, score = i, r
                    if best < 0 or score < 0.6:
                        continue
                    merged = three_way("\n\n".join(keys[best:best + n]), nj, rep)
                    if merged is None:
                        continue
                    at, rep = best, merged
                a, b = sp[at][0], sp[at + n - 1][1]
                if rep:
                    text = text[:a] + rep + text[b:]
                else:                                        # 这条是新加的段落：删掉，连带多出来的空行
                    text = (text[:a].rstrip("\n") + "\n\n" + text[b:].lstrip("\n")) if text[:a].strip() else text[b:].lstrip("\n")
                done += 1
            elif old:                                        # 这条删了几段：放回原处（跟在原来的前一段后面）
                if prev is None:
                    text = "\n\n".join(old) + "\n\n" + text
                elif prev in keys:
                    b = sp[keys.index(prev)][1]
                    text = text[:b] + "\n\n" + "\n\n".join(old) + text[b:]
                else:
                    continue
                done += 1
        return text, done

    # ---------- 撤回：按词级来源整篇重放（2026-11-13） ----------
    # 老办法（_rollback_text_ops）按段落记改动、按字三方合并，撤销测试里有两类错（evidence/undo_test_before.md）：
    #   ① 后一条插在被撤那条新加的句子里，按字对齐时被对到别处的同一个句号上，拼出 "training and data."；
    #   ② 先拆段再改后半段，撤拆段时那条独立修改被静默冲掉（段落列表不完全相等就连不成链）。
    # 新办法：改稿时存下每条任务改前改后的整篇（hands._capture_changes 的 before/after），撤回时从第一条任务改前的稿子起，
    # 按改稿先后把没撤的任务一条条重新改一遍。每条的改动是"改前 → 改后"按词的差别；落到重放中的稿子上时，
    # 先把它的"改前"和当前稿子按词对齐：要删的词、插入点两边的词，只要有一个对不上（是被撤或已连带撤下的任务加的），
    # 这条就整条连带撤下（按任务，跨段也一致），照实返回。两边都对得上但中间隔开了（被撤那条删掉的词放回来了）：插在右侧锚点前面。
    # 你手改的部分（重放结果和现在稿子的差别）当成不可撤的伪任务，最后搬回去。没存全文的旧记录退回老办法。
    _TOK = re.compile(r"\s+|[A-Za-z0-9]+|[一-鿿]|.", re.S)

    _TOK_CACHE: dict = {}                         # 切过词的全文（同一场会里每次撤回都要把各条改前改后的整篇切一遍）

    @classmethod
    def _toks(cls, s: str) -> list[str]:
        s = s or ""
        if len(s) < 2000:
            return cls._TOK.findall(s)
        c = cls._TOK_CACHE
        if s not in c:
            if len(c) > 64:                           # 最多留 64 份（一场会几十条任务够用），满了清掉重来
                c.clear()
            c[s] = cls._TOK.findall(s)
        return list(c[s])

    _ALIGN_CTX = 1                                # _tok_align 跳过前后相同部分时，两边各多带一段上下文（不带的话，加段 + 并段交织时对齐变差：撤销测试 99.5% → 99.2%）

    @classmethod
    def _tok_align(cls, a: list[str], b: list[str]) -> list[tuple]:
        """a → b 的按词 opcodes。先按段落块对齐、再在改了的块里按词对齐：长文快，也不会把相隔很远的同一个标点对到一起。
        前后一模一样的部分先整块跳过（退到段落边界），只对中间改过的那几段对齐：整篇 7 万词、只改一处时快几十倍。"""
        import difflib

        sep = lambda t: "\n" in t and t.count("\n") >= 2 and not t.strip()   # noqa: E731  段落分隔（空白里有两个换行）
        na, nb = len(a), len(b)
        lim = min(na, nb)
        p, step = 0, 4096                            # 相同的开头：先按大块比（列表切片比较走 C），对不上再缩小步子
        while step:
            while p + step <= lim and a[p:p + step] == b[p:p + step]:
                p += step
            step //= 8
        while p < lim and a[p] == b[p]:
            p += 1
        if p == na == nb:
            return [("equal", 0, na, 0, nb)]
        s, step = 0, 4096                            # 相同的结尾：同样
        while step:
            while s + step <= lim - p and a[na - s - step:na - s] == b[nb - s - step:nb - s]:
                s += step
            step //= 8
        while s < lim - p and a[na - 1 - s] == b[nb - 1 - s]:
            s += 1
        while p > 0 and not sep(a[p - 1]):           # 退到段落边界：中间那块从整段开始、到整段结束
            p -= 1
        while s > 0 and not sep(a[na - s]):
            s -= 1
        for _ in range(cls._ALIGN_CTX):              # 再往两边各多带几段上下文，段落级对齐时有参照
            if p > 0:
                p -= 1
                while p > 0 and not sep(a[p - 1]):
                    p -= 1
            if s > 0:
                s -= 1
                while s > 0 and not sep(a[na - s]):
                    s -= 1
        head = [("equal", 0, p, 0, p)] if p else []
        tail = [("equal", na - s, na, nb - s, nb)] if s else []
        a, b, off_a, off_b = a[p:na - s], b[p:nb - s], p, p

        def chunks(ts):
            out, st = [], 0
            for k, t in enumerate(ts):
                if sep(t):
                    if k > st:
                        out.append((st, k))
                    out.append((k, k + 1))
                    st = k + 1
            if st < len(ts):
                out.append((st, len(ts)))
            return out

        ca, cb = chunks(a), chunks(b)
        ka = ["".join(a[x:y]) for x, y in ca]
        kb = ["".join(b[x:y]) for x, y in cb]
        ops = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
            a1 = ca[i1][0] if i1 < len(ca) else len(a)
            a2 = ca[i2 - 1][1] if i2 > i1 else a1
            b1 = cb[j1][0] if j1 < len(cb) else len(b)
            b2 = cb[j2 - 1][1] if j2 > j1 else b1
            if tag == "equal":
                ops.append(("equal", off_a + a1, off_a + a2, off_b + b1, off_b + b2))
                continue
            for t2, x1, x2, y1, y2 in difflib.SequenceMatcher(None, a[a1:a2], b[b1:b2], autojunk=False).get_opcodes():
                ops.append((t2, off_a + a1 + x1, off_a + a1 + x2, off_b + b1 + y1, off_b + b1 + y2))
        return head + ops + tail

    class _BlockMap:
        """a 的第几个词对应 b 的第几个词（只算对齐上、一模一样的）：按对齐块二分查找，不逐词建表（整篇 7 万词时省一秒多）。"""

        def __init__(self, blocks):
            self.blocks = blocks                  # [(a 起, a 止, b 起)]，按 a 起排好
            self.starts = [x[0] for x in blocks]

        def get(self, i, default=None):
            import bisect
            k = bisect.bisect_right(self.starts, i) - 1
            if k >= 0:
                a1, a2, b1 = self.blocks[k]
                if a1 <= i < a2:
                    return b1 + (i - a1)
            return default

    @classmethod
    def _tok_map(cls, a: list[str], b: list[str]) -> "Viewer._BlockMap":
        """a 的第几个词对应 b 的第几个词。两份一模一样（重放时最常见）：直接一一对应，不对齐。"""
        if a == b:
            return cls._BlockMap([(0, len(a), 0)])
        return cls._BlockMap([(i1, i2, j1) for tag, i1, i2, j1, j2 in cls._tok_align(a, b) if tag == "equal"])

    @classmethod
    def _tok_plan(cls, before: list[str], after: list[str], state: list[str], atomic: bool) -> list | None:
        """把 before → after 的改动落到 state 上，返回 [(起, 止, 换成的词)]。对不上的：atomic（任务）整条返回 None；
        否则（你手改的部分）跳过那一处、其余照搬。
        试过"落不上再把没对上的那段词往左右挪一挪、换等价的对齐再试"：多连带撤的从 4 次降到 2 次，该连带却没撤的从 2 次
        升到 8 次（改动被挪到错的地方，拼出错句），总体更差，所以只用原样对齐：宁可多连带撤（会照实说出来），也不拼错句。"""
        return cls._tok_plan_with(cls._tok_map(before, state), before, after, state, atomic)

    @classmethod
    def _tok_plan_with(cls, m, before: list[str], after: list[str], state: list[str], atomic: bool,
                       ops: list | None = None) -> list | None:
        n = len(before)

        def span_ok(i1, i2):
            ks = [m.get(x) for x in range(i1, i2)]
            return (ks[0], ks[-1] + 1) if None not in ks and ks == list(range(ks[0], ks[0] + len(ks))) else None

        def anchors(i):
            L = m.get(i - 1) if i > 0 else -1
            R = m.get(i) if i < n else len(state)
            return (L, R) if L is not None and R is not None and R > L else None

        plan = []
        for tag, i1, i2, j1, j2 in (ops if ops is not None else cls._tok_align(before, after)):
            if tag == "equal":
                continue
            ins = list(after[j1:j2])
            got = None
            if i2 > i1 and ins:                              # 换词：被换掉的那几个词得都在、而且连着
                ab = span_ok(i1, i2)
                got = (ab[0], ab[1], ins) if ab else None
            elif i2 > i1:                                    # 纯删：前后是同样的词时可以等价地挪一挪（"a b b" 删哪个 b 都一样）
                cands = [(i1, i2)]
                a, b = i1, i2
                while b < n and before[a] == before[b]:
                    a, b = a + 1, b + 1
                    cands.append((a, b))
                a, b = i1, i2
                while a > 0 and before[a - 1] == before[b - 1]:
                    a, b = a - 1, b - 1
                    cands.append((a, b))
                for a, b in cands:
                    ab = span_ok(a, b)
                    if ab:
                        got = (ab[0], ab[1], [])
                        break
            else:                                            # 纯插入：空格算在词前还是词后都一样，挪到两边都对得上、最好挨着的位置
                cands = [(i1, ins)]
                k, x = i1, ins
                while k < n and before[k] == x[0]:
                    k, x = k + 1, x[1:] + [x[0]]
                    cands.append((k, x))
                k, x = i1, ins
                while k > 0 and before[k - 1] == x[-1]:
                    k, x = k - 1, [x[-1]] + x[:-1]
                    cands.append((k, x))
                ok = [(anchors(k), x) for k, x in cands if anchors(k)]
                best = next(((lr, x) for lr, x in ok if lr[1] == lr[0] + 1), ok[0] if ok else None)
                if best:
                    got = (best[0][1], best[0][1], best[1])  # 两边隔开了（中间是放回来的词）：插在右侧锚点前面
            if got is None:
                if atomic:
                    return None
                continue
            plan.append(got)
        return plan

    @staticmethod
    def _tok_apply(state: list[str], plan: list) -> list[str]:
        out, pos = [], 0                          # 从前往后拼一遍（每处都切片重拼的话，7 万词的稿子要复制十几次）
        for a, b, ins in sorted(plan, key=lambda p: (p[0], p[1])):
            out += state[pos:a]
            out += list(ins)
            pos = max(pos, b)
        out += state[pos:]
        return out

    def _undo_replay(self, text: str, tid: str) -> tuple[str, int, list] | None:
        """新办法撤回 tid（见上面的说明）。用不了（有任务没存全文、这条没记改动）返回 None，退回老办法。"""
        nl = lambda s: str(s or "").replace("\r\n", "\n")    # noqa: E731
        recs = []
        for t in self._paper_tasks(tid):              # 只重放这一篇上的（别篇的"改前全文"是另一篇论文）
            ch = self._changes(t["id"])
            if not ch:
                continue
            if ch.get("before") is None or ch.get("after") is None:
                if ch.get("changed") is not False and ch.get("ops"):
                    return None                  # 这场会里有旧记录：整篇重放缺一段历史，退回老办法
                continue
            if ch["before"] == ch["after"]:
                continue
            recs.append((float(ch.get("seq") or 0), t.get("idx") or 0, t["id"], bool(t.get("rejected")), ch))
        recs.sort(key=lambda r: (r[0], r[1]))
        if tid not in {r[2] for r in recs}:
            return None
        rejected = {r[2] for r in recs if r[3]}
        cache: dict = {}                          # 上一条的"改后"多半就是下一条的"改前"：同一份文字只切一次词

        def toks(s):
            s = nl(s)
            if s not in cache:
                cache[s] = self._toks(s)
            return cache[s]
        steps = [(i, toks(ch["before"]), toks(ch["after"])) for _, _, i, _, ch in recs]
        diffs = {i: [op for op in self._tok_align(b, a) if op[0] != "equal"] for i, b, a in steps}
        start = steps[0][1]

        def replay(skip: set) -> tuple[list, list]:
            state, dropped = list(start), []
            for i, b, a in steps:
                if i in skip:
                    continue
                plan = self._tok_plan_with(self._tok_map(b, state), b, a, state, True, ops=diffs[i])
                if plan is None:
                    dropped.append(i)            # 改在被撤（或已连带撤下）的那几条加的词上：离了它不成立
                    skip = skip | {i}
                    continue
                state = self._tok_apply(state, plan)
            return state, dropped

        ref, ref_dropped = replay(set(rejected))
        if tid in ref_dropped:                   # 重放时这条自己都落不上（中间有手改等历史没记全）：别冒险，退回老办法
            return None
        new, dropped = replay(set(rejected) | {tid})
        cur = self._toks(text)
        if ref != cur:                           # 你手改过的（或基线这种只并了一部分的稿子）：差别照搬到新结果上
            new = self._tok_apply(new, self._tok_plan(ref, cur, new, atomic=False))
        return "".join(new), max(1, len(diffs[tid])), [i for i in dropped if i not in rejected]

    def _rollback_text(self, text: str, tid: str) -> tuple[str, int, list]:
        """撤回 tid：有整篇记录的走按词重放（_undo_replay），否则退回老办法（_rollback_text_ops）。"""
        try:
            r = self._undo_replay(text, tid)
        except Exception:
            r = None
        if r is not None:
            return r
        return self._rollback_text_ops(text, tid)

    def _rollback_text_ops(self, text: str, tid: str) -> tuple[str, int, list]:
        """（老办法）撤回 tid：只撤它自己改的那几个字，同一段里别的任务改的别处留着。
        改动按段落记（ops：old → new），摘要这种一整段里先后好几条各改各的句子，后一条的"改前那段"就是前一条改完的那段，
        看着像一条链（2026-11-13 第 2 轮：撤 T1 把同在摘要里的 T2–T5 全冲掉了）。所以顺着链从链头原文起，
        把除 tid 和已撤回的以外的几条按先后重改一遍（三方合并，按字挪）。后面某条改的正好是 tid 改出来的那几个字（挪不过去）：
        它离了 tid 不成立，一起撤掉，返回给调用方照实说。返回 (新文本, 撤成了几处, 一起撤掉的任务 id)。"""
        import difflib

        def norm(xs):
            return [str(x).replace("\r\n", "\n") for x in (xs or [])]

        tasks = sorted(self._paper_tasks(tid), key=lambda t: t.get("idx") or 0)   # 只看这一篇上的
        gone = {t["id"] for t in tasks if t.get("rejected")}
        ops_of = {t["id"]: [{"old": norm(o.get("old")), "new": norm(o.get("new")), "prev": o.get("prev")}
                            for o in (self._changes(t["id"]).get("ops") or [])] for t in tasks}
        order = [t["id"] for t in tasks]
        if tid not in ops_of:
            return text, 0, []
        me, J, done, also = order.index(tid), "\n\n".join, 0, []

        def replay(chain, root, skip):
            """从链头原文 root 起，跳过 skip 里的，其余按先后重改。返回 (结果, tid 之后挪不过去的任务)；tid 之前的就挪不过去：None。"""
            state, dropped, passed = J(root), [], False
            for k_id, o in chain:
                passed = passed or k_id == tid
                if k_id in skip or k_id in dropped:
                    continue
                if state == J(o["old"]):
                    state = J(o["new"])
                    continue
                merged = self._three_way(state, J(o["old"]), J(o["new"]))
                if merged is None:
                    if not passed:
                        return None, []
                    dropped.append(k_id)
                    continue
                state = merged
            return state, dropped

        for op in ops_of[tid]:
            if not op["new"]:                       # 这条删了几段：放回原处
                out, n = self._revert_text(text, [op])
                text, done = out, done + n
                continue
            chain, root = [(tid, op)], op["old"]
            for k in range(me - 1, -1, -1):         # 往前找：谁改出了这一条改之前的那几段
                hit = next((o for o in ops_of[order[k]] if o["new"] and o["new"] == root), None)
                if hit:
                    chain.insert(0, (order[k], hit))
                    root = hit["old"]
            tail = op["new"]
            for k in range(me + 1, len(order)):     # 往后找：谁接着改了这一条改出来的那几段
                hit = next((o for o in ops_of[order[k]] if o["old"] and o["old"] == tail), None)
                if hit:
                    chain.append((order[k], hit))
                    tail = hit["new"]
            now, _ = replay(chain, root, gone)      # 稿子里这一处现在应该是的样子
            want, dropped = replay(chain, root, gone | {tid})
            if now is None or want is None:
                continue
            sp = self._spans(text)
            keys = [k for _, _, k in sp]
            cur = now.split("\n\n") if now else []
            n = max(1, len(cur))
            at = next((i for i in range(len(keys) - len(cur) + 1) if cur and keys[i:i + len(cur)] == cur), -1)
            if at < 0:                              # 后来又被手改过：找最像的那几段
                best, score = -1, 0.0
                for i in range(len(keys) - n + 1):
                    r = difflib.SequenceMatcher(None, now, J(keys[i:i + n]), autojunk=False).ratio()
                    if r > score:
                        best, score = i, r
                if best < 0 or score < 0.6:
                    continue
                at = best
            a, b = sp[at][0], sp[at + n - 1][1]
            if want:
                text = text[:a] + want + text[b:]
            else:                                   # 这条是新加的段落、后面也没人接着改：删掉
                text = (text[:a].rstrip("\n") + "\n\n" + text[b:].lstrip("\n")) if text[:a].strip() else text[b:].lstrip("\n")
            also += dropped
            done += 1
        return text, done, sorted(set(also), key=order.index)

    @staticmethod
    def _three_way(cur: str, base: str, target: str) -> str | None:
        """cur 是 base 后来又变过的样子：把 base → target 的改动挪到 cur 上；有一处挪不过去就 None。
        插入点两边都对得上时，看要插进去的字在原来那边跟谁挨着（前后各两个字），挨着哪边就放哪边。"""
        import difflib
        al = [b for b in difflib.SequenceMatcher(None, base, cur, autojunk=False).get_matching_blocks() if b.size]

        def inside(i):
            for b in al:
                if b.a <= i < b.a + b.size:
                    return b.b + (i - b.a)
            return None

        def boundary(i, ctx_l, ctx_r):
            left = next((b.b + b.size for b in al if b.a + b.size == i), None)
            right = next((b.b for b in al if b.a == i), None)
            if left is None or right is None or left == right:
                return left if right is None else right
            sl = cur[max(0, left - 2):left] == ctx_l
            sr = cur[right:right + 2] == ctx_r
            return left if (sl and not sr) else right

        edits = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, target, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            if i1 == i2:
                a = inside(i1)
                if a is None or (i1 > 0 and inside(i1 - 1) is not None and inside(i1 - 1) + 1 != a):
                    a = boundary(i1, base[max(0, i1 - 2):i1], base[i1:i1 + 2])
                b = a
            else:
                a = inside(i1)
                e = inside(i2 - 1)
                b = e + 1 if e is not None else None
            if a is None or b is None or b < a:
                return None
            edits.append((a, b, target[j1:j2]))
        for a, b, rep in sorted(edits, reverse=True):
            cur = cur[:a] + rep + cur[b:]
        return cur

    def reject_task(self, tid: str) -> dict:
        """批注卡片上点了 ×：这条改动不要了。从稿子里撤回去，批注收起，快嘴的任务板上记成"已撤回"。"""
        t = self.board.get(tid)
        if not t:
            return {"ok": False, "error": "修改不存在"}
        if t.get("status") != "done":
            return {"ok": False, "error": "修改未完成"}
        # 同一篇上有一条在改才等：工人只碰它那条任务的那一篇，别篇在改不碍事
        if any(x.get("status") == "working" for x in self._paper_tasks(tid)):
            return {"ok": False, "error": "另一处正在修改，请稍后撤回"}
        ops = self._changes(tid).get("ops") or []
        if not ops:
            return {"ok": False, "error": "撤回失败：没有修改记录"}
        mirror, base = self._files_of(t)            # 这条改的那一篇（切走了也撤那一篇，不碰屏幕上这篇）

        also: list = []
        held: list = []

        def rewrite(path: Path, keep_also: bool = True) -> int:
            src, bom, nl = _read_doc(path)          # 换行统一成 \n 再撤，写回时 CRLF / LF、BOM 照原样
            out, n, more = self._rollback_text(src, tid)
            if keep_also:
                also[:] = more
            # 这条改完以后有人手改过的段落原样留着（2026-11-21：撤一条改动被整段重写盖掉的任务，作者打的半句话被撤没了）。
            # 要撤的全在手改过的段落里，或者还要连带撤别的几条（它们的字可能就在留着的段落里）：一处都不撤，照实说
            if n and out != src and (guard[0] or guard[1]):
                kept = self._keep_paras(src, out, *guard)
                same = [k for _, _, k in self._spans(kept)] == [k for _, _, k in self._spans(src)]   # 只差空行也算没撤
                if keep_also and kept != out and (same or more):
                    n = 0
                    held.append(path)
                else:
                    out = src if same else kept
            if n and out != src:
                _write_doc(path, out, bom, nl)
            return n

        with _DOC_LOCK:                              # 和打勾、手改一段、开会定原文不同时改同一份文件
            guard = self._hand_guard(mirror, tid)
            try:
                n = rewrite(mirror)
            except (OSError, DocReadError, UnicodeDecodeError) as e:
                return {"ok": False, "error": f"读写文档失败：{e}"}
            if not n:
                if held:
                    en = getattr(self, "ui_lang", "zh") == "en"
                    return {"ok": False, "error": "Undo failed: edited by hand" if en else "撤回失败：此处已手动修改"}
                return {"ok": False, "error": "撤回失败：此处已被再次修改"}
            # 之前打过勾、已经并进基线的（它自己或连带撤下的那几条）：基线也撤回去
            if base.exists() and (t.get("accepted") or any((self.board.get(x) or {}).get("accepted") for x in also)):
                try:
                    rewrite(base, keep_also=False)
                except (OSError, DocReadError, UnicodeDecodeError):
                    pass
            for x in [tid] + also:                   # 回滚到这条之前：之后接着改同一处的也一起撤了
                self.board.resolve(x, accepted=False, rejected=True)
        self._sig = None
        if self.on_edit:
            try:
                self.on_edit()
            except Exception:
                pass
        return {"ok": True, "reverted": n, "of": len(ops), "ids": [tid] + also}

    def resolve_task(self, tid: str) -> dict:
        """打勾：看过了。改过正文的并进基线（修订标记消掉）。同一处几条一起打勾时请求是同时来的：整段拿 _DOC_LOCK 排队，
        并完再把之前打过勾、当时没并进去的（它前面那条还没打勾）重试一遍。"""
        with _DOC_LOCK:
            t = self.board.get(tid)
            if not t:
                return {"ok": False, "error": "修改不存在"}
            accepted = False
            if t.get("status") == "done" and not t.get("note"):
                try:
                    accepted = self._accept_into_base(tid)
                except Exception:
                    accepted = False
            self.board.resolve(tid, accepted=accepted)
            self._retry_accepts(tid)
            accepted = bool((self.board.get(tid) or {}).get("accepted"))
        self._sig = None
        return {"ok": True, "accepted": accepted}

    def resolve_tasks(self, ids: list) -> dict:
        """一处的几条一起打勾（对比页一次发来）：按改稿先后一条条并，前一条并进去了后一条的段落才对得上。"""
        seq = lambda i: (float(self._changes(i).get("seq") or 0), (self.board.get(i) or {}).get("idx") or 0)   # noqa: E731
        out = [dict(self.resolve_task(str(i)), id=str(i)) for i in sorted({str(x) for x in ids or [] if x}, key=seq)]
        return {"ok": all(r.get("ok") for r in out) if out else False, "results": out}

    def _retry_accepts(self, tid: str) -> None:
        """之前打过勾、当时没并进基线的（同一段前面还压着没打勾的改动）：前面那条现在并进去了，再试一次。
        按改稿先后试，一轮里有并进去的就再来一轮。调用方拿着 _DOC_LOCK。"""
        pend = []
        for x in self._paper_tasks(tid):
            if (x.get("resolved") and not x.get("accepted") and not x.get("rejected") and not x.get("note")
                    and x.get("status") == "done"):
                ch = self._changes(x["id"])
                if ch.get("ops"):
                    pend.append((float(ch.get("seq") or 0), x.get("idx") or 0, x["id"]))
        pend.sort()
        moved = True
        while pend and moved:
            moved = False
            for item in list(pend):
                try:
                    ok = self._accept_into_base(item[2])
                except Exception:
                    ok = False
                if ok:
                    self.board.annotate(item[2], accepted=True)
                    pend.remove(item)
                    moved = True

    # ---------- 变化探测 ----------
    def signature(self, paper=None) -> str:
        """页面该不该重算：稿子、基线、任务板、改动记录、图的修改时间。paper：算别的一篇（预先做好它的页面，切过去马上能看）。"""
        mirror = Path(paper) if paper is not None else self.mirror
        base = base_for(mirror) if paper is not None else self.base
        parts = []
        for p in (mirror, base, self.board.path):
            try:
                parts.append(f"{p.name}:{p.stat().st_mtime_ns}")
            except OSError:
                parts.append(f"{p.name}:-")
        try:
            parts += sorted(f"{f.name}:{f.stat().st_mtime_ns}" for f in CHANGES_DIR.glob("*.json"))
        except OSError:
            pass
        try:
            parts += sorted(f"{f.name}:{f.stat().st_mtime_ns}" for f in (mirror.parent / "figures").glob("*"))
        except OSError:
            pass
        return "|".join(parts)

    # ---------- spec ----------
    def _changes(self, task_id: str) -> dict:
        p = CHANGES_DIR / f"{task_id}.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def spec(self, final: bool = False, paper=None) -> dict:
        """final=True：散会快照，从开会那一刻的原文比起，打过勾的也都带着批注；会上摊开过几篇就一页里几篇。
        实时页（final=False）只画屏幕上这一篇和它的任务。paper：给别的一篇做实时页（切过去之前先做好）。"""
        notes, rules, pending = {}, [], []
        me = html.escape(self.me_name())
        mirror = Path(paper) if paper is not None else self.mirror
        base = base_for(mirror) if paper is not None else self.base
        base0 = Path(str(base) + ".orig")
        for t in (self.board.tasks() if final else self._tasks_of(mirror)):
            if _is_app_task(t):
                continue                      # 说的是软件本身（界面、声音、功能），不是论文：不进论文页
            if t.get("note"):
                continue                      # 标注：正文没动，稿子上由小旗图钉标出来（见 _inject），不进对比页的批注和"还没落到正文里的"
            if t.get("resolved") and not final:
                continue                      # 打过勾的：批注收起，改动已并进基线
            key = str(t["idx"])
            teacher = html.escape(_clip(t.get("remark"), 160))
            if t["status"] == "done":
                notes[key] = [_clip(t.get("title"), 14), f"<b>{me}</b> {teacher}<br><b>AI</b> {html.escape(_clip(t.get('result'), 200))}"]
                ch = self._changes(t["id"])
                ins = [s for s in ch.get("ins", []) if len(s) >= 2][:6]
                dele = [s for s in ch.get("del", []) if len(s) >= 2][:6]
                if ins:
                    rules.append({"note": key, "ins": ins})
                elif dele:
                    rules.append({"note": key, "del": dele})
                if ch.get("new_paras"):
                    rules.append({"note": key, "kind": "new", "text": [s[:20] for s in ch["new_paras"][:4]]})
            elif t["status"] == "failed":
                # 不写"第几条"（界面上不出现编号）：直接写是哪一处
                pending.append(f"<li><b>{html.escape(_clip(t.get('title'), 40))}</b> {'修改失败' if self.ui_lang != 'en' else 'failed'}"
                               f"{('：' if self.ui_lang != 'en' else ': ') + html.escape(_clip(t.get('error'), 120)) if t.get('error') else ''}</li>")
            else:
                st = ('修改中' if t['status'] == 'working' else '排队中') if self.ui_lang != 'en' else ('editing' if t['status'] == 'working' else 'queued')
                pending.append(f"<li><b>{html.escape(_clip(t.get('title'), 60))}</b> {st}</li>")
        try:
            base_t = time.strftime("%H:%M", time.localtime(base.stat().st_mtime))
        except OSError:
            base_t = "—"
        en = self.ui_lang == "en"
        closing = (f"<h2>{'Unfinished edits' if en else '未完成的修改'}</h2><ul>{''.join(pending)}</ul>" if pending else "")
        before = base0 if (final and base0.exists()) else base
        if final and self._own_ok(base):
            before = self._own_of(base)       # 会上有人手改过：从"原文 + 手改"比起，散会的修订对比只算 AI 的改动
        docs = [{"name": self._doc_name() if paper is None else self._name_of(mirror), "before": str(before), "after": str(mirror)}]
        papers = [p for p in (getattr(self, "papers", None) or []) if Path(str(p.get("path") or "")).is_file()]
        if final and len(papers) > 1:
            # 会上摊开过几篇：散会快照一页里几篇，每篇从它加进会那一刻的原文比起
            docs = []
            for p in papers:
                b = base_for(p["path"])
                b0 = self._own_of(b) if self._own_ok(b) else Path(str(b) + ".orig")
                docs.append({"name": str(p.get("name") or Path(p["path"]).stem),
                             "before": str(b0 if b0.exists() else b if b.exists() else p["path"]), "after": str(p["path"])})
        return {
            "title": "Meeting revisions" if en else "组会修订对比",
            "intro": (f"Original: the version at the start ({base_t})   Current: after the AI's edits"
                      if en else f"原文：开会时（{base_t}）的版本　现稿：AI 修改后的版本"),
            "ui": "en" if en else "zh", "out": "diff.html", "lang": "auto",
            "docs": docs,
            "notes": notes, "rules": rules, "closing_html": closing,
        }

    # ---------- 生成 ----------
    def _rt(self) -> None:
        """切论文提速、生成失败重试用的几样状态（测试里 Viewer.__new__ 出来的也能用）。"""
        if getattr(self, "_pages", None) is None:
            self._pages = {}                  # 论文编号 → (算它时的 signature, 生成器出的页面)：切回去马上能看
            self._fails = 0                   # 实时页连着生成失败了几次
            self._shown = None                # 现在显示的是哪篇的页面（论文编号）
            self._urgent = threading.Event()  # 刚换了论文：不等防抖，马上重算
            self._wake = threading.Event()

    def _install(self, out: str, mirror: Path) -> None:
        """把一份生成器页面配上这篇的批注、图、轮询脚本，换成现在的页面（build 加一，对比页轮询到就刷新）。"""
        self._rt()
        tasks = self._tasks_of(mirror)            # 这篇的批注（会中摊开几篇时别篇的不画上来）
        me = self.me_name()
        pid = paper_id(mirror)
        figs = self.figures(mirror)
        with self.lock:
            self.build += 1
            self._raw = out                       # 不带注入的页面：访客的只读快照从它做
            from .pptx_conv import readonly as _ro
            inj = _inject(self.build, tasks, me, figs, self.ui_lang, paper=pid, readonly=_ro(mirror))
            self.html = out.replace("</body>", inj + "</body>", 1) if "</body>" in out else out + inj
            self._shown = pid

    def rebuild(self) -> bool:
        """重算屏幕上这篇的页面。生成失败返回 False（_loop 隔一会儿重试）：前两次失败接着显示上一版好的页面，
        连着三次都失败才换成说明页。"""
        self._rt()
        with self.lock:
            self.building = True
        ok = True
        try:
            mirror = self.mirror
            sig = self.signature()
            if not mirror.exists():                   # 不带本机路径：这一页也会推给访客
                out = PLACEHOLDER.format(msg="暂无文档")
            elif not HTMLDIFF.exists():
                out = PLACEHOLDER.format(msg="文档页生成失败：缺少组件")
            else:
                self.ensure_base()
                ok, out = self._generate(final=False)
                if ok:
                    self._fails = 0
                    self._pages[paper_id(mirror)] = (sig, out)
                else:
                    self._fails += 1
                    if self._fails < 3 and self._shown == paper_id(mirror):
                        return False                  # 上一版好的页面先留着，等重试
                    out = PLACEHOLDER.format(msg="文档页生成失败，正在重试<pre>" + html.escape(out[-1500:]) + "</pre>")
            if not _same_file(mirror, self.mirror):
                return ok                             # 算的时候又切走了：这一版不往屏幕上放
            self._install(out, mirror)
        finally:
            with self.lock:
                self.building = False
        return ok

    def _snap_docs(self, docs: list) -> list[str]:
        """生成器读的是稿子和基线的快照（VIEW_DIR/snap），不直接读原文件：工人、打勾正写到一半时读到的是完整的一份
        （_read_raw 读到能解开、前后没变的才算）。一直解不开的基线先按原文 + 打过勾的重建（_heal_base）；还不行就把
        解不开的字节换成替代字符，页面照样出来。返回出过的问题（写进 htmldiff.log）。"""
        snap = VIEW_DIR / "snap"
        snap.mkdir(parents=True, exist_ok=True)
        notes = []
        for i, d in enumerate(docs):
            for k in ("before", "after"):
                src = Path(d[k])
                try:
                    raw = _read_raw(src)
                except DocReadError as e:
                    raw = e.raw
                    if k == "before" and self._heal_base(d["after"]):
                        try:
                            raw = _read_raw(src)
                            notes.append(f"{src.name} 解不开，按原文和打过勾的改动重建了")
                        except (DocReadError, OSError) as e2:
                            raw = getattr(e2, "raw", raw)
                    else:
                        notes.append(f"{src.name} 有解不开的字节，换成了替代字符")
                except OSError as e:
                    notes.append(f"{src.name} 读不到 {e}")
                    continue
                dst = snap / f"{i}-{k}{src.suffix or '.md'}"
                text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
                # 按字节写（write_text 在 Windows 上会把 \n 再换成 \r\n）；生成器读的时候本来就不管换行是哪种
                dst.write_bytes((text[1:] if text.startswith("﻿") else text).encode("utf-8"))
                d[k] = str(dst)
        return notes

    def _generate(self, final: bool, paper=None) -> tuple[bool, str]:
        """跑一次对比生成器 → (成功?, 完整页面或报错文字)。paper：给别的一篇做实时页。"""
        with self._gen_lock:
            spec = self.spec(final=final, paper=paper)
            try:
                notes = self._snap_docs(spec["docs"])
            except Exception as e:                   # 快照出岔子：照旧让生成器直接读原文件
                notes = [f"快照没做成 {e!r}"]
            spec_p = VIEW_DIR / "spec.json"
            spec_p.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
            r = subprocess.run([sys.executable, "-X", "utf8", str(HTMLDIFF), str(spec_p)], cwd=str(VIEW_DIR),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            (VIEW_DIR / "htmldiff.log").write_text(("\n".join(notes) + "\n" if notes else "") + (r.stdout or "")
                                                  + "\n--- stderr ---\n" + (r.stderr or ""), encoding="utf-8")
            sa = VIEW_DIR / "diff.standalone.html"
            if r.returncode != 0 or not sa.exists():
                return False, (r.stderr or r.stdout or "")
            return True, sa.read_text(encoding="utf-8")

    def _heal_base(self, paper) -> bool:
        """基线坏了（解不开 UTF-8，比如 2026-11-14 那份尾巴上多了半个字）：从加进会时的原文（.orig）起，
        把这篇上打过勾、没撤回的改动按改稿先后重新并进去，写回基线。没有 .orig 就拿现在的稿子当基线。"""
        mirror = Path(str(paper))
        with _DOC_LOCK:
            base = self.base if _same_file(mirror, self.mirror) else base_for(mirror)
            b0 = self._own_of(base) if self._own_ok(base) else Path(str(base) + ".orig")   # 会上手改过的：手改照样算看过
            try:
                text, bom, nl = _read_doc(b0)
            except (OSError, DocReadError, UnicodeDecodeError):
                try:
                    _copy_doc(mirror, base)
                    return True
                except (OSError, DocReadError):
                    return False
            acc = []
            for t in self.board.tasks():
                if t.get("accepted") and not t.get("rejected") and _same_file(self._files_of(t)[0], mirror):
                    ch = self._changes(t["id"])
                    if ch.get("ops"):
                        acc.append((float(ch.get("seq") or 0), t.get("idx") or 0, ch))
            for _, _, ch in sorted(acc, key=lambda x: (x[0], x[1])):
                out = self._apply_para_ops(text, ch["ops"])
                if out is None:
                    out = self._apply_tok_change(text, ch)
                if out is not None:
                    text = self._keep_paras(text, out, *self._hand_guard(mirror, ch.get("id") or ""))
            try:
                _write_doc(base, text, bom, nl)
            except OSError:
                return False
            return True

    def snapshot_final(self) -> Path | None:
        """散会快照：从开会那一刻的原文比到现在，所有批注都在（打过勾的也在）。写到 view/final.standalone.html。"""
        if not (self.mirror.exists() and HTMLDIFF.exists()):
            return None
        self.ensure_base()
        ok, out = self._generate(final=True)
        if not ok:
            return None
        p = VIEW_DIR / "final.standalone.html"
        p.write_text(out, encoding="utf-8")
        self._sig = None    # 输出文件被快照占用过，实时页重算一遍
        return p

    RETRY_S = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)    # 生成失败后隔多久重试（不等下一次改动）

    def _loop(self) -> None:
        """稿子、基线、任务板变了就重算（等 1.2 秒防抖：工人一次改好几处）。刚换论文不等防抖；生成失败按 RETRY_S 自己重试；
        闲下来时把会上摊开的别的几篇的页面先做好（切过去马上能看）。"""
        self._rt()
        last_change, dirty, fails, retry_at, idle_at = 0.0, True, 0, 0.0, time.time()
        while not self._stop.is_set():
            sig = self.signature()
            now = time.time()
            if sig != self._sig:
                self._sig, last_change, dirty, retry_at = sig, now, True, 0.0
            urgent = self._urgent.is_set()
            if dirty and (urgent or now - last_change >= 1.2) and (urgent or now >= retry_at):
                self._urgent.clear()
                dirty = False
                try:
                    ok = self.rebuild()
                except Exception as e:
                    ok = False
                    with self.lock:
                        self.html = PLACEHOLDER.format(msg="文档页生成失败，正在重试<pre>" + html.escape(repr(e)) + "</pre>")
                        self.build += 1
                if ok:
                    fails, retry_at = 0, 0.0
                else:                                # 不等下一次改动：隔一会儿自己再算（读到写了一半的文件这类，再读一次就好）
                    dirty = True
                    retry_at = time.time() + self.RETRY_S[min(fails, len(self.RETRY_S) - 1)]
                    fails += 1
                idle_at = time.time()
            elif not dirty and now - idle_at >= 2.0:
                try:
                    self._prebuild_one()
                except Exception:
                    pass
                idle_at = time.time()
            self._wake.wait(0.5 if not dirty else 0.25)
            self._wake.clear()

    def _prebuild_one(self) -> bool:
        """会上摊开的别的几篇：页面过期了的先做好一篇（_pages），切过去时马上换上，不用等生成器。"""
        self._rt()
        for p in list(getattr(self, "papers", None) or []):
            path = Path(str(p.get("path") or ""))
            if not path.is_file() or _same_file(path, self.mirror) or not base_for(path).is_file():
                continue
            pid = paper_id(path)
            sig = self.signature(path)
            if (self._pages.get(pid) or (None,))[0] == sig:
                continue
            ok, out = self._generate(final=False, paper=path)
            if ok:
                self._pages[pid] = (sig, out)
            return ok
        return False

    # ---------- 给口喷鸡前端的数据 ----------
    on_paper = None  # app 设置：path → info（导入 + 切换论文 + 重置任务板）

    def set_mirror(self, path: Path, rebase: bool = True) -> None:
        """换屏幕上那篇。rebase=True：以现在的稿子为原文（新导入、刚加进会的那篇）；
        False：会中切回一篇摊开过的，接着用它加进会时的原文（之前的修订标记都还在）。
        切回摊开过的：先把之前做好的那一版页面换上（马上能看），再叫生成循环不等防抖马上重算一版最新的。"""
        self._rt()
        with self.lock:
            self.mirror = Path(path)
            self.base = base_for(self.mirror)
            self.focus = None
            self._ready_cache = (0.0, None)          # 准备情况里"论文"那一项马上跟着变
        self.ensure_base(rebase=rebase)
        self._sig = None
        cached = None if rebase else self._pages.get(paper_id(self.mirror))
        if cached:
            try:
                self._install(cached[1], self.mirror)
                sig = self.signature()
                if cached[0] == sig:              # 做好之后什么都没变：就是最新的，不用再算
                    self._sig = sig
                    return
            except Exception:
                pass
        self._urgent.set()
        self._wake.set()

    def set_mirror_from(self, src: str) -> dict:
        from .papers import import_file
        info = import_file(src)
        self.board.reset()
        for f in CHANGES_DIR.glob("*.json"):
            try:
                f.unlink()
            except OSError:
                pass
        self.set_mirror(Path(info["path"]), rebase=True)
        return info

    def paper_info(self) -> dict:
        from .config import NO_PAPER
        if _same_file(self.mirror, NO_PAPER):
            return {"path": "", "name": "", "exists": False}   # 没选论文：没有名字、路径可显示
        try:
            st = self.mirror.stat()
            from .config import current_paper
            cur = current_paper() or {}
            name = cur.get("name") if Path(str(cur.get("path") or "")) == self.mirror else None
            words, outline = self._paper_outline(st)
            from . import pdf_full
            return {"path": str(self.mirror), "name": name or self.mirror.stem, "exists": True, "bytes": st.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                    "kind": "latex" if self.mirror.suffix.lower() == ".tex" else "markdown",
                    "words": words, "outline": outline,
                    # 新 PDF 先出的纯文本，后台还在识别公式和表格（pdf_full.py）：大厅那行小字显示"正在识别…"
                    "pending_full": pdf_full.pending(self.mirror)}
        except OSError:
            return {"path": str(self.mirror), "name": self.mirror.stem, "exists": False}

    _outline_cache: tuple = (None, (0, []))

    def _paper_outline(self, st) -> tuple[int, list[str]]:
        """论文大概多少字（中文按字、英文按词）和一二级标题（大厅里看是不是这篇、哪一版）。按修改时间缓存。"""
        key = (str(self.mirror), st.st_mtime_ns, st.st_size)
        if self._outline_cache[0] == key:
            return self._outline_cache[1]
        words, heads = 0, []
        try:
            if st.st_size < 8_000_000:
                txt = self.mirror.read_text(encoding="utf-8", errors="replace")
                body = re.sub(r"\\[A-Za-z]+\*?", " ", txt) if self.mirror.suffix.lower() == ".tex" else txt
                words = len(re.findall(r"[\u4e00-\u9fff]", body)) + len(re.findall(r"[A-Za-z]+(?:'[a-z]+)?", body))
                for line in txt.splitlines():
                    m = re.match(r"^#{1,2}\s+(.+?)\s*#*\s*$", line) or re.match(r"^\s*\\(?:chapter|section)\*?\{(.+?)\}", line)
                    if not m:
                        continue
                    h = re.sub(r"[*_`$]|\\[A-Za-z]+\{([^}]*)\}", r"\1", m.group(1)).strip()
                    if h:
                        heads.append(h[:60])
                    if len(heads) >= 12:
                        break
        except OSError:
            pass
        out = (int(words), heads)
        self._outline_cache = (key, out)
        return out

    _ready_cache = (0.0, None)

    def readiness(self) -> dict:
        now = time.time()
        if now - self._ready_cache[0] < 3 and self._ready_cache[1]:
            return self._ready_cache[1]
        import urllib.request
        from .config import is_no_paper
        r = {"paper": not is_no_paper(self.mirror), "fast": False, "tts": False, "blurt": False, "recording": False, "hands": self.cfg["hands"].get("backend", "cli")}
        try:
            from . import provider
            mode = provider.mode()
            if mode == "claude":
                # 用 Claude Code 账号：找得到 claude 就算接上了
                from .hands import _resolve_claude
                exe = _resolve_claude(str((self.cfg.get("hands") or {}).get("claude", "claude")))
                r["fast"] = Path(exe).is_absolute() and Path(exe).exists()
            else:
                r["fast"] = mode == "api"           # API：设好了就算接上（通不通看设置里的"测试连接"）；没设：没接上
        except Exception:
            pass
        from .onboard import tts_engine
        eng = tts_engine()
        r["tts_off"] = eng is None                              # 设置 → 语音 选了不出声
        if eng != "clone":
            r["tts"] = eng is not None                          # 微软在线 / Windows 自带：不用等服务加载
        else:
            try:                                                # 克隆音色：本机服务加载好了没有
                port = self.cfg["mouth"].get("tts_port", 47321)
                try:
                    port = json.loads((STATE / "tts_server.json").read_text(encoding="utf-8")).get("port") or port
                except Exception:
                    pass
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(f"http://127.0.0.1:{port}/health", timeout=1.5) as h:
                    r["tts"] = bool(json.loads(h.read().decode("utf-8")).get("loaded"))
            except Exception:
                pass
        try:
            import glob as _g
            jobs = Path(os.environ.get("BLURT_HOME") or Path.home() / ".blurt") / "jobs"
            for f in _g.glob(str(jobs / "j-*.json")):
                try:
                    j = json.loads(Path(f).read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
                if j.get("type") == "record" and j.get("state") == "running":
                    r["recording"] = True
                    break
            r["blurt"] = (Path(os.environ.get("BLURT_HOME") or Path.home() / ".blurt") / "run" / "control.json").exists()
        except Exception:
            pass
        # 各部件叫什么、细一点的状态：前端把它们放在 Claude 头像的悬浮面板里
        asr = "Fun-ASR"
        try:
            bc = json.loads((Path(os.environ.get("BLURT_HOME") or Path.home() / ".blurt") / "config.json").read_text(encoding="utf-8-sig"))
            name = str((((bc.get("record") or {}).get("dictation") or {}).get("asr_model")) or "")
            if name:
                asr = {"fun-asr-nano-onnx": "Fun-ASR Nano"}.get(name, name)
        except Exception:
            pass
        hcfg = self.cfg.get("hands") or {}
        from . import provider
        from .llm_api import model as api_model
        mode = provider.mode()
        pv = mode == "api"                                        # 用 API：显示那家的型号
        from .config import pretty_model
        effort = "" if mode != "claude" else {"low": " · Low", "medium": " · Medium", "high": " · High", "xhigh": " · Extra",
                                "max": " · Max"}.get(str(hcfg.get("effort", "xhigh")), "")    # 思考强度只有 Claude 有
        r["models"] = {"fast": pretty_model(api_model("fast")) if pv else _pretty_model(self.cfg["fast"].get("model")),
                       "hands": (pretty_model(api_model("main")) if pv else _pretty_model(hcfg.get("model", "claude-opus-5-5"))) + effort,
                       "tts": _tts_name(), "asr": asr}
        if mode not in ("api", "claude"):                       # 还没设模型来源：没有在用的型号（不说成 Claude）
            r["models"]["fast"] = r["models"]["hands"] = "—"
        try:                                                    # 接话分类模型：没开 None；加载中 / 就绪（在哪块设备上）/ 加载失败
            from . import addr as _addr
            m = _addr._insts.get(_addr.model_path(self.cfg))
            r["addr"] = None if m is None else (f"ready:{getattr(m, 'dev', '?')}" if m.ready else ("error" if m.error else "loading"))
        except Exception:
            r["addr"] = None
        r["fast_online"] = self.status.get("fast_ok")          # None = 还没测过
        r["fast_ms"] = self.status.get("fast_ms")
        try:
            from .hands import _resolve_claude
            exe = _resolve_claude(str(hcfg.get("claude", "claude")))
            r["hands_ready"] = bool(shutil.which(exe) or Path(exe).exists())
        except Exception:
            r["hands_ready"] = False
        try:
            c = self.board.counts()
            r["working"], r["queued"] = int(c.get("working", 0)), int(c.get("queued", 0))
        except Exception:
            r["working"], r["queued"] = 0, 0
        try:
            r["devices"] = self.devices()
        except Exception:
            r["devices"] = None
        self._ready_cache = (now, r)
        return r

    # ---------- 模型（参会人卡片上 Claude 那张：点一行换模型）----------
    # app 设置这两个钩子（helper B）：models_get() -> {"dialog": {"current", "options": [{"id", "label"}]}, "edit": {...},
    # "effort": {...}, "tts": "<名字>", "asr": "<名字>"}；models_set(part, value, by=None) 换完返回同样的形状，
    # 值不对抛 ValueError，没权限抛 PermissionError。没接上时 /api/models 回 503，卡片只显示名字、不能点。
    models_get = None
    brain = None                          # Meeting.brain_state：Claude 在干什么（前台在听/想/说，后台在查什么）
    talking = None                        # Meeting._talking：此刻谁在出声（"me"、"claude"、访客的名字）
    on_profile = None                     # Meeting._on_profile(你的昵称, AI 的名字)：设置里改了名字
    on_provider = None                    # Meeting._on_provider()：设置里换了模型来源
    mic_mute = None                       # Ears.set_muted：mic_mute(True/False) 静音 / 打开，mic_mute() 只问 → 现在是否静音
    models_set = None

    def _models_now(self) -> dict | None:
        fn = self.models_get
        if not callable(fn):
            return None
        try:
            d = fn()
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def assistant_card(self) -> dict:
        """Claude 那张卡上的四行（访客页也画这张卡）：对话模型、改稿模型 + 思考强度、语音合成、语音识别，都是给人看的名字。"""
        from .config import assistant_name
        out = {"name": assistant_name(self.cfg), "dialog": "", "edit": "", "effort": "", "tts": "", "asr": ""}
        mg = self._models_now()
        if mg:
            for part in ("dialog", "edit", "effort", "tts", "asr"):
                x = mg.get(part)
                if isinstance(x, dict):
                    cur = x.get("current")
                    lab = next((o.get("label") for o in (x.get("options") or []) if isinstance(o, dict) and o.get("id") == cur), None)
                    out[part] = str(lab or cur or "")
                else:
                    out[part] = str(x or "")
            return out
        m = (self.readiness().get("models") or {})
        edit, _, effort = str(m.get("hands") or "").partition(" · ")
        out.update(dialog=str(m.get("fast") or ""), edit=edit, effort=effort, tts=str(m.get("tts") or ""), asr=str(m.get("asr") or ""))
        return out

    # ---------- 自己的麦克风 / 扬声器（只有自己看得到自己的）----------
    _dev = (0.0, None)
    _dev_busy = False

    def devices(self, refresh: bool = False) -> dict:
        """{"mic": 名字|None, "speaker": 名字|None, "at": 秒}。录音那边（helper A）要是把实际在用的设备写进
        ~/.zuhui/devices.json（{"mic", "speaker"}）就用它；否则问系统默认设备（另起小进程，结果缓存，refresh 才重问）。
        设置 → 语音 → 麦克风选了某一个（~/.blurt/config.json 的 record.mic，zuhui/mic_prefs.py）：mic 就是它，
        missing = 现在找不到它；手改成不录的（"off"）：mic None、off True。"""
        from . import mic_prefs
        want = mic_prefs.chosen()
        d = _read_json(STATE / "devices.json")
        if not want and (d.get("mic") or d.get("speaker")):
            return {"mic": d.get("mic"), "speaker": d.get("speaker"), "at": time.time(), "source": "recorder"}
        at, val = self._dev
        if (refresh or val is None) and not self._dev_busy and time.time() - at > 4:
            self._dev_busy = True
            threading.Thread(target=self._probe_devices, name="view-devices", daemon=True).start()
        out = dict(val or {"mic": None, "speaker": None})
        inputs = out.pop("inputs", None)
        out.update(at=at, source="system", probing=self._dev_busy)
        if want == "off":
            out.update(mic=None, off=True, source="setting")
        elif want:
            out.update(mic=want, source="setting", missing=isinstance(inputs, list) and not mic_prefs.present(want, inputs))
            if d.get("speaker"):
                out["speaker"] = d["speaker"]
        return out

    def _probe_devices(self) -> None:
        val = None
        try:
            r = subprocess.run([sys.executable, "-X", "utf8", "-c", _DEVICE_PROBE], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=20,
                               creationflags=0x08000000 if sys.platform == "win32" else 0)   # CREATE_NO_WINDOW
            line = (r.stdout or "").strip().splitlines()
            val = json.loads(line[-1]) if line else None
        except Exception:
            val = None
        self._dev = (time.time(), val if isinstance(val, dict) else (self._dev[1] or {"mic": None, "speaker": None}))
        self._dev_busy = False

    # ---------- 回顾 ----------
    def processing(self, mid: str) -> bool:
        """这场会散会后的收尾（保存录屏 → 转写 → 整理 → 纪要）还在跑：回顾里它的纪要、逐字稿还会变。"""
        cur = self.meetings.current() if self.meetings else None
        if cur and cur.get("id") == mid:
            return True
        d = _read_json(STATE / "postmeeting.json")
        if str(d.get("meeting") or "") != str(mid) or d.get("done"):
            return False
        try:
            return time.time() - float(d.get("started") or 0) < 3 * 3600
        except (TypeError, ValueError):
            return False

    def meeting_detail(self, mid: str) -> dict | None:
        """GET /api/meetings/<id>：meetings.get 原样（干净版 transcript、transcript_raw、cleanup 都照传），
        加上当时的参会人、录屏（有的话：地址、时长，每句话的 vt = 它在录屏里从哪一秒开始）、收尾是不是还在跑。"""
        m = self.meetings.get(mid)
        if not m:
            return None
        # 以往的会：当时在场的人（散会时存下的）；老的会没存过，就只有你和 Claude
        from .meetings import mode_of
        m["participants"] = m.get("participants") or self.roster(guests=False, mode=mode_of(m.get("meeting")))
        m["processing"] = self.processing(mid)
        # 收尾到哪一步了（会议页不再有"纪要"那一页，散会后的进度在回顾里看）：[{key, label, state}]
        post = _read_json(STATE / "postmeeting.json")
        m["post"] = ({"steps": post.get("steps") or [], "progress": post.get("progress"), "error": post.get("error")}
                     if m["processing"] and str(post.get("meeting") or "") == str(mid) else None)
        m["video"] = None
        try:
            meeting = m.get("meeting") or {}
            sd = _session_of(meeting)
            f = _video_of(sd)
            if f:
                smeta = _read_json(sd / "meta.json")
                a, b = _iso_epoch(meeting.get("started")), _iso_epoch(smeta.get("started_at"))
                offset = (a - b) if (a is not None and b is not None and abs(a - b) < 6 * 3600) else 0.0
                segs = _asr_segments(sd)
                raw, clean = m.get("transcript_raw"), m.get("transcript")
                stats = None
                if isinstance(raw, list):
                    # 原始版的字和录屏那一路的转写一样，先对它；干净版的句子（同一个人、同一个落笔时刻）照抄过去
                    stats = align_video_times(raw, segs, offset)
                    if isinstance(clean, list):
                        pool: dict = {}
                        for l in raw:
                            pool.setdefault((l.get("who"), l.get("sec")), []).append(l.get("vt"))
                        rest = []
                        for l in clean:
                            q = pool.get((l.get("who"), l.get("sec")))
                            if q:
                                l["vt"] = q.pop(0)
                            else:
                                rest.append(l)
                        if rest:
                            align_video_times(rest, segs, offset)
                elif isinstance(clean, list):
                    stats = align_video_times(clean, segs, offset)
                m["video"] = {"url": f"/api/meetings/{mid}/video", "type": VIDEO_TYPES.get(f.suffix.lower(), "video/mp4"),
                              "duration": smeta.get("duration"), "bytes": f.stat().st_size, "offset": round(offset, 3),
                              "align": stats}
        except Exception as e:                  # 录屏对不上不该让整场会打不开
            m["video"] = None
            m["video_error"] = repr(e)[:200]
        m["summary_job"] = self.summary_job(mid)
        try:                                    # 第 6 批：会上在论文上打的批注（回顾里只读）
            from .comments import load as _cm_load
            m["comments"] = _cm_load(self.meetings.root / Path(str(mid)).name)
        except Exception:
            m["comments"] = []
        return m

    # ---------- 回顾：换一种格式重新生成纪要（用户点了才调模型） ----------
    SUMMARY_JOB_KEEP_S = 600                    # 生成失败的那条消息留多久（回顾页上显示"生成失败：原因"）

    def summary_job(self, mid: str) -> dict | None:
        """这场会重新生成纪要的进度：{"state": running|failed, "template", "started", "error"}；没在生成、成功了：None。"""
        jobs = self.__dict__.setdefault("_summary_jobs", {})
        j = jobs.get(str(mid))
        if not j or j.get("state") == "done":
            return None
        if j.get("state") == "failed" and time.time() - float(j.get("ended") or 0) > self.SUMMARY_JOB_KEEP_S:
            jobs.pop(str(mid), None)
            return None
        return {k: j.get(k) for k in ("state", "template", "started", "error")}

    def regen_summary(self, mid: str, template: str) -> dict:
        """POST /api/meetings/<id>/summary：后台按 template 重新生成这场会的纪要，马上返回进度。
        不认识的格式 ValueError("template")；自定义格式还没写要求 ValueError("custom_empty")；没有这场会 KeyError；
        会还开着、散会收尾还在跑、上一次重新生成还没完 RuntimeError("busy")。"""
        from . import summary_prefs
        from .meetings import regenerate
        mid = Path(str(mid or "")).name
        if template not in summary_prefs.TEMPLATES:
            raise ValueError("template")
        g = self.meetings.get(mid) if (mid and self.meetings) else None
        if not g:
            raise KeyError("not_found")
        if template == "custom" and not summary_prefs.load()["custom"].strip():
            raise ValueError("custom_empty")
        if (g.get("meeting") or {}).get("state") == "live" or self.processing(mid):
            raise RuntimeError("busy")
        jobs = self.__dict__.setdefault("_summary_jobs", {})
        lock = self.__dict__.setdefault("_summary_lock", threading.Lock())
        with lock:
            if (jobs.get(mid) or {}).get("state") == "running":
                raise RuntimeError("busy")
            job = {"state": "running", "template": template, "started": time.time(), "error": None}
            jobs[mid] = job

        def run():
            try:
                from . import usage
                with usage.bind(self.meetings.root / mid):      # 重新生成纪要的用量算那一场的
                    regenerate(self.meetings, self.cfg, mid, template)
                job.update(state="done", ended=time.time())
            except Exception as e:
                job.update(state="failed", ended=time.time(), error=(str(e) or type(e).__name__)[:200])

        threading.Thread(target=run, name=f"summary-{mid}", daemon=True).start()
        return {k: job.get(k) for k in ("state", "template", "started", "error")}

    # ---------- 往期会议：大厅的遗留项、回顾里提问、阶段汇总（zuhui/past.py） ----------
    def paper_here(self) -> dict:
        """屏幕上这篇论文在会议 meta 里的样子 {id, name, path, source}：认往期会是不是同一篇用。"""
        from .config import current_paper
        from .meetings import paper_entry
        cur = current_paper() or {}
        same = _same_file(cur.get("path"), self.mirror) if cur.get("path") else False
        return paper_entry(self.mirror, (cur.get("name") if same else None) or self.mirror.stem,
                           source=cur.get("source") if same else None)

    def carry_items(self) -> dict:
        """GET /api/carry：这篇论文最近几场会留下、还没勾掉的事（大厅里列出来，能勾掉）。"""
        from . import past
        if not self.meetings or not self.mirror.is_file():
            return {"paper": None, "items": []}
        live = self.meetings.current()
        here = self.paper_here()
        return {"paper": here.get("name"), "items": past.carry(self.meetings, here, exclude=live.get("id") if live else None)}

    def past_ask(self, question, mids=None) -> dict:
        """POST /api/past/ask：回顾里问往期会议（调模型）。问题空的 / 太长 ValueError，模型没答出来 RuntimeError。"""
        from . import past
        return past.ask(self.meetings, self.cfg, question, mids=mids if isinstance(mids, list) else None)

    def start_digest(self, mids) -> dict:
        """POST /api/digests：选的几场会写一份阶段汇总（后台调模型），马上返回"生成中"的那份。"""
        from . import past
        d = past.start_digest(self.meetings, mids)
        threading.Thread(target=past.run_digest, args=(self.meetings, self.cfg, d["id"]), name=f"digest-{d['id']}",
                         daemon=True).start()
        return d

    def video_path(self, mid: str) -> Path | None:
        m = self.meetings.get(Path(str(mid)).name) if self.meetings else None
        return _video_of(_session_of((m or {}).get("meeting"))) if m else None

    # ---------- 访客看会（推到中继，见 share.Share.set_feed） ----------
    def guest_state(self) -> dict | None:
        """开会时给访客看的会议信息；没在开会返回 None（中继那边就把论文删掉）。
        逐字稿只给主持人：访客这边不推整份逐字稿、也不推正在说的半句（像腾讯会议，只有主持人能看和导出）；
        只推最近几句当字幕（captions，见 share.captions），散会时中继就删掉。
        assistant：Claude 那张卡（访客页也画）——名字和四个部件现在用的是什么。"""
        cur = self.meetings.current() if self.meetings else None
        if not cur:
            return None
        from .share import captions as share_captions, rec_mode as share_rec_mode
        people = self.participants(guests=False)
        title = re.sub(r"\s*·\s*\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*$", "", str(cur.get("title") or "")).strip()
        try:
            assistant = self.assistant_card()
        except Exception:
            from .config import assistant_name
            assistant = {"name": assistant_name(self.cfg), "dialog": "", "edit": "", "effort": "", "tts": "", "asr": ""}
        lines = self.meetings.current_lines()
        everyone = self.participants()
        from .meetings import mode_of
        mode = mode_of(cur)
        chat = mode == "chat"                      # 没有论文的会：没有论文页，AI 不在名单上
        shown = [p for p in people if not (chat and p.get("id") == "claude")]
        out = {"title": title or self.paper_info().get("name") or "组会", "host": people[0]["name"], "live": True,
               "started": cur.get("started"), "paper": not chat, "mode": mode,
               "people": [{"name": p["name"], "role": p["role"], "rank": p.get("rank")} for p in shown],
               "colors": {p["name"]: p.get("color") for p in everyone if p.get("color")},
               "assistant": assistant,
               "captions": share_captions(lines, people[0]["name"], people[1]["name"]),   # 旧访客页的字幕：最近几句
               "rec": share_rec_mode(),             # 第 8 批：录制告知（录屏 screen / 录音 audio / 没在录 None）
               # 主持人屏幕上正看着哪一节哪一段：访客页据此"跟随主持人"（section 名 + 那一段的编号和开头几个字）
               "focus": ({k: (self.focus or {}).get(k) for k in ("section", "no", "text", "top", "ri", "off", "k")} if self.focus else None)}
        # v2（2026-11-17）：访客页用和主持人一样的会议界面，看到的东西和主持人一样（逐字稿、修改卡片、几篇论文、Claude 在做什么），
        # 只是没有 Dev、不能设共同主持人。本机路径（论文文件、截图）不出这台电脑
        try:
            out["v"] = 2
            out["meeting"] = {k: cur.get(k) for k in ("id", "title", "started", "state", "lang", "duration_s", "title_auto")}
            out["meeting"]["mode"] = mode
            from .meetings import papers_of
            out["papers"] = [{"id": p.get("id"), "name": p.get("name")} for p in papers_of(cur)]
            try:
                out["active_paper"] = None if chat else paper_id(self.mirror)
            except Exception:
                out["active_paper"] = cur.get("active_paper")
            out["meeting"]["papers"], out["meeting"]["active_paper"] = out["papers"], out["active_paper"]
            out["transcript"] = [{k: v for k, v in l.items() if k not in ("shot", "path")} for l in lines]
            out["partial"] = self.meetings.partial
            hide = {"shot", "paper_path", "files", "path", "mirror"}
            out["board"] = {"counts": self.board.counts(), "tasks": [{k: v for k, v in t.items() if k not in hide} for t in self.board.tasks()]}
            out["participants"] = [{k: p.get(k) for k in ("id", "name", "role", "rank", "color")} for p in everyone
                                   if not (chat and p.get("id") == "claude")]
            if callable(self.brain):
                try:
                    out["brain"] = self.brain()
                except Exception:
                    out["brain"] = None
            # 第 3 批：书签、共同主持人选中的那段。多久没人说话不推给访客
            from . import live_api
            out.update(live_api.extras(self, host=False))
            from .comments import current as _cm_now   # 第 6 批：论文上的批注
            out["comments"] = _cm_now(self)
        except Exception as e:
            out["v2_error"] = repr(e)[:200]
        return out

    GUEST_UPLOADS_PENDING = 2                                        # 访客传的论文同时在导入、排队的最多几份
    GUEST_OPEN = ("/api/paper/switch",)                              # 谁都能做：切论文（新增走 upload）
    GUEST_COHOST = ("/api/board/resolve", "/api/board/reject", "/api/para/source", "/api/para/save")   # 只有共同主持人

    def guest_act(self, a: dict, fetch_upload) -> dict:
        """访客在会议页上做的一件事（中继排队转过来，a = {id, by, path, body}）。谁都能新增、切论文；
        确认 / 撤回改动、就地改一段原文只有共同主持人能做（主持人在 Bloom 里点的，每场重设）。
        办法和主持人自己点一样：转给本机同一个接口，结果原样回给访客。"""
        from . import roles
        path, by, body = str(a.get("path") or ""), str(a.get("by") or "").strip(), a.get("body") or {}
        if not (self.meetings and self.meetings.current()):
            return {"ok": False, "error": "会议已结束"}
        log = getattr(self, "on_log", None)
        if path == "upload":
            # 论文一次只导入一篇（app 里排队）：访客连着传，排着的最多 GUEST_UPLOADS_PENDING 份，
            # 再多的先不取（中继那边留着、满了让他稍等），免得一份份 50 MB 在内存里、磁盘上越堆越多
            with _GUEST_UP_LOCK:
                if _GUEST_UP[0] >= self.GUEST_UPLOADS_PENDING:
                    return {"ok": False, "error": "正在导入其他文档，请稍后重试"}
                _GUEST_UP[0] += 1
            try:
                name, data = fetch_upload(str(body.get("upload") or ""))
                mid = self.meetings.current()["id"]
                d = STATE / "guest_papers" / mid
                d.mkdir(parents=True, exist_ok=True)
                base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", Path(str(name)).name).strip(" .") or "paper.pdf"
                fp, k = d / base, 2
                while fp.exists():
                    fp = d / f"{Path(base).stem}-{k}{Path(base).suffix}"
                    k += 1
                fp.write_bytes(data)
                size = len(data)
                del data
                if callable(log):
                    log("guest_act", text=f"{by} 新增论文：{fp.name}（{size // 1024} KB）")
                return self._local_post("/api/paper", {"path": str(fp)}, timeout=600)
            finally:
                with _GUEST_UP_LOCK:
                    _GUEST_UP[0] -= 1
        if path in self.GUEST_COHOST and not roles.is_cohost(by):
            if callable(log):
                log("guest_act", text=f"{by}（参会者）想做 {path}：只有主持人和共同主持人能做")
            return {"ok": False, "error": "仅主持人和共同主持人可操作"}
        if path in self.GUEST_OPEN or path in self.GUEST_COHOST:
            if callable(log):
                log("guest_act", text=f"{by}：{path} {json.dumps(body, ensure_ascii=False)[:120]}")
            if path == "/api/para/save" and isinstance(body, dict):
                body = dict(body, by=by)              # 逐字稿上记"谁改了哪一段"：记访客的名字（中继认出来的，不信页面自己报的）
            return self._local_post(path, body)
        from . import live_api
        from . import paper_api
        if path in paper_api.GUEST_PATHS:          # 第 6 批：论文上打字批注（谁都能写、回复、解决；删只有共同主持人）
            if callable(log):
                log("guest_act", text=f"{by}：{path} {json.dumps(body, ensure_ascii=False)[:120]}")
            out, _code = paper_api.post(self, path, body if isinstance(body, dict) else {}, by=by, guest=True)
            return out
        if path in live_api.GUEST_PATHS:           # 打字问 AI、标记、选中一段：谁能做什么在 live_api 里按人定
            if callable(log):
                log("guest_act", text=f"{by}：{path} {json.dumps(body, ensure_ascii=False)[:120]}")
            out, _code = live_api.post(self, path, body if isinstance(body, dict) else {}, by=by, guest=True)
            return out
        return {"ok": False, "error": "不支持此操作"}

    def _local_post(self, path: str, body: dict, timeout: float = 60) -> dict:
        """转给本机的会议接口（和主持人在 Bloom 里点的是同一个）。"""
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8") or "{}")
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}"}

    def guest_paper_key(self):
        """论文快照的版本（变了中继那边重新推）。没有论文的会：None = 没有论文页，不推。"""
        if self.mode_now() == "chat":
            return None
        with self.lock:
            return (self.build, getattr(self, "_raw", None) is not None)

    def guest_paper(self) -> tuple[str, dict]:
        """访客看的论文：和你窗口里同一份修订对比页，只读（不轮询、不上报、不能编辑），图和公式都从中继拿。
        占位页（还没生成好、生成出错）给访客的只是一句"正在准备论文"：出错的细节里可能有本机路径，不出这台电脑。"""
        from .share import room as share_room
        with self.lock:
            raw = getattr(self, "_raw", None) or self.html or ""
        if PLACEHOLDER_MARK in raw:
            return PLACEHOLDER.format(msg="正在准备文档…"), {}
        base = f"/view/{share_room()}/fig/"
        figs, assets = {}, {}
        for no, f in self.figures().items():
            name = Path(str(f.get("src") or "").split("?")[0]).name
            fp = self.mirror.parent / "figures" / name
            ctype = FIG_TYPES.get(fp.suffix.lower())
            if not (ctype and fp.is_file()):
                continue
            try:
                data = fp.read_bytes()
            except OSError:
                continue
            if len(data) > 8_000_000:
                continue
            name = _guest_asset_name(name, data)          # 带内容哈希：两篇的同名图不互相盖、手机能长期缓存
            assets[name] = (ctype.split(";")[0], data)
            figs[no] = dict(f, src=base + name)
        from .pptx_conv import readonly as _ro
        inj = _inject(None, self._active_tasks(), self.me_name(), figs, readonly=_ro(self.mirror))   # 访客看到的也只是屏幕上这一篇的批注
        page = raw.replace("</body>", inj + "</body>", 1) if "</body>" in raw else raw + inj
        return _guest_lite(_embed(page)), assets          # 字体、公式、插图都不挡着访客页露出论文（见 _guest_lite）

    def participants(self, guests: bool = True) -> list[dict]:
        """谁在会上：你、Claude，加上通过邀请链接进来的人（没有自己头像的统一用 Bloom 的花）。"""
        import getpass
        me = str((self.cfg.get("meeting") or {}).get("me") or getpass.getuser() or "我")

        def avatar(*names):
            for n in names:
                for ext in AVATAR_TYPES:
                    f = AVATARS / f"{n}{ext}"
                    if n and f.exists():           # 地址带着修改时间：设置里换了头像，各页面拿到的是新图
                        return f"/avatars/{n}{ext}?v={f.stat().st_mtime_ns // 1_000_000}"
            return "/avatars/bloom.png" if (AVATARS / "bloom.png").exists() else None

        from . import roles
        from .config import assistant_name
        from . import host_profile
        ai = _AI_NAME[0] = assistant_name(self.cfg)
        host_c, ai_c = host_profile.colors()                   # 设置里选的颜色（没选过是蓝、橙）
        out = [{"id": "me", "name": me, "role": "teacher", "rank": "host", "you": True, "avatar": avatar("me", me), "color": host_c},
               {"id": "claude", "name": ai, "role": "student", "rank": "assistant", "avatar": avatar("claude"), "color": ai_c}]
        gs = [g.get("name") or "参会者" for g in (list(self.guests) + list(getattr(self.share, "guests", []) or []))]
        cmap = self.guest_colors(gs)
        for g in (list(self.guests) + list(getattr(self.share, "guests", []) or [])) if guests else []:
            name = g.get("name") or "参会者"
            out.append({"id": g.get("id") or name, "name": name, "role": g.get("role") or "guest",
                        "rank": "cohost" if roles.is_cohost(name) else "participant", "avatar": avatar(g.get("avatar")),
                        "color": cmap.get(name)})
        return out

    def dev_people(self, meta: dict) -> list[dict]:
        """Dev 里一场模拟会的参会人：主持人（扮演你）、Claude、扮演的访客；头像和颜色照会议页的规矩。"""
        base = self.participants(guests=False)
        me = dict(base[0], name=str(meta.get("host") or base[0]["name"]))
        out = [me, base[1]]
        teacher = bool(meta.get("teacher"))
        for i, g in enumerate(meta.get("guests") or []):
            out.append({"id": f"g{i}", "name": g, "role": "guest", "rank": "cohost" if teacher else "participant",
                        "avatar": base[1].get("avatar") and "/avatars/bloom.png", "color": GUEST_PALETTE[i % len(GUEST_PALETTE)]})
        return out

    def guest_colors(self, present: list[str]) -> dict:
        """访客的颜色：第一次见到时取调色板里在场的人没用着的第一个，之后一直是这个色（桌面端、访客页都照这个画）。"""
        cm = _GCOLOR
        pal = _guest_palette()
        for n in present:
            if n not in cm:
                used = {cm.get(x) for x in present if x in cm}
                cm[n] = next((c for c in pal if c not in used), pal[len(cm) % len(pal)])
        return {n: cm[n] for n in present}

    def mode_now(self) -> str:
        """开着会：这场会是哪种（"chat" 没有论文的会：只记逐字稿，AI 不接话 / "paper" 改论文的会）；
        没开会：现在开会会是哪种（没选论文 → "chat"）。"""
        from .meetings import mode_of
        cur = self.meetings.current() if self.meetings else None
        if cur:
            return mode_of(cur)
        from .config import is_no_paper
        return "chat" if is_no_paper(self.mirror) else "paper"

    def roster(self, guests: bool = True, mode: str | None = None) -> list[dict]:
        """给人看的参会人名单（主持人的会议页、访客页）：没有论文的会里 AI 不说话，名单上不放它。
        participants() 本身照旧（别处按位置取你和 AI）。"""
        people = self.participants(guests=guests)
        if (mode or self.mode_now()) == "chat":
            people = [p for p in people if p.get("id") != "claude"]
        return people

    def clear_paper(self) -> None:
        """大厅里点「移除」：现在没有论文（current_paper.json 记成"没有"、摊开的单子清空），对比页换成占位页。
        app 接上时由 app.clear_paper 调（它还要换掉快嘴、工人手里那篇）。开着会不能移除。"""
        if self.meetings and self.meetings.current():
            raise RuntimeError("会议中无法移除文档")
        from .config import clear_paper as _clear, paper_path as _pp
        from .meetings import save_open_papers
        _clear()
        save_open_papers([], None)
        self.papers = []
        self.set_mirror(_pp(self.cfg), rebase=False)

    def lobby_seen(self) -> None:
        """前端在大厅、窗口可见时轮询：大厅刚变成可见、又没在开会，就把 MinerU 常驻服务起好（导入 PDF 时省掉加载模型）。
        一直开着的大厅不续命：10 分钟没有转换就关（mineru_svc.py）。没装 MinerU 什么都不做。"""
        try:
            from . import mineru_svc
            mineru_svc.lobby_seen(meeting=bool(self.meetings and self.meetings.current()))
        except Exception:
            pass

    def api_current(self) -> dict:
        cur = self.meetings.current()
        mode = self.mode_now()
        out = {"backend": True, "paper": self.paper_info(), "ready": self.readiness(), "mode": mode,
               "board": {"counts": self.board.counts(), "tasks": self.board.tasks()},
               "last_report": self.meetings.last_report, "participants": self.roster(mode=mode),
               "focus": self.focus, "partial": self.meetings.partial if cur else None, "brain": None}
        if cur and callable(self.brain):
            try:
                out["brain"] = self.brain()
            except Exception:
                pass
        try:                                          # 上一场是共同主持人、这场还没设的人（只提醒在场的）：顶栏「上次是共同主持人，恢复？」
            from . import roles
            here = {p["name"] for p in out["participants"] if p.get("rank") == "participant"}
            out["role_suggest"] = [x for x in roles.suggestions() if x.get("name") in here]
        except Exception:
            out["role_suggest"] = []
        # 摊开的几篇论文（"新增"、来回切的那个菜单）和屏幕上是哪篇：开会时是这场会的；没开会时是开会前新增过的那张单子
        # （2026-11-15：开会前新增一篇也不换掉上一篇，开会时整张单子带进会）
        from .meetings import load_open_papers, papers_of
        out["papers"] = [{k: p.get(k) for k in ("id", "name")} for p in (papers_of(cur) if cur else load_open_papers()["papers"])]
        try:
            out["active_paper"] = None if not out["paper"].get("exists") and not out["papers"] else paper_id(self.mirror)
        except Exception:
            out["active_paper"] = None
        last = None
        if cur:
            from .meetings import mode_of
            out.update({"meeting": dict(cur, mode=mode_of(cur)), "transcript": self.meetings.current_lines(), "summary": None})
        else:
            last = self.meetings.list()
            if last:
                m = self.meetings.get(last[0]["id"]) or {}
                # 散会后：干净版逐字稿、原始版、删改清单（整理那一步做完才有；没有时是 None，界面和以前一样）
                out.update({"meeting": m.get("meeting"), "transcript": m.get("transcript", []), "summary": m.get("summary"),
                            "diff_url": m.get("diff_url"), "transcript_raw": m.get("transcript_raw"),
                            "cleanup": m.get("cleanup"), "processing": self.processing(last[0]["id"])})
            else:
                out.update({"meeting": None, "transcript": [], "summary": None})
        try:                                          # 第 3 批：书签、选中的那段、多久没人说话
            from . import live_api
            out.update(live_api.extras(self, host=True, last_mid=last[0]["id"] if last else None))
        except Exception as e:
            out["extras_error"] = repr(e)[:200]
        try:                                          # 第 6 批：论文上打的批注（开着会是这场的，散会后是上一场的）
            from .comments import current as _cm_now
            out["comments"] = _cm_now(self)
        except Exception:
            out["comments"] = []
        try:                                          # 第 5 批：散会后自动保存的结果（失败了界面提示"保存失败：原因"）
            from . import autosave
            out["autosave"] = autosave.last_result()
        except Exception:
            out["autosave"] = None
        return out

    # ---------- HTTP ----------
    def serve(self, open_browser: bool = True) -> None:
        viewer = self
        try:
            self.share.start()
            self.share.set_feed(self.guest_state, self.guest_paper_key, self.guest_paper)
            self.share.set_actions(self.guest_act)
        except Exception:
            pass

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                """跨源只给 Bloom 自己的窗口和本机页面（_LOCAL_ORIGIN），回的就是对方的 Origin；别的来源不给这个头，浏览器读不到。"""
                o = self.headers.get("Origin")
                if o and _LOCAL_ORIGIN.match(o):
                    self.send_header("Access-Control-Allow-Origin", o)
                    self.send_header("Vary", "Origin")

            def _origin_ok(self) -> bool:
                """这个请求能不能改东西：来自 Bloom 自己的窗口、本机页面（Origin 对得上），或者没带 Origin 的本机脚本。
                没带 Origin 的还要看 Sec-Fetch-Site：别的网站用 <img>、链接发的 GET 不带 Origin，浏览器会标成 cross-site / same-site。"""
                o = self.headers.get("Origin")
                if o:
                    return bool(_LOCAL_ORIGIN.match(o))
                return (self.headers.get("Sec-Fetch-Site") or "none").strip().lower() in ("none", "same-origin")

            def _host_ok(self) -> bool:
                """Host 只认 127.0.0.1 / localhost / [::1]：防 DNS 重绑定（别的网站把自己的域名解析到 127.0.0.1 来读会议）。"""
                h = (self.headers.get("Host") or "").strip()
                return not h or bool(_LOCAL_HOST.match(h))

            def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", cache: str = "no-store"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", cache)
                self._cors()                            # 桌面端的 webview 跨源来拿
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass                         # 页面已经不等了（超时放弃）：不再打一整段 traceback

            def _brain_stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()

                def key(b):                      # 每秒都在变的（秒数、后台的现在）不算"变了"
                    if not isinstance(b, dict):
                        return "null"
                    c = {k: v for k, v in b.items() if k != "now"}
                    c["deeps"] = [{k: v for k, v in d.items() if k not in ("secs", "idle")} for d in (b.get("deeps") or [])]
                    c.pop("deep", None)
                    return json.dumps(c, ensure_ascii=False, sort_keys=True, default=str)
                last, beat = None, time.time()
                try:
                    while True:
                        b = None
                        if viewer.meetings is not None and viewer.meetings.current() and callable(viewer.brain):
                            try:
                                b = viewer.brain()
                            except Exception:
                                b = None
                        k = key(b)
                        if k != last:
                            self.wfile.write(("data: " + json.dumps(b, ensure_ascii=False, default=str) + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                            last, beat = k, time.time()
                        elif time.time() - beat > 15:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            beat = time.time()
                        time.sleep(0.25)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                    return

            def _mic_stream(self):
                """主持人麦克风和谁在出声：每 0.1 秒看一次，变了才推 {"muted": 是否静音（POST /api/mic/mute）,
                "talk": 此刻在出声的人（见 _mic_state）}。"""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                last, beat = "", time.time()
                try:
                    while True:
                        k = json.dumps(_mic_state(viewer), separators=(",", ":"))
                        if k != last:
                            self.wfile.write(("data: " + k + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                            last, beat = k, time.time()
                        elif time.time() - beat > 15:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            beat = time.time()
                        time.sleep(0.1)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                    return

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def _stream(self, f: Path, ctype: str, head: bool = False):
                """录屏：支持 Range（播放器拖进度条就是按段要），一段一段读一段一段写，整份文件从不进内存。
                开放式的 bytes=N- 一次最多给 VIDEO_CAP，浏览器拿完会自己接着要。"""
                try:
                    size = f.stat().st_size
                except OSError:
                    return self._send(404, b"not found", "text/plain")
                start, end, partial = 0, size - 1, False
                rng = (self.headers.get("Range") or "").strip()
                m = re.fullmatch(r"bytes\s*=\s*(\d*)\s*-\s*(\d*)", rng) if rng and "," not in rng else None
                if m and (m.group(1) or m.group(2)):          # 别的写法（多段、坏的）按没带 Range 处理：整份 200
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else min(size - 1, start + VIDEO_CAP - 1)
                    else:
                        start, end = max(0, size - int(m.group(2))), size - 1
                    end = min(end, size - 1)
                    if start >= size or end < start:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self._cors()
                        self.end_headers()
                        return
                    partial = True
                length = end - start + 1
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", "no-cache")
                self._cors()
                self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length")
                self.end_headers()
                if head:
                    return
                try:
                    with open(f, "rb") as fh:
                        fh.seek(start)
                        left = length
                        while left > 0:
                            buf = fh.read(min(VIDEO_CHUNK, left))
                            if not buf:
                                break
                            self.wfile.write(buf)
                            left -= len(buf)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass                                    # 播放器拖走了、换了一段：这一段不要了
                except OSError:
                    pass

            def do_HEAD(self):
                if not self._host_ok():
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                p = self.path.split("?")[0]
                if p.startswith("/api/meetings/") and p.endswith("/video"):
                    f = viewer.video_path(p.split("/")[3])
                    if f:
                        return self._stream(f, VIDEO_TYPES.get(f.suffix.lower(), "video/mp4"), head=True)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                for enc in ("utf-8", "gbk"):
                    try:
                        d = json.loads(raw.decode(enc))
                        return d if isinstance(d, dict) else {}
                    except Exception:
                        continue
                return {"_bad_json": True}

            def do_OPTIONS(self):
                if not self._host_ok():
                    return self._send(403, b"host not allowed", "text/plain")
                self._send(204, b"", "text/plain")

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, b"host not allowed", "text/plain")
                p = self.path.split("?")[0]
                embed = "embed=1" in self.path
                hosted = "host=bloom" in self.path       # Bloom 窗口里那份：工具栏在外壳那一行、用本机字体
                ui = "en" if "ui=en" in self.path else "zh" if "ui=zh" in self.path else None
                if p == "/" and hosted and ui and ui != viewer.ui_lang:
                    viewer.ui_lang = ui                  # 换了界面语言：实时页按新语言重算（访客的地址不带 ui，不会改回去）
                    viewer._sig = None
                if p == "/":
                    with viewer.lock:
                        body = viewer.html
                    if "demo=1" in self.path:      # 看批注标记的样子：几条假任务，不碰任务板
                        body = TASKS_RE.sub(lambda _m: "/*zh-tasks*/" + _js(DEMO_TASKS) + "/*zh-tasks-end*/", body, count=1)
                    self._send(200, (_embed(body, local_fonts=hosted) if embed else body).encode("utf-8"))
                elif p == "/version":
                    with viewer.lock:
                        v = {"build": viewer.build, "building": viewer.building}
                    v["counts"] = viewer.board.counts()
                    try:
                        v["mirror"] = time.strftime("%H:%M:%S", time.localtime(viewer.mirror.stat().st_mtime))
                    except OSError:
                        v["mirror"] = None
                    self._json(v)
                elif p == "/api/current":
                    if "lobby=1" in self.path:     # 大厅开着：装了 MinerU 的，后台把常驻服务起好（mineru_svc.py）
                        viewer.lobby_seen()
                    self._json(viewer.api_current())
                elif p == "/api/brain/stream":     # "Claude 在干什么"：变了就推（前端只管每秒计时）
                    self._brain_stream()
                elif p == "/api/provider":         # 设置 → 模型来源（密钥只给最后四位）
                    from . import provider
                    self._json(provider.public())
                elif p == "/api/usage":            # 设置 → 模型来源 → 本月用量 {month, models, tokens, costs}；Claude Code 账号 {"month", "mode": "claude"}
                    from . import provider, usage
                    self._json(dict(usage.month(), mode=provider.mode()))
                elif p == "/api/profile":          # 设置 → 你在会上：昵称、头像
                    self._json(_profile(viewer))
                elif p == "/api/setup":            # 首次引导：走没走过；基础组件在不在；会上实际先用哪种声音
                    from . import onboard, voice_prefs
                    v = voice_prefs.public(viewer.cfg)
                    self._json({**onboard.state(), "components": onboard.components(),
                                "voice_engine": onboard.voice_engine(v["voice"], v["has_clone"], v["has_edge"])})
                elif p == "/api/voice":            # 设置 → 语音：AI 的声音、能选的声音、专有名词
                    from . import voice_prefs
                    self._json(voice_prefs.public(viewer.cfg))
                elif p == "/api/relay":            # 设置 → 通用 → 邀请：中继配没配、地址（密钥只给最后四位）
                    from . import relay_cfg
                    self._json(relay_cfg.public(viewer.cfg))
                elif p == "/api/summary/prefs":    # 设置 → 通用 → 纪要：格式、自定义要求
                    from . import summary_prefs
                    self._json(summary_prefs.public())
                elif p == "/api/storage":          # 设置 → 存储：占了多少、各是什么、能清什么（zuhui/storage.py）
                    from . import storage
                    self._json(storage.scan(viewer))
                elif p == "/api/update":           # 大厅：有没有新版本、正在更新、更新失败（zuhui/updates.py；从源码跑的一律 dev）
                    from . import updates
                    out, code = updates.http_get(viewer)
                    self._json(out, code)
                elif p == "/api/mic/stream":       # 静音没有、谁在出声：会议页上的静音标记和头像外的亮圈
                    self._mic_stream()
                elif p == "/api/paper":
                    self._json(viewer.paper_info())
                elif p == "/api/ready":
                    self._json(viewer.readiness())
                elif p == "/api/paper/status":     # 正在导入的论文转到哪了（MinerU 识别公式表格要几十秒）
                    from . import papers
                    self._json(papers.import_status())
                elif p == "/api/invite/text":      # 悬浮岛"复制邀请"：整段邀请文字（和顶栏邀请弹层里复制的一样）
                    inv = viewer.share.invite()
                    if not inv.get("ok"):
                        self._json(inv, 503)
                    else:
                        topic = viewer.paper_info().get("name") or "组会"
                        cur = viewer.meetings.current() if viewer.meetings else None
                        if cur and cur.get("title"):
                            # 只去掉旧会议名后面带的日期（"· 11月11日 05:03"）：手改的名字里本来就有"·"的照留
                            topic = re.sub(r"\s*·\s*\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*$", "", str(cur["title"])).strip() or topic
                        host = viewer.participants(guests=False)[0]["name"]
                        text = chr(10).join([f"{host} 邀请你参加 Bloom 会议", f"会议主题：{topic}", f"点击链接直接加入：{inv['url']}"])
                        self._json({"ok": True, "text": text, "url": inv["url"]})
                elif p == "/api/invite":
                    self._json(viewer.share.invite())
                elif p == "/api/models":         # Claude 那张卡：四个部件现在用的什么、能换成什么
                    fn = viewer.models_get
                    if not callable(fn):
                        return self._json({"ok": False, "error": "换模型失败：服务未就绪"}, 503)
                    try:
                        self._json(fn())
                    except NotImplementedError as e:
                        self._json({"ok": False, "error": str(e) or "换模型失败：服务未就绪"}, 503)
                    except Exception as e:
                        self._json({"ok": False, "error": repr(e)[:200]}, 500)
                elif p == "/api/devices":        # 自己那张卡：麦克风、扬声器（点开卡片时重问一次系统）
                    self._json(viewer.devices(refresh=True))
                elif p == "/api/mic/device":     # 设置 → 语音 → 麦克风：选的哪个、本机有哪些（zuhui/mic_prefs.py）
                    from . import mic_prefs
                    out, code = mic_prefs.http_get(viewer)
                    self._json(out, code)
                elif p == "/api/meetings":
                    rows = viewer.meetings.list()
                    for r in rows:
                        try:
                            r["processing"] = viewer.processing(r.get("id"))
                        except Exception:
                            r["processing"] = False
                    if not rows and "sample=1" in self.path:   # 第 8 批：还没开过会时回顾里列出自带的示例会议（zuhui/sample.py；删过就不再有）
                        try:
                            from . import sample
                            b = sample.brief(viewer.meetings, ui or viewer.ui_lang)
                            if b:
                                rows.append(b)
                        except Exception as e:
                            print(f"[sample] {e!r}", flush=True)
                    self._json(rows)
                elif p == "/api/carry":         # 大厅：这篇论文上一场（最近几场）留下、还没勾掉的事
                    self._json(viewer.carry_items())
                elif p == "/api/digests":       # 回顾 → 阶段汇总：存下的几份（新的在前，不带正文）
                    from . import past
                    self._json(past.list_digests())
                elif p.startswith("/api/digests/"):
                    from . import past
                    try:
                        d = past.get_digest(p.rsplit("/", 1)[-1])
                    except ValueError:
                        d = None
                    self._json(d if d else {"error": "not_found"}, 200 if d else 404)
                elif p == "/api/dev/runs":      # Dev：模拟组会一场一场（只读 scratch_ab/sim/runs，不碰真的会议库）
                    from . import dev
                    self._json(dev.list_runs(viewer.cfg))
                elif p.startswith("/api/dev/runs/") and p.endswith("/diff.html"):
                    from . import dev
                    body, dtasks = dev.diff_html(viewer.cfg, p.split("/")[4], HTMLDIFF, viewer.me_name(), ui=ui or viewer.ui_lang)
                    if not body:
                        self._send(404, "这场还没有修订对比".encode("utf-8"), "text/plain; charset=utf-8")
                    else:
                        if embed:
                            inj = _inject(None, dtasks, viewer.me_name(), {}, ui or viewer.ui_lang)
                            body = body.replace("</body>", inj + "</body>", 1) if "</body>" in body else body + inj
                        self._send(200, (_embed(body, local_fonts=hosted) if embed else body).encode("utf-8"))
                elif p.startswith("/api/dev/runs/"):
                    from . import dev
                    d = dev.detail(viewer.cfg, p.rsplit("/", 1)[-1], viewer.dev_people)
                    self._json(d if d else {"error": "not found"}, 200 if d else 404)
                elif p == "/api/autosave" or (p.startswith("/api/meetings/") and p.endswith("/minutes")):
                    from . import export                 # 第 5 批：自动保存的设置；纪要带格式复制（HTML + 纯文本），zuhui/export.py
                    code, out = export.http_get(viewer, self.path)
                    self._json(out, code)
                elif p.startswith("/api/meetings/") and p.endswith("/export"):   # 回顾 → 分享：逐字稿导出成 md / txt，干净版或原始版
                    from urllib.parse import parse_qs, urlsplit
                    q = parse_qs(urlsplit(self.path).query)
                    fmt = "txt" if (q.get("fmt") or ["md"])[0] == "txt" else "md"
                    version = "raw" if (q.get("version") or ["clean"])[0] == "raw" else "clean"
                    mid = p.split("/")[3]
                    try:
                        r = viewer.meetings.export(mid, fmt, version)
                    except TypeError:                    # 整理逐字稿那一步还没接上：只有一个版本
                        r = viewer.meetings.export(mid, fmt)
                    self._json(r, 200 if r.get("ok") else 404)
                elif p.startswith("/api/meetings/") and p.endswith("/video"):    # 回顾：这场会的录屏（Range）
                    f = viewer.video_path(p.split("/")[3])
                    if not f:
                        self._send(404, b"no recording", "text/plain")
                    else:
                        self._stream(f, VIDEO_TYPES.get(f.suffix.lower(), "video/mp4"))
                elif p.startswith("/api/meetings/"):
                    m = viewer.meeting_detail(p.rsplit("/", 1)[-1])
                    self._json(m if m else {"error": "not found"}, 200 if m else 404)
                elif p.startswith("/meetings/") and p.endswith("/diff.html"):
                    f = viewer.meetings.diff_path(p.split("/")[2])
                    if not f:
                        self._send(404, b"no diff snapshot", "text/plain")
                    else:
                        body = f.read_text(encoding="utf-8", errors="replace")
                        if embed:      # 以往会议：章节栏和批注标记照样有，只读
                            m = viewer.meetings.get(p.split("/")[2]) or {}
                            tasks = ((m.get("board") or {}).get("tasks")) or []
                            inj = _inject(None, tasks, viewer.me_name(), viewer.figures(), ui or viewer.ui_lang)
                            body = body.replace("</body>", inj + "</body>", 1) if "</body>" in body else body + inj
                        self._send(200, (_embed(body, local_fonts=hosted) if embed else body).encode("utf-8"))
                elif p in ("/api/paper/prefs", "/api/paper/versions", "/api/paper/versions/diff"):
                    from . import paper_api      # 第 6 批：Word 交回方式、论文的历次版本和两版对比（zuhui/paper_api.py）
                    code, data, ctype = paper_api.get(viewer, self.path)
                    if ctype == "json":
                        self._json(data, code)
                    else:
                        self._send(code, data, ctype)
                elif p in ("/record", "/rebuild", "/rebase") and not self._origin_ok():
                    self._json({"error": "origin not allowed"}, 403)    # 这几个 GET 会改东西：别的网站不行
                elif p == "/record":
                    self._json({"ok": _deep_link("blurt://meeting")})
                elif p == "/rebuild":
                    viewer._sig = None
                    self._json({"ok": True})
                elif p == "/rebase":
                    viewer.ensure_base(rebase=True)
                    viewer._sig = None
                    self._json({"ok": True})
                elif p == "/assets/mathjax.js":
                    # 2 MB 的 MathJax：论文页每次刷新（后端一重算就整页刷新）都要它。地址带版本号（文件的修改时间），
                    # 让浏览器长期缓存，连编译好的字节码一起留着；文件换了地址就变
                    if MATHJAX.is_file():
                        self._send(200, MATHJAX.read_bytes(), "application/javascript; charset=utf-8",
                                   cache="public, max-age=31536000, immutable" if "v=" in self.path else "no-cache")
                    else:
                        self._send(404, b"no mathjax", "text/plain")
                elif p.startswith("/figures/"):
                    from urllib.parse import unquote
                    f = viewer.mirror.parent / "figures" / Path(unquote(p.rsplit("/", 1)[-1])).name
                    ctype = FIG_TYPES.get(f.suffix.lower())
                    if ctype and f.is_file():           # 地址带着图的修改时间（figures()）：图没换就一直用缓存
                        self._send(200, f.read_bytes(), ctype, cache="public, max-age=31536000, immutable" if "v=" in self.path else "no-cache")
                    else:
                        self._send(404, b"not found", "text/plain")
                elif p.startswith("/avatars/"):
                    name = Path(p.rsplit("/", 1)[-1]).name
                    if name == "me":             # 侧栏头像：不用知道文件名
                        name = Path(str(viewer.participants()[0].get("avatar") or "bloom.png").split("?")[0]).name
                    f = AVATARS / name
                    ctype = AVATAR_TYPES.get(f.suffix.lower())
                    if ctype and f.is_file():           # 头像几乎不换：缓存五分钟，来回切页面不用每次重拿
                        self._send(200, f.read_bytes(), ctype, cache="public, max-age=300")
                    else:
                        self._send(404, b"not found", "text/plain")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                p = self.path.split("?")[0]
                body = self._body()
                if not self._host_ok():
                    return self._json({"error": "host not allowed"}, 403)
                if not self._origin_ok():      # 别的网站发来的（text/plain 的 POST 不走预检，光靠响应头拦不住）
                    return self._json({"error": "origin not allowed"}, 403)
                if p == "/api/scroll":         # 连续流：滚动中的每一帧，直接转给访客（不记、不进中继）
                    cb = getattr(viewer, "on_scroll", None)
                    if cb is not None:
                        try:
                            cb({"s": body.get("s"), "ri": body.get("ri"), "off": body.get("off")})
                        except Exception:
                            pass
                    return self._json({"ok": True, "g": len(getattr(viewer.share, "guests", None) or [])})
                if p == "/api/profile":        # 设置里改昵称 {"name"}、AI 的名字 {"assistant"（空的 = 按对话模型叫）} → 现在的资料；不行 400 {"error": 代码}
                    from . import host_profile
                    from .config import assistant_name
                    try:
                        if "name" in body:
                            name = host_profile.save_name(body.get("name"), taken=[assistant_name(viewer.cfg)])
                            viewer.cfg.setdefault("meeting", {})["me"] = name     # 后端各处共用这一份配置：马上用新名字
                        if "assistant" in body:
                            ai = host_profile.save_assistant(body.get("assistant"), taken=[viewer.me_name()])
                            viewer.cfg.setdefault("assistant", {})["name"] = ai or ""
                        for which in ("color", "ai_color"):       # 名字颜色 #rrggbb
                            if which in body:
                                host_profile.save_color(which, body.get(which))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    except OSError:
                        return self._json({"error": "failed"}, 500)
                    cb = getattr(viewer, "on_profile", None)
                    if callable(cb):
                        try:
                            cb(viewer.me_name(), assistant_name(viewer.cfg))
                        except Exception:
                            pass
                    return self._json(_profile(viewer))
                if p == "/api/provider":       # 设置里换模型来源 {kind, base_url, key, fast, main} → 现在的（密钥只给最后四位）
                    from . import provider
                    try:
                        provider.save(provider.clean(body if isinstance(body, dict) else {}))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    except OSError:
                        return self._json({"error": "failed"}, 500)
                    cb = getattr(viewer, "on_provider", None)
                    if callable(cb):
                        try:
                            cb()
                        except Exception:
                            pass
                    return self._json(provider.public())
                if p == "/api/provider/models":   # OpenAI 格式的接口有哪些型号 {kind, base_url, key?} → {models} / {error}（不花钱）
                    from . import llm_api, provider
                    kind = str(body.get("kind") or "")
                    if provider.PRESETS.get(kind, {}).get("format") != "openai":
                        return self._json({"error": "kind"}, 400)
                    try:
                        base = provider.clean_base(kind, body.get("base_url") or provider.PRESETS[kind]["base_url"])
                    except ValueError:
                        return self._json({"error": "base_url"}, 400)
                    key = str(body.get("key") or "").strip()[:400]
                    old = provider.load()
                    if not key and old.get("kind") == kind and provider.key_rule(kind) != "none":
                        key = str(old.get("key") or "")          # 没重填：用存着的
                    try:
                        return self._json({"models": llm_api.list_models(kind, base, key)})
                    except llm_api.ApiError as e:
                        return self._json({"error": str(e)[:200]})
                if p == "/api/provider/lit":   # 设置里"查文献"开关 {on} → 现在的来源（含 lit）
                    from . import littools, provider
                    try:
                        littools.set_enabled(bool(body.get("on")))
                    except OSError:
                        return self._json({"error": "failed"}, 500)
                    return self._json(provider.public())
                if p == "/api/provider/test":  # 用存好的来源问一句"好"：{ok, ms} 或 {ok: false, error}（会花一点点那家的额度）
                    from . import claude_text
                    t0 = time.time()
                    try:
                        claude_text.ask("只回一个字：好", model=claude_text.HAIKU, timeout=90)   # API 时直接调接口
                        return self._json({"ok": True, "ms": int((time.time() - t0) * 1000)})
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)[-160:]})
                if p == "/api/setup":          # 引导走完 {"done": true} / 跳过 {"skipped": true} / 再走一遍 {"reset": true}
                    from . import onboard
                    try:
                        return self._json(onboard.mark(done=bool(body["done"]) if "done" in body else None,
                                                       skipped=bool(body["skipped"]) if "skipped" in body else None,
                                                       reset=bool(body.get("reset"))))
                    except OSError:
                        return self._json({"error": "failed"}, 500)
                if p == "/api/voice":          # 换声音 {voice, edge_voice, sapi_voice}、专有名词 {hotwords: 列表或一行一个} → 现在的
                    from . import voice_prefs
                    try:
                        voice_prefs.save(body if isinstance(body, dict) else {})
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    except OSError:
                        return self._json({"error": "failed"}, 500)
                    voice_prefs.apply(viewer.cfg)              # 后端共用的这份配置马上换上（专有名词下一场转写生效）
                    return self._json(voice_prefs.public(viewer.cfg))
                if p == "/api/summary/prefs":  # 纪要格式 {"template"?, "custom"?} → 现在的；不对 400 {"error": template|too_long}
                    from . import summary_prefs
                    try:
                        summary_prefs.save(body if isinstance(body, dict) else {})
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    except OSError:
                        return self._json({"error": "failed"}, 500)
                    return self._json(summary_prefs.public())
                if p == "/api/relay":          # 中继：{"mode": public|off} / 自己服务器的连接码 {"code"} / 回到公共中继 {"clear"} /
                    from . import relay_cfg     # 允许别人加入 {"invite_on": bool} → 现在的；不对 400 {"error": code|mode}
                    try:
                        if "invite_on" in body:
                            relay_cfg.set_invite(bool(body.get("invite_on")))
                        elif body.get("clear"):
                            relay_cfg.clear()
                        elif "mode" in body:
                            relay_cfg.set_mode(str(body.get("mode") or ""))
                        else:
                            relay_cfg.save(str(body.get("code") or ""))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    except OSError:
                        return self._json({"error": "failed"}, 500)
                    url = str(((load().get("share") or {}).get("base_url")) or "")
                    viewer.cfg.setdefault("share", {})["base_url"] = url
                    try:
                        viewer.share.reconfigure(url)
                    except Exception:
                        pass
                    cb = getattr(viewer, "on_invite", None)
                    if body.get("invite_on") and callable(cb):   # 开会时才打开邀请：语音桥马上进房间
                        try:
                            cb()
                        except Exception:
                            pass
                    return self._json(relay_cfg.public(viewer.cfg))
                if p == "/api/relay/test":     # 测中继：{ok, ms} 或 {ok: false, error: unset|unreachable|auth|livekit}
                    from . import relay_cfg
                    return self._json(relay_cfg.test(viewer.share))
                if p == "/api/voice/test":     # 试听：按现在选的声音念一句 → {ok, engine} 或 {ok: false, error: off|busy|live|failed|not_ready}
                    from . import voice_prefs
                    if voice_prefs.voice() == "off":
                        return self._json({"ok": False, "error": "off"})
                    fn = getattr(viewer, "voice_test", None)
                    if not callable(fn):
                        return self._json({"ok": False, "error": "not_ready"})
                    try:
                        eng = fn(str(body.get("text") or "").strip()[:120])
                    except RuntimeError as e:
                        return self._json({"ok": False, "error": str(e) if str(e) in ("busy", "live") else "failed"})
                    except Exception:
                        return self._json({"ok": False, "error": "failed"})
                    return self._json({"ok": True, "engine": eng} if eng else {"ok": False, "error": "failed"})
                from . import live_api
                if p in live_api.PATHS:        # 会中：打字问 AI、标记、先选中再说（zuhui/live_api.py）
                    out, code = live_api.post(viewer, p, body)
                    return self._json(out, code)
                from . import paper_api
                if p in paper_api.POST_PATHS:  # 第 6 批：批注、恢复历史版本、Word 交回方式（zuhui/paper_api.py）
                    out, code = paper_api.post(viewer, p, body)
                    return self._json(out, code)
                if p.startswith("/api/storage/"):  # 设置 → 存储：清录屏 / 自动删除 / 不用的模型 / 临时文件（zuhui/storage.py）
                    from . import storage
                    out, code = storage.post(viewer, p, body)
                    return self._json(out, code)
                if p == "/api/update":         # 大厅里点了「更新」：开会中、会后整理没做完 409；否则后台跑新版的 install.ps1（zuhui/updates.py）
                    from . import updates
                    out, code = updates.http_post(viewer, body)
                    return self._json(out, code)
                if p == "/api/profile/avatar":  # 设置里换头像 {"data": data: 地址或 base64}，移除 {"remove": true}
                    from . import host_profile
                    try:
                        if body.get("remove"):
                            host_profile.clear_avatar()
                        else:
                            host_profile.save_avatar(host_profile.decode_image(body.get("data")))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    except OSError:                # 文件被占着（Windows）之类：没换成，过一会儿再试
                        return self._json({"error": "failed"}, 500)
                    return self._json(_profile(viewer))
                if p == "/api/mic/device":     # 设置 → 语音 → 麦克风 {"mic": ""（系统默认）| 设备名}：存进 ~/.blurt/config.json 的 record.mic；开着会 409
                    from . import mic_prefs
                    out, code = mic_prefs.http_post(viewer, body)
                    return self._json(out, code)
                if p == "/api/mic/mute":       # 主持人麦克风静音 {"muted": true|false} → {"muted": 现在是否静音}（见 Ears.set_muted）
                    fn = getattr(viewer, "mic_mute", None)
                    want = body.get("muted") if isinstance(body, dict) else None
                    if isinstance(want, str) and want.strip().lower() in ("true", "1", "false", "0"):
                        want = want.strip().lower() in ("true", "1")
                    elif isinstance(want, int) and not isinstance(want, bool) and want in (0, 1):
                        want = bool(want)
                    if not isinstance(want, bool):
                        return self._json({"muted": _mic_state(viewer)["muted"], "error": "muted 要是 true 或 false"}, 400)
                    if not callable(fn):
                        return self._json({"muted": False, "error": "静音失败：麦克风未就绪"}, 503)
                    try:
                        return self._json({"muted": bool(fn(want))})
                    except Exception as e:
                        return self._json({"muted": _mic_state(viewer)["muted"], "error": repr(e)[:120]}, 500)
                if p == "/api/focus":          # 对比页：屏幕上正显示哪一节哪一段
                    sec = (body.get("section") or "").strip() if isinstance(body.get("section"), str) else ""
                    txt = str(body.get("text") or "")[:200]
                    # 翻到最顶上（还没到第一节）时 section 是空的：照样记下来（带 top），访客页才能跟着翻回顶上
                    try:
                        ri = int(body.get("ri")) if body.get("ri") is not None else None
                    except (TypeError, ValueError):
                        ri = None
                    try:
                        off = max(0.0, min(1.0, float(body.get("off") or 0)))
                    except (TypeError, ValueError):
                        off = 0.0
                    viewer.focus_k = max(getattr(viewer, "focus_k", 0) + 1, int(time.time() * 1000))   # 只增不减（后端重启也是）
                    viewer.focus = {"section": sec[:200] or None, "text": txt or None, "no": body.get("no"),
                                    "ri": ri, "off": off, "at": time.time(), "top": not sec and not txt, "k": viewer.focus_k}
                    sent = viewer.__dict__.setdefault("focus_sent", {})
                    sent[viewer.focus_k] = time.time()
                    for old in [k for k in sent if k < viewer.focus_k - 200]:
                        sent.pop(old, None)
                    cb = getattr(viewer, "on_focus", None)
                    if cb is not None:           # 走房间的数据通道马上推给访客
                        try:
                            cb({k: viewer.focus.get(k) for k in ("section", "no", "text", "top", "ri", "off", "k")})
                        except Exception:
                            pass
                    self._json({"ok": True})
                elif p == "/api/para/source":    # 编辑：页面上的一段 → 稿子里的原文
                    self._json(viewer.para_source(str(body.get("text") or "")))
                elif p == "/api/para/save":      # 编辑：保存这一段
                    self._json(viewer.para_save(body.get("index"), str(body.get("old") or ""), str(body.get("text") or ""),
                                                by=str(body.get("by") or "")[:32] or None))   # by：共同主持人改的（guest_act 填）
                elif p.startswith("/api/meetings/") and p.endswith("/title"):    # 改会议名（开着的会、以往的会都行；空串回到自动起的名字）
                    fn = getattr(viewer.meetings, "rename", None)
                    if not callable(fn):
                        return self._json({"ok": False, "error": "重命名失败：服务未就绪"}, 503)
                    m = fn(p.split("/")[3], str(body.get("title") or ""))
                    if not m:
                        return self._json({"ok": False, "error": "会议不存在"}, 404)
                    cb = getattr(viewer, "on_renamed", None)
                    if callable(cb):                     # app：散会收尾的进度条上那个名字也跟着改
                        try:
                            cb(m)
                        except Exception:
                            pass
                    self._json({"ok": True, "meeting": m})
                elif p == "/api/paper/switch":   # 会中切到摊开过的另一篇（加论文走 /api/paper：开会时就是加进这场会并切过去）
                    fn = getattr(viewer, "on_switch_paper", None)
                    if not callable(fn):
                        return self._json({"ok": False, "error": "切换文档失败：服务未就绪"}, 503)
                    try:
                        self._json({"ok": True, "paper": fn(str(body.get("id") or ""))})
                    except (KeyError, ValueError, RuntimeError) as e:
                        self._json({"ok": False, "error": str(e)}, 400)
                elif p == "/api/carry":          # 大厅里勾掉 / 取消勾掉一条遗留 {"id": "<会议编号>/<key>", "done"}
                    from . import past
                    try:
                        r = past.tick(viewer.meetings, str(body.get("id") or ""), bool(body.get("done", True)))
                    except ValueError as e:
                        return self._json({"ok": False, "error": str(e)}, 404 if str(e) == "not_found" else 400)
                    except OSError:
                        return self._json({"ok": False, "error": "failed"}, 500)
                    self._json({"ok": True, **r})
                elif p == "/api/past/ask":       # 回顾里问往期会议 {"question", "mids"?}（会调模型；问题最长 past.ASK_MAX 字）
                    try:
                        r = viewer.past_ask(body.get("question"), body.get("mids"))
                    except ValueError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    except RuntimeError as e:
                        return self._json({"ok": False, "error": str(e)[:200] or "failed"}, 502)
                    self._json({"ok": True, **r})
                elif p == "/api/digests":        # 回顾 → 阶段汇总：选几场会写一份 {"mids": [...]}（后台调模型）
                    try:
                        d = viewer.start_digest(body.get("mids"))
                    except ValueError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    except RuntimeError as e:
                        return self._json({"ok": False, "error": str(e) or "busy"}, 409)
                    self._json({"ok": True, "digest": d})
                elif p.startswith("/api/meetings/") and p.endswith("/summary"):  # 回顾 → 纪要：换一种格式重新生成 {"template"}（会调模型）
                    try:
                        job = viewer.regen_summary(p.split("/")[3], str(body.get("template") or ""))
                    except ValueError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    except KeyError:
                        return self._json({"ok": False, "error": "not_found"}, 404)
                    except RuntimeError as e:
                        return self._json({"ok": False, "error": str(e) or "busy"}, 409)
                    self._json({"ok": True, "job": job})
                elif p.startswith("/api/meetings/") and p.endswith("/transcript"):   # 回顾里手改逐字稿 {op: line|replace|speaker|undo}（zuhui/transcript_edit.py）
                    from . import transcript_edit
                    out, code = transcript_edit.post(viewer, p.split("/")[3], body)
                    self._json(out, code)
                elif p == "/api/meetings/sample/delete":   # 第 8 批：删掉示例会议（记下来以后不再出现，zuhui/sample.py）
                    from . import sample
                    self._json(sample.delete(viewer.meetings))
                elif p.startswith("/api/meetings/") and p.endswith("/delete"):   # 回顾里删一场会（会议挪进回收站，录屏送进 Windows 回收站）
                    busy = viewer.busy_session() if callable(getattr(viewer, "busy_session", None)) else None
                    self._json(viewer.meetings.delete(p.split("/")[3], busy_session=busy))
                elif p == "/api/models":         # Claude 那张卡上点了一行：换对话模型 / 改稿模型 / 思考强度
                    fn = viewer.models_set
                    if not callable(fn):
                        return self._json({"ok": False, "error": "换模型失败：服务未就绪"}, 503)
                    part, value = str(body.get("part") or ""), str(body.get("value") or "")
                    if not part or not value:
                        return self._json({"ok": False, "error": "缺 part 或 value"}, 400)
                    try:
                        out = fn(part, value)
                        viewer._ready_cache = (0.0, None)     # 大厅"准备情况"里的型号马上跟着变
                        self._json(out)
                    except ValueError as e:
                        self._json({"ok": False, "error": str(e)}, 400)
                    except PermissionError as e:
                        self._json({"ok": False, "error": str(e) or "仅主持人可切换"}, 403)
                    except ConnectionError as e:     # 想换的模型现在连不上（比如没钱了）：没换，照实说
                        self._json({"ok": False, "error": str(e)}, 409)
                    except NotImplementedError as e:
                        self._json({"ok": False, "error": str(e) or "换模型失败：服务未就绪"}, 503)
                elif p == "/api/roles":          # 顶栏参会人卡片：设为共同主持人 / 改回参会者
                    from . import roles
                    try:
                        self._json({"ok": True, "person": roles.set_role(str(body.get("name") or ""), str(body.get("role") or ""))})
                    except ValueError as e:
                        self._json({"ok": False, "error": str(e)}, 400)
                elif p == "/api/roles/dismiss":  # 「上次是共同主持人，恢复？」的提醒：不是
                    from . import roles
                    roles.dismiss(str(body.get("name") or ""))
                    self._json({"ok": True})
                elif p == "/api/board/resolve":  # 对比页上给一条批注打了勾：看过了（ids：一处的几条一起，按改稿先后并）
                    if isinstance(body.get("ids"), list):
                        self._json(viewer.resolve_tasks(body.get("ids")))
                    else:
                        self._json(viewer.resolve_task(str(body.get("id") or "")))
                elif p == "/api/board/reject":   # 批注卡片上点了 ×：这条改动不要了，撤回原样
                    self._json(viewer.reject_task(str(body.get("id") or "")))
                elif p == "/api/autosave" or (p.startswith("/api/meetings/") and p.endswith("/save")):
                    from . import export                 # 第 5 批：自动保存的设置；纪要存成 Word / PDF、会议包，存到你选的位置
                    code, out = export.http_post(viewer, p, body)
                    self._json(out, code)
                elif p == "/api/recording/save":   # 回顾：存录屏（复制到你选的位置）
                    src = viewer.video_path(str(body.get("meeting") or ""))
                    to = Path(str(body.get("to") or "")).expanduser()
                    if not src or not src.exists():
                        return self._json({"ok": False, "error": "此会议没有录屏"}, 404)
                    if not to.is_absolute() or not to.parent.is_dir():
                        return self._json({"ok": False, "error": "保存失败：路径无效"}, 400)
                    if to.suffix.lower() != src.suffix.lower():
                        to = to.with_suffix(src.suffix)
                    try:
                        shutil.copyfile(src, to)
                    except OSError as e:
                        return self._json({"ok": False, "error": f"保存失败：{e}"}, 500)
                    self._json({"ok": True, "path": str(to)})
                elif p == "/api/paper/writeback":   # 从 Word 转来的论文：会上改完的另存成原 .docx 旁边的「（会上修订）.docx」
                    fn = getattr(viewer, "on_writeback", None)
                    if not callable(fn):
                        return self._json({"ok": False, "error": "写回 Word 失败：服务未就绪"}, 503)
                    try:
                        r = fn()
                        self._json(r, 200 if r.get("ok") else 400)
                    except Exception as e:
                        self._json({"ok": False, "error": repr(e)[:200]}, 500)
                elif p == "/api/paper/clear":   # 大厅里点「移除」：没有论文了，之后开的会只记逐字稿（mode=chat）
                    fn = getattr(viewer, "on_clear_paper", None)
                    try:
                        if callable(fn):
                            fn()
                        else:
                            viewer.clear_paper()
                        self._json({"ok": True})
                    except RuntimeError as e:
                        self._json({"ok": False, "error": str(e)}, 409)
                    except Exception as e:
                        self._json({"ok": False, "error": f"移除失败：{e}"[:200]}, 500)
                elif p == "/api/paper":        # 拖进来一份论文：导入并切成当前论文
                    path = (body.get("path") or "").strip()
                    if not path:
                        return self._json({"ok": False, "error": "缺 path（请求体要是 UTF-8 JSON：{\"path\": \"<本机文件路径>\"}）"}, 400)
                    try:
                        info = viewer.on_paper(path) if viewer.on_paper else viewer.set_mirror_from(path)
                        self._json({"ok": True, "paper": info})
                    except Exception as e:
                        self._json({"ok": False, "error": str(e)}, 400)
                elif p == "/api/meeting/start":     # 开会：Bloom 只录它自己的窗口（不弹框选，切到别的软件也只录它）
                    fn = getattr(viewer, "on_start_now", None)
                    if callable(fn):                # 嗓子空闲退出过：趁录屏起来的这几秒先拉起
                        try:
                            fn()
                        except Exception:
                            pass
                    self._json({"ok": _deep_link("blurt://meeting")})
                elif p == "/api/meeting/end":       # 散会：后端先把会散了（页面马上变成"已散会"），录屏在后台收尾
                    fn = getattr(viewer, "on_end_now", None)
                    if callable(fn):
                        try:
                            fn()
                            self._json({"ok": True})
                        except Exception as e:
                            self._json({"ok": False, "error": repr(e)[:200]}, 500)
                    else:
                        self._json({"ok": False, "error": "no end_now"}, 501)
                elif p == "/api/meeting/stop":
                    self._json({"ok": _deep_link("blurt://stop")})
                elif p == "/api/board/reset":
                    viewer.board.reset()
                    viewer.ensure_base(rebase=True)
                    viewer._sig = None
                    self._json({"ok": True})
                else:
                    self._send(404, b"not found", "text/plain")

        self.ensure_base()
        threading.Thread(target=self._loop, name="view-builder", daemon=True).start()
        class Srv(ThreadingHTTPServer):
            # Windows 上 SO_REUSEADDR 会让第二个进程也绑上同一个端口：关掉它，再加独占，第二个后端直接绑不上
            allow_reuse_address = False
            # 排队上限（默认 5）：进程卡几秒时排满，桌面端 500 毫秒的连接探测就判"后端没了"、去拉第二个
            request_queue_size = 128
            daemon_threads = True

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        srv = Srv(("127.0.0.1", self.port), H)
        threading.Thread(target=srv.serve_forever, name="view-http", daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}/"
        if open_browser:
            try:
                webbrowser.open(self.url)
            except Exception:
                pass
        return srv

    def stop(self):
        self._stop.set()
