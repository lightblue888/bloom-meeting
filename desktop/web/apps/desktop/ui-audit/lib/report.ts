// At the end of a run: read out/findings/*.json, group the same problem seen on many screens / sizes / themes /
// languages into one entry, rank by severity, point at the likely source (the element's classes / test ids looked up
// in src/), and write out/report.md (for people) and out/findings.json (grouped, for tools).
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import type { Reporter } from '@playwright/test/reporter';
import type { Finding } from './audit';

const OUT = resolve(import.meta.dirname, '../out');
const SRC = resolve(import.meta.dirname, '../../src');
const APP = resolve(import.meta.dirname, '../..');

export type Severity = 'blocker' | 'major' | 'minor' | 'info';
const ORDER: Severity[] = ['blocker', 'major', 'minor', 'info'];

export const LABEL: Record<string, string> = {
  'page-overflow-x': 'Page wider than the window',
  'page-overflow-y': 'The app window scrolls as a page',
  'offscreen-control': 'Control partly outside the window',
  'covered-control': 'Control covered by another element',
  'clipped-control': 'Control cut off by its container',
  'overlap-controls': 'Two controls overlap',
  'overlap-text': 'Text overlaps other text',
  'covered-text': 'Text hidden under another element',
  'clipped-x': 'Text cut off sideways (no "…")',
  'clipped-y': 'Text cut off vertically',
  'offscreen-text': 'Text runs past the window edge',
  spill: 'Text spills out of its box',
  'h-scrollbar': 'Panel scrolls sideways',
  'truncated-label': 'App label shortened with "…"',
  'truncated-user': 'User text shortened with "…" (intended)',
  'placeholder-clipped': 'Placeholder longer than its field',
  'value-clipped': 'Longest allowed value does not fit its field',
  'layout-shift': 'Layout shift: something else moved',
  'layout-shift-cls': 'Layout shift (browser measure)',
  'control-resized': 'A control changed size',
  'page-error': 'Uncaught error in the page',
  'console-error': 'Console error',
  'console-warning': 'Console warning',
  'request-failed': 'Request failed',
  'http-error': 'HTTP error answer',
  unmocked: 'Endpoint not mocked (audit setup)',
  'external-request': 'Request to another host (blocked)',
  'live-backend-request': 'Request to the live backend (blocked)',
  'audit-step-failed': 'Audit could not perform a step',
};

export function severity(f: Finding): Severity {
  const c = f.check;
  if (f.byDesign && (c === 'layout-shift' || c === 'control-resized' || c === 'layout-shift-cls')) return 'info';
  if (c.startsWith('axe:')) {
    // hard to read for everyone: major; the rest matters to screen-reader / keyboard users: minor
    if (c === 'axe:color-contrast') return Number(f.ratio ?? 0) < 3 ? 'major' : 'minor';
    return 'minor';
  }
  switch (c) {
    case 'page-overflow-x':
    case 'offscreen-control':
    case 'page-error':
    case 'live-backend-request':
      return 'blocker';
    case 'covered-control':
      return /5\/5/.test(f.detail) ? 'blocker' : 'major';
    case 'clipped-control':
    case 'overlap-controls':
    case 'overlap-text':
    case 'covered-text':
    case 'clipped-x':
    case 'clipped-y':
    case 'offscreen-text':
    case 'h-scrollbar':
    case 'truncated-label':
    case 'console-error':
      return 'major';
    case 'spill':
      return /runs (\d+)px/.test(f.detail) && Number(f.detail.match(/runs (\d+)px/)![1]) >= 6 ? 'major' : 'minor';
    case 'layout-shift': {
      const d = Math.max(Math.abs(Number(f.dx ?? 0)), Math.abs(Number(f.dy ?? 0)));
      return (f.interactive && d >= 4) || d >= 8 ? 'major' : 'minor';
    }
    case 'truncated-user':
    case 'layout-shift-cls':
    case 'unmocked':
    case 'external-request':
    case 'audit-step-failed':
      return 'info';
    default:
      return 'minor';
  }
}

// ------------------------------------------------------------------------------------------------ source index
interface Index {
  css: Map<string, string[]>;
  tsx: Map<string, string[]>;
  tid: Map<string, string[]>;
}
function walk(dir: string, out: string[] = []) {
  for (const n of readdirSync(dir)) {
    const p = join(dir, n);
    if (n.startsWith('.') || n.includes('.before-')) continue;
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(css|tsx|ts)$/.test(n) && !/\.test\.ts$/.test(n)) out.push(p);
  }
  return out;
}
function buildIndex(): Index {
  const idx: Index = { css: new Map(), tsx: new Map(), tid: new Map() };
  const put = (m: Map<string, string[]>, k: string, v: string) => {
    const a = m.get(k) ?? [];
    if (!a.includes(v) && a.length < 4) a.push(v);
    m.set(k, a);
  };
  for (const f of walk(SRC)) {
    const name = relative(APP, f).replace(/\\/g, '/');
    const lines = readFileSync(f, 'utf8').split(/\r?\n/);
    lines.forEach((line, i) => {
      const at = `${name}:${i + 1}`;
      if (f.endsWith('.css')) {
        if (!/[{,]\s*$/.test(line) && !/\{/.test(line)) return;
        const sel = line.split('{')[0];
        for (const m of sel.matchAll(/\.([a-zA-Z][\w-]*)/g)) put(idx.css, m[1], at);
      } else {
        for (const m of line.matchAll(/className=\{?[`'"]([^`'"]*)/g)) for (const c of m[1].split(/[\s$]+/)) if (/^[a-z][\w-]+$/.test(c)) put(idx.tsx, c, at);
        for (const m of line.matchAll(/className="([^"]*)"/g)) for (const c of m[1].split(/\s+/)) if (c) put(idx.tsx, c, at);
        for (const m of line.matchAll(/data-testid=\{?[`'"]([\w-]+)/g)) put(idx.tid, m[1].replace(/-$/, ''), at);
        for (const m of line.matchAll(/data-testid=\{[^}]*?'([\w-]+)'/g)) put(idx.tid, m[1], at);
      }
    });
  }
  return idx;
}
const classesIn = (desc: unknown) => [...String(desc ?? '').matchAll(/\.([a-zA-Z][\w-]*)/g)].map((m) => m[1]);
function sourceOf(f: Finding, idx: Index): string[] {
  const out: string[] = [];
  const add = (s: string[] | undefined, n = 1) => {
    for (const x of (s ?? []).slice(0, n)) if (!out.includes(x)) out.push(x);
  };
  const own = classesIn(f.el);
  const cls = [...own.reverse(), ...(f.classes ?? [])];
  // the component that renders it: a test id, else a class used in a className
  for (const t of f.testids ?? []) {
    const k = [...idx.tid.keys()].find((k) => t === k || t.startsWith(k + '-'));
    if (k) {
      add(idx.tid.get(k));
      break;
    }
  }
  if (!out.length) for (const c of cls) if (idx.tsx.has(c)) {
    add(idx.tsx.get(c));
    break;
  }
  // its style rules: the element's own class first, then the box that clips / covers it
  let css = 0;
  for (const c of cls) if (idx.css.has(c) && css < 2) {
    add(idx.css.get(c), 2);
    css++;
  }
  for (const k of ['clipper', 'box', 'by', 'other']) {
    const c = classesIn(f[k]).pop();
    if (c && idx.css.has(c)) add(idx.css.get(c));
  }
  return out.slice(0, 5);
}

// ------------------------------------------------------------------------------------------------ grouping
const norm = (s: unknown) => String(s ?? '').replace(/m-\d{3}/g, 'm-#').replace(/\b(g|p|T|N)\d+\b/g, '$1#').replace(/\d+(\.\d+)?px/g, 'Npx').replace(/:\d+:\d+/g, '');
function keyOf(f: Finding) {
  if (f.check === 'layout-shift' || f.check === 'control-resized' || f.check === 'layout-shift-cls') return `${f.check}|${f.screen.replace(/-stress$/, '')}|${f.transition}|${norm(f.el)}`;
  if (f.check.startsWith('console') || f.check === 'page-error' || f.check === 'http-error' || f.check === 'request-failed') return `${f.check}|${norm(f.detail).replace(/https?:\/\/[^ ]+/g, 'URL').slice(0, 120)}`;
  if (f.check === 'unmocked' || f.check === 'external-request' || f.check === 'live-backend-request') return `${f.check}|${f.detail}`;
  if (f.check.startsWith('axe:')) {
    // the element itself, without its position or state ("li:nth-child(3)", ".is-guest"): one entry per kind of element
    const last = String(f.el ?? '').replace(/:nth-child\(\d+\)|:nth-of-type\(\d+\)|\[[^\]]*\]/g, '').split(/\s*>\s*|\s+/).filter(Boolean).pop() ?? '';
    const el = last.replace(/\.is-[\w-]+/g, '') || last;
    return `${f.check}|${norm(el)}|${f.check === 'axe:color-contrast' ? f.theme : ''}`;
  }
  return `${f.check}|${norm(f.el)}|${f.check === 'overlap-text' || f.check === 'overlap-controls' ? norm(f.other) : ''}`;
}

export interface Group {
  severity: Severity;
  check: string;
  label: string;
  count: number;
  screens: string[];
  sizes: string[];
  themes: string[];
  langs: string[];
  transitions: string[];
  example: Finding;
  examples: Finding[];
  source: string[];
}

export function group(all: Finding[]): Group[] {
  const idx = buildIndex();
  const groups = new Map<string, Finding[]>();
  for (const f of all) {
    const k = keyOf(f);
    groups.set(k, [...(groups.get(k) ?? []), f]);
  }
  const uniq = (a: unknown[]) => [...new Set(a.filter((x) => x != null && x !== ''))].map(String).sort();
  const out: Group[] = [];
  for (const fs of groups.values()) {
    const sev = fs.map(severity).sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b))[0];
    // the example: the most severe, at the most common size
    const ex = [...fs].sort((a, b) => ORDER.indexOf(severity(a)) - ORDER.indexOf(severity(b)) || (a.size === '1280x800' ? -1 : 0))[0];
    out.push({
      severity: sev,
      check: ex.check,
      label: ex.check.startsWith('axe:') ? `Accessibility: ${ex.check.slice(4)}` : LABEL[ex.check] ?? ex.check,
      count: fs.length,
      screens: uniq(fs.map((f) => f.screen)),
      sizes: uniq(fs.map((f) => f.size)),
      themes: uniq(fs.map((f) => f.theme)),
      langs: uniq(fs.map((f) => f.lang)),
      transitions: uniq(fs.map((f) => f.transition)),
      example: ex,
      examples: fs.slice(0, 5),
      source: sourceOf(ex, idx),
    });
  }
  return out.sort((a, b) => ORDER.indexOf(a.severity) - ORDER.indexOf(b.severity) || b.screens.length - a.screens.length || b.count - a.count);
}

const md = (s: unknown) => String(s ?? '').replace(/\|/g, '\\|').replace(/\n/g, ' ');

export function writeReport(): { groups: Group[]; total: number } {
  const dir = resolve(OUT, 'findings');
  const all: Finding[] = existsSync(dir) ? readdirSync(dir).filter((n) => n.endsWith('.json')).flatMap((n) => JSON.parse(readFileSync(join(dir, n), 'utf8')) as Finding[]) : [];
  const groups = group(all);
  writeFileSync(resolve(OUT, 'findings.json'), JSON.stringify(groups, null, 1));
  const L: string[] = [];
  const counts = Object.fromEntries(ORDER.map((s) => [s, groups.filter((g) => g.severity === s).length]));
  const screens = new Set(all.map((f) => f.screen));
  L.push(`# UI audit — generated ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`);
  L.push('');
  L.push(`${screens.size} screens · ${all.length} raw findings → ${groups.length} distinct problems: **${counts.blocker} blocker · ${counts.major} major · ${counts.minor} minor · ${counts.info} info**.`);
  L.push('Screenshots: `out/shots/<screen>/<lang>-<theme>-<size>.png` (transitions: `<lang>-shift-<size>-NN-<name>.png`). Grouped data: `out/findings.json`.');
  L.push('');
  for (const sev of ORDER) {
    const gs = groups.filter((g) => g.severity === sev);
    if (!gs.length) continue;
    L.push(`## ${sev[0].toUpperCase() + sev.slice(1)} (${gs.length})`);
    L.push('');
    gs.forEach((g, i) => {
      const e = g.example;
      L.push(`### ${sev[0].toUpperCase()}${i + 1}. ${g.label}${e.el ? ` — \`${md(e.el).slice(0, 120)}\`` : ''}`);
      L.push(`- **Where:** ${g.screens.join(', ')} · sizes ${g.sizes.join(', ')} · ${g.themes.join('/')} · ${g.langs.join('/')}${g.transitions.length ? ` · after: ${g.transitions.join('; ')}` : ''} (${g.count}×)`);
      L.push(`- **What:** ${md(e.detail)}`);
      if (e.text) L.push(`- **Text:** "${md(e.text).slice(0, 100)}"`);
      if (e.note) L.push(`- **Note:** ${md(e.note)}`);
      if (e.byDesign) L.push(`- **By design:** ${md(e.byDesign)}`);
      if (e.appeared && (e.appeared as string[]).length) L.push(`- **Appeared:** ${(e.appeared as string[]).slice(0, 3).map(md).join('; ')}`);
      if (e.culprit) L.push(`- **Widest content:** \`${md(e.culprit)}\` "${md(e.culpritText).slice(0, 60)}"`);
      if (e.culprits) L.push(`- **Wide elements:** ${(e.culprits as string[]).map((c) => `\`${md(c)}\``).join(', ')}`);
      if (e.shot) L.push(`- **Screenshot:** \`${e.shot}\``);
      if (g.source.length) L.push(`- **Likely source:** ${g.source.map((s) => `\`${s}\``).join(', ')}`);
      if (e.helpUrl) L.push(`- **Rule:** ${e.helpUrl}`);
      L.push('');
    });
  }
  writeFileSync(resolve(OUT, 'report.md'), L.join('\n'));
  return { groups, total: all.length };
}

export default class ReportReporter implements Reporter {
  onEnd() {
    try {
      const { groups, total } = writeReport();
      const c = (s: Severity) => groups.filter((g) => g.severity === s).length;
      console.log(`\nui-audit: ${total} findings → ${groups.length} problems (${c('blocker')} blocker, ${c('major')} major, ${c('minor')} minor, ${c('info')} info)`);
      console.log(`ui-audit: report → ${resolve(OUT, 'report.md')}`);
    } catch (e) {
      console.error('ui-audit: report failed', e);
    }
  }
  printsToStdio() {
    return true;
  }
}

// node ui-audit/lib/report.ts — rebuild the report from the last run's findings without running the browser
if (process.argv[1] && resolve(process.argv[1]) === resolve(import.meta.filename)) {
  const { groups, total } = writeReport();
  console.log(`${total} findings → ${groups.length} problems; ${resolve(OUT, 'report.md')}`);
}
