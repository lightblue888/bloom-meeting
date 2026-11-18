# -*- coding: utf-8 -*-
"""论文上打字批注（第 6 批）：会中参会的人（主持人、共同主持人、访客）在论文某一段上留批注、回复、标为已解决；
主持人和共同主持人能删。存进这场会的记录 <会议目录>/comments.json，回顾里能看（只读）。

    {"version": 1, "items": [{
        "id": "c3", "paper": 论文编号, "key": 那一段的字（只留字母数字汉字）的开头, "quote": 那一段开头一截原文,
        "section": 在哪一节, "ri": 第几段（对不上字时退回用它）,
        "by": 谁, "at": ISO 时间, "t": 会上第几分几秒 "12:03",
        "text": 批注（≤ TEXT_MAX 字）, "resolved": bool, "resolved_by": 谁,
        "replies": [{"id": "c3-1", "by", "at", "t", "text"}]}]}

论文页（对比页 iframe）里画：每段左边一个小气泡（这一段有几条没解决的），点开是浮在正文上的卡片，正文排版不动；
悬停一段时左边出一个"批注"按钮。卡片和按钮的脚本、样式在 INJECT（view._inject 拼在页尾）。论文页不自己拉批注：
外壳（主持人的 Bloom、访客页）把批注 postMessage 给它（{zhComments: {...}}），它发批注走 fetch('/api/comment')
（访客页里经中继转给主持人这边）。出错抛 ValueError(代码)：empty / too_long / not_found / full / cohost_only。"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

FILE = "comments.json"
TEXT_MAX = 500
QUOTE_MAX = 160
KEY_MAX = 40
ITEMS_MAX = 1000
REPLIES_MAX = 100
NAME_MAX = 32
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_lock = threading.RLock()


def _clean(s, n: int) -> str:
    return _CTRL.sub("", str(s or "").replace("\r\n", "\n").replace("\r", "\n")).strip()[:n]


def clean_text(s) -> str:
    t = _CTRL.sub("", str(s or "").replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not t:
        raise ValueError("empty")
    if len(t) > TEXT_MAX:
        raise ValueError("too_long")
    return t


def load(d: Path | None) -> list[dict]:
    if d is None:
        return []
    try:
        x = json.loads((Path(d) / FILE).read_text(encoding="utf-8"))
        items = x.get("items") if isinstance(x, dict) else None
        return [i for i in items if isinstance(i, dict) and i.get("id")] if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _save(d: Path, items: list[dict]) -> None:
    p = Path(d) / FILE
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clock(sec: float | None) -> str | None:
    if sec is None:
        return None
    s = int(max(0, sec))
    return f"{s // 60:02d}:{s % 60:02d}"


def _next_id(items: list[dict]) -> str:
    n = 0
    for i in items:
        m = re.match(r"c(\d+)$", str(i.get("id") or ""))
        if m:
            n = max(n, int(m.group(1)))
    return f"c{n + 1}"


def add(d: Path, paper: str | None, anchor: dict | None, text, by: str, sec: float | None = None) -> dict:
    t = clean_text(text)
    a = anchor if isinstance(anchor, dict) else {}
    try:
        ri = int(a.get("ri")) if a.get("ri") is not None else None
    except (TypeError, ValueError):
        ri = None
    with _lock:
        items = load(d)
        if len(items) >= ITEMS_MAX:
            raise ValueError("full")
        it = {"id": _next_id(items), "paper": _clean(paper, 16) or None,
              "key": re.sub(r"[^0-9A-Za-zÀ-ɏͰ-Ͽ一-鿿]+", "", str(a.get("key") or ""))[:KEY_MAX],
              "quote": _clean(a.get("quote"), QUOTE_MAX), "section": _clean(a.get("section"), 120) or None, "ri": ri,
              "by": _clean(by, NAME_MAX) or "?", "at": _now(), "t": _clock(sec), "text": t,
              "resolved": False, "resolved_by": None, "replies": []}
        items.append(it)
        _save(d, items)
    return it


def _find(items: list[dict], cid) -> dict:
    it = next((i for i in items if i.get("id") == str(cid or "")), None)
    if it is None:
        raise ValueError("not_found")
    return it


def reply(d: Path, cid, text, by: str, sec: float | None = None) -> dict:
    t = clean_text(text)
    with _lock:
        items = load(d)
        it = _find(items, cid)
        reps = it.setdefault("replies", [])
        if len(reps) >= REPLIES_MAX:
            raise ValueError("full")
        n = max([int(str(r.get("id", "")).rsplit("-", 1)[-1]) for r in reps if str(r.get("id", "")).rsplit("-", 1)[-1].isdigit()]
                + [0]) + 1
        reps.append({"id": f"{it['id']}-{n}", "by": _clean(by, NAME_MAX) or "?", "at": _now(), "t": _clock(sec), "text": t})
        _save(d, items)
    return it


def resolve(d: Path, cid, resolved: bool, by: str) -> dict:
    with _lock:
        items = load(d)
        it = _find(items, cid)
        it["resolved"] = bool(resolved)
        it["resolved_by"] = (_clean(by, NAME_MAX) or None) if resolved else None
        _save(d, items)
    return it


def delete(d: Path, cid, reply_id=None) -> None:
    """删一条批注（连同回复），或其中一条回复。谁能删由调用方查（主持人、共同主持人）。"""
    with _lock:
        items = load(d)
        it = _find(items, cid)
        if reply_id:
            reps = it.get("replies") or []
            keep = [r for r in reps if r.get("id") != str(reply_id)]
            if len(keep) == len(reps):
                raise ValueError("not_found")
            it["replies"] = keep
        else:
            items = [i for i in items if i is not it]
        _save(d, items)


def for_paper(items: list[dict], paper: str | None) -> list[dict]:
    return [i for i in items if not paper or not i.get("paper") or i.get("paper") == paper]


# ======================================================================
# 接口（view.py 转过来；访客的经 view.guest_act）
# ======================================================================
def post(viewer, body: dict, by: str | None = None, guest: bool = False) -> tuple[dict, int]:
    """POST /api/comment {op: add|reply|resolve|delete, ...} → ({"ok", "comments": 这场会的全部批注}, 状态码)。
    add {paper, anchor: {key, quote, section, ri}, text}；reply {id, text}；resolve {id, resolved}；delete {id, reply?}。"""
    from . import roles
    body = body if isinstance(body, dict) else {}
    lib = getattr(viewer, "meetings", None)
    if lib is None or not lib.current():
        return {"ok": False, "error": "not_live"}, 409
    d = lib.live_dir()
    if guest:
        name = str(by or "").strip()[:NAME_MAX]
        if not name:
            return {"ok": False, "error": "who"}, 403
    else:
        import getpass
        cfg = getattr(viewer, "cfg", None) or {}
        name = str((cfg.get("meeting") or {}).get("me") or getpass.getuser() or "主持人")
    op = str(body.get("op") or "add")
    try:
        sec = lib.elapsed()
    except Exception:
        sec = None
    try:
        if op == "add":
            add(d, body.get("paper"), body.get("anchor"), body.get("text"), name, sec)
        elif op == "reply":
            reply(d, body.get("id"), body.get("text"), name, sec)
        elif op == "resolve":
            resolve(d, body.get("id"), bool(body.get("resolved", True)), name)
        elif op == "delete":
            if guest and not roles.is_cohost(name):
                return {"ok": False, "error": "cohost_only"}, 403
            delete(d, body.get("id"), body.get("reply"))
        else:
            return {"ok": False, "error": "op"}, 400
    except ValueError as e:
        code = str(e)
        return {"ok": False, "error": code}, 404 if code == "not_found" else 400
    except OSError:
        return {"ok": False, "error": "failed"}, 500
    log = getattr(viewer, "on_log", None)
    if callable(log):
        try:
            log("live_act", text=f"{name} 批注：{op} {str(body.get('text') or body.get('id') or '')[:60]}")
        except Exception:
            pass
    return {"ok": True, "comments": load(d)}, 200


def current(viewer) -> list[dict]:
    """这场会（开着的）的批注；没开会：最近一场的。"""
    lib = getattr(viewer, "meetings", None)
    if lib is None:
        return []
    if lib.current():
        return load(lib.live_dir())
    try:
        last = lib.list()
        return load(lib.root / str(last[0]["id"])) if last else []
    except Exception:
        return []


# ======================================================================
# 论文页里的脚本和样式（view._inject 拼在页尾）
# ======================================================================
TEXT = {
    "add": ("批注", "Comment"), "ph_new": ("写批注", "Add a comment"), "ph_reply": ("回复 ", "Reply to "),
    "send": ("发送", "Send"), "cancel": ("取消", "Cancel"), "reply": ("回复", "Reply"),
    "resolve": ("解决", "Resolve"), "reopen": ("重新打开", "Reopen"), "resolved": ("已解决", "Resolved"),
    "del": ("删除", "Delete"), "send_fail": ("发送失败：", "Send failed: "), "del_fail": ("删除失败：", "Delete failed: "),
    "e_too_long": ("超过 500 字", "over 500 characters"), "e_empty": ("是空的", "it's empty"),
    "e_not_live": ("会议没在开", "the meeting isn't on"), "e_cohost_only": ("只有主持人和共同主持人能删", "only the host and co-hosts can delete"),
    "e_not_found": ("这条已经删了", "it was deleted"), "e_full": ("批注太多了", "too many comments"),
    "e_failed": ("请重试", "try again"),
}

INJECT = r"""
<style id="zh-cm-css">
.zh-cm-pin, #zh-cm-add { position: absolute; left: 3px; top: 6px; z-index: 4; width: 22px; height: 22px; padding: 0; border-radius: 6px;
  display: inline-grid; place-items: center; cursor: pointer; font: 600 10px/1 var(--sans); font-variant-numeric: tabular-nums;
  border: 1px solid var(--zh-ctl-line, var(--rule)); background: var(--zh-ctl, var(--sheet)); color: var(--muted); }
.sect > .zh-cm-pin, .sect > #zh-cm-add { top: 50%; transform: translateY(-50%); }
.zh-cm-pin.is-open { background: #d99a2b; border-color: #d99a2b; color: #1a1408; }
.zh-cm-pin.is-done { opacity: .55; }
.zh-cm-pin:hover, .zh-cm-pin.on, #zh-cm-add:hover { border-color: var(--zh-accent); color: var(--ink); }
.zh-cm-pin.is-open:hover, .zh-cm-pin.is-open.on { color: #1a1408; box-shadow: 0 0 0 3px rgba(217,154,43,.25); }
.zh-cm-pin svg, #zh-cm-add svg { display: block; }
.zh-cm-pin[data-n]::after { content: attr(data-n); position: absolute; right: -6px; top: -6px; min-width: 14px; height: 14px; padding: 0 3px;
  box-sizing: border-box; border-radius: 7px; background: var(--paper); color: var(--ink); border: 1px solid var(--zh-ctl-line, var(--rule));
  font: 600 9px/12px var(--sans); text-align: center; }
#zh-cm-add { opacity: 0; pointer-events: none; transition: opacity .12s; }
#zh-cm-add.show { opacity: .9; pointer-events: auto; }
@media (min-width: 900px) { .zh-cm-pin, #zh-cm-add { left: 18px; } }
@media (min-width: 1200px) { .zh-cm-pin, #zh-cm-add { left: 34px; } }
#zh-cm-card { position: absolute; z-index: 60; box-sizing: border-box; width: min(360px, calc(100vw - 24px)); max-height: min(70vh, 560px);
  display: flex; flex-direction: column; background: var(--zh-ctl, var(--sheet)); border: 1px solid var(--zh-ctl-line, var(--rule)); border-radius: 8px;
  box-shadow: 0 0 0 1px rgba(255,255,255,.06), 0 10px 28px rgba(0,0,0,.45); font: 400 13px/1.55 var(--sans); color: var(--ink); text-align: left; }
.zh-cm-list { overflow-y: auto; padding: 4px 12px; scrollbar-width: thin; }
.zh-cm-list:empty { display: none; }
.zh-cm-th { padding: 9px 0; }
.zh-cm-th + .zh-cm-th { border-top: 1px solid var(--zh-ctl-line, var(--rule)); }
.zh-cm-th.is-done > .zh-cm-msg, .zh-cm-th.is-done .zh-cm-rep { opacity: .6; }
.zh-cm-q { margin: 0 0 6px; color: var(--muted); font-size: 12px; font-style: italic; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zh-cm-head { display: flex; align-items: center; gap: 7px; min-height: 22px; }
.zh-cm-who { font-weight: 600; font-size: 12px; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: color-mix(in srgb, var(--c, var(--ink)) var(--zh-nm, 55%), #000); }
html:root[data-theme="dark"] { --zh-nm: 100%; }
@media (prefers-color-scheme: dark) { html:root:not([data-theme="light"]) { --zh-nm: 100%; } }
.zh-cm-t { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.zh-cm-sp { flex: 1; }
.zh-cm-st { color: var(--muted); font-size: 11px; }
.zh-cm-btn { font: inherit; font-size: 12px; padding: 2px 8px; border-radius: 5px; border: 1px solid transparent; background: transparent;
  color: var(--muted); cursor: pointer; white-space: nowrap; }
.zh-cm-btn:hover { background: var(--zh-ctl-hi, transparent); color: var(--ink); }
.zh-cm-btn.w { min-width: 64px; text-align: center; }
.zh-cm-x { width: 22px; height: 22px; padding: 0; font-size: 14px; line-height: 1; }
.zh-cm-x:hover { color: var(--del); }
.zh-cm-msg { margin: 2px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.zh-cm-rep { margin: 7px 0 0 12px; padding-left: 9px; border-left: 2px solid var(--zh-ctl-line, var(--rule)); }
.zh-cm-acts { display: flex; gap: 2px; margin-top: 4px; }
.zh-cm-new { border-top: 1px solid var(--zh-ctl-line, var(--rule)); padding: 9px 12px 10px; }
.zh-cm-list:empty + .zh-cm-new { border-top: 0; }
.zh-cm-to { display: flex; align-items: center; gap: 6px; height: 20px; font-size: 12px; color: var(--muted); }
.zh-cm-to span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zh-cm-to.off { visibility: hidden; }
#zh-cm-card textarea { display: block; width: 100%; box-sizing: border-box; min-height: 64px; max-height: 180px; resize: vertical; margin-top: 2px;
  font: 400 13px/1.5 var(--sans); color: var(--ink); background: var(--paper); border: 1px solid var(--zh-ctl-line, var(--rule)); border-radius: 6px;
  padding: 7px 9px; outline: none; }
#zh-cm-card textarea:focus { border-color: var(--zh-accent); }
.zh-cm-bar { display: flex; align-items: center; gap: 6px; margin-top: 7px; }
.zh-cm-n { width: 64px; color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.zh-cm-err { flex: 1; min-width: 0; color: var(--del); font-size: 12px; line-height: 1.35; height: 2.7em; overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.zh-cm-send { background: var(--zh-accent) !important; border-color: var(--zh-accent) !important; color: #fff !important; font-weight: 600; }
.zh-cm-send:disabled { opacity: .5; cursor: default; }
</style>
<script>
(function(){
  var docs = document.querySelector('.docs'); if (!docs) return;
  var READONLY = __CM_RO__;
  if (READONLY) {           /* PPT 讲稿：只能看，"编辑"按钮去掉 */
    var eb = document.getElementById('zh-editbtn'); if (eb && eb.parentNode) eb.parentNode.removeChild(eb);
    if (window.__zhSend) setTimeout(function(){ window.__zhSend(); }, 0);
  }
  var T = __CM_T__;
  var ICON = '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z"/></svg>';
  var ICON_ADD = '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z"/><path d="M8 5.2v3.6M6.2 7h3.6"/></svg>';
  var ST = {items: [], can: {add: false, del: false}, colors: {}};
  var card = null, cardRow = null, replyTo = null;
  function kOf(s){ return (s || '').replace(/[^0-9A-Za-zÀ-ɏͰ-Ͽ一-鿿]+/g, ''); }
  function norm(s){ return (s || '').replace(/\s+/g, ' ').trim(); }
  function esc(s){ var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  function rows(){ return Array.prototype.slice.call(docs.querySelectorAll('.row, .sect')); }
  function textOf(el){
    var c = el.cloneNode(true);
    Array.prototype.forEach.call(c.querySelectorAll('del, .notes, .zh-pins, .zh-note, .rowhead, .zh-cm-pin, #zh-cm-add'), function(x){ x.parentNode.removeChild(x); });
    return c.textContent || '';
  }
  function sectOf(el){
    var s = el.classList.contains('sect') ? el : null, x = el;
    while (!s && x) { x = x.previousElementSibling || (x.parentNode && x.parentNode !== docs ? x.parentNode : null); if (x && x.classList && x.classList.contains('sect')) s = x; }
    return s ? norm(textOf(s)).slice(0, 120) : null;
  }
  function anchorOf(el, i){ var t = textOf(el); return {key: kOf(t).slice(0, 40), quote: norm(t).slice(0, 160), section: sectOf(el), ri: i}; }
  /* 一条批注贴在哪一段：开头的字对得上的那段；对不上退回原来的位置；都没有就放在开头 */
  function hostOf(it, list, keys){
    var k = it.key || '';
    if (k.length >= 4) {
      for (var i = 0; i < list.length; i++) { if (keys[i] && (keys[i].indexOf(k) === 0 || (keys[i].length >= 6 && k.indexOf(keys[i]) === 0))) return {el: list[i], exact: true}; }
      var k2 = k.slice(0, 12);
      for (var j = 0; j < list.length; j++) { if (k2.length >= 6 && keys[j].indexOf(k2) >= 0) return {el: list[j], exact: false}; }
    }
    if (it.ri != null && list[it.ri]) return {el: list[it.ri], exact: false};
    return {el: list[0] || null, exact: false};
  }
  function paint(){
    Array.prototype.forEach.call(docs.querySelectorAll('.zh-cm-pin'), function(x){ x.parentNode.removeChild(x); });
    var list = rows(), keys = list.map(function(el){ return kOf(textOf(el)); });
    var by = new Map();
    (ST.items || []).forEach(function(it){
      var h = hostOf(it, list, keys); if (!h.el) return;
      it.__exact = h.exact;
      if (!by.has(h.el)) by.set(h.el, []);
      by.get(h.el).push(it);
    });
    by.forEach(function(its, el){
      var open = its.filter(function(x){ return !x.resolved; }).length;
      var b = document.createElement('button'); b.type = 'button';
      b.className = 'zh-cm-pin ' + (open ? 'is-open' : 'is-done') + (cardRow === el ? ' on' : '');
      b.innerHTML = ICON;
      if (its.length > 1) b.setAttribute('data-n', String(its.length));     /* 条数用 CSS 画（不进正文的字：章节名、查找、编辑都不受影响） */
      b.setAttribute('aria-label', T.add + ' ' + its.length);
      b.zhItems = its;
      b.onclick = function(e){ e.stopPropagation(); e.preventDefault(); if (cardRow === el && card) return close(); openCard(el, its); };
      el.appendChild(b);
    });
    if (card && cardRow) { var its2 = by.get(cardRow) || []; fill(its2); }
  }
  function close(){
    if (card && card.parentNode) card.parentNode.removeChild(card);
    card = null; cardRow = null; replyTo = null;
    Array.prototype.forEach.call(docs.querySelectorAll('.zh-cm-pin.on'), function(x){ x.classList.remove('on'); });
  }
  /* 卡片浮在那一段下面（放不下就放上面，都放不下贴着窗口底边），左边和正文对齐：批注的那段字一直看得见 */
  function place(){
    if (!card || !cardRow) return;
    var r = cardRow.getBoundingClientRect(), para = cardRow.querySelector('.para') || cardRow;
    var pr = para.getBoundingClientRect(), w = card.offsetWidth, h = card.offsetHeight, vh = window.innerHeight;
    var left = Math.max(12, Math.min(pr.left, window.innerWidth - w - 16));
    var below = r.bottom + 4, above = r.top - h - 4;
    var y = below + h <= vh - 8 ? below : above >= 8 ? above : Math.max(8, vh - h - 8);
    card.style.left = (left + window.scrollX) + 'px';
    card.style.top = (y + window.scrollY) + 'px';
  }
  function api(body){
    return fetch('/api/comment', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function(r){ return r.json(); });
  }
  function errText(prefix, code){ return prefix + (T['e_' + code] || code || T.e_failed); }
  function fill(its){
    if (!card) return;
    var listBox = card.querySelector('.zh-cm-list'), html = '';
    its.forEach(function(it){
      var col = (ST.colors || {})[it.by];
      html += '<div class="zh-cm-th' + (it.resolved ? ' is-done' : '') + '" data-id="' + esc(it.id) + '">'
        + (!it.__exact && it.quote ? '<p class="zh-cm-q" title="' + esc(it.quote) + '">' + esc(it.quote) + '</p>' : '')
        + '<div class="zh-cm-head"><span class="zh-cm-who"' + (col ? ' style="--c:' + esc(col) + '"' : '') + ' title="' + esc(it.by) + '">' + esc(it.by) + '</span>'
        + (it.t ? '<span class="zh-cm-t">' + esc(it.t) + '</span>' : '') + '<span class="zh-cm-sp"></span>'
        + (it.resolved ? '<span class="zh-cm-st">' + T.resolved + '</span>' : '')
        + (ST.can.del ? '<button type="button" class="zh-cm-btn zh-cm-x" data-a="del" aria-label="' + T.del + '">×</button>' : '') + '</div>'
        + '<p class="zh-cm-msg">' + esc(it.text) + '</p>';
      (it.replies || []).forEach(function(rp){
        var c2 = (ST.colors || {})[rp.by];
        html += '<div class="zh-cm-rep" data-rid="' + esc(rp.id) + '"><div class="zh-cm-head"><span class="zh-cm-who"' + (c2 ? ' style="--c:' + esc(c2) + '"' : '') + ' title="' + esc(rp.by) + '">' + esc(rp.by) + '</span>'
          + (rp.t ? '<span class="zh-cm-t">' + esc(rp.t) + '</span>' : '') + '<span class="zh-cm-sp"></span>'
          + (ST.can.del ? '<button type="button" class="zh-cm-btn zh-cm-x" data-a="delr" aria-label="' + T.del + '">×</button>' : '') + '</div>'
          + '<p class="zh-cm-msg">' + esc(rp.text) + '</p></div>';
      });
      if (ST.can.add) html += '<div class="zh-cm-acts"><button type="button" class="zh-cm-btn" data-a="reply">' + T.reply + '</button>'
        + '<button type="button" class="zh-cm-btn w" data-a="res">' + (it.resolved ? T.reopen : T.resolve) + '</button></div>';
      html += '</div>';
    });
    listBox.innerHTML = html;
    Array.prototype.forEach.call(listBox.querySelectorAll('button[data-a]'), function(b){
      var th = b.closest('.zh-cm-th'), id = th.getAttribute('data-id'), it = its.filter(function(x){ return x.id === id; })[0];
      b.onclick = function(e){
        e.stopPropagation();
        var a = b.getAttribute('data-a');
        if (a === 'reply') { replyTo = it; setTo(); var ta = card.querySelector('textarea'); if (ta) ta.focus(); return; }
        b.disabled = true;
        var req = a === 'res' ? {op: 'resolve', id: id, resolved: !it.resolved}
          : a === 'del' ? {op: 'delete', id: id} : {op: 'delete', id: id, reply: b.closest('.zh-cm-rep').getAttribute('data-rid')};
        api(req).then(function(d){
          if (d && d.ok) { if (replyTo && replyTo.id === id && a === 'del') { replyTo = null; setTo(); } take(d.comments); return; }
          b.disabled = false; showErr(errText(a === 'res' ? T.send_fail : T.del_fail, d && d.error));
        }).catch(function(){ b.disabled = false; showErr(errText(a === 'res' ? T.send_fail : T.del_fail, 'failed')); });
      };
    });
    if (!its.length && !ST.can.add) close();
    else place();
  }
  function showErr(s){ var e = card && card.querySelector('.zh-cm-err'); if (e) { e.textContent = s; e.title = s; } }
  function setTo(){
    if (!card) return;
    var to = card.querySelector('.zh-cm-to'), ta = card.querySelector('textarea'); if (!to || !ta) return;
    to.classList.toggle('off', !replyTo);
    to.querySelector('span').textContent = replyTo ? T.ph_reply + replyTo.by : '';
    ta.placeholder = replyTo ? T.ph_reply + replyTo.by : T.ph_new;
  }
  function mineOnly(list){ return (list || []).filter(function(x){ return !ST.paper || !x.paper || x.paper === ST.paper; }); }
  function take(list){
    /* 发完、删完、改完：后端回来这场会的全部批注，马上画上（外壳下一次也会推过来） */
    if (!Array.isArray(list)) return;
    ST.items = mineOnly(list); paint();
  }
  function openCard(el, its){
    close();
    cardRow = el;
    card = document.createElement('div'); card.id = 'zh-cm-card'; card.setAttribute('role', 'dialog');
    card.innerHTML = '<div class="zh-cm-list"></div>' + (ST.can.add ? '<div class="zh-cm-new"><div class="zh-cm-to off"><span></span>'
      + '<button type="button" class="zh-cm-btn zh-cm-x" data-a="unreply" aria-label="' + T.cancel + '">×</button></div>'
      + '<textarea maxlength="500" spellcheck="false"></textarea><div class="zh-cm-bar"><span class="zh-cm-n">0/500</span><span class="zh-cm-err"></span>'
      + '<button type="button" class="zh-cm-btn" data-a="cancel">' + T.cancel + '</button><button type="button" class="zh-cm-btn zh-cm-send" data-a="send" disabled>' + T.send + '</button></div></div>' : '');
    document.body.appendChild(card);
    card.addEventListener('click', function(e){ e.stopPropagation(); });
    card.addEventListener('mousedown', function(e){ e.stopPropagation(); });
    fill(its);
    Array.prototype.forEach.call(docs.querySelectorAll('.zh-cm-pin'), function(p){ p.classList.toggle('on', p.parentNode === el); });
    var ta = card.querySelector('textarea');
    if (ta) {
      var n = card.querySelector('.zh-cm-n'), send = card.querySelector('[data-a="send"]');
      var upd = function(){ n.textContent = ta.value.length + '/500'; send.disabled = !ta.value.trim(); };
      ta.placeholder = T.ph_new;
      ta.addEventListener('input', function(){ upd(); showErr(''); });
      var go = function(){
        var text = ta.value.trim(); if (!text || send.disabled) return;
        if (text.length > 500) { showErr(errText(T.send_fail, 'too_long')); return; }
        send.disabled = true;
        var list = rows(), i = list.indexOf(el);
        var req = replyTo ? {op: 'reply', id: replyTo.id, text: text} : {op: 'add', paper: PAPER_ID(), anchor: anchorOf(el, i), text: text};
        api(req).then(function(d){
          if (d && d.ok) { ta.value = ''; upd(); replyTo = null; setTo(); take(d.comments); return; }
          upd(); showErr(errText(T.send_fail, d && d.error));
        }).catch(function(){ upd(); showErr(errText(T.send_fail, 'failed')); });
      };
      send.onclick = go;
      card.querySelector('[data-a="cancel"]').onclick = close;
      card.querySelector('[data-a="unreply"]').onclick = function(){ replyTo = null; setTo(); ta.focus(); };
      ta.addEventListener('keydown', function(e){
        if (e.key === 'Escape') { e.preventDefault(); close(); }
        else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(); }
        e.stopPropagation();
      });
      setTimeout(function(){ ta.focus(); }, 0);
    }
    place();
  }
  function PAPER_ID(){ return ST.paper || null; }
  /* 悬停一段：左边出"批注"按钮（会上能批注的人才有） */
  var add = document.createElement('button'); add.type = 'button'; add.id = 'zh-cm-add'; add.innerHTML = ICON_ADD;
  add.setAttribute('aria-label', T.add); add.title = T.add;
  add.onclick = function(e){
    e.stopPropagation(); e.preventDefault();
    var el = add.parentNode; if (!el || el === document.body) return;
    var its = []; var pin = null;
    for (var i = 0; i < el.children.length; i++) { if (el.children[i].classList.contains('zh-cm-pin')) pin = el.children[i]; }
    if (pin) its = pin.zhItems || [];
    openCard(el, its);
  };
  function showAdd(el){
    if (!el || !docs.contains(el)) return;
    if (el.querySelector(':scope > .zh-cm-pin')) { add.classList.remove('show'); return; }
    if (add.parentNode !== el) el.appendChild(add);
    add.classList.add('show');
  }
  docs.addEventListener('mouseover', function(e){
    if (!ST.can.add || !e.target.closest) return;
    showAdd(e.target.closest('.row, .sect'));
  });
  /* 手机上没有悬停：点一下那一段，按钮出在它左边 */
  docs.addEventListener('click', function(e){
    if (!ST.can.add || !e.target.closest || e.target.closest('a, button, summary, .zh-editor')) return;
    var s = window.getSelection && window.getSelection(); if (s && !s.isCollapsed) return;
    showAdd(e.target.closest('.row, .sect'));
  });
  docs.addEventListener('mouseleave', function(){ add.classList.remove('show'); });
  document.addEventListener('click', function(e){ if (card && !(e.target.closest && e.target.closest('#zh-cm-card, .zh-cm-pin, #zh-cm-add'))) close(); });
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && card) close(); });
  window.addEventListener('resize', function(){ if (card) place(); });
  /* 外壳推来批注：{zhComments: {items, can: {add, del}, colors, paper}} */
  window.addEventListener('message', function(e){
    var d = e.data || {}; if (!d.zhComments) return;
    var c = d.zhComments, can = c.can || {};
    ST.paper = c.paper || null; ST.items = mineOnly(Array.isArray(c.items) ? c.items : []);
    ST.can = {add: !!can.add, del: !!can.del}; ST.colors = c.colors || {};
    if (!ST.can.add) add.classList.remove('show');
    paint();
  });
  try { if (window.parent !== window) window.parent.postMessage({zhCmReady: 1}, '*'); } catch (e) {}
})();
</script>
"""


def inject(lang: str = "zh", readonly: bool = False) -> str:
    en = lang == "en"
    t = {k: (v[1] if en else v[0]) for k, v in TEXT.items()}
    return INJECT.replace("__CM_T__", json.dumps(t, ensure_ascii=False).replace("</", "<\\/")).replace(
        "__CM_RO__", "true" if readonly else "false")
