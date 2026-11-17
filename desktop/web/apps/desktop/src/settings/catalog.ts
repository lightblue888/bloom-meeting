// Every setting the Settings page shows, as data: the panes in the sidebar's groups,
// each pane's sections, one entry per row (its id, title and one-line description as string keys, a longer
// explanation, search keywords st.settings.kw.<id>). The panes render their rows from these entries, and the search
// runs over the same entries, so what search finds is what the page shows.
//
// Search (the Mac's rule, unchanged): every word of the query has to appear in an entry's title, description,
// keywords or section — in the UI language or the other one — scored title first; plain substring matching. Results
// come in groups, one per pane, best group first (ties: the sidebar's order), each by section. A query that is (the
// start of) a pane's name finds the pane as a whole; when no row matches by its own words, a pane whose keywords hold
// every word does.
import { CATALOGS } from '../i18n';
import type { IconName } from '@blurt/icons';

export type Lang = 'en' | 'zh-Hans';
/** a string key in the settings catalogs (without the "st." prefix) */
export type Key = string;

export interface PaneInfo {
  id: PaneId;
  title: Key;
  icon: IconName;
  group: 'preferences' | 'mac';
}
export type PaneId = 'general' | 'speech' | 'storage';

export interface Entry {
  id: string;
  kind: 'row' | 'section';
  section: string | null;
  title: Key;
  detail?: Key;
  more?: Key;
}

export const GROUPS: { id: PaneInfo['group']; title: Key }[] = [
  { id: 'preferences', title: 'settings.group.preferences' },
  { id: 'mac', title: 'settings.group.mac' },
];

export const PANES: PaneInfo[] = [
  { id: 'general', title: 'settings.kit.general', icon: 'settings', group: 'preferences' },
  { id: 'speech', title: 'settings.kit.speech', icon: 'cpu', group: 'preferences' },
  { id: 'storage', title: 'settings.kit.storage', icon: 'drive', group: 'mac' },
];
export const pane = (id: string) => PANES.find((p) => p.id === id);

/** Old links (blurt://settings/<pane>, a remembered last pane) and where they land now. */
export const MOVED: Record<string, string> = { cloud: 'speech', 'speech.cloud': 'speech' };

export const SECTIONS: Record<string, Key> = {
  'general.profile': 'settings.general.profile',
  'general.app': 'settings.general.app',
  'general.about': 'settings.general.about',
  'speech.models': 'speech.section.models',
  'storage.recordings': 'settings.storage.recordings',
  'storage.models': 'settings.storage.models',
  'storage.files': 'settings.storage.app_data',
};

const row = (id: string, section: string | null, title: Key, o: { detail?: Key; more?: Key } = {}): Entry => ({ id, kind: 'row', section, title, ...o });
const whole = (id: string, detail?: Key): Entry => ({ id, kind: 'section', section: id, title: SECTIONS[id] ?? id, detail });

export const ENTRIES: Entry[] = [
  // General
  row('general.avatar', 'general.profile', 'settings.general.avatar'),
  row('general.name', 'general.profile', 'settings.general.name'),
  row('general.ai_name', 'general.profile', 'settings.general.ai_name', { detail: 'settings.general.ai_name.detail' }),
  row('general.login', 'general.app', 'settings.general.login', { detail: 'settings.general.login.detail' }),
  row('general.language', 'general.app', 'settings.general.language'),
  whole('general.about'),
  // Models
  whole('speech.models', 'speech.models.footer'),
  // Storage
  whole('storage.recordings'),
  row('storage.auto_delete', 'storage.recordings', 'settings.storage.videos.auto', { detail: 'settings.storage.videos.footer' }),
  whole('storage.models', 'settings.storage.model.footer'),
  whole('storage.files'),
];

export const entry = (id: string) => ENTRIES.find((e) => e.id === id);
export const paneOf = (id: string) => id.split('.')[0] as PaneId;

// ------------------------------------------------------------------ text

type Catalog = Record<string, string>;
const cat = (lang: Lang) => CATALOGS[lang] as Catalog;

/** Arguments for a title with placeholders (the hotkey in "Alt+Shift+T translates into"), by key — set by the page;
 * without them a placeholder reads "…". */
let args: (key: Key) => Record<string, string> | undefined = () => undefined;
export const setArguments = (f: typeof args) => (args = f);

/** A key's text in a language (the settings catalog's "st." key, else the main window's own); undefined when missing. */
export function textIn(key: Key | undefined, lang: Lang): string | undefined {
  if (!key) return undefined;
  const s = cat(lang)[`st.${key}`] ?? (cat(lang) as Catalog)[key];
  const a = args(key);
  return s?.replace(/\{(\w+)\}/g, (_, n: string) => a?.[n] ?? '…');
}
const kwKey = (e: Entry) => `settings.kw.${e.id}`;
const paneKw = (p: PaneInfo) => `settings.kw.pane.${p.id}`;

/** Case, accents and full / half width don't matter. */
export const fold = (s: string) => s.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
export const words = (q: string) => q.split(/\s+/).map(fold).filter(Boolean);

interface Field { text: string; title: boolean; pane: boolean; weight: number }

function fields(e: Entry, here: Lang, other: Lang): Field[] {
  const out: Field[] = [];
  const add = (k: Key | undefined, lang: Lang, w: number, o: { title?: boolean; pane?: boolean } = {}) => {
    if (!k) return;
    const s = textIn(k, lang);
    if (s) out.push({ text: fold(s), title: !!o.title, pane: !!o.pane, weight: w });
  };
  const p = pane(paneOf(e.id))!;
  const sec = e.section ? SECTIONS[e.section] : undefined;
  add(e.title, here, 100, { title: true });
  add(kwKey(e), here, 45);
  add(e.title, other, 40);
  add(kwKey(e), other, 35);
  add(sec, here, 30);
  add(p.title, here, 20, { pane: true });
  add(paneKw(p), here, 20, { pane: true });
  add(e.detail, here, 20);
  add(e.more, here, 15);
  add(sec, other, 12);
  add(p.title, other, 10, { pane: true });
  add(paneKw(p), other, 10, { pane: true });
  add(e.detail, other, 8);
  return out;
}

const isLatin = (s: string) => /^[\x00-\x7f]*$/.test(s);
const isLetter = (c: string) => /\p{L}/u.test(c);

/** A field's best match for one word: a title scores by where the word sits (its start, a word's start, anywhere);
 * elsewhere a Latin word has to start a word ("voice" isn't found in "SenseVoice"); Chinese matches anywhere. */
function score(term: string, fs: Field[]): number {
  let best = 0;
  const latin = isLatin(term);
  for (const f of fs) {
    let from = 0;
    for (;;) {
      const i = f.text.indexOf(term, from);
      if (i < 0) break;
      const atStart = i === 0;
      const atWord = atStart || !isLetter(f.text[i - 1]);
      let s = 0;
      if (f.title) s = atStart ? 100 : atWord ? 80 : 60;
      else if (atWord || !latin) s = f.weight;
      best = Math.max(best, s);
      if (s > 0 && (atWord || !f.title)) break;
      from = i + term.length;
    }
  }
  return best;
}

export interface Hit { entry: Entry; score: number }
export interface ResultGroup { pane: PaneId; paneHit: boolean; hits: Hit[]; score: number }

function bySection(hits: Hit[]): Hit[] {
  const keys: string[] = [];
  const runs = new Map<string, Hit[]>();
  for (const h of hits) {
    const k = h.entry.section ?? '';
    if (!runs.has(k)) {
      keys.push(k);
      runs.set(k, []);
    }
    runs.get(k)!.push(h);
  }
  const ordered = [...keys.filter((k) => k), ...keys.filter((k) => !k)];
  return ordered.flatMap((k) => {
    const r = runs.get(k) ?? [];
    return [...r.filter((h) => h.entry.kind === 'section'), ...r.filter((h) => h.entry.kind !== 'section')];
  });
}

/** The results by pane, best group first; ties keep the sidebar's order. */
export function results(query: string, here: Lang, shown: (e: Entry) => boolean = () => true): ResultGroup[] {
  const terms = words(query);
  if (!terms.length) return [];
  const other: Lang = here === 'en' ? 'zh-Hans' : 'en';
  const entries = ENTRIES.filter(shown);
  const all = entries.map((e) => fields(e, here, other));
  const pass = (paneLevel: boolean): Hit[] => {
    const hits: [Hit, number][] = [];
    entries.forEach((e, n) => {
      const f = paneLevel ? all[n] : all[n].filter((x) => !x.pane);
      let total = 0;
      for (const t of terms) {
        const s = score(t, f);
        if (!s) return;
        total += s;
      }
      hits.push([{ entry: e, score: total }, n]);
    });
    return hits.sort((a, b) => (a[0].score !== b[0].score ? b[0].score - a[0].score : a[1] - b[1])).map((h) => h[0]);
  };
  const q = fold(query.trim());
  const longEnough = q.length >= (isLatin(q) ? 2 : 1);
  const whole = new Set<PaneId>(
    longEnough ? PANES.filter((p) => [here, other].some((l) => fold(textIn(p.title, l) ?? '').startsWith(q))).map((p) => p.id) : [],
  );
  let own = pass(false);
  if (!own.length && !whole.size) {
    for (const p of PANES) {
      const pf: Field[] = [textIn(p.title, here), textIn(paneKw(p), here), textIn(p.title, other), textIn(paneKw(p), other)]
        .filter((s): s is string => !!s)
        .map((s) => ({ text: fold(s), title: false, pane: true, weight: 10 }));
      if (terms.every((t) => score(t, pf) > 0)) whole.add(p.id);
    }
    if (!whole.size) own = pass(true);
  }
  const groups: ResultGroup[] = [];
  const lead = 100_000;
  for (const p of PANES) {
    if (!whole.has(p.id)) continue;
    const rows = entries.filter((e) => paneOf(e.id) === p.id).map((e) => ({ entry: e, score: lead }));
    groups.push({ pane: p.id, paneHit: true, hits: bySection(rows), score: lead });
  }
  for (const p of PANES) {
    if (whole.has(p.id)) continue;
    const rows = own.filter((h) => paneOf(h.entry.id) === p.id);
    if (!rows.length) continue;
    groups.push({ pane: p.id, paneHit: false, hits: bySection(rows), score: rows[0].score });
  }
  const order = new Map(PANES.map((p, i) => [p.id, i]));
  return groups.sort((a, b) => (a.score !== b.score ? b.score - a.score : order.get(a.pane)! - order.get(b.pane)!));
}

/** The sentence of a text that holds the most of `terms`, without its parentheses. */
function excerpt(texts: string[], terms: string[]): string | undefined {
  const hits = (t: string) => terms.filter((x) => fold(t).includes(x)).length;
  let best: { t: string; n: number } | undefined;
  for (const t of texts) {
    const n = hits(t);
    if (n > (best?.n ?? 0)) best = { t, n };
    if (n === terms.length) break;
  }
  if (!best) return undefined;
  const sentences = best.t.match(/[^。！？.!?]+(?:[。！？]|[.!?](?=\s|$))?/g)?.map((s) => s.trim()).filter(Boolean) ?? [best.t];
  const most = Math.max(...sentences.map(hits));
  const pick = sentences.find((s) => hits(s) === most) ?? best.t;
  const bare = pick.replace(/\s*[（(][^）)]*[）)]/g, '');
  return bare || pick;
}

const keywordList = (k: Key, lang: Lang) => (textIn(k, lang) ?? '').split(/[,，、]/).map((s) => s.trim()).filter(Boolean);

/** Why an entry was found when its title doesn't say so (always in the UI's language); undefined when the title holds
 * every word. */
export function reason(e: Entry, query: string, here: Lang): string | undefined {
  const missing = words(query).filter((w) => !fold(textIn(e.title, here) ?? '').includes(w));
  if (!missing.length) return undefined;
  const texts = [textIn(e.detail, here), ...keywordList(kwKey(e), here), textIn(e.more, here)].filter((s): s is string => !!s);
  return excerpt(texts, missing) ?? textIn(e.detail, here);
}

/** Why a pane was found as a whole when its name doesn't say so: the keyword that holds the query. */
export function paneReason(id: PaneId, query: string, here: Lang): string | undefined {
  const p = pane(id)!;
  const missing = words(query).filter((w) => !fold(textIn(p.title, here) ?? '').includes(w));
  if (!missing.length) return undefined;
  return excerpt(keywordList(paneKw(p), here), missing);
}

/** The searches the no-match page offers. */
export const SUGGESTIONS: Key[] = ['model', 'language'].map((s) => `settings.search.try.${s}`);
