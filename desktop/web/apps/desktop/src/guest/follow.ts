// Following the host on the guest page, ported from the first guest page (relay/index.html): where the host's paper is
// (the section, the paragraph's number and first words, how far into it), this paper scrolls there too. Two ways in:
// the meeting's data channel (tens of ms; every frame while the host scrolls) and the relay's state (the fallback).
// The guest scrolling the paper themselves stops following; 回到主持人位置 picks it up again.
/* eslint-disable @typescript-eslint/no-explicit-any */

export interface HostFocus {
  section?: string;
  no?: string | null;
  text?: string | null;
  top?: boolean;
  ri?: number | null;
  off?: number | null;
  k?: number | null;
}

const norm = (t: unknown) => String(t ?? '').replace(/\s+/g, ' ').trim();
const focusKey = (f: HostFocus | string | null | undefined) =>
  !f ? '' : typeof f === 'string' ? f
    : f.top ? 'top' : `${f.no ?? ''}|${f.section ?? ''}|${String(f.text ?? '').slice(0, 24)}|${f.ri ?? ''}|${Math.round((Number(f.off) || 0) * 10)}`;

/** the paper's page takes the guest page's light / dark (it would otherwise follow the system on its own) */
const themed = new Set<Document>();
function syncTheme(d: Document) {
  const t = document.documentElement.dataset.theme;
  if (t) d.documentElement.dataset.theme = t;
  themed.add(d);
}
new MutationObserver(() => {
  for (const d of Array.from(themed)) {
    if (!d.defaultView) themed.delete(d);
    else syncTheme(d);
  }
}).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

export class Follow {
  following = true;
  private frame: HTMLIFrameElement | null = null;
  private cur: HostFocus | null = null;
  private lastKey = '';
  private lastK = -1;
  private lastManual = 0;
  private spotFound = false;
  private spotRow = false;
  private goal: number | null = null;
  private goalDoc: Document | null = null;
  private ackT = 0;
  private sSeq = 0;
  private sPend: any = null;
  private sRaf = 0;
  private sRows: NodeListOf<Element> | null = null;
  private watched = new WeakSet<Document>();
  /** where the guest was reading on their own (not following): a new version of the paper opens there again */
  private lastAt: { i: number; text: string; off: number; ratio: number } | null = null;
  private atRaf = 0;

  constructor(
    private readonly onChange: () => void,
    private readonly ack: (msg: unknown) => void,
  ) {}

  /** the paper's frame on screen changed (first load, or a new version swapped in) */
  setFrame(el: HTMLIFrameElement | null) {
    this.frame = el;
    this.sRows = null;
    if (!el) return;
    const ready = () => {
      const d = this.doc();
      if (!d) return;
      syncTheme(d);
      try { d.querySelectorAll('details.fold').forEach((x) => ((x as HTMLDetailsElement).open = true)); } catch { /* */ }
      this.watch(d);
      if (this.following) window.setTimeout(() => this.toHost(), 300);
      else if (this.lastAt) {
        const at = this.lastAt;
        this.readTo(d, at);
        window.setTimeout(() => this.readTo(d, at), 450);       // after the formulas and figures have their size
      }
    };
    if (el.contentDocument?.readyState === 'complete' && el.contentDocument.location.href !== 'about:blank') ready();
    el.addEventListener('load', ready);
  }

  private doc(): Document | null {
    try { return this.frame?.contentDocument ?? null; } catch { return null; }
  }

  /** the host's place: from the data channel ('dc') or the relay's state ('relay'), whichever comes first */
  onFocus(f: HostFocus | null | undefined, via: 'dc' | 'relay') {
    if (f && typeof f === 'object' && f.k != null && f.k <= this.lastK) return;
    const fk = focusKey(f);
    if (!fk || fk === this.lastKey) return;
    this.lastKey = fk;
    if (f && f.k != null) this.lastK = f.k;
    this.cur = f ?? null;
    if (this.following || Date.now() - this.lastManual > 2500) this.setFollow(true);
    else this.onChange();
    clearTimeout(this.ackT);
    this.ackT = window.setTimeout(() => this.sendAck(f, via), !this.following ? 0 : 60);
  }

  /** one frame of the host scrolling (topic bloom-s): follow it frame by frame */
  onStream(m: any) {
    if (!this.following || !m || m.s == null) return;
    if (m.s <= this.sSeq && this.sSeq - m.s < 1000) return;
    this.sSeq = m.s;
    this.sPend = m;
    if (!this.sRaf) this.sRaf = requestAnimationFrame(() => this.applyStream());
  }

  setFollow(on: boolean) {
    this.following = on;
    if (on) this.toHost();
    this.onChange();
  }

  hasHost() {
    return !!this.cur;
  }

  private sendAck(f: HostFocus | null | undefined, via: string) {
    if (!f || f.k == null) return;
    const at = this.following ? this.landed() : { ok: false, dy: null };
    this.ack({ t: 'at', k: f.k, ok: !!this.spotFound && at.ok, found: !!this.spotFound, dy: at.dy, follow: this.following, via });
  }

  private watch(d: Document) {
    if (this.watched.has(d)) return;
    this.watched.add(d);
    const stop = () => {
      this.lastManual = Date.now();
      if (this.following) { this.following = false; this.onChange(); }
    };
    d.addEventListener('scroll', () => {
      if (this.following || this.atRaf) return;
      this.atRaf = requestAnimationFrame(() => {
        this.atRaf = 0;
        if (!this.following && d === this.doc()) this.lastAt = this.readingAt(d);
      });
    }, { passive: true });
    d.addEventListener('wheel', stop, { passive: true });
    d.addEventListener('touchmove', stop, { passive: true });
    d.addEventListener('keydown', (e) => { if (/^(Arrow|Page|Home|End| )/.test((e as KeyboardEvent).key)) stop(); });
  }

  private readingAt(d: Document) {
    const se = (d.scrollingElement || d.documentElement) as HTMLElement;
    const top = this.barOffset(d);
    const rows = d.querySelectorAll('.row');
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i].getBoundingClientRect();
      if (r.bottom > top) return { i, text: norm(rows[i].textContent).slice(0, 40), off: r.height > 0 ? (top - r.top) / r.height : 0, ratio: se.scrollTop / Math.max(1, se.scrollHeight) };
    }
    return null;
  }

  private readTo(d: Document, at: { i: number; text: string; off: number; ratio: number }) {
    const se = (d.scrollingElement || d.documentElement) as HTMLElement;
    const rows = d.querySelectorAll('.row');
    let el: Element | null = null;
    if (at.text.length >= 6) for (const r of Array.from(rows)) { if (norm(r.textContent).startsWith(at.text.slice(0, 24))) { el = r; break; } }
    el = el ?? rows[at.i] ?? null;
    if (el) {
      const r = el.getBoundingClientRect();
      se.scrollTop = Math.max(0, r.top + se.scrollTop - this.barOffset(d) + at.off * r.height);
    } else se.scrollTop = at.ratio * se.scrollHeight;
  }

  private barOffset(d: Document) {
    const bar = d.querySelector('.bar') as HTMLElement | null;
    return (bar && getComputedStyle(bar).display !== 'none' ? bar.getBoundingClientRect().height : 0) + 10;
  }

  private hostSpot(d: Document): Element | null {
    const f = this.cur;
    let el: Element | null = null;
    this.spotRow = false;
    if (!f) return null;
    if (f.no != null && f.no !== '') el = d.querySelector(`.row[data-no="${String(f.no).replace(/[^0-9A-Za-z_-]/g, '')}"]`);
    if (!el && f.text) {
      const key = norm(f.text).slice(0, 24);
      if (key.length >= 6) for (const r of Array.from(d.querySelectorAll('.row'))) { if (norm(r.textContent).includes(key)) { el = r; break; } }
    }
    if (!el && f.ri != null && f.ri >= 0 && !f.text) el = d.querySelectorAll('.row')[f.ri] ?? null;
    if (el) this.spotRow = true;
    if (!el) {
      const sec = norm(f.section);
      if (sec) for (const s of Array.from(d.querySelectorAll('.sect'))) { const t = norm(s.textContent); if (t && (t.startsWith(sec) || sec.startsWith(t))) { el = s; break; } }
    }
    return el;
  }

  private goTo(d: Document, y: number) {
    const se = (d.scrollingElement || d.documentElement) as HTMLElement;
    y = Math.max(0, Math.min(Math.round(y), se.scrollHeight - se.clientHeight));
    this.goal = y;
    this.goalDoc = d;
    se.scrollTo({ top: y, behavior: 'auto' });
  }

  private landed() {
    if (this.goal == null || !this.goalDoc) return { ok: false, dy: null as number | null };
    const se = (this.goalDoc.scrollingElement || this.goalDoc.documentElement) as HTMLElement;
    let dy = Math.round(se.scrollTop - this.goal);
    if (Math.abs(dy) > 48) { se.scrollTop = this.goal; dy = Math.round(se.scrollTop - this.goal); }
    return { ok: Math.abs(dy) <= 48, dy };
  }

  private toHost() {
    const d = this.doc();
    if (!d || !this.cur) return;
    if (this.cur.top) { this.spotFound = true; this.goTo(d, 0); return; }
    const el = this.hostSpot(d);
    this.spotFound = !!el && this.spotRow;
    if (!el) return;
    const fold = el.closest('details') as HTMLDetailsElement | null;
    if (fold && !fold.open) fold.open = true;
    const se = (d.scrollingElement || d.documentElement) as HTMLElement;
    const r = el.getBoundingClientRect();
    const into = this.spotRow ? (Number(this.cur.off) || 0) * r.height : 0;
    this.goTo(d, r.top + se.scrollTop - this.barOffset(d) + into);
  }

  private applyStream() {
    this.sRaf = 0;
    const m = this.sPend;
    const d = this.doc();
    if (!m || !this.following || !d) return;
    const se = (d.scrollingElement || d.documentElement) as HTMLElement;
    if (m.ri == null || m.ri < 0) { se.scrollTop = 0; return; }
    if (!this.sRows || !this.sRows.length || !this.sRows[0].isConnected) this.sRows = d.querySelectorAll('.row');
    const el = this.sRows[m.ri];
    if (!el) return;
    const fold = el.closest('details') as HTMLDetailsElement | null;
    if (fold && !fold.open) fold.open = true;
    const r = el.getBoundingClientRect();
    se.scrollTop = Math.max(0, r.top + se.scrollTop - this.barOffset(d) + (Number(m.off) || 0) * r.height);
  }
}
