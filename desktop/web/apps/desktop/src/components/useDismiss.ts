// One rule for every popover and dropdown on the meeting pages (邀请, 保存, the people cards' pickers): it closes on a
// pointer press anywhere outside it, on a click inside the paper (the diff page is an iframe: its clicks never reach
// this document — the page posts {zhPointer} / {zhKey: 'Escape'} over, and focus moving into the iframe blurs this
// window), on Escape, and when its page is switched away (`active` false).
import { useEffect, useRef, type RefObject } from 'react';

export function useDismiss(open: boolean, close: () => void, box: RefObject<HTMLElement | null>, active = true) {
  const shut = useRef(close);
  shut.current = close;
  useEffect(() => {
    if (!open) return;
    if (!active) {
      shut.current();
      return;
    }
    const c = () => shut.current();
    const down = (e: PointerEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) c();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') c();
    };
    const msg = (e: MessageEvent) => {
      const d = e.data as { zhPointer?: unknown; zhKey?: unknown } | null;
      if (d && (d.zhPointer || d.zhKey === 'Escape')) c();
    };
    // focus went into the paper (an iframe takes focus on a click) or to another window
    const blur = () => setTimeout(() => (document.activeElement instanceof HTMLIFrameElement || !document.hasFocus()) && c(), 0);
    document.addEventListener('pointerdown', down, true);
    document.addEventListener('keydown', key);
    window.addEventListener('message', msg);
    window.addEventListener('blur', blur);
    return () => {
      document.removeEventListener('pointerdown', down, true);
      document.removeEventListener('keydown', key);
      window.removeEventListener('message', msg);
      window.removeEventListener('blur', blur);
    };
  }, [open, active, box]);
}

/** structural sharing for polled JSON: keep the previous object (and so React's memo) where nothing changed */
export function keep<T>(prev: T | null | undefined, next: T): T {
  if (prev == null || next == null || typeof next !== 'object' || typeof prev !== 'object') return next;
  if (Array.isArray(next)) {
    if (!Array.isArray(prev)) return next;
    let same = prev.length === next.length;
    const out = next.map((x, i) => {
      const k = keep((prev as unknown[])[i], x);
      if (k !== (prev as unknown[])[i]) same = false;
      return k;
    });
    return (same ? prev : out) as T;
  }
  const p = prev as Record<string, unknown>;
  const n = next as Record<string, unknown>;
  const pk = Object.keys(p);
  const nk = Object.keys(n);
  let same = pk.length === nk.length;
  const out: Record<string, unknown> = {};
  for (const k of nk) {
    const v = k in p ? keep(p[k], n[k]) : n[k];
    if (v !== p[k]) same = false;
    out[k] = v;
  }
  return (same ? prev : out) as T;
}
