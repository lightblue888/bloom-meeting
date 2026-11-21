// Runs inside the page (page.addInitScript, before the app's own scripts). Plain browser JS on purpose: no imports,
// no build step, nothing from the app. It provides:
//   · a controllable EventSource: the first message comes from the mocked HTTP response (page.route), later ones are
//     pushed by the test with __uiAudit.push('/api/mic/stream', {...}) — a real SSE response can't be held open and fed
//     from Playwright, and an ended stream would fire `error` (the app treats that as "the push channel is down");
//   · a layout-shift PerformanceObserver (CLS entries with their sources);
//   · check(): the geometry checks (overflow, clipped text, overlaps, covered or off-window controls, spills);
//   · mark()/diff(): bounding boxes of the key elements before and after a state change.
(() => {
  if (window.__uiAudit) return;
  const A = (window.__uiAudit = { shifts: [], sse: [], ids: new WeakMap(), n: 0 });

  // ------------------------------------------------------------------ EventSource
  class FakeEventSource extends EventTarget {
    constructor(url) {
      super();
      this.url = new URL(String(url), location.href).href;
      this.readyState = 0;
      this.withCredentials = false;
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      A.sse.push(this);
      fetch(this.url, { cache: 'no-store' })
        .then((r) => {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.text();
        })
        .then((body) => {
          if (this.readyState === 2) return;
          this.readyState = 1;
          this._fire('open', new Event('open'));
          for (const block of body.split(/\r?\n\r?\n/)) {
            const data = block
              .split(/\r?\n/)
              .filter((l) => l.startsWith('data:'))
              .map((l) => l.slice(5).replace(/^ /, ''))
              .join('\n');
            if (data) this._msg(data);
          }
        })
        .catch(() => {
          if (this.readyState === 2) return;
          this.readyState = 0;
          this._fire('error', new Event('error'));
        });
    }
    _fire(type, ev) {
      const h = this['on' + type];
      if (typeof h === 'function') h.call(this, ev);
      this.dispatchEvent(ev);
    }
    _msg(data) {
      if (this.readyState !== 1) return;
      this._fire('message', new MessageEvent('message', { data, origin: location.origin }));
    }
    close() {
      this.readyState = 2;
    }
  }
  FakeEventSource.CONNECTING = 0;
  FakeEventSource.OPEN = 1;
  FakeEventSource.CLOSED = 2;
  window.EventSource = FakeEventSource;
  /** push one message to every open stream whose URL contains `part`; returns how many got it */
  A.push = (part, data) => {
    let n = 0;
    for (const es of A.sse) {
      if (es.readyState === 1 && es.url.includes(part)) {
        es._msg(typeof data === 'string' ? data : JSON.stringify(data));
        n++;
      }
    }
    return n;
  };
  /** drop every open stream (as if the backend went away) */
  A.dropStreams = (part) => {
    for (const es of A.sse) if (es.readyState === 1 && (!part || es.url.includes(part))) {
      es.readyState = 0;
      es._fire('error', new Event('error'));
    }
  };

  // ------------------------------------------------------------------ helpers
  const rectOf = (r) => (r ? { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) } : null);
  function short(el) {
    if (!el || el.nodeType !== 1) return '?';
    let s = el.tagName.toLowerCase();
    const tid = el.getAttribute('data-testid');
    if (tid) return s + '[data-testid="' + tid + '"]';
    if (el.id) s += '#' + el.id;
    const cls = [...el.classList].filter((c) => !/^(is-|has-)|^(on|off|sel|dim|wide|hidden|grow|primary)$/.test(c)).slice(0, 2);
    if (cls.length) s += '.' + cls.join('.');
    const role = el.getAttribute('role');
    if (role && !cls.length) s += '[role=' + role + ']';
    return s;
  }
  function describe(el) {
    const parts = [];
    let e = el;
    for (let i = 0; e && e.nodeType === 1 && i < 4 && e !== document.body; i++, e = e.parentElement) {
      parts.unshift(short(e));
      if (i > 0 && e.getAttribute('data-testid')) break;
    }
    return parts.join(' > ');
  }
  /** what the report maps to source: the classes / test ids of the element and a few ancestors */
  function anchors(el) {
    const classes = [];
    const testids = [];
    let e = el;
    for (let i = 0; e && e.nodeType === 1 && i < 6 && e !== document.body; i++, e = e.parentElement) {
      for (const c of e.classList) if (!classes.includes(c)) classes.push(c);
      const t = e.getAttribute('data-testid');
      if (t && !testids.includes(t)) testids.push(t);
    }
    return { classes: classes.slice(0, 14), testids: testids.slice(0, 4) };
  }
  const textOf = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80) : '');
  A.describe = describe;

  function visible(el) {
    if (!el || !el.isConnected) return false;
    if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true })) return false;
    if (el.closest('[inert]')) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0.5 && r.height > 0.5;
  }
  const area = (r) => Math.max(0, r.right - r.left) * Math.max(0, r.bottom - r.top);
  const box = (r) => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom });
  function inter(a, b) {
    return { left: Math.max(a.left, b.left), top: Math.max(a.top, b.top), right: Math.min(a.right, b.right), bottom: Math.min(a.bottom, b.bottom) };
  }
  function union(rs) {
    const u = { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity };
    for (const r of rs) {
      u.left = Math.min(u.left, r.left);
      u.top = Math.min(u.top, r.top);
      u.right = Math.max(u.right, r.right);
      u.bottom = Math.max(u.bottom, r.bottom);
    }
    return u;
  }
  /** the padding box (what overflow clips to), without the scrollbars */
  function clipRect(el) {
    const r = el.getBoundingClientRect();
    const left = r.left + el.clientLeft;
    const top = r.top + el.clientTop;
    return { left, top, right: left + el.clientWidth, bottom: top + el.clientHeight };
  }
  const isCB = (cs) =>
    cs.position !== 'static' || cs.transform !== 'none' || cs.filter !== 'none' || /paint|layout|strict|content/.test(cs.contain) || /transform/.test(cs.willChange);
  /** the ancestors whose overflow clips `el` (abs/fixed boxes escape the ones between them and their containing block) */
  function clippers(el) {
    const out = [];
    const own = getComputedStyle(el);
    let skipToCB = own.position === 'absolute';
    let fixed = own.position === 'fixed';
    for (let a = el.parentElement; a && a !== document.documentElement; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (fixed) {
        if (cs.transform !== 'none' || cs.filter !== 'none' || /paint|layout|strict|content/.test(cs.contain)) fixed = false;
        else continue;
      } else if (skipToCB) {
        if (isCB(cs)) skipToCB = false;
        else continue;
      }
      const clipX = cs.overflowX !== 'visible' || /paint|strict|content/.test(cs.contain);
      const clipY = cs.overflowY !== 'visible' || /paint|strict|content/.test(cs.contain);
      if (clipX || clipY) {
        out.push({
          el: a,
          rect: clipRect(a),
          clipX,
          clipY,
          scrollX: cs.overflowX === 'auto' || cs.overflowX === 'scroll',
          scrollY: cs.overflowY === 'auto' || cs.overflowY === 'scroll',
        });
      }
      if (cs.position === 'absolute') skipToCB = true;
      else if (cs.position === 'fixed') fixed = true;
    }
    return out;
  }
  /** for text: the element holding it clips it too (its own overflow / "…"), then its clipping ancestors */
  function textClippers(el) {
    const cs = getComputedStyle(el);
    const clipX = cs.overflowX !== 'visible' || /paint|strict|content/.test(cs.contain);
    const clipY = cs.overflowY !== 'visible' || /paint|strict|content/.test(cs.contain);
    const own = clipX || clipY ? [{ el, rect: clipRect(el), clipX, clipY, scrollX: cs.overflowX === 'auto' || cs.overflowX === 'scroll', scrollY: cs.overflowY === 'auto' || cs.overflowY === 'scroll' }] : [];
    return [...own, ...clippers(el)];
  }
  /** is the text painted on top where it is (not under a dialog / popup / another box)? */
  function onTop(t) {
    const r = (t.vrects && t.vrects[0]) || t.rects[0];
    const x = Math.min(innerWidth - 1, Math.max(0, (r.left + r.right) / 2));
    const y = Math.min(innerHeight - 1, Math.max(0, (r.top + r.bottom) / 2));
    const h = document.elementFromPoint(x, y);
    t.coveredBy = null;
    if (!h || h === t.el || t.el.contains(h) || h.contains(t.el)) return true;
    // can't tell for text that lets the pointer through (tooltips, toasts)
    if (getComputedStyle(t.el).pointerEvents === 'none') return true;
    t.coveredBy = h;
    return false;
  }
  /** the part of `el` that can be seen: its box cut by every clipping ancestor and the window */
  function visibleRect(el, chain) {
    let r = box(el.getBoundingClientRect());
    for (const c of chain || clippers(el)) {
      if (c.clipX) r = { ...r, left: Math.max(r.left, c.rect.left), right: Math.min(r.right, c.rect.right) };
      if (c.clipY) r = { ...r, top: Math.max(r.top, c.rect.top), bottom: Math.min(r.bottom, c.rect.bottom) };
    }
    return inter(r, { left: 0, top: 0, right: innerWidth, bottom: innerHeight });
  }
  function upTo(el, stop, f) {
    for (let e = el; e; e = e.parentElement) {
      if (f(e, getComputedStyle(e))) return true;
      if (e === stop) break;
    }
    return false;
  }
  const hasEllipsis = (el, stop) => upTo(el, stop, (e, cs) => cs.textOverflow === 'ellipsis');
  const hasLineClamp = (el, stop) =>
    upTo(el, stop, (e, cs) => {
      const v = cs.getPropertyValue('-webkit-line-clamp') || cs.getPropertyValue('line-clamp');
      return !!v && v !== 'none';
    });

  const POPUP = '[role=dialog],[role=alertdialog],[role=menu],[role=tooltip],.mw-menu,.mw-tip,.mt-status-pop,.mt-card-pop,.mt-invite-pop,.mw-toast,.shell-toast,.g-toasts,.g-toast,.mt-drop-veil,.mt-share-pop,.mt-share-menu';
  const isPopup = (el) => !!(el && el.closest && el.closest(POPUP));
  /** the box a stacking overlay starts at: the nearest fixed / absolute ancestor with a z-index */
  function layerOf(el) {
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.position === 'fixed' || (cs.position === 'absolute' && cs.zIndex !== 'auto' && Number(cs.zIndex) > 0)) return e;
    }
    return document.body;
  }

  const INTERACTIVE =
    'button, a[href], input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab], [role=option], [role=menuitem], [role=menuitemradio], [role=menuitemcheckbox], [role=checkbox], [role=radio], [role=switch], [role=slider], [tabindex]:not([tabindex="-1"]), [contenteditable=""], [contenteditable=true]';
  const SKIP_TEXT = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'OPTION', 'TEXTAREA', 'TITLE']);

  /** each visible text node, trimmed, with its line boxes */
  function textItems() {
    const out = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (/\S/.test(n.data) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
    });
    const range = document.createRange();
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const el = n.parentElement;
      if (!el || SKIP_TEXT.has(el.tagName) || el.closest('svg')) continue;
      if (!visible(el)) continue;
      const s = n.data;
      const a = s.search(/\S/);
      const b = s.length - s.match(/\s*$/)[0].length;
      range.setStart(n, a);
      range.setEnd(n, b);
      const rects = [...range.getClientRects()].filter((r) => r.width > 0.5 && r.height > 0.5).map(box);
      if (!rects.length) continue;
      out.push({ node: n, el, text: s.trim().replace(/\s+/g, ' '), rects, bbox: union(rects) });
    }
    return out;
  }

  function hitsOutside(el, x, y) {
    const h = document.elementFromPoint(x, y);
    if (!h) return null;
    if (h === el || el.contains(h)) return null;
    // a <label> for this control, or the control's own shadow parts
    if (h.tagName === 'LABEL' && (h.control === el || h.contains(el))) return null;
    return h;
  }

  /**
   * The geometry checks. opts:
   *   userStrings: strings that came from the fixture (names, titles, model names): truncating them with "…" is the
   *     app's intended behaviour; truncating the app's own labels is not.
   *   appShell: the page is an app window (no page scrolling expected)
   *   ignore: selectors whose subtrees are not checked
   */
  A.check = (opts = {}) => {
    const F = [];
    const vw = innerWidth;
    const vh = innerHeight;
    const ustr = (opts.userStrings || []).filter((s) => s && s.length >= 3);
    const isUser = (t) => ustr.some((u) => (t.length >= 3 && u.includes(t)) || t.includes(u) || (t.endsWith('…') && u.startsWith(t.slice(0, -1).trim())));
    const ignored = (el) => (opts.scope && !el.closest(opts.scope)) || (opts.ignore || []).some((sel) => el.closest(sel));
    const add = (check, el, detail, extra = {}) => {
      if (el && ignored(el)) return;
      const f = { check, detail, ...extra };
      if (el) {
        f.el = describe(el);
        f.text = extra.text ?? textOf(el);
        f.rect = rectOf(el.getBoundingClientRect());
        Object.assign(f, anchors(el));
      }
      F.push(f);
    };
    const all = [...document.body.querySelectorAll('*')];
    const styles = new Map();
    const cs = (el) => {
      let s = styles.get(el);
      if (!s) styles.set(el, (s = getComputedStyle(el)));
      return s;
    };

    // 1. the page wider than the window
    const de = document.documentElement;
    const sw = Math.max(de.scrollWidth, document.body.scrollWidth);
    if (sw > vw + 1) {
      const wide = all.filter((el) => {
        const r = el.getBoundingClientRect();
        if (r.right <= vw + 1 || r.width < 1 || !visible(el)) return false;
        return clippers(el).every((c) => !c.clipX || c.el === de || c.el === document.body);
      });
      const top = wide.filter((el) => !wide.includes(el.parentElement));
      add('page-overflow-x', top[0] || document.body, `the page is ${sw}px wide in a ${vw}px window`, {
        culprits: top.slice(0, 5).map((e) => describe(e) + ' (right edge ' + Math.round(e.getBoundingClientRect().right) + 'px)'),
      });
    }
    const sh = Math.max(de.scrollHeight, document.body.scrollHeight);
    if (opts.appShell && sh > vh + 1) add('page-overflow-y', document.body, `the window scrolls: the page is ${sh}px tall in a ${vh}px window`);

    // 2. panels that scroll sideways (a long word / URL / name pushing a list wider than itself)
    for (const el of all) {
      const s = cs(el);
      if (!(s.overflowX === 'auto' || s.overflowX === 'scroll')) continue;
      if (el.scrollWidth <= el.clientWidth + 1 || !visible(el)) continue;
      if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') continue;
      if ((opts.hScrollOk || []).some((sel) => el.matches(sel))) continue;
      const cr = clipRect(el);
      let worst = null;
      let over = 1;
      for (const d of el.querySelectorAll('*')) {
        const r = d.getBoundingClientRect();
        if (r.right - cr.right > over && visible(d)) {
          over = r.right - cr.right;
          worst = d;
        }
      }
      add('h-scrollbar', el, `scrolls sideways: content ${el.scrollWidth}px wide in a ${el.clientWidth}px panel`, {
        culprit: worst ? describe(worst) : null,
        culpritText: worst ? textOf(worst) : null,
      });
    }

    // 3. text: cut off by a box, spilling out of its box, outside the window
    const texts = textItems();
    const truncated = [];
    for (const t of texts) {
      if (ignored(t.el)) continue;
      const chain = textClippers(t.el);
      const tb = t.bbox;
      // what of it can be seen: its lines cut by every clipping box (a scrolled-away line, the part behind a "…")
      let region = { left: -1e9, top: -1e9, right: 1e9, bottom: 1e9 };
      for (const c of chain) {
        if (c.clipX) region = { ...region, left: Math.max(region.left, c.rect.left), right: Math.min(region.right, c.rect.right) };
        if (c.clipY) region = { ...region, top: Math.max(region.top, c.rect.top), bottom: Math.min(region.bottom, c.rect.bottom) };
      }
      t.vrects = t.rects.map((r) => inter(r, region)).filter((r) => r.right - r.left > 0.5 && r.bottom - r.top > 0.5);
      t.vbbox = t.vrects.length ? union(t.vrects) : null;
      // under a dialog, a popup or another box: what it looks like doesn't matter here
      if (t.vbbox && !onTop(t)) {
        t.hidden = true;
        // covered on purpose by an open popup / dialog / toast, or a loading veil: fine. By anything else that stays on
        // screen (a floating bar, a badge, a box laid over the page): the text can't be read
        const by = t.coveredBy;
        const veil = by && by.closest('.mt-diff-wait, .mt-drop-veil, [aria-busy="true"]');
        if (by && !isPopup(by) && !isPopup(t.el) && !veil && by.tagName !== 'IFRAME') {
          add('covered-text', t.el, `text hidden under ${describe(by)}`, { text: t.text.slice(0, 80), user: isUser(t.text), by: describe(by), byText: textOf(by) });
        }
        continue;
      }
      let flagged = false;
      let shortened = false;
      for (const c of chain) {
        const cr = c.rect;
        if (c.clipX && !flagged) {
          const over = Math.max(cr.left - tb.left, tb.right - cr.right);
          if (over > 1.5 && !c.scrollX) {
            if (hasEllipsis(t.el, c.el)) {
              truncated.push({ t, c });
              shortened = true;
            } else {
              add('clipped-x', t.el, `text cut off at the ${tb.right - cr.right > cr.left - tb.left ? 'right' : 'left'} edge by ${describe(c.el)} (${Math.round(over)}px hidden, no "…")`, {
                text: t.text.slice(0, 80),
                user: isUser(t.text),
                clipper: describe(c.el),
                over: Math.round(over),
              });
              flagged = true;
            }
          }
        }
        if (c.clipY && !flagged && !c.scrollY) {
          const lines = t.rects;
          const hidden = lines.filter((r) => r.bottom <= cr.top + 1 || r.top >= cr.bottom - 1).length;
          const cut = lines.filter((r) => {
            const tol = Math.max(3, (r.bottom - r.top) * 0.3);
            return r.top < cr.top - tol || r.bottom > cr.bottom + tol;
          }).length;
          if (hidden || cut) {
            if (hasLineClamp(t.el, c.el) || (hasEllipsis(t.el, c.el) && !hidden)) {
              truncated.push({ t, c, y: true });
              shortened = true;
            } else {
              add('clipped-y', t.el, hidden ? `${hidden} of ${lines.length} line(s) hidden below/above ${describe(c.el)}` : `text cut through vertically by ${describe(c.el)}`, {
                text: t.text.slice(0, 80),
                user: isUser(t.text),
                clipper: describe(c.el),
              });
              flagged = true;
            }
          }
        }
        if (c.scrollX || c.scrollY) break; // past a scrolling panel: what's out of view is a scroll away
      }
      if (flagged || !t.vbbox) continue;
      const vb = t.vbbox;
      const inScroller = chain.some((c) => c.scrollX || c.scrollY);
      if (!inScroller) {
        const out = Math.max(-vb.left, -vb.top, vb.right - vw, vb.bottom - vh);
        const allOut = vb.right <= 0 || vb.bottom <= 0 || vb.left >= vw || vb.top >= vh;
        if (out > 1.5 && !allOut) {
          add('offscreen-text', t.el, `text runs ${Math.round(out)}px past the window edge`, { text: t.text.slice(0, 80), user: isUser(t.text) });
          continue;
        }
      }
      // spill: the text runs out of the nearest painted box (a chip, a button, a card) that doesn't clip it
      if (shortened) continue;
      let painted = null;
      for (let e = t.el, i = 0; e && e !== document.body && i < 6; e = e.parentElement, i++) {
        const s = cs(e);
        if (s.display === 'contents' || s.display === 'inline') continue;
        const bg = s.backgroundColor;
        const hasBg = bg && !/rgba\([^)]*,\s*0\)|transparent/.test(bg);
        const hasBorder = ['Top', 'Right', 'Bottom', 'Left'].some((k) => parseFloat(s['border' + k + 'Width']) > 0 && !/rgba\([^)]*,\s*0\)|transparent/.test(s['border' + k + 'Color']));
        if (hasBg || hasBorder) {
          painted = e;
          break;
        }
      }
      if (painted) {
        const pr = painted.getBoundingClientRect();
        const pa = pr.width * pr.height;
        if (pa < vw * vh * 0.3) {
          const ox = Math.max(pr.left - vb.left, vb.right - pr.right);
          const oy = Math.max(pr.top - vb.top, vb.bottom - pr.bottom);
          if (ox > 2 || oy > 4) {
            add('spill', t.el, `text runs ${Math.round(Math.max(ox, oy))}px out of its box ${describe(painted)}`, { text: t.text.slice(0, 80), user: isUser(t.text), box: describe(painted) });
          }
        }
      }
    }
    // "…": fine for what people typed (names, titles, model names); not for the app's own words
    const seenTrunc = new Set();
    for (const { t, c, y } of truncated) {
      // one entry per element kind and text (a name repeated on every turn of the transcript is one finding)
      const tk = describe(t.el) + '|' + t.text;
      if (seenTrunc.has(tk)) continue;
      seenTrunc.add(tk);
      // a line that mixes the app's words with what people typed (a status with a task's title) counts as theirs
      const user = isUser(t.text) || isUser(textOf(c.el)) || ustr.some((u) => (c.el.textContent || '').includes(u));
      add(user ? 'truncated-user' : 'truncated-label', t.el, `${user ? 'user text' : 'label'} shortened with "…"${y ? ' (line clamp)' : ''} inside ${describe(c.el)}`, {
        text: t.text.slice(0, 80),
        user,
      });
    }

    // 4. text over text (in the same layer: a popup over the page is meant to cover it)
    const lines = [];
    for (const t of texts) if (!ignored(t.el) && !t.hidden) for (const r of t.vrects || []) lines.push({ t, r });
    lines.sort((a, b) => a.r.left - b.r.left);
    const seenPair = new Set();
    for (let i = 0; i < lines.length; i++) {
      const a = lines[i];
      for (let j = i + 1; j < lines.length && lines[j].r.left < a.r.right; j++) {
        const b = lines[j];
        if (a.t === b.t) continue;
        const x = inter(a.r, b.r);
        const w = x.right - x.left;
        const h = x.bottom - x.top;
        if (w <= 2 || h <= 3) continue;
        const small = Math.min(area(a.r), area(b.r));
        if (w * h < Math.max(12, small * 0.2)) continue;
        if (a.t.el.contains(b.t.el) && a.t.el !== b.t.el && cs(b.t.el).position === 'static') continue;
        const la = layerOf(a.t.el);
        const lb = layerOf(b.t.el);
        if (la !== lb && (isPopup(la) || isPopup(lb) || isPopup(a.t.el) || isPopup(b.t.el))) continue;
        // only when both can be seen there (not one hidden under an opaque third box)
        const cx = (x.left + x.right) / 2;
        const cy = (x.top + x.bottom) / 2;
        const hit = document.elementFromPoint(cx, cy);
        if (hit && !(a.t.el.contains(hit) || b.t.el.contains(hit) || hit.contains(a.t.el) || hit.contains(b.t.el))) continue;
        const key = [describe(a.t.el), describe(b.t.el)].sort().join('|');
        if (seenPair.has(key)) continue;
        seenPair.add(key);
        add('overlap-text', a.t.el, `text overlaps other text: "${a.t.text.slice(0, 30)}" / "${b.t.text.slice(0, 30)}" (${Math.round(w)}×${Math.round(h)}px)`, {
          text: a.t.text.slice(0, 60),
          other: describe(b.t.el),
          otherText: b.t.text.slice(0, 60),
        });
      }
    }

    // 5. controls: covered by something else, cut off, outside the window, overlapping each other
    const ctrls = [...document.querySelectorAll(INTERACTIVE)].filter((el) => {
      if (!visible(el) || ignored(el)) return false;
      const s = cs(el);
      if (s.pointerEvents === 'none') return false;
      if (el.tagName === 'INPUT' && (el.type === 'file' || el.hidden)) return false;
      return true;
    });
    const vis = new Map();
    for (const el of ctrls) {
      const chain = clippers(el);
      const r = box(el.getBoundingClientRect());
      const v = visibleRect(el, chain);
      vis.set(el, v);
      const full = area(r);
      const seen = area(v);
      if (full < 16) continue;
      const scroller = chain.find((c) => c.scrollX || c.scrollY);
      // cut by a non-scrolling box, or by the window
      if (seen < full * 0.9) {
        // the part hidden by the window (when nothing scrolls it into view)
        const winR = inter(r, { left: 0, top: 0, right: vw, bottom: vh });
        if (!scroller && area(winR) < full * 0.9) {
          if (area(winR) > 0 || opts.reportFullyOffscreen)
            add('offscreen-control', el, `${Math.round(100 - (100 * area(winR)) / full)}% of this control is outside the window`, {});
        } else {
          // which ancestor cuts it (ignore scrolling panels: scrolled out of view is fine)
          let cutBy = null;
          for (const c of chain) {
            if (c.scrollX || c.scrollY) break;
            const cut = inter(r, c.rect);
            if (area(cut) < full * 0.9) {
              cutBy = c;
              break;
            }
          }
          if (cutBy) add('clipped-control', el, `${Math.round(100 - (100 * area(inter(r, cutBy.rect))) / full)}% of this control is cut off by ${describe(cutBy.el)}`, { clipper: describe(cutBy.el) });
        }
      }
      if (seen < 16) continue;
      // scrolled almost out of its panel: a sliver under 2px (the bottom 0.4px of a button above the list's top edge,
      // under the fade) can't be seen or clicked, and its points land on the parent — not "covered"
      if (v.bottom - v.top < 2 || v.right - v.left < 2) continue;
      // hit test: is it on top where it can be seen?
      const pts = [
        [0.5, 0.5],
        [0.3, 0.3],
        [0.7, 0.3],
        [0.3, 0.7],
        [0.7, 0.7],
      ].map(([fx, fy]) => [v.left + (v.right - v.left) * fx, v.top + (v.bottom - v.top) * fy]);
      const covers = pts.map(([x, y]) => hitsOutside(el, x, y)).filter(Boolean);
      if (covers.length >= 3) {
        const by = covers[0];
        if (isPopup(by) && !isPopup(el)) continue; // an open popup covers the page on purpose
        if (by.tagName === 'IFRAME' && !opts.iframeCovers) {
          add('covered-control', el, `covered by the paper frame (${describe(by)}) at ${covers.length}/5 points`, { by: describe(by) });
        } else add('covered-control', el, `covered by ${describe(by)} at ${covers.length}/5 points: clicks go to that instead`, { by: describe(by), byText: textOf(by) });
      }
    }
    const seenCtrl = new Set();
    for (let i = 0; i < ctrls.length; i++) {
      const a = ctrls[i];
      const va = vis.get(a);
      if (area(va) < 16) continue;
      for (let j = i + 1; j < ctrls.length; j++) {
        const b = ctrls[j];
        if (a.contains(b) || b.contains(a)) continue;
        const vb = vis.get(b);
        if (area(vb) < 16) continue;
        const x = inter(va, vb);
        const w = x.right - x.left;
        const h = x.bottom - x.top;
        if (w <= 3 || h <= 3 || w * h < 16) continue;
        if (a.tagName === 'LABEL' || b.tagName === 'LABEL') continue;
        const la = layerOf(a);
        const lb = layerOf(b);
        if (la !== lb && (isPopup(la) || isPopup(lb) || isPopup(a) || isPopup(b))) continue;
        const key = [describe(a), describe(b)].sort().join('|');
        if (seenCtrl.has(key)) continue;
        seenCtrl.add(key);
        add('overlap-controls', a, `two controls overlap (${Math.round(w)}×${Math.round(h)}px): ${describe(a)} and ${describe(b)}`, { other: describe(b), otherText: textOf(b) });
      }
    }

    // 6. fields: the placeholder doesn't fit; the longest value allowed (maxlength) doesn't fit
    const canvas = A._canvas || (A._canvas = document.createElement('canvas'));
    const g = canvas.getContext('2d');
    for (const el of document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]):not([type=file]), textarea')) {
      if (!visible(el) || ignored(el)) continue;
      const s = cs(el);
      g.font = `${s.fontStyle} ${s.fontWeight} ${s.fontSize} ${s.fontFamily}`;
      const inner = el.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight);
      if (el.placeholder && !el.value && el.tagName === 'INPUT') {
        const w = g.measureText(el.placeholder).width;
        if (w > inner + 1) add('placeholder-clipped', el, `placeholder "${el.placeholder.slice(0, 40)}" is ${Math.round(w)}px in a ${Math.round(inner)}px field`, { text: el.placeholder });
      }
      if (el.value && el.tagName === 'INPUT' && el.type !== 'password') {
        const w = g.measureText(el.value).width;
        if (w > inner + 1) add('value-clipped', el, `the value "${el.value.slice(0, 40)}" (${el.value.length} chars${el.maxLength > 0 ? ', limit ' + el.maxLength : ''}) is ${Math.round(w)}px in a ${Math.round(inner)}px field: its end can't be seen without scrolling inside the field`, { text: el.value, user: true });
      }
    }
    return F;
  };

  // ------------------------------------------------------------------ before / after a state change
  const KEYSEL =
    INTERACTIVE + ', [data-testid], img, video, iframe, h1, h2, h3, h4, p, li, time, label, .mt-avatar, .mt-av-wrap, .mt-person-name, .mt-state, .mt-meta-item, .mt-title, .mt-title-text';
  function snapshot() {
    const out = new Map();
    for (const el of document.body.querySelectorAll(KEYSEL)) {
      if (!visible(el)) continue;
      let id = A.ids.get(el);
      if (!id) A.ids.set(el, (id = ++A.n));
      // where it is on screen (what the user sees move); only what can be seen counts (not scrolled out of view)
      const r = el.getBoundingClientRect();
      if (area(visibleRect(el)) < 1) continue;
      out.set(id, { el, x: r.left, y: r.top, w: r.width, h: r.height, sc: null });
    }
    return out;
  }
  /** before a change: the key elements' boxes, and where the layout-shift entries start */
  A.mark = () => {
    A._before = snapshot();
    A._shiftFrom = A.shifts.length;
    return A._before.size;
  };
  /** after it: what moved or changed size that the change wasn't about (`expect`: selectors that may change) */
  A.diff = (opts = {}) => {
    const before = A._before || new Map();
    const after = snapshot();
    const expect = opts.expect || [];
    const expected = (el) => expect.some((sel) => el.matches(sel) || el.closest(sel));
    const moved = [];
    const resized = [];
    const delta = new Map();
    for (const [id, b] of before) {
      const a = after.get(id);
      if (!a || a.el !== b.el) continue;
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const dw = a.w - b.w;
      const dh = a.h - b.h;
      delta.set(b.el, { dx, dy });
      if (expected(b.el)) continue;
      if (Math.abs(dx) >= 1.5 || Math.abs(dy) >= 1.5) moved.push({ el: b.el, dx, dy, sc: a.sc !== b.sc });
      else if ((Math.abs(dw) >= 1.5 || Math.abs(dh) >= 1.5) && b.el.matches(INTERACTIVE)) resized.push({ el: b.el, dw, dh });
    }
    // keep the outermost moved element of each group (its contents ride along)
    const roots = moved.filter((m) => {
      for (let p = m.el.parentElement; p && p !== document.body; p = p.parentElement) {
        const d = delta.get(p);
        if (d) return !(Math.abs(d.dx - m.dx) < 1 && Math.abs(d.dy - m.dy) < 1);
      }
      return true;
    });
    const appeared = [];
    for (const [id, a] of after) if (!before.has(id) && !expected(a.el)) appeared.push(a.el);
    const appearedTop = appeared.filter((e) => !appeared.includes(e.parentElement)).slice(0, 6);
    const gone = [];
    for (const [id, b] of before) if (!after.has(id) && !expected(b.el)) gone.push(b.el);
    const shifts = A.shifts.slice(A._shiftFrom || 0);
    const cls = shifts.filter((s) => !s.input).reduce((a, s) => a + s.value, 0);
    return {
      moved: roots.slice(0, 12).map((m) => ({ el: describe(m.el), text: textOf(m.el).slice(0, 40), dx: Math.round(m.dx), dy: Math.round(m.dy), interactive: m.el.matches(INTERACTIVE), ...anchors(m.el) })),
      movedCount: roots.length,
      resized: resized.slice(0, 8).map((m) => ({ el: describe(m.el), text: textOf(m.el).slice(0, 40), dw: Math.round(m.dw), dh: Math.round(m.dh), ...anchors(m.el) })),
      appeared: appearedTop.map((e) => describe(e) + (textOf(e) ? ' "' + textOf(e).slice(0, 30) + '"' : '')),
      gone: gone.filter((e) => !gone.includes(e.parentElement)).slice(0, 6).map((e) => describe(e)),
      cls: Math.round(cls * 10000) / 10000,
      shiftSources: shifts.flatMap((s) => s.sources).slice(0, 8),
    };
  };

  // ------------------------------------------------------------------ layout shift entries
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        A.shifts.push({
          t: Math.round(e.startTime),
          value: e.value,
          input: e.hadRecentInput,
          sources: (e.sources || []).map((s) => ({
            el: s.node ? describe(s.node.nodeType === 1 ? s.node : s.node.parentElement) : null,
            from: rectOf(s.previousRect),
            to: rectOf(s.currentRect),
          })),
        });
      }
    }).observe({ type: 'layout-shift', buffered: true });
  } catch {
    /* not supported */
  }

  /** wait until the page is still: fonts, two frames, no finite animation running */
  A.settle = async (ms = 250) => {
    try {
      await document.fonts.ready;
    } catch {
      /* */
    }
    const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
    await frame();
    await frame();
    // (the clock is frozen in the audit: count rounds, not milliseconds)
    for (let round = 0; round < 40; round++) {
      const busy = document.getAnimations().filter((a) => {
        if (a.playState !== 'running') return false;
        const t = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : null;
        return !t || t.iterations !== Infinity;
      });
      if (!busy.length) break;
      await new Promise((r) => setTimeout(r, 60));
    }
    await new Promise((r) => setTimeout(r, ms));
    await frame();
  };
})();
