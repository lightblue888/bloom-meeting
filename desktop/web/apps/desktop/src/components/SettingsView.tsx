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
import { Group, Page, Popup, requestFlash, Row, S, TextLink, Toggle, lang } from '../settings/kit';
import '../settings/settings.css';
import { meetingApi, avatarUrl, type Profile } from '../meetingApi';
import { isImeKey } from '../ime';
import { Flower, HOST_COLOR } from './Flower';
import { ModelsPane } from './ModelsSettings';
import { StoragePane } from './StorageSettings';

// a tray "nav" to a pane can arrive before this page is mounted: kept until it is
let pendingNav: { pane?: string } | null = null;
let navListener: ((p: { pane?: string }) => void) | null = null;
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
      </Group>

      {/* About, as one quiet line: the version */}
      <div className="st-about st-about-line" data-setting="general.about" data-kind="section">
        <span>{S('settings.general.about.version', status.version)}</span>
      </div>
    </Page>
  );
}

const PROFILE_ERRORS = ['empty', 'too_long', 'taken', 'too_big', 'bad_image'];
const profileError = (e: unknown) => {
  const c = String((e as Error)?.message ?? e);
  return S(`settings.profile.err.${PROFILE_ERRORS.includes(c) ? c : 'failed'}`);
};

/** Profile: your photo, your name, and the AI's name in meetings (empty: it goes by the model). They live in the Bloom
 *  backend (not this app's config), so they need it running; while it isn't, the group says so and waits. */
function ProfileGroup() {
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
    <Group id="general.profile" footer={down ? S('settings.profile.offline') : undefined}>
      <Row id="general.avatar" detail={picErr ?? undefined} warn={!!picErr}>
        <span className="st-avatar" data-testid="profile-avatar">
          {p?.avatar ? <img src={avatarUrl(p.avatar)} alt="" draggable={false} /> : <Flower color={HOST_COLOR} />}
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
    </Group>
  );
}
