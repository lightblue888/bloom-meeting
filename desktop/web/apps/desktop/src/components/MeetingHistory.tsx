// 回顾 (docs/meeting-ui-spec.md §以往会议): the backend's /api/meetings, newest first. Two panes like Tencent Meeting's
// 回顾: the list on the left (title, date, length, a search over titles and the transcripts already loaded), the chosen
// meeting on the right — its 纪要 and its 逐字稿, with 打开 (the live page's still version: the screen recording, the
// diff snapshot, the saved summary and transcript), 分享 and 删除. A click picks a meeting; hovering only lights the row
// (and fetches the meeting quietly). A meeting's details are shown from the cache at once and fetched again behind it
// every time it is picked, and every few seconds while its post-meeting work is still running — the summary and the
// cleaned transcript arrive after the meeting ends.
// Across meetings: the list filters by paper (the papers each meeting had open); 提问 turns the right pane into a
// question box over the past meetings (the model runs only when asked; the answer names its sources, a click opens
// that meeting at that moment); 阶段汇总 turns the list's rows into picks and the right pane into the digests (one is
// written from the picked meetings when asked; they are saved, and copy as Markdown).
// Switching is kept cheap: the list stays mounted (hidden) while a meeting is open, the last opened meeting stays
// mounted (hidden) after 返回 so opening it again is instant (its paper and recording are still loaded), and the polls
// keep unchanged data identical (useDismiss.keep) so nothing re-renders for them.
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '@blurt/icons';
import { i18n, t } from '../i18n';
import {
  ASK_MAX,
  cleanTitle,
  DIGEST_MAX,
  DIGEST_MIN,
  exportName,
  exportReason,
  fmtDay,
  fmtDuration,
  fmtWhen,
  itemText,
  meetingApi,
  splitCites,
  type Digest,
  type DigestBrief,
  type ExportKind,
  type Line,
  type MeetingBrief,
  type MeetingDetail,
  type Participant,
  type PastAnswer,
  type PastSource,
  type VideoInfo,
} from '../meetingApi';
import { MeetingLayout } from './MeetingLayout';
import { useTranscriptEdit } from './TranscriptEdit';
import { Highlight, Names } from './Highlight';
import { colorsByName, nameColor } from './Flower';
import { useMenu } from './Menu';
import { keep, useDismiss } from './useDismiss';
import { isImeKey } from '../ime';
import { NAV_EVENT, takePendingAt, takePendingMeeting } from '../nav';
import { copyRich } from '../richCopy';

/** what, in the list, says a meeting's details have changed */
const sig = (m: MeetingBrief | undefined) => (m ? [m.n_lines, m.has_summary, m.has_diff, m.n_tasks, m.n_done, m.state, m.ended, m.processing].join('|') : '');
const busyOf = (m: MeetingBrief | undefined, d: MeetingDetail | undefined) =>
  !!(m?.processing || d?.processing || m?.state === 'live' || d?.meeting?.state === 'live' || d?.summary_job?.state === 'running');

export function MeetingHistory({ onToast, active = true }: { onToast: (msg: string) => void; active?: boolean }) {
  const [list, setListRaw] = useState<MeetingBrief[] | null>(null);
  const setList = useCallback((l: MeetingBrief[] | ((x: MeetingBrief[] | null) => MeetingBrief[])) => setListRaw((prev) => keep(prev, typeof l === 'function' ? l(prev) : l)), []);
  const [down, setDown] = useState(false);
  const [q, setQ] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  /** the meeting opened last: it stays mounted (hidden) after 返回, so opening it again shows it at once */
  const [kept, setKept] = useState<string | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [details, setDetailsRaw] = useState<Record<string, MeetingDetail>>({});
  const setDetails = useCallback(
    (f: (x: Record<string, MeetingDetail>) => Record<string, MeetingDetail>) => setDetailsRaw((x) => keep(x, f(x))),
    [],
  );
  const [loading, setLoading] = useState<string | null>(null);
  /** the meeting whose delete is waiting for 删除 / 取消 */
  const [confirm, setConfirm] = useState<string | null>(null);
  /** only the meetings that had this paper open (its name, lower-cased); null = all */
  const [paperF, setPaperF] = useState<string | null>(null);
  /** the right pane: the picked meeting, the question box, or the digests (the list's rows are picks then) */
  const [mode, setMode] = useState<'preview' | 'ask' | 'digest'>('preview');
  /** 阶段汇总: the meetings picked for the next one */
  const [picks, setPicks] = useState<string[]>([]);
  /** where to go once a meeting opens (an answer's source, a left-over item in the lobby) */
  const [jump, setJump] = useState<{ id: string; sec: number; k: number } | null>(null);
  const lang = i18n().lang;
  const cache = useRef(details);
  cache.current = details;

  const remove = async (id: string) => {
    setConfirm(null);
    try {
      const res = await meetingApi.deleteMeeting(id);
      const rec = res.recording;
      if (rec && rec.deleted) onToast(t('meeting.history.deleted_rec', { a: rec.mb >= 1000 ? `${(rec.mb / 1000).toFixed(1)} GB` : `${Math.round(rec.mb)} MB` }));
      else if (rec && rec.pending) onToast(t('meeting.history.deleted_rec_later'));
      else if (rec && !rec.deleted) onToast(t('meeting.history.deleted_rec_failed', { a: rec.error ?? '' }));
      setList((l) => (l ?? []).filter((m) => m.id !== id));
      setDetails((x) => {
        const { [id]: _gone, ...rest } = x;
        return rest;
      });
      setSel(null);
    } catch (e) {
      onToast(t('meeting.history.delete_failed', { a: String((e as Error)?.message ?? e) }));
    }
  };

  // the list: every 10 s, every 3 s while some meeting's post-meeting work is still running
  const anyBusy = (list ?? []).some((m) => m.processing || m.state === 'live');
  // failures in a row: only 3 count as "backend down" (a busy backend can miss an answer or two)
  const fails = useRef(0);
  const loaded = useRef(false); // the list arrived once (the effect's closure would see a stale `list`)
  useEffect(() => {
    let alive = true;
    const load = () =>
      meetingApi.meetings({ sample: true }).then(
        (l) => {
          if (!alive) return;
          fails.current = 0;
          loaded.current = true;
          setList(Array.isArray(l) ? l : []);
          setDown(false);
        },
        () => {
          if (!alive) return;
          fails.current += 1;
          if (fails.current >= 3 || !loaded.current) setDown(true);
        },
      );
    void load();
    // 后端还在起（正在连接）：2 秒问一次，起来了马上出列表
    const h = setInterval(load, down ? 2000 : anyBusy ? 3000 : 10_000);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [anyBusy, down]);

  /** a meeting's details, fetched again (the cached copy stays on screen meanwhile); a spinner only when there is none */
  const toast = useRef(onToast);
  toast.current = onToast;
  /** the same for the opened meeting's view (kept stable: that view is memoized) */
  const say = useCallback((msg: string) => toast.current(msg), []);
  const refresh = useCallback(async (id: string): Promise<MeetingDetail | null> => {
    const had = !!cache.current[id];
    if (!had) setLoading(id);
    try {
      const d = await meetingApi.meeting(id);
      setDetails((x) => ({ ...x, [id]: d }));
      return d;
    } catch (e) {
      if (!had) toast.current(t('meeting.history.load_failed', { a: String((e as Error)?.message ?? e) }));
      return null;
    } finally {
      setLoading((l) => (l === id ? null : l));
    }
  }, [setDetails]);

  // hovering a meeting fetches it quietly (once), so the right pane is there the moment it's clicked
  const asked = useRef(new Set<string>());
  const prefetch = async (id: string) => {
    if (cache.current[id] || asked.current.has(id)) return;
    asked.current.add(id);
    try {
      const d = await meetingApi.meeting(id);
      setDetails((x) => (x[id] ? x : { ...x, [id]: d }));
    } catch {
      asked.current.delete(id);
    }
  };

  const show = async (id: string) => {
    if (cache.current[id]) {
      setOpen(id);
      setKept(id);
      void refresh(id);
    } else if (await refresh(id)) {
      setOpen(id);
      setKept(id);
    }
  };
  const back = useCallback(() => setOpen(null), []);
  // a meeting the lobby asked for (its recent list): opened when this page is on screen
  const showRef = useRef(show);
  showRef.current = show;
  useEffect(() => {
    if (!active) return;
    const go = () => {
      const id = takePendingMeeting();
      const at = takePendingAt();
      if (id) {
        setSel(id);
        if (at != null) setJump((j) => ({ id, sec: at, k: (j?.k ?? 0) + 1 }));
        void showRef.current(id);
      }
    };
    go();
    window.addEventListener(NAV_EVENT, go);
    return () => window.removeEventListener(NAV_EVENT, go);
  }, [active]);

  // the papers the meetings had open, newest first, each with how many meetings had it (the filter's menu)
  const paperOpts = useMemo(() => {
    const m = new Map<string, { name: string; n: number }>();
    for (const x of list ?? []) {
      for (const k of new Set((x.papers ?? []).map((p) => p.name.trim().toLowerCase()).filter(Boolean))) {
        const o = m.get(k);
        if (o) o.n += 1;
        else m.set(k, { name: (x.papers ?? []).find((p) => p.name.trim().toLowerCase() === k)!.name.trim(), n: 1 });
      }
    }
    return [...m.entries()].map(([key, v]) => ({ key, ...v }));
  }, [list]);
  const paperOn = paperF && paperOpts.some((o) => o.key === paperF) ? paperF : null;
  const paperName = paperOn ? paperOpts.find((o) => o.key === paperOn)!.name : null;
  const ofPaper = useMemo(
    () => (list ?? []).filter((m) => !paperOn || (m.papers ?? []).some((p) => p.name.trim().toLowerCase() === paperOn)),
    [list, paperOn],
  );
  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return ofPaper;
    return ofPaper.filter((m) => cleanTitle(m.title).toLowerCase().includes(s) || details[m.id]?.transcript.some((l) => l.text.toLowerCase().includes(s)));
  }, [ofPaper, q, details]);
  const paperMenu = useMenu();
  const openPaperMenu = (e: { currentTarget: HTMLElement }) =>
    paperMenu.open(
      [
        { label: t('meeting.history.paper_all'), checked: !paperOn, run: () => setPaperF(null), testId: 'history-paper-all' },
        ...paperOpts.map((o) => ({
          label: o.name,
          badge: i18n().formatNumber(o.n),
          checked: o.key === paperOn,
          run: () => setPaperF(o.key),
          testId: 'history-paper-opt',
        })),
      ],
      { x: 0, y: 0, anchor: e.currentTarget, align: 'start', minWidth: 220 },
      t('meeting.history.paper_filter'),
    );
  /** opens a meeting (an answer's source, a digest's meeting), at this moment when given */
  const goTo = (mid: string, sec?: number | null) => {
    setSel(mid);
    if (typeof sec === 'number') setJump((j) => ({ id: mid, sec, k: (j?.k ?? 0) + 1 }));
    void show(mid);
  };
  const togglePick = (id: string) => setPicks((ps) => (ps.includes(id) ? ps.filter((x) => x !== id) : [...ps, id]));
  const goSource = useCallback((s: PastSource) => goToRef.current(s.mid, s.sec), []);
  const goMeeting = useCallback((mid: string) => goToRef.current(mid, null), []);
  const goToRef = useRef(goTo);
  goToRef.current = goTo;

  // the chosen meeting (the newest until one is clicked) and its details for the right pane
  const picked = sel && shown.some((m) => m.id === sel) ? sel : (shown[0]?.id ?? null);
  const pickedBrief = shown.find((m) => m.id === picked);
  // picked → shown from the cache at once and fetched again behind it; again whenever the list says it changed
  const pickedSig = sig(pickedBrief);
  useEffect(() => {
    if (picked) void refresh(picked);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picked, pickedSig]);
  useEffect(() => {
    setConfirm(null);
  }, [picked]);
  // the opened meeting: the same
  const openBrief = (list ?? []).find((m) => m.id === open);
  const openSig = sig(openBrief);
  useEffect(() => {
    if (open) void refresh(open);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, openSig]);
  // while a meeting's post-meeting work runs (recording → transcript → cleanup → summary), every 3 s
  const watch = open ?? picked;
  const watchBusy = watch ? busyOf((list ?? []).find((m) => m.id === watch), details[watch]) : false;
  useEffect(() => {
    if (!watch || !watchBusy) return;
    const h = setInterval(() => void refresh(watch), 3000);
    return () => clearInterval(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watch, watchBusy]);

  // the opened meeting (or the one opened last, kept mounted behind the list)
  const layoutId = open ?? kept;
  const layoutD = layoutId ? details[layoutId] : undefined;
  const opened = !!(open && details[open]);
  const layoutView = layoutId && layoutD && (
    <div className={`mt-hlayer ${opened ? '' : 'is-off'}`} aria-hidden={!opened || undefined}>
      <OpenMeeting key={layoutId} d={layoutD} active={active && opened} onBack={back} onRefresh={refresh} onToast={say} uiLang={i18n().lang}
        jump={jump && jump.id === layoutId ? jump : null} />
    </div>
  );

  const brief = pickedBrief ?? null;
  const d = picked ? details[picked] : undefined;
  const plines = d ? d.transcript : [];
  // the chosen meeting's people and their colours (host blue, Claude orange, everyone else their own colour, as their flower)
  const meName = d ? ((d.participants ?? []).find((p) => p.you)?.name ?? (d.participants ?? []).find((p) => p.role === 'teacher')?.name ?? t('meeting.rank.host')) : null;
  const guestNames = d ? [...(d.participants ?? []).filter((p) => !p.you && p.id !== 'claude').map((p) => p.name), ...d.transcript.filter((l) => l.who === 'guest' && l.name).map((l) => l.name as string)] : [];
  const colors = colorsByName(d?.participants ?? [], [...guestNames, ...(d?.summary?.speakers ?? [])], meName);
  const allNames = Object.keys(colors);
  const nameOf = (who: string, name?: string) => {
    const ps = d?.participants ?? [];
    if (who === 'guest') return name || t('meeting.who.guest');
    if (who === 'teacher') return ps.find((p) => p.you)?.name ?? ps.find((p) => p.role === 'teacher')?.name ?? t('meeting.rank.host');
    if (who === 'student') return ps.find((p) => p.id === 'claude' || p.role === 'student')?.name ?? t('meeting.who.student');
    return '';
  };
  // the whole transcript of the chosen meeting: built again only when its lines, the search or the meeting change
  // (not on every poll of the list); off-screen lines skip layout and paint (content-visibility in meeting.css)
  const linesView = useMemo(
    () => (
      <ol className="mt-hp-lines">
        {plines.map((l: Line, i: number) => (
          <li key={i} className={`is-${l.who}`}>
            <span className="mt-hp-who" style={{ color: nameColor(colors[nameOf(l.who, l.name)]) }} title={nameOf(l.who, l.name)}>{nameOf(l.who, l.name)}</span>
            <span className="mt-hp-t">{l.t}</span>
            <span className="mt-hp-text"><Highlight text={l.text} q={q} /></span>
          </li>
        ))}
      </ol>
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [plines, q, d?.participants, picked],
  );

  return (
    <div className="mt-hstack">
    <div className={`mt-hlayer ${opened ? 'is-off' : ''}`} aria-hidden={opened || undefined}>
    <div className="mt-root mt-history" data-testid="meeting-history">
      <header className="mt-head">
        <div className="mt-head-main">
          <h1 className="mt-title">{t('meeting.history.title')}</h1>
          <div className="mt-meta">
            {list && <span className="mt-meta-item">{t('meeting.history.count', { n: i18n().formatNumber(list.length) })}</span>}
          </div>
        </div>
        <div className="mt-htools">
          {/* always there (off until the list says what papers there are): nothing appears beside the search box later */}
          <button className={`mt-btn mt-hpaper ${paperOn ? 'is-on' : ''}`} onClick={openPaperMenu} aria-haspopup="menu" aria-expanded={paperMenu.isOpen}
            disabled={!paperOpts.length} title={paperName ?? undefined} data-testid="history-paper">
            <Icon name="filter" size={13} />
            <span className="mt-hpaper-t">{paperName ?? t('meeting.history.paper_all')}</span>
            <Icon name="chevron-down" size={12} />
          </button>
          <div className="mt-search mt-search-head">
            <Icon name="search" size={14} />
            <input value={q} placeholder={t('meeting.history.search')} onChange={(e) => setQ(e.target.value)} aria-label={t('meeting.history.search')} data-testid="history-search" />
            {q && (
              <button className="icon-btn" onClick={() => setQ('')} aria-label={t('main.empty.clear_search')}>
                <Icon name="close" size={12} />
              </button>
            )}
          </div>
          <button className={`mt-btn mt-hmode ${mode === 'ask' ? 'is-on' : ''}`} aria-pressed={mode === 'ask'} disabled={!list?.some((m) => !m.sample)}
            onClick={() => setMode((m) => (m === 'ask' ? 'preview' : 'ask'))} data-testid="history-ask">
            <Icon name="help-circle" size={13} />
            {t('meeting.history.ask')}
          </button>
          <button className={`mt-btn mt-hmode ${mode === 'digest' ? 'is-on' : ''}`} aria-pressed={mode === 'digest'} disabled={!list?.some((m) => !m.sample)}
            onClick={() => setMode((m) => (m === 'digest' ? 'preview' : 'digest'))} data-testid="history-digest">
            <Icon name="list" size={13} />
            {t('meeting.history.digest')}
          </button>
        </div>
        {paperMenu.node}
      </header>
      <div className="mt-history-body">
        {!list ? (
          // 后端还没起来或者列表还在拿：和会议页一样只显示"正在连接…"（app 会自己把后端拉起来）
          <div className="mt-empty">
            <div className="mt-down-card">
              <span className="mw-spinner" aria-hidden="true" />
              <h2>{t('meeting.backend.checking')}</h2>
            </div>
          </div>
        ) : shown.length === 0 ? (
          <div className="mt-empty">
            <Icon name="history" size={28} />
            {q.trim() ? <p>{t('meeting.history.no_match')}</p> : null}
          </div>
        ) : (
          <div className={`mt-hsplit is-${mode}`}>
            <ul className="mt-hlist" role="listbox" aria-label={t('meeting.history.title')} aria-multiselectable={mode === 'digest' || undefined}>
              {shown.map((m) => (
                <li key={m.id} className="mt-hitem" role="none">
                  <button
                    className={`mt-hrow ${(mode === 'digest' ? picks.includes(m.id) : m.id === picked) ? 'is-sel' : ''}`}
                    role="option"
                    aria-selected={mode === 'digest' ? picks.includes(m.id) : m.id === picked}
                    onMouseDown={(e) => mode !== 'digest' && e.button === 0 && setSel(m.id)}
                    // hovering only lights the row and fetches the meeting quietly; the choice changes on a click
                    onMouseEnter={() => void prefetch(m.id)}
                    onClick={() => (mode === 'digest' ? togglePick(m.id) : setSel(m.id))}
                    onDoubleClick={() => void show(m.id)}
                    onKeyDown={(e) => {
                      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
                      e.preventDefault();
                      const i = shown.findIndex((x) => x.id === m.id) + (e.key === 'ArrowDown' ? 1 : -1);
                      const next = shown[Math.max(0, Math.min(shown.length - 1, i))];
                      if (next) {
                        setSel(next.id);
                        (document.querySelector(`[data-testid="history-${next.id}"]`) as HTMLElement | null)?.focus();
                      }
                    }}
                    data-testid={`history-${m.id}`}
                  >
                    {/* one slot for the state dot and, while picking meetings for a digest, the tick box */}
                    <span className="mt-hslot" aria-hidden="true">
                      {mode === 'digest' ? (
                        <span className={`mt-hpick ${picks.includes(m.id) ? 'is-on' : ''}`}>{picks.includes(m.id) && <Icon name="check" size={11} />}</span>
                      ) : (
                        <span className={`mt-state-dot is-${m.state ?? 'ended'}`} />
                      )}
                    </span>
                    <span className="mt-hrow-main">
                      <span className="mt-hrow-title">{cleanTitle(m.title) || m.id}</span>
                      <span className="mt-hrow-sub">
                        <span>{fmtWhen(m.started, lang)}</span>
                        <span>{fmtDuration(m.duration_s)}</span>
                        {m.sample && <span className="mt-hrow-sample" data-testid="history-sample-tag">{t('meeting.history.sample')}</span>}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {mode === 'ask' && (
              <AskPane mids={paperOn ? ofPaper.map((m) => m.id) : null}
                scope={paperOn ? t('meeting.history.ask.scope_paper', { a: paperName ?? '', n: i18n().formatNumber(ofPaper.length) }) : t('meeting.history.ask.scope_all', { n: i18n().formatNumber(list.length) })}
                onClose={() => setMode('preview')} onSource={goSource} />
            )}
            {mode === 'digest' && (
              <DigestPane picks={picks} onClear={() => setPicks([])} onClose={() => setMode('preview')} onToast={onToast} onMeeting={goMeeting} active={active && !opened} />
            )}
            <section className={`mt-hpreview ${mode === 'preview' ? '' : 'is-off'}`} aria-live="polite" data-testid="history-preview" hidden={mode !== 'preview' || undefined}>
              {brief && (
                <>
                  <div className="mt-hp-head">
                    <div className="mt-hp-titles">
                      <h2 className="mt-hp-title">{cleanTitle(brief.title) || brief.id}</h2>
                      <div className="mt-hrow-sub">
                        <span>{fmtWhen(brief.started, lang)}</span>
                        <span>{fmtDuration(brief.duration_s)}</span>
                      </div>
                    </div>
                    <div className="mt-hp-acts">
                      {d && (d.transcript.length > 0 || !!d.video || hasPack(brief, d)) && (
                        <ShareMenu
                          id={brief.id}
                          title={cleanTitle(brief.title) || brief.id}
                          started={brief.started}
                          summary={!!d.summary && brief.state !== 'live'}
                          transcript={d.transcript.length > 0}
                          pack={hasPack(brief, d)}
                          video={d.video ?? null}
                          fileName={recordingName(cleanTitle(brief.title) || brief.id, brief.started)}
                          onToast={onToast}
                          active={active && !opened}
                        />
                      )}
                      {brief.state !== 'live' && (
                        // two steps on one button: 删除 turns red into 确认删除 in place (as wide as the longer of the
                        // two words, so nothing beside it moves); a second click deletes, Esc or clicking elsewhere backs out
                        <button
                          className={`mt-btn mt-hdel ${confirm === brief.id ? 'is-confirm' : ''}`}
                          title={t(brief.sample ? 'meeting.history.delete_ask_sample' : 'meeting.history.delete_ask')}
                          onClick={() => (confirm === brief.id ? void remove(brief.id) : setConfirm(brief.id))}
                          onBlur={() => setConfirm((c) => (c === brief.id ? null : c))}
                          onKeyDown={(e) => e.key === 'Escape' && setConfirm(null)}
                          data-testid={confirm === brief.id ? `history-delete-yes-${brief.id}` : `history-delete-${brief.id}`}
                        >
                          <Icon name="trash" size={13} />
                          <span className="mt-swap">
                            <span className={confirm === brief.id ? 'is-off' : ''}>{t('meeting.history.delete')}</span>
                            <span className={confirm === brief.id ? '' : 'is-off'}>{t('meeting.history.delete_confirm')}</span>
                          </span>
                        </button>
                      )}
                      <button className="mt-btn is-primary" onClick={() => void show(brief.id)} disabled={loading === brief.id} data-testid="history-open">
                        {t('meeting.history.open')}
                        <Icon name="chevron-right" size={13} />
                      </button>
                    </div>
                  </div>
                  {!d ? (
                    <div className="mt-empty">
                      <span className="mw-spinner" aria-hidden="true" />
                    </div>
                  ) : (
                    <div className="mt-hp-body">
                      <h3 className="mt-hp-h">{t('meeting.history.preview.summary')}</h3>
                      {d.summary?.abstract ? (
                        <>
                          <p className="mt-hp-abstract"><Names text={d.summary.abstract} names={allNames} colors={colors} /></p>
                          {(d.summary.conclusions?.length ?? 0) > 0 && (
                            <ul className="mt-hp-points">
                              {d.summary.conclusions.slice(0, 5).map((c, i) => (
                                <li key={i}><Names text={itemText(c)} names={allNames} colors={colors} /></li>
                              ))}
                            </ul>
                          )}
                        </>
                      ) : busyOf(brief, d) ? (
                        <PostSteps post={d.post} />
                      ) : (
                        <p className="mt-hp-none">{t(d.meeting.records_removed ? 'meeting.history.preview.removed' : 'meeting.history.preview.no_summary')}</p>
                      )}
                      <div className="mt-hp-hrow mt-hp-trhead">
                        <h3 className="mt-hp-h">{t('meeting.history.preview.transcript')}</h3>
                      </div>
                      {plines.length ? (
                        linesView
                      ) : (
                        <p className="mt-hp-none">{t(d.meeting.records_removed ? 'meeting.history.preview.removed' : 'meeting.history.preview.no_lines')}</p>
                      )}
                    </div>
                  )}
                </>
              )}
            </section>
          </div>
        )}
      </div>
    </div>
    </div>
    {layoutView}
    </div>
  );
}

/** an opened past meeting: the page with 修订对比 / 纪要 / 录屏; its handlers stay the same between polls. Its name is
 *  locked (2026-11-14, the user: a meeting's name is set before it starts, not afterwards) */
const OpenMeeting = memo(function OpenMeeting({ d, active, onBack, onRefresh, onToast, uiLang, jump }: {
  /** the app's language: a change re-renders this cached view (the back button, the tabs) */
  uiLang: string;
  /** go to this moment once it is on screen (a new k goes again) */
  jump?: { sec: number; k: number } | null;
  d: MeetingDetail;
  active: boolean;
  onBack: () => void;
  /** fetch this meeting again (重新生成 was accepted: its summary_job is running now) */
  onRefresh: (id: string) => unknown;
  onToast: (msg: string) => void;
}) {
  const id = d.meeting.id;
  const started = useCallback(() => void onRefresh(id), [onRefresh, id]);
  // 第 7 批: the transcript fixed by hand (编辑 / 替换 / 撤销 in its head)
  const editing = useTranscriptEdit(d, onRefresh, onToast);
  // 第 6 批: the comments typed on the paper in that meeting, to read
  const reviewComments = useMemo(() => ({ items: d.comments ?? [], canAdd: false, canDelete: false }), [d.comments]);
  const actions = useMemo(
    () => (
      <button className="mt-btn" onClick={onBack} data-testid="history-back">
        <Icon name="arrow-left" size={13} />
        {t('meeting.history.back')}
      </button>
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [onBack, uiLang],
  );
  return (
    <MeetingLayout
      review
      active={active}
      meeting={d.meeting}
      transcript={d.transcript}
      people={d.participants ?? NOBODY}
      board={d.board}
      summary={d.summary}
      talk={d.talk ?? null}
      usage={d.usage ?? null}
      summaryJob={d.summary_job ?? null}
      postBusy={!!d.processing}
      onSummaryStarted={started}
      onToast={onToast}
      diffSrc={meetingApi.diffUrl(d.diff_url)}
      video={d.video ?? null}
      live={false}
      actions={actions}
      jump={jump}
      bookmarks={d.bookmarks ?? null}
      comments={reviewComments}
      carryDone={d.carry_done ?? null}
      transcriptEdit={editing}
    />
  );
});
const NOBODY: Participant[] = [];

/** the post-meeting work while it runs (保存录屏 → 转写 → 整理 → 纪要, from ~/.zuhui/postmeeting.json): the 会议 page no
 *  longer has a 纪要 tab, so this is where its progress shows */
function PostSteps({ post }: { post: MeetingDetail['post'] }) {
  return (
    <div className="mt-hp-post" data-testid="history-post">
      <p className="mt-hp-none">{t('meeting.history.preview.summary_pending')}</p>
      {post?.steps?.length ? (
        <ol className="mt-post-steps">
          {post.steps.map((s) => (
            <li key={s.key} className={`is-${s.state}`}>
              {s.state === 'done' ? <Icon name="check" size={12} /> : s.state === 'running' ? <span className="mw-spinner sm" aria-hidden="true" /> : s.state === 'failed' ? <Icon name="close" size={12} /> : <span className="mt-post-dot" aria-hidden="true" />}
              <span>{s.label}</span>
            </li>
          ))}
        </ol>
      ) : null}
      {post?.error && <p className="mt-hp-none mt-post-err">{post.error}</p>}
    </div>
  );
}

/** 回顾 → 提问: a question over the past meetings (the filtered ones, or all). The model runs only on 提问; the
 *  answer's [n] marks and the sources under it open that meeting at that moment. A failure is said where the
 *  answer would be (nothing above it moves). */
function AskPane({ mids, scope, onClose, onSource }: {
  /** only these meetings (the paper filter's); null = all */
  mids: string[] | null;
  /** what it asks over, in words ("全部 12 场") */
  scope: string;
  onClose: () => void;
  onSource: (s: PastSource) => void;
}) {
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<PastAnswer | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const lang = i18n().lang;
  const len = [...q].length;
  const go = async () => {
    const s = q.trim();
    if (!s || busy) return;
    setBusy(true);
    setErr(null);
    try {
      setRes(await meetingApi.askPast(s, mids));
    } catch (e) {
      const c = String((e as Error)?.message ?? e);
      setRes(null);
      setErr(t('meeting.history.ask.failed', { a: c === 'empty' || c === 'too_long' ? t(`meeting.history.ask.err.${c}`) : c }));
    } finally {
      setBusy(false);
    }
  };
  const where = (s: PastSource) => [fmtDay(s.started, lang), cleanTitle(s.title), s.t].filter(Boolean).join(' · ');
  const byN = new Map((res?.sources ?? []).map((s) => [s.n, s]));
  return (
    <section className="mt-hpreview mt-hpane mt-ask" data-testid="history-ask-pane">
      <div className="mt-hp-head">
        <div className="mt-hp-titles">
          <h2 className="mt-hp-title">{t('meeting.history.ask.title')}</h2>
          <div className="mt-hrow-sub">
            <span className="mt-ask-scope" title={scope}>{scope}</span>
          </div>
        </div>
        <div className="mt-hp-acts">
          <button className="mt-btn" onClick={onClose} data-testid="history-ask-close">
            <Icon name="close" size={12} />
            {t('meeting.history.close')}
          </button>
        </div>
      </div>
      <div className="mt-ask-box">
        <textarea
          value={q}
          maxLength={ASK_MAX}
          rows={3}
          spellCheck={false}
          placeholder={t('meeting.history.ask.ph')}
          aria-label={t('meeting.history.ask.title')}
          onChange={(e) => setQ([...e.target.value].slice(0, ASK_MAX).join(''))}
          onKeyDown={(e) => {
            // Enter asks, Shift+Enter is a new line (not while an input method is choosing characters)
            if (e.key !== 'Enter' || e.shiftKey || isImeKey(e)) return;
            e.preventDefault();
            void go();
          }}
          data-testid="history-ask-input"
        />
        <div className="mt-ask-foot">
          <span className="mt-ask-n">{`${len}/${ASK_MAX}`}</span>
          <button className="mt-btn is-primary mt-ask-go" disabled={!q.trim() || busy} aria-busy={busy || undefined} onClick={() => void go()} data-testid="history-ask-go">
            {/* the words stacked in one box as wide as the widest: 提问 → 回答中… never changes its size */}
            <span className="mt-wstack">
              <span className={busy ? 'is-ghost' : undefined} aria-hidden={busy || undefined}>{t('meeting.history.ask.go')}</span>
              <span className={busy ? undefined : 'is-ghost'} aria-hidden={busy ? undefined : true}>
                <span className="mw-spinner sm" aria-hidden="true" />
                {t('meeting.history.ask.asking')}
              </span>
            </span>
          </button>
        </div>
      </div>
      <div className="mt-ask-out" aria-live="polite">
        {err ? (
          <p className="mt-hp-none mt-ask-err" data-testid="history-ask-error">{err}</p>
        ) : res && !res.found ? (
          <p className="mt-hp-none" data-testid="history-ask-none">{t('meeting.history.ask.none')}</p>
        ) : res ? (
          <>
            <p className="mt-ask-a" data-testid="history-ask-answer">
              {splitCites(res.answer, [...byN.keys()]).map((x, i) =>
                typeof x === 'number' ? (
                  <button key={i} className="mt-ask-cite" onClick={() => onSource(byN.get(x)!)} title={where(byN.get(x)!)}>
                    {x}
                  </button>
                ) : (
                  <span key={i}>{x}</span>
                ),
              )}
            </p>
            {res.sources.length > 0 && (
              <>
                <h3 className="mt-hp-h">{t('meeting.history.ask.sources')}</h3>
                <ol className="mt-ask-src">
                  {res.sources.map((s) => (
                    <li key={s.n}>
                      <button className="mt-ask-srow" onClick={() => onSource(s)} data-testid="history-ask-source">
                        <span className="mt-ask-sn">{s.n}</span>
                        <span className="mt-ask-swhere" title={where(s)}>{where(s)}</span>
                        <span className="mt-ask-stext" title={s.text}>{s.text}</span>
                      </button>
                    </li>
                  ))}
                </ol>
              </>
            )}
          </>
        ) : null}
      </div>
    </section>
  );
}

/** 回顾 → 阶段汇总: the meetings picked in the list, 生成 (the model runs, in the background), the saved digests
 *  (newest first; one being written shows so in its row) and the one opened, which copies as Markdown. A failure
 *  to start is said in a toast; a digest that failed says so in its own row. */
function DigestPane({ picks, onClear, onClose, onToast, onMeeting, active }: {
  picks: string[];
  onClear: () => void;
  onClose: () => void;
  onToast: (msg: string) => void;
  /** a meeting named in the digest clicked: open it */
  onMeeting: (mid: string) => void;
  active: boolean;
}) {
  const [list, setList] = useState<DigestBrief[] | null>(null);
  const [cur, setCur] = useState<string | null>(null);
  const [doc, setDoc] = useState<Digest | null>(null);
  const [starting, setStarting] = useState(false);
  const [copied, setCopied] = useState(false);
  const lang = i18n().lang;
  const running = (list ?? []).some((d) => d.state === 'running');
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = () =>
      meetingApi.digests().then(
        (l) => alive && setList(Array.isArray(l) ? l : []),
        () => alive && setList((x) => x ?? []),
      );
    void load();
    // while one is being written: every 2 s, until it is done
    const h = running ? window.setInterval(load, 2000) : undefined;
    return () => {
      alive = false;
      if (h) clearInterval(h);
    };
  }, [active, running]);
  const shownId = cur && (list ?? []).some((d) => d.id === cur) ? cur : (list ?? []).find((d) => d.state === 'done')?.id ?? null;
  const shownState = (list ?? []).find((d) => d.id === shownId)?.state;
  useEffect(() => {
    if (!shownId || shownState !== 'done') return;
    let alive = true;
    meetingApi.digest(shownId).then(
      (d) => alive && setDoc(d),
      () => {},
    );
    return () => {
      alive = false;
    };
  }, [shownId, shownState]);
  const n = picks.length;
  const ok = n >= DIGEST_MIN && n <= DIGEST_MAX;
  const go = async () => {
    if (!ok || starting) return;
    setStarting(true);
    try {
      const d = await meetingApi.startDigest(picks);
      setList((l) => [d, ...(l ?? []).filter((x) => x.id !== d.id)]);
      setCur(d.id);
      onClear();
    } catch (e) {
      const c = String((e as Error)?.message ?? e);
      // (the catalog can't have keys ending in _few / _many: those are plural forms there)
      const known: Record<string, string> = { too_few: 'min', too_many: 'max', not_found: 'not_found', busy: 'busy' };
      onToast(t('meeting.history.digest.failed', { a: known[c] ? t(`meeting.history.digest.err.${known[c]}`) : c }));
    } finally {
      setStarting(false);
    }
  };
  const copy = async () => {
    if (!doc?.markdown) return;
    try {
      await navigator.clipboard.writeText(doc.markdown);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* no clipboard here */
    }
  };
  const span = (ms: { started: string | null }[]) =>
    ms.length ? `${fmtDay(ms[0].started, lang)} – ${fmtDay(ms[ms.length - 1].started, lang)} · ${t('meeting.history.digest.meetings', { n: i18n().formatNumber(ms.length) })}` : '';
  const showing = doc && doc.id === shownId ? doc : null;
  const r = showing?.result;
  const days = (ids: number[]) =>
    ids
      .map((i) => showing?.meetings[i - 1])
      .filter((m): m is NonNullable<typeof m> => !!m)
      .map((m) => (
        <button key={m.id} className="mt-dg-day" onClick={() => onMeeting(m.id)} title={cleanTitle(m.title)}>
          {fmtDay(m.started, lang)}
        </button>
      ));
  const busy = starting || running;
  return (
    <section className="mt-hpreview mt-hpane mt-dg" data-testid="history-digest-pane">
      <div className="mt-hp-head">
        <div className="mt-hp-titles">
          <h2 className="mt-hp-title">{t('meeting.history.digest.title')}</h2>
          <div className="mt-hrow-sub">
            <span className="mt-dg-picked" data-testid="history-digest-picked">{t('meeting.history.digest.picked', { n: i18n().formatNumber(n) })}</span>
          </div>
        </div>
        <div className="mt-hp-acts">
          <button className="mt-btn is-primary mt-dg-go" disabled={!ok || busy} aria-busy={busy || undefined} onClick={() => void go()}
            title={ok ? undefined : t('meeting.history.digest.range')} data-testid="history-digest-go">
            <span className="mt-wstack">
              <span className={busy ? 'is-ghost' : undefined} aria-hidden={busy || undefined}>{t('meeting.history.digest.go')}</span>
              <span className={busy ? undefined : 'is-ghost'} aria-hidden={busy ? undefined : true}>
                <span className="mw-spinner sm" aria-hidden="true" />
                {t('meeting.history.digest.writing')}
              </span>
            </span>
          </button>
          <button className="mt-btn" onClick={onClose} data-testid="history-digest-close">
            <Icon name="close" size={12} />
            {t('meeting.history.close')}
          </button>
        </div>
      </div>
      <h3 className="mt-hp-h">{t('meeting.history.digest.saved')}</h3>
      {list === null ? (
        <p className="mt-hp-none">…</p>
      ) : !list.length ? (
        <p className="mt-hp-none">{t('meeting.history.digest.none')}</p>
      ) : (
        <ul className="mt-dg-list" data-testid="history-digest-list">
          {list.map((d) => (
            <li key={d.id}>
              <button className={`mt-dg-row ${d.id === shownId ? 'is-sel' : ''}`} onClick={() => setCur(d.id)} disabled={d.state !== 'done'} data-testid="history-digest-row">
                <span className={`mt-dg-row-t is-${d.state}`} title={d.state === 'failed' ? d.error ?? undefined : d.title || undefined}>
                  {d.state === 'running' ? (
                    <>
                      <span className="mw-spinner sm" aria-hidden="true" />
                      {t('meeting.history.digest.writing')}
                    </>
                  ) : d.state === 'failed' ? (
                    t('meeting.history.digest.failed', { a: d.error ?? '' })
                  ) : (
                    d.title || t('meeting.history.digest.title')
                  )}
                </span>
                <span className="mt-dg-row-m">{span(d.meetings)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {showing && r && (
        <article className="mt-dg-doc" data-testid="history-digest-doc">
          <div className="mt-dg-doc-head">
            <h3 className="mt-dg-doc-t">{r.title}</h3>
            <button className="mt-btn mt-dg-copy" onClick={() => void copy()} data-testid="history-digest-copy">
              <span className="mt-wstack">
                <span className={copied ? 'is-ghost' : undefined} aria-hidden={copied || undefined}>
                  <Icon name="copy" size={13} />
                  {t('meeting.history.digest.copy')}
                </span>
                <span className={copied ? undefined : 'is-ghost'} aria-hidden={copied ? undefined : true}>
                  <Icon name="check" size={13} />
                  {t('meeting.history.digest.copied')}
                </span>
              </span>
            </button>
          </div>
          <p className="mt-dg-range">{span(showing.meetings)}</p>
          {r.overview && <p className="mt-hp-abstract">{r.overview}</p>}
          {r.issues.length > 0 && (
            <>
              <h4 className="mt-hp-h">{t('meeting.history.digest.issues')}</h4>
              <ul className="mt-dg-items">
                {r.issues.map((x, i) => (
                  <li key={i}>
                    <span>{x.text}</span>
                    <span className="mt-dg-days">{days(x.meetings)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {r.feedback.length > 0 && (
            <>
              <h4 className="mt-hp-h">{t('meeting.history.digest.feedback')}</h4>
              <ul className="mt-dg-items">
                {r.feedback.map((x, i) => (
                  <li key={i}>
                    <span>{x.by ? `${x.by}：${x.text}` : x.text}</span>
                    <span className="mt-dg-days">{days(x.meetings)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {r.open.length > 0 && (
            <>
              <h4 className="mt-hp-h">{t('meeting.history.digest.open')}</h4>
              <ul className="mt-dg-items is-open">
                {r.open.map((x, i) => (
                  <li key={i}>
                    <span>{x.who ? `${x.text}（${x.who}）` : x.text}</span>
                    <span className="mt-dg-days">{days(x.meeting ? [x.meeting] : [])}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </article>
      )}
    </section>
  );
}

/** the recording's file type, from the backend's video type */
const VIDEO_EXT: Record<string, string> = { 'video/webm': 'webm', 'video/quicktime': 'mov', 'video/x-matroska': 'mkv' };

/** 存录屏's default file name: "<title> <MM-DD HHmm>" (the extension follows the recording's type), nothing Windows refuses in a name */
function recordingName(title: string, started: string | null | undefined): string {
  const d = started ? new Date(started) : null;
  const p = (n: number) => String(n).padStart(2, '0');
  const when = d && !Number.isNaN(d.getTime()) ? ` ${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}${p(d.getMinutes())}` : '';
  // eslint-disable-next-line no-control-regex
  const base = title.replace(/[\\/:*?"<>|\u0000-\u001f]+/g, ' ').replace(/\s+/g, ' ').trim().replace(/[. ]+$/, '');
  return `${base}${when}`;
}

/** 导出会议包: a meeting that is over and has something to pack (a 纪要, to-dos, the revisions) */
const hasPack = (m: MeetingBrief, d: MeetingDetail) => m.state !== 'live' && (!!d.summary || !!d.diff_url || (d.board?.tasks?.length ?? 0) > 0);

/** 导出会议包's "with the transcript" tick, remembered per viewer */
const PACK_TR_KEY = 'bloom.pack.transcript';
const packTrSaved = () => {
  try {
    return localStorage.getItem(PACK_TR_KEY) === '1';
  } catch {
    return false;
  }
};

/** 回顾 → 保存 (only on the host's machine; every file goes where the user picks):
 *  纪要 — 存成 Word, 存成 PDF, 复制（带格式） (HTML and plain text on the clipboard: headings and lists survive a paste
 *  into Word, 飞书, 腾讯文档); 逐字稿 — 存成 Markdown / TXT; 导出会议包 — one HTML file with the 纪要, the to-dos and the
 *  revisions (the transcript when ticked) that opens anywhere offline; 存录屏. A file that takes a while (a PDF, the
 *  pack, the recording) keeps the menu open with its item busy, the spinner in the item's own corner (nothing moves);
 *  the result is said in a toast. */
function ShareMenu({ id, title, started, summary, transcript, pack, video, fileName, onToast, active }: {
  id: string;
  /** the meeting's name and start: the files' default names ("2026-11-18 组会 纪要.docx") */
  title: string;
  started: string | null | undefined;
  /** the meeting has a 纪要: Word / PDF / 复制 */
  summary: boolean;
  /** the transcript has lines: 存成 Markdown / TXT, and the pack's transcript tick */
  transcript: boolean;
  /** the meeting is over: 导出会议包 */
  pack: boolean;
  /** the meeting has a recording: 存录屏 */
  video: VideoInfo | null;
  /** 存录屏's default file name, without the extension */
  fileName: string;
  onToast: (msg: string) => void;
  active: boolean;
}) {
  const [open, setOpen] = useState(false);
  /** what is being saved: docx | pdf | copy | pack | md | txt | rec */
  const [busy, setBusy] = useState<string | null>(null);
  const busyRef = useRef<string | null>(null);
  const [packTr, setPackTr] = useState(packTrSaved);
  const box = useRef<HTMLDivElement>(null);
  // a press anywhere else, Esc, or leaving the page closes it — not while something is being saved (the save dialog
  // takes the focus; the busy item stays in view until the file is written)
  useDismiss(
    open,
    () => {
      if (!busyRef.current) setOpen(false);
    },
    box,
    active,
  );
  /** one save at a time; `stay`: the menu stays open (busy) until it is done */
  const run = async (key: string, job: () => Promise<string | null>, stay: boolean) => {
    if (busyRef.current) return;
    busyRef.current = key;
    setBusy(key);
    if (!stay) setOpen(false);
    try {
      const path = await job();
      if (path) onToast(t('meeting.history.shared', { a: path }));
    } catch (e) {
      onToast(t('meeting.history.share_failed', { a: exportReason(e) }));
    } finally {
      busyRef.current = null;
      setBusy(null);
      setOpen(false);
    }
  };
  const save = (kind: ExportKind) =>
    void run(
      kind,
      () =>
        meetingApi.saveExport(
          id,
          kind,
          exportName(title, started, t(kind === 'pack' ? 'meeting.export.word.pack' : 'meeting.export.word.notes'), kind === 'pack' ? 'html' : kind),
          kind === 'pack' && transcript && packTr,
        ),
      true,
    );
  const copy = async () => {
    if (busyRef.current) return;
    busyRef.current = 'copy';
    setBusy('copy');
    try {
      const c = await meetingApi.minutesClip(id);
      await copyRich(c.html, c.text);
      onToast(t('meeting.history.share.copied'));
    } catch (e) {
      onToast(t('meeting.history.share.copy_failed', { a: exportReason(e) }));
    } finally {
      busyRef.current = null;
      setBusy(null);
      setOpen(false);
    }
  };
  const saveRec = () => {
    if (!video) return;
    const ext = VIDEO_EXT[video.type ?? ''] ?? 'mp4';
    void run('rec', () => meetingApi.saveRecording(id, `${fileName}.${ext}`, ext), true);
  };
  /** an item: its words, and its spinner in the right corner while it works (a fixed slot: the words never move) */
  const item = (key: string, label: string, onClick: () => void, testId: string) => (
    <button role="menuitem" className="mt-share-it" onClick={onClick} disabled={!!busy} aria-busy={busy === key || undefined} data-testid={testId}>
      <span className="mt-share-l">{label}</span>
      {busy === key && <span className="mw-spinner sm" aria-hidden="true" />}
    </button>
  );
  const setTr = (v: boolean) => {
    setPackTr(v);
    try {
      localStorage.setItem(PACK_TR_KEY, v ? '1' : '0');
    } catch {
      /* private window: this time only */
    }
  };
  return (
    <div className="mt-share" ref={box}>
      <button className="mt-btn mt-share-btn" onClick={() => !busyRef.current && setOpen((v) => !v)} disabled={!!busy && !open} aria-expanded={open} data-testid="history-share">
        <Icon name="download" size={13} />
        {t('meeting.history.share')}
      </button>
      {open && (
        <div className="mt-share-pop" role="menu" data-testid="history-share-menu">
          {summary && (
            <div className="mt-share-group" role="group" aria-label={t('meeting.history.preview.summary')}>
              <div className="mt-share-head" aria-hidden="true">{t('meeting.history.preview.summary')}</div>
              {item('docx', t('meeting.history.share.docx'), () => save('docx'), 'history-share-docx')}
              {item('pdf', t('meeting.history.share.pdf'), () => save('pdf'), 'history-share-pdf')}
              {item('copy', t('meeting.history.share.copy'), () => void copy(), 'history-share-copy')}
            </div>
          )}
          {transcript && (
            <div className="mt-share-group" role="group" aria-label={t('meeting.history.preview.transcript')}>
              <div className="mt-share-head" aria-hidden="true">{t('meeting.history.preview.transcript')}</div>
              {item('md', t('meeting.history.share.md'), () => void run('md', () => meetingApi.exportTranscript(id, 'md'), false), 'history-share-md')}
              {item('txt', t('meeting.history.share.txt'), () => void run('txt', () => meetingApi.exportTranscript(id, 'txt'), false), 'history-share-txt')}
            </div>
          )}
          {pack && (
            <div className="mt-share-group mt-share-packrow">
              {item('pack', t('meeting.history.share.pack'), () => save('pack'), 'history-share-pack')}
              {transcript && (
                <label className="mt-share-opt">
                  <input type="checkbox" checked={packTr} disabled={!!busy} onChange={(e) => setTr(e.target.checked)} data-testid="history-share-pack-tr" />
                  <span>{t('meeting.history.share.pack_tr')}</span>
                </label>
              )}
            </div>
          )}
          {video && <div className="mt-share-group">{item('rec', t('meeting.history.share.video'), saveRec, 'history-share-video')}</div>}
        </div>
      )}
    </div>
  );
}
