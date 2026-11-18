// Settings: a sidebar with the search field on top and the panes — General (Open at login, language, the version),
// Models (the speech models: what the meeting's live transcript can run on, downloaded here) and Storage —, and the
// page: the pane, or the search results while there is a query. Ctrl+F focuses the search, Esc clears it, ↓ / ↑ walk
// the results, Enter opens one. The last pane is remembered (app.settings_pane). The page lives in the main window;
// the tray's Settings… opens it.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '@blurt/icons';
import { api, listen, type AppStatus, type LoginItem, type Settings } from '../api';
import { i18n, t } from '../i18n';
import { ENTRIES, GROUPS, MOVED, PANES, pane, paneOf, paneReason, reason, results, SECTIONS, SUGGESTIONS, textIn, type Key, type PaneId, type ResultGroup } from '../settings/catalog';
import { Group, Page, Popup, requestFlash, Row, S, Switch, TextLink, Toggle, lang } from '../settings/kit';
import '../settings/settings.css';
import { meetingApi, avatarUrl, AUTOSAVE_ITEMS, DOCX_MODES, type DocxMode, PROFILE_EVENT, SUMMARY_TEMPLATES, type AutosaveItem, type AutosavePrefs, type Profile, type SummaryPrefs, type SummaryTemplate } from '../meetingApi';
import { isImeKey } from '../ime';
import { Flower, HOST_COLOR } from './Flower';
import { ModelsPane } from './ModelsSettings';
import { setupApi } from '../setupApi';
import { RelayGroup } from './RelaySettings';
import { ProviderGroup } from './ProviderSettings';
import { StoragePane } from './StorageSettings';

// a tray "nav" to a pane can arrive before this page is mounted: kept until it is
let pendingNav: { pane?: string } | null = null;
let navListener: ((p: { pane?: string }) => void) | null = null;
/** open Settings at a pane / row ("general/source") — from the main window's "set up a model" bar */
export function openSettingsAt(link: string) {
  pendingNav = { pane: link };
  navListener?.({ pane: link });
}
listen<{ view?: string; pane?: string }>('nav', (p) => {
  if (p.view !== 'settings') return;
  pendingNav = p;
  navListener?.(p);
});

/** "speech" or "speech/speech.models" (a pane, then a row) → [pane, row]; a pane that moved goes where it went;
 *  a name that isn't a pane: General. */
function resolveLink(link: string | undefined | null, last: string | undefined): [PaneId, string | null] {
  const [p0, row] = (link ?? '').split('/');
  const p = MOVED[p0] ?? p0;
  if (p && pane(p)) return [p as PaneId, row ? (row.includes('.') ? row : `${p}.${row}`) : null];
  if (p) return ['general', null];
  const l = MOVED[last ?? ''] ?? last;
  return [l && pane(l) ? (l as PaneId) : 'general', null];
}

export function SettingsView({ status, onToast, onLang, onBack }: { status: AppStatus; onToast: (m: string) => void; onLang: (lang: string) => void; onBack?: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [cur, setCur] = useState<PaneId>('general');
  const [query, setQuery] = useState('');
  const search = useRef<HTMLInputElement>(null);

  const reload = useCallback(() => api.settings().then(setS, (e) => onToast(String(e))), [onToast]);
  useEffect(() => {
    reload();
  }, [reload]);

  // the pane to open: a nav (tray, link), else the last one
  const go = useCallback((p: PaneId, row?: string | null) => {
    setQuery('');
    setCur(p);
    api.setSettings({ app: { ...(s?.config?.app ?? {}), settings_pane: p } }).catch(() => {});
    if (row) requestFlash(row);
  }, [s]);
  const opened = useRef(false);
  useEffect(() => {
    if (!s || opened.current) return;
    opened.current = true;
    const [p, row] = resolveLink(pendingNav?.pane, s.config?.app?.settings_pane);
    pendingNav = null;
    setCur(p);
    if (row) requestFlash(row);
    // the cursor in the search field, ready to type
    if (!row) setTimeout(() => search.current?.focus(), 0);
  }, [s]);
  useEffect(() => {
    navListener = (n) => {
      const [p, row] = resolveLink(n.pane, cur);
      pendingNav = null;
      setQuery('');
      setCur(p);
      if (row) requestFlash(row);
    };
    return () => {
      navListener = null;
    };
  }, [cur]);

  // Ctrl+F: the search field
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'f') {
        e.preventDefault();
        search.current?.focus();
        search.current?.select();
      }
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, []);

  const groups = useMemo(() => (query.trim() ? results(query, lang()) : []), [query]);
  const counts = useMemo(() => new Map(groups.map((g) => [g.pane, g.paneHit ? ENTRIES.filter((e) => paneOf(e.id) === g.pane).length : g.hits.length])), [groups]);
  const searching = !!query.trim();
  // the no-match page's common searches: only those that still find something here
  const suggestions = useMemo(() => SUGGESTIONS.filter((k) => results(S(k), lang()).length > 0), []);

  // the results' stops for ↑ / ↓ / Enter
  const stops = useMemo(() => groups.flatMap((g) => [...(g.paneHit ? [{ pane: g.pane, row: null as string | null }] : []), ...g.hits.slice(0, g.paneHit ? 6 : 99).map((h) => ({ pane: g.pane, row: h.entry.id as string | null }))]), [groups]);
  const [sel, setSel] = useState(0);
  useEffect(() => setSel(0), [query]);
  const open = (i: number) => {
    const st = stops[i];
    if (st) go(st.pane, st.row);
  };

  if (!s) return <div className="blank">{t('settings.loading')}</div>;
  const cfg = s.config ?? {};
  const save = async (patch: Record<string, unknown>) => {
    await api.setSettings(patch);
    await reload();
  };

  return (
    <div className="st" data-testid="settings">
      <nav className="st-side" aria-label={S('settings.kit.general')}>
        {onBack && (
          <button type="button" className="st-back" onClick={onBack} data-testid="settings-back">
            <Icon name="chevron-left" size={14} />
            <span>{t('settings.back')}</span>
          </button>
        )}
        <div className="st-search">
          <Icon name="search" size={14} />
          <input
            ref={search}
            type="search"
            data-testid="settings-search"
            placeholder={S('settings.search.placeholder')}
            aria-label={S('settings.search.placeholder')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape' && query) {
                e.preventDefault();
                setQuery('');
              } else if (e.key === 'ArrowDown' && searching) {
                e.preventDefault();
                setSel((x) => Math.min(stops.length - 1, x + 1));
              } else if (e.key === 'ArrowUp' && searching) {
                e.preventDefault();
                setSel((x) => Math.max(0, x - 1));
              } else if (e.key === 'Enter' && searching) {
                e.preventDefault();
                open(sel);
              }
            }}
          />
        </div>
        {GROUPS.map((g) => (
          <div key={g.id} role="group" aria-label={S(g.title)}>
            <div className="st-gtitle">{S(g.title)}</div>
            {PANES.filter((p) => p.group === g.id).map((p) => {
              const n = searching ? counts.get(p.id) ?? 0 : null;
              return (
                <button key={p.id} className={`st-pane ${!searching && cur === p.id ? 'sel' : ''} ${n === 0 && groups.length ? 'dim' : ''}`} aria-current={!searching && cur === p.id ? 'page' : undefined}
                  data-testid={`settings-tab-${p.id}`} onClick={() => go(p.id)}>
                  <Icon name={p.icon} size={16} />
                  <span>{S(p.title)}</span>
                  {n ? <span className="st-count">{i18n().formatNumber(n)}</span> : null}
                </button>
              );
            })}
          </div>
        ))}
      </nav>
      {searching ? (
        <SearchResults query={query} groups={groups} stops={stops} sel={sel} setSel={setSel} open={open} clear={() => setQuery('')} setQuery={setQuery}
          counts={counts} suggestions={suggestions} />
      ) : cur === 'general' ? (
        <GeneralPane status={status} cfg={cfg} save={save} onLang={onLang} />
      ) : cur === 'speech' ? (
        <ModelsPane />
      ) : (
        <StoragePane status={status} s={s} save={save} onToast={onToast} />
      )}
    </div>
  );
}

// ------------------------------------------------------------------ search results

/** The matched words in bold (the Mac's results), the rest as it is. */
function Marked({ text, q }: { text: string; q: string }) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return <>{text}</>;
  const lo = text.toLowerCase();
  const at = words.map((w) => [lo.indexOf(w), w.length] as const).filter(([i]) => i >= 0).sort((a, b) => a[0] - b[0])[0];
  if (!at) return <>{text}</>;
  return <>{text.slice(0, at[0])}<b>{text.slice(at[0], at[0] + at[1])}</b>{text.slice(at[0] + at[1])}</>;
}

function SearchResults({ query, groups, stops, sel, setSel, open, clear, setQuery, counts, suggestions }: {
  query: string;
  groups: ResultGroup[];
  stops: { pane: PaneId; row: string | null }[];
  sel: number;
  setSel: (i: number) => void;
  open: (i: number) => void;
  clear: () => void;
  setQuery: (q: string) => void;
  /** each pane's hits, as the sidebar shows them: the lead's total is their sum, so the three never disagree */
  counts: Map<PaneId, number>;
  /** the common searches the no-match page offers (SUGGESTIONS, less the ones the shown panes can't answer) */
  suggestions: Key[];
}) {
  const [more, setMore] = useState<Set<string>>(new Set());
  const total = [...counts.values()].reduce((a, n) => a + n, 0);
  const q = query.trim();
  const L = lang();
  const idx = (p: PaneId, row: string | null) => stops.findIndex((s) => s.pane === p && s.row === row);
  return (
    <div className="st-page" data-testid="settings-results">
      <div className="st-col">
        <h1 className="st-title">{S('settings.search.title_query', q)}</h1>
        {!groups.length ? (
          // the Mac's empty state: the query stays the title; a magnifier hangs left of "No results", the hint,
          // the common searches as links, and Clear Search as a real button
          <div className="st-none">
            <Icon name="search" size={20} />
            <div className="st-none-body">
              <div className="title">{S('settings.search.none.title_short')}</div>
              <div>{S('settings.search.none.hint')}</div>
              <div className="try">
                <span>{S('settings.search.none.common')}</span>
                {suggestions.map((k, i) => (
                  <span key={k}>
                    {i > 0 && <span aria-hidden> · </span>}
                    <TextLink ink onClick={() => setQuery(S(k))}>{S(k)}</TextLink>
                  </span>
                ))}
              </div>
              <button className="st-none-clear" onClick={clear}>{S('settings.search.clear')}</button>
            </div>
          </div>
        ) : (
          <>
            <div className="st-results-lead">
              <span>{total === 1 ? S('settings.search.count_only.one') : S('settings.search.count_only', total)}</span>
              {total > 1 && (
                <span className="st-keyhint"><kbd>↑</kbd><kbd>↓</kbd> {S('settings.search.hint.choose')} <kbd>Enter</kbd> {S('settings.search.hint.open')}</span>
              )}
            </div>
            {groups.map((g) => {
              const p = pane(g.pane)!;
              const all = more.has(g.pane);
              const hits = g.paneHit && !all ? g.hits.slice(0, 6) : g.hits;
              return (
                <section key={g.pane} className="st-rgroup">
                  <h2 className="st-rhead"><Icon name={p.icon} size={16} />{S(p.title)}<span className="st-rcount">{i18n().formatNumber(counts.get(g.pane) ?? g.hits.length)}</span></h2>
                  {g.paneHit && (() => {
                    const k = idx(g.pane, null);
                    const why = paneReason(g.pane, q, L);
                    return (
                      <button className={`st-hit ${sel === k ? 'sel' : ''}`} onMouseMove={() => setSel(k)} onClick={() => open(k)}>
                        <span className="st-hit-text"><span className="st-hit-title"><Marked text={S('settings.search.open_pane', S(p.title))} q={q} /></span>{why && <span className="why">{why}</span>}</span>
                        <Icon name="chevron-right" size={14} />
                      </button>
                    );
                  })()}
                  {hits.map((h) => {
                    const e = h.entry;
                    const k = idx(g.pane, e.id);
                    const why = reason(e, q, L);
                    const sec = e.section && e.kind !== 'section' ? textIn(SECTIONS[e.section], L) : undefined;
                    // a section's name only where it tells two hits apart, never the pane's own name again
                    const where = sec && sec !== S(p.title) && g.hits.filter((x) => x.entry.section === e.section).length === 1 ? sec : null;
                    return (
                      <div key={e.id} className="st-hitwrap">
                        <button className={`st-hit ${sel === k ? 'sel' : ''}`} onMouseMove={() => k >= 0 && setSel(k)} onClick={() => (k >= 0 ? open(k) : undefined)}
                          aria-label={S('settings.search.result.ax', textIn(e.title, L) ?? e.id, S(p.title))}>
                          <span className="st-hit-text">
                            <span className="st-hit-title"><Marked text={textIn(e.title, L) ?? ''} q={q} /></span>
                            {why && <span className="why">{why}</span>}
                          </span>
                          {where ? <span className="where">{where}</span> : null}
                        </button>
                      </div>
                    );
                  })}
                  {g.paneHit && g.hits.length > 6 && (
                    <TextLink ink onClick={() => setMore((m) => {
                      const n = new Set(m);
                      if (all) n.delete(g.pane);
                      else n.add(g.pane);
                      return n;
                    })}>{all ? S('settings.search.fewer') : S('settings.search.more', g.hits.length - 6)}<Icon name={all ? 'chevron-up' : 'chevron-down'} size={12} /></TextLink>
                  )}
                </section>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ General

function GeneralPane({ status, cfg, save, onLang }: {
  status: AppStatus;
  cfg: Record<string, any>;
  save: (patch: Record<string, unknown>) => Promise<void>;
  onLang: (lang: string) => void;
}) {
  const [login, setLogin] = useState<LoginItem | null>(null);
  const [loginErr, setLoginErr] = useState(false);
  useEffect(() => {
    api.loginItem().then(setLogin, () => {});
  }, []);
  const language = cfg.lang === 'zh' ? 'zh-Hans' : ['en', 'zh-Hans'].includes(cfg.lang) ? cfg.lang : 'auto';
  return (
    <Page id="general">
      <ProfileGroup />
      <ProviderGroup />
      <SummaryGroup />
      <PaperGroup />
      <AutosaveGroup />
      <RelayGroup />
      <Group id="general.app">
        {login?.supported && (
          <Toggle id="general.login" on={login.enabled} detail={loginErr ? S('settings.general.login_failed') : undefined} warn={loginErr}
            set={(v) => api.setLoginItem(v).then((l) => (setLogin(l), setLoginErr(l.enabled !== v)), () => setLoginErr(true))} />
        )}
        <Row id="general.language">
          <Popup label={S('settings.general.language')} value={language} dividers={[1]}
            choices={[['auto', S('settings.general.language.system')], ['en', 'English'], ['zh-Hans', '简体中文']]}
            onChange={async (v) => {
              await save({ lang: v === 'auto' ? null : v });
              onLang(v);
            }} />
        </Row>
        <Row id="general.guide">
          <button onClick={() => void setupApi.mark({ reset: true }).catch(() => {})} data-testid="guide-again">{S('settings.guide.again')}</button>
        </Row>
      </Group>

      {/* About, as one quiet line: the version */}
      <div className="st-about st-about-line" data-setting="general.about" data-kind="section">
        <span>{S('settings.general.about.version', status.version)}</span>
      </div>
    </Page>
  );
}

/** Notes: the format a meeting's 纪要 is written in after it ends (回顾 can write one again in another), and for 自定义
 *  the group's own requirements (up to custom_max characters, the backend checks too). They live in the Bloom backend,
 *  so they wait for it like the profile does. The requirements save when the field is left; Esc puts them back. */
function SummaryGroup() {
  const [p, setP] = useState<SummaryPrefs | null>(null);
  const [down, setDown] = useState(false);
  const [text, setText] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  const editing = useRef(false);
  const cancelled = useRef(false);
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = () =>
      meetingApi.summaryPrefs().then(
        (x) => {
          if (!alive) return;
          setP(x);
          setDown(false);
          if (!editing.current) setText(x.custom);
        },
        () => {
          if (!alive) return;
          setDown(true);
          timer = window.setTimeout(load, 4000);
        },
      );
    void load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);
  const max = p?.custom_max ?? 2000;
  const fail = (e: unknown) => (String((e as Error)?.message ?? e) === 'too_long' ? S('summary.err.too_long', max) : S('summary.err.failed'));
  const pick = async (k: SummaryTemplate) => {
    setMsg(null);
    try {
      setP(await meetingApi.setSummaryPrefs({ template: k }));
    } catch (e) {
      setMsg(fail(e));
    }
  };
  const saveText = async () => {
    editing.current = false;
    if (cancelled.current) {
      cancelled.current = false;
      return;
    }
    if (!p) return;
    if (text.trim() === p.custom) {
      setMsg(null);
      return;
    }
    try {
      const x = await meetingApi.setSummaryPrefs({ custom: text });
      setP(x);
      setText(x.custom);
      setMsg(null);
    } catch (e) {
      setMsg(fail(e));
    }
  };
  const tpl = p?.template ?? 'default';
  const custom = tpl === 'custom';
  return (
    <Group id="general.summary" footer={down ? S('settings.profile.offline') : undefined}>
      <Row id="general.summary_template" detail={!custom ? msg : null} warn={!custom && !!msg}>
        <Popup wide label={S('settings.general.summary_template')} value={tpl} disabled={!p} dividers={[4]}
          choices={SUMMARY_TEMPLATES.map((k) => [k, S(`meeting.summary.template.${k}`)] as [SummaryTemplate, string])}
          onChange={(k) => void pick(k)} />
      </Row>
      {custom && (
        <>
          <Row id="general.summary_custom" detail={msg ?? `${text.length} / ${max}`} warn={!!msg} />
          <div className="st-hot" data-testid="summary-custom-box">
            <textarea
              value={text}
              rows={6}
              maxLength={max}
              disabled={!p}
              spellCheck={false}
              placeholder={S('summary.placeholder')}
              aria-label={S('settings.general.summary_custom')}
              data-testid="summary-custom"
              onFocus={() => (editing.current = true)}
              onChange={(e) => setText(e.target.value)}
              onBlur={() => void saveText()}
              onKeyDown={(e) => {
                if (isImeKey(e)) return;
                if (e.key === 'Escape') {
                  cancelled.current = true;
                  setText(p?.custom ?? '');
                  setMsg(null);
                  (e.target as HTMLTextAreaElement).blur();
                }
              }}
            />
          </div>
        </>
      )}
    </Group>
  );
}

const AUTOSAVE_ERRORS = ['empty', 'not_abs', 'too_long', 'not_dir', 'no_write', 'items'];

/** 散会后自动保存 (off by default): after a meeting's post-meeting work the ticked things go into the folder, named
 *  "2026-11-18 组会 纪要.docx". The folder comes from the system picker; the backend checks it (a full path, not too
 *  long, there, writable). Its row's second line always holds one thing — the path (cut with …, whole on hover), or in
 *  the same place why it was refused — so nothing below moves. Turning it on without a folder opens the picker. The
 *  last ticked item can't be unticked. It lives in the backend like the 纪要 format and waits for it the same way. */
function AutosaveGroup() {
  const [p, setP] = useState<AutosavePrefs | null>(null);
  const [down, setDown] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = () =>
      meetingApi.autosave().then(
        (x) => {
          if (!alive) return;
          setP(x);
          setDown(false);
        },
        () => {
          if (!alive) return;
          setDown(true);
          timer = window.setTimeout(load, 4000);
        },
      );
    void load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);
  const fail = (e: unknown) => {
    const c = String((e as Error)?.message ?? e);
    return S('autosave.err', S(`autosave.err.${AUTOSAVE_ERRORS.includes(c) ? c : 'failed'}`, p?.dir_max ?? 180));
  };
  const set = async (patch: { on?: boolean; dir?: string; items?: AutosaveItem[] }) => {
    try {
      setP(await meetingApi.setAutosave(patch));
      setMsg(null);
    } catch (e) {
      setMsg(fail(e));
    }
  };
  const pick = async (andOn = false) => {
    const dir = await meetingApi.pickFolder();
    if (dir) await set(andOn ? { dir, on: true } : { dir });
  };
  const items = p?.items ?? [];
  const flip = (k: AutosaveItem) => {
    const next = items.includes(k) ? items.filter((x) => x !== k) : AUTOSAVE_ITEMS.filter((x) => x === k || items.includes(x));
    if (next.length) void set({ items: next });
  };
  const dirText = msg ?? (p?.dir || S('autosave.none'));
  return (
    <Group id="general.autosave" footer={down ? S('settings.profile.offline') : undefined}>
      <Row id="general.autosave_on">
        <Switch on={!!p?.on} disabled={!p} label={S('settings.general.autosave_on')}
          set={(v) => void (v && !p?.dir ? pick(true) : set({ on: v }))} />
      </Row>
      <Row id="general.autosave_dir" warn={!!msg}
        detail={<span className="st-path" title={msg ? undefined : p?.dir || undefined} data-testid="autosave-dir">{dirText}</span>}>
        <button onClick={() => void pick()} disabled={!p} data-testid="autosave-pick">{S('autosave.pick')}</button>
      </Row>
      <Row id="general.autosave_items" />
      <div className="st-autosave-items" role="group" aria-label={S('settings.general.autosave_items')} data-testid="autosave-items">
        {AUTOSAVE_ITEMS.map((k) => {
          const on = items.includes(k);
          return (
            <label key={k} className="st-autosave-item">
              <input type="checkbox" checked={on} disabled={!p || (on && items.length === 1)} onChange={() => flip(k)} data-testid={`autosave-item-${k}`} />
              <span>{S(`autosave.item.${k}`)}</span>
            </label>
          );
        })}
      </div>
    </Group>
  );
}

/** 第 6 批 · 论文: how a Word paper's edits go back after the meeting (zuhui/paper_prefs.py): Word's tracked changes
 *  (each can be accepted or rejected in Word; the default) or straight into the text. Lives in the backend. */
function PaperGroup() {
  const [mode, setMode] = useState<DocxMode | null>(null);
  const [down, setDown] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = () =>
      meetingApi.paperPrefs().then(
        (x) => alive && (setMode(x.docx_mode), setDown(false)),
        () => {
          if (!alive) return;
          setDown(true);
          timer = window.setTimeout(load, 4000);
        },
      );
    void load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);
  const pick = async (m: DocxMode) => {
    setMsg(null);
    try {
      setMode((await meetingApi.setPaperPrefs({ docx_mode: m })).docx_mode);
    } catch {
      setMsg(S('settings.general.paper_err'));
    }
  };
  return (
    <Group id="general.paper" footer={down ? S('settings.profile.offline') : undefined}>
      <Row id="general.paper_docx" detail={msg} warn={!!msg}>
        <Popup label={S('settings.general.paper_docx')} value={mode ?? 'track'} disabled={!mode}
          choices={DOCX_MODES.map((k) => [k, S(`settings.general.paper_docx.${k}`)] as [DocxMode, string])}
          onChange={(k) => void pick(k)} />
      </Row>
    </Group>
  );
}

const PROFILE_ERRORS = ['empty', 'too_long', 'taken', 'too_big', 'bad_image', 'color'];

/** a row of colour dots; the picked one ringed. A click picks it. */
function Swatches({ value, palette, onPick, disabled, label }: { value: string; palette: string[]; onPick: (c: string) => void; disabled?: boolean; label: string }) {
  return (
    <span className="st-swatches" role="radiogroup" aria-label={label}>
      {palette.map((c) => (
        <button
          key={c}
          type="button"
          role="radio"
          aria-checked={c === value}
          aria-label={c}
          className={`st-swatch ${c === value ? 'on' : ''}`}
          style={{ background: c }}
          disabled={disabled}
          onClick={() => c !== value && onPick(c)}
        />
      ))}
    </span>
  );
}
const profileError = (e: unknown) => {
  const c = String((e as Error)?.message ?? e);
  return S(`settings.profile.err.${PROFILE_ERRORS.includes(c) ? c : 'failed'}`);
};

/** Profile: your photo, your name, and the AI's name in meetings (empty: it goes by the model). They live in the Bloom
 *  backend (not this app's config), so they need it running; while it isn't, the group says so and waits. The first-run
 *  guide shows your part and the AI's part on two steps (`part`), without the heading (`title` ''). */
export function ProfileGroup({ part = 'all', title }: { part?: 'all' | 'me' | 'ai'; title?: string } = {}) {
  const me = part !== 'ai';
  const them = part !== 'me';
  const [p, setP] = useState<Profile | null>(null);
  const [down, setDown] = useState(false);
  const [name, setName] = useState('');
  const [nameErr, setNameErr] = useState<string | null>(null);
  const [ai, setAi] = useState('');
  const [aiErr, setAiErr] = useState<string | null>(null);
  const aiEditing = useRef(false);
  const [picErr, setPicErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const editing = useRef(false);
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = () =>
      meetingApi.profile().then(
        (x) => {
          if (!alive) return;
          setP(x);
          setDown(false);
          if (!editing.current) setName(x.name);
          if (!aiEditing.current) setAi(x.assistant_custom ? x.assistant : '');
        },
        () => {
          if (!alive) return;
          setDown(true);
          timer = window.setTimeout(load, 4000);
        },
      );
    void load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);
  const saveName = async () => {
    editing.current = false;
    if (!p) return;
    if (name.trim() === p.name) {
      setName(p.name);
      setNameErr(null);
      return;
    }
    try {
      const x = await meetingApi.setProfileName(name);
      setP(x);
      setName(x.name);
      setNameErr(null);
    } catch (e) {
      setNameErr(profileError(e));
    }
  };
  // the AI's default name follows the model source (Settings → General → Model source): read again when that changes
  useEffect(() => {
    const again = () =>
      meetingApi.profile().then(
        (x) => {
          setP(x);
          if (!editing.current) setName(x.name);
          if (!aiEditing.current) setAi(x.assistant_custom ? x.assistant : '');
        },
        () => {},
      );
    window.addEventListener(PROFILE_EVENT, again);
    return () => window.removeEventListener(PROFILE_EVENT, again);
  }, []);
  const [colorErr, setColorErr] = useState<string | null>(null);
  const pickColor = async (which: 'color' | 'ai_color', c: string) => {
    setColorErr(null);
    try {
      setP(await meetingApi.setProfileColor(which, c));
    } catch (e) {
      setColorErr(profileError(e));
    }
  };
  const saveAi = async () => {
    aiEditing.current = false;
    if (!p) return;
    const was = p.assistant_custom ? p.assistant : '';
    if (ai.trim() === was) {
      setAi(was);
      setAiErr(null);
      return;
    }
    try {
      const x = await meetingApi.setAssistantName(ai);
      setP(x);
      setAi(x.assistant_custom ? x.assistant : '');
      setAiErr(null);
    } catch (e) {
      setAiErr(profileError(e));
    }
  };
  const pick = async (f: File | undefined) => {
    if (!f) return;
    setPicErr(null);
    if (f.size > 8_000_000) {
      setPicErr(profileError(new Error('too_big')));
      return;
    }
    setBusy(true);
    try {
      const data = await new Promise<string>((ok, bad) => {
        const r = new FileReader();
        r.onload = () => ok(String(r.result));
        r.onerror = () => bad(new Error('bad_image'));
        r.readAsDataURL(f);
      });
      setP(await meetingApi.setAvatar(data));
    } catch (e) {
      setPicErr(profileError(e));
    } finally {
      setBusy(false);
      if (file.current) file.current.value = '';
    }
  };
  const remove = async () => {
    setBusy(true);
    setPicErr(null);
    try {
      setP(await meetingApi.removeAvatar());
    } catch (e) {
      setPicErr(profileError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Group id="general.profile" title={title} footer={down ? S('settings.profile.offline') : undefined}>
      {me && <>
      <Row id="general.avatar" detail={picErr ?? undefined} warn={!!picErr}>
        <span className="st-avatar" data-testid="profile-avatar">
          {p?.avatar ? <img src={avatarUrl(p.avatar)} alt="" draggable={false} /> : <Flower color={p?.color || HOST_COLOR} />}
        </span>
        <button disabled={!p || busy} onClick={() => file.current?.click()} data-testid="profile-pick">
          {S('settings.profile.change')}
        </button>
        {p?.custom && (
          <button disabled={busy} onClick={() => void remove()} data-testid="profile-remove">
            {S('settings.profile.remove')}
          </button>
        )}
        <input ref={file} type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden onChange={(e) => void pick(e.target.files?.[0])} />
      </Row>
      <Row id="general.name" detail={nameErr ?? undefined} warn={!!nameErr}>
        <input
          className="st-name"
          value={name}
          maxLength={24}
          disabled={!p}
          spellCheck={false}
          aria-label={S('settings.general.name')}
          data-testid="profile-name"
          onFocus={() => (editing.current = true)}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => void saveName()}
          onKeyDown={(e) => {
            if (isImeKey(e)) return;
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
            if (e.key === 'Escape') {
              setName(p?.name ?? '');
              setNameErr(null);
              editing.current = false;
              (e.target as HTMLInputElement).blur();
            }
          }}
        />
      </Row>
      <Row id="general.color" detail={colorErr} warn={!!colorErr}>
        {p && <Swatches value={p.color} palette={p.palette} label={S('settings.general.color')} onPick={(c) => void pickColor('color', c)} />}
      </Row>
      </>}
      {them && <>
      <Row id="general.ai_name" detail={aiErr ?? undefined} warn={!!aiErr}>
        <input
          className="st-name"
          value={ai}
          maxLength={24}
          disabled={!p}
          spellCheck={false}
          placeholder={p?.assistant_default ?? ''}
          aria-label={S('settings.general.ai_name')}
          data-testid="profile-ai-name"
          onFocus={() => (aiEditing.current = true)}
          onChange={(e) => setAi(e.target.value)}
          onBlur={() => void saveAi()}
          onKeyDown={(e) => {
            if (isImeKey(e)) return;
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
            if (e.key === 'Escape') {
              setAi(p?.assistant_custom ? p.assistant : '');
              setAiErr(null);
              aiEditing.current = false;
              (e.target as HTMLInputElement).blur();
            }
          }}
        />
      </Row>
      <Row id="general.ai_color">
        {p && <Swatches value={p.ai_color} palette={p.palette} label={S('settings.general.ai_color')} onPick={(c) => void pickColor('ai_color', c)} />}
      </Row>
      </>}
    </Group>
  );
}

// Model source (Claude Code's own login, an Anthropic- or OpenAI-format API, Ollama / LM Studio): ProviderSettings.tsx
export { ProviderGroup };
