// The window's own tooltip, in place of the WebView's `title` box (square, light, Win95-looking even in dark mode, and
// it hung across the panes in the shots). Any element with `data-tip` gets it: after half a second over it, a small
// rounded panel in the theme's raised surface, 12 px, under the element and kept inside the window. `data-tip-trunc`
// shows it only while the element's text is cut (a row title that fits says nothing more); one that fits inside an
// element with a tip of its own gives way to that one (the AI strip's model names: the strip's whole status). Several
// lines (a "\n" in the words): one part a line, a part too long for the width going on under itself, indented. Any
// key, click, scroll or leaving the element hides it.
import { useEffect, useRef, useState } from 'react';

type Shown = { text: string; x: number; y: number; above: boolean };

const fits = (el: HTMLElement) => el.scrollWidth <= el.clientWidth + 1;

/** the element whose tip shows: the nearest with one, past a cut-only one that isn't cut while another holds it */
function tipOf(from: EventTarget | null): HTMLElement | null {
  let el = (from as HTMLElement | null)?.closest?.('[data-tip]') as HTMLElement | null;
  while (el?.hasAttribute('data-tip-trunc') && fits(el)) {
    const up = el.parentElement?.closest('[data-tip]') as HTMLElement | null;
    if (!up) break;
    el = up;
  }
  return el;
}

export function Tooltip() {
  const [tip, setTip] = useState<Shown | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const over = useRef<HTMLElement | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const hide = () => {
      clearTimeout(timer.current);
      over.current = null;
      setTip(null);
    };
    const onOver = (e: MouseEvent) => {
      const el = tipOf(e.target);
      if (el === over.current) return;
      hide();
      if (!el) return;
      over.current = el;
      timer.current = setTimeout(() => {
        if (over.current !== el || !el.isConnected) return;
        const text = el.dataset.tip;
        if (!text) return;
        if (el.hasAttribute('data-tip-trunc') && fits(el)) return;
        const r = el.getBoundingClientRect();
        const above = r.bottom + 40 > window.innerHeight;
        setTip({ text, x: Math.max(8, Math.min(e.clientX - 12, window.innerWidth - 8)), y: above ? r.top - 6 : r.bottom + 6, above });
      }, 500);
    };
    // a scroll closes it only when it moves the element pointed at: the live transcript scrolling itself to a new line
    // used to close the tip of the AI strip above it while it was being read (2026-11-21)
    const onScroll = (e: Event) => {
      const el = over.current;
      const from = e.target;
      if (el && from instanceof Element && !from.contains(el)) return;
      hide();
    };
    document.addEventListener('mouseover', onOver, true);
    document.addEventListener('mousedown', hide, true);
    document.addEventListener('keydown', hide, true);
    document.addEventListener('scroll', onScroll, true);
    window.addEventListener('blur', hide);
    return () => {
      document.removeEventListener('mouseover', onOver, true);
      document.removeEventListener('mousedown', hide, true);
      document.removeEventListener('keydown', hide, true);
      document.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('blur', hide);
      clearTimeout(timer.current);
    };
  }, []);
  // keep it inside the window once its width is known
  useEffect(() => {
    const el = box.current;
    if (!el || !tip) return;
    const w = el.offsetWidth;
    if (tip.x + w > window.innerWidth - 8) el.style.left = `${Math.max(8, window.innerWidth - 8 - w)}px`;
  }, [tip]);
  if (!tip) return null;
  const lines = tip.text.includes('\n') ? tip.text.split('\n') : null;
  return (
    <div
      ref={box}
      className={`mw-tip${lines ? ' is-lines' : ''}`}
      role="tooltip"
      style={{ left: tip.x, top: tip.above ? undefined : tip.y, bottom: tip.above ? window.innerHeight - tip.y : undefined }}
    >
      {lines ? lines.map((x, i) => <div key={i} className="mw-tip-l">{x}</div>) : tip.text}
    </div>
  );
}
