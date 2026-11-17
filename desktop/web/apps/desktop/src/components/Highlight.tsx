// The search word marked in a line of text (逐字稿 search): every occurrence, case-insensitive for Latin letters.
import { Fragment, type ReactNode } from 'react';

export function Highlight({ text, q }: { text: string; q: string }): ReactNode {
  const needle = q.trim();
  if (!needle) return text;
  const hay = text.toLowerCase();
  const n = needle.toLowerCase();
  const out: ReactNode[] = [];
  let at = 0;
  for (let i = hay.indexOf(n); i !== -1; i = hay.indexOf(n, at)) {
    if (i > at) out.push(<Fragment key={`t${at}`}>{text.slice(at, i)}</Fragment>);
    out.push(
      <mark key={`m${i}`} className="mt-hl">
        {text.slice(i, i + n.length)}
      </mark>,
    );
    at = i + n.length;
  }
  if (at < text.length) out.push(<Fragment key={`t${at}`}>{text.slice(at)}</Fragment>);
  return out;
}

/** People's names in a line of the 纪要 shown as small chips, set apart from the rest of the text. */
export function Names({ text, names, colors }: { text: string; names: string[]; colors?: Record<string, string> }): ReactNode {
  const list = [...new Set(names.map((n) => n.trim()).filter((n) => n.length > 0))].sort((a, b) => b.length - a.length);
  if (!list.length || !text) return text;
  const out: ReactNode[] = [];
  let at = 0;
  const lower = text.toLowerCase();
  while (at < text.length) {
    let hit = -1;
    let len = 0;
    for (const n of list) {
      const i = lower.indexOf(n.toLowerCase(), at);
      if (i !== -1 && (hit === -1 || i < hit || (i === hit && n.length > len))) {
        hit = i;
        len = n.length;
      }
    }
    if (hit === -1) break;
    if (hit > at) out.push(<Fragment key={`t${at}`}>{text.slice(at, hit)}</Fragment>);
    out.push(
      <span key={`n${hit}`} className="mt-name" style={nameStyle(colors?.[list.find((n) => n.toLowerCase() === lower.slice(hit, hit + len)) ?? ''])}>
        {text.slice(hit, hit + len)}
      </span>,
    );
    at = hit + len;
  }
  if (at < text.length) out.push(<Fragment key={`t${at}`}>{text.slice(at)}</Fragment>);
  return out;
}

// Name colours in a meeting: each person's own colour, the one their flower has (Flower.tsx: colorsByName).

/** A name chip in its person's colour. */
export function nameStyle(color: string | undefined) {
  return color
    ? { color, background: `color-mix(in srgb, ${color} 14%, transparent)`, borderColor: `color-mix(in srgb, ${color} 34%, transparent)` }
    : undefined;
}
