// 回顾 (docs/meeting-ui-spec.md §以往会议): the backend's /api/meetings, newest first. Two panes like Tencent Meeting's
// 回顾: the list on the left (title, date, length, a search over titles and the transcripts already loaded), the chosen
// meeting on the right — its 纪要 and its 逐字稿, with 打开 (the live page's still version: the screen recording, the
// diff snapshot, the saved summary and transcript), 分享 and 删除. A click picks a meeting; hovering only lights the row
// (and fetches the meeting quietly). A meeting's details are shown from the cache at once and fetched again behind it
// every time it is picked, and every few seconds while its post-meeting work is still running — the summary and the
// cleaned transcript arrive after the meeting ends.
// Switching is kept cheap: the list stays mounted (hidden) while a meeting is open, the last opened meeting stays
// mounted (hidden) after 返回 so opening it again is instant (its paper and recording are still loaded), and the polls
// keep unchanged data identical (useDismiss.keep) so nothing re-renders for them.
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '@blurt/icons';
import { i18n, t } from '../i18n';
import { cleanTitle, fmtDuration, fmtWhen, meetingApi, type Line, type MeetingBrief, type MeetingDetail, type Participant, type VideoInfo } from '../meetingApi';
import { MeetingLayout } from './MeetingLayout';
import { Highlight, Names } from './Highlight';
import { colorsByName } from './Flower';
import { keep, useDismiss } from './useDismiss';

/** what, in the list, says a meeting's details have changed */
const sig = (m: MeetingBrief | undefined) => (m ? [m.n_lines, m.has_summary, m.has_diff, m.n_tasks, m.n_done, m.state, m.ended, m.processing].join('|') : '');
const busyOf = (m: MeetingBrief | undefined, d: MeetingDetail | undefined) => !!(m?.processing || d?.processing || m?.state === 'live' || d?.meeting?.state === 'live');

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
      meetingApi.meetings().then(
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

  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    const all = list ?? [];
    if (!s) return all;
    return all.filter((m) => cleanTitle(m.title).toLowerCase().includes(s) || details[m.id]?.transcript.some((l) => l.text.toLowerCase().includes(s)));
  }, [list, q, details]);

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
      <OpenMeeting key={layoutId} d={layoutD} active={active && opened} onBack={back} uiLang={i18n().lang} />
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
            <span className="mt-hp-who" style={{ color: colors[nameOf(l.who, l.name)] }}>{nameOf(l.who, l.name)}</span>
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
        <div className="mt-search mt-search-head">
          <Icon name="search" size={14} />
          <input value={q} placeholder={t('meeting.history.search')} onChange={(e) => setQ(e.target.value)} aria-label={t('meeting.history.search')} data-testid="history-search" />
          {q && (
            <button className="icon-btn" onClick={() => setQ('')} aria-label={t('main.empty.clear_search')}>
              <Icon name="close" size={12} />
            </button>
          )}
        </div>
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
          <div className="mt-hsplit">
            <ul className="mt-hlist" role="listbox" aria-label={t('meeting.history.title')}>
              {shown.map((m) => (
                <li key={m.id} className="mt-hitem">
                  <button
                    className={`mt-hrow ${m.id === picked ? 'is-sel' : ''}`}
                    role="option"
                    aria-selected={m.id === picked}
                    onMouseDown={(e) => e.button === 0 && setSel(m.id)}
                    // hovering only lights the row and fetches the meeting quietly; the choice changes on a click
                    onMouseEnter={() => void prefetch(m.id)}
                    onClick={() => setSel(m.id)}
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
                    <span className={`mt-state-dot is-${m.state ?? 'ended'}`} aria-hidden="true" />
                    <span className="mt-hrow-main">
                      <span className="mt-hrow-title">{cleanTitle(m.title) || m.id}</span>
                      <span className="mt-hrow-sub">
                        <span>{fmtWhen(m.started, lang)}</span>
                        <span>{fmtDuration(m.duration_s)}</span>
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <section className="mt-hpreview" aria-live="polite" data-testid="history-preview">
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
                      {d && (d.transcript.length > 0 || !!d.video) && (
                        <ShareMenu
                          id={brief.id}
                          transcript={d.transcript.length > 0}
                          video={d.video ?? null}
                          fileName={recordingName(cleanTitle(brief.title) || brief.id, brief.started)}
                          onToast={onToast}
                          active={active && !opened}
                        />
                      )}
                      {brief.state !== 'live' &&
                        (confirm === brief.id ? (
                          <span className="mt-hdel-ask">
                            <span>{t('meeting.history.delete_ask')}</span>
                            <button className="mt-hdel-yes" onClick={() => void remove(brief.id)} data-testid={`history-delete-yes-${brief.id}`}>
                              {t('meeting.history.delete')}
                            </button>
                            <button className="mt-hdel-no" onClick={() => setConfirm(null)}>
                              {t('meeting.history.delete_cancel')}
                            </button>
                          </span>
                        ) : (
                          <button className="mt-btn" onClick={() => setConfirm(brief.id)} data-testid={`history-delete-${brief.id}`}>
                            <Icon name="trash" size={13} />
                            {t('meeting.history.delete')}
                          </button>
                        ))}
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
                                <li key={i}><Names text={c} names={allNames} colors={colors} /></li>
                              ))}
                            </ul>
                          )}
                        </>
                      ) : busyOf(brief, d) ? (
                        <PostSteps post={d.post} />
                      ) : (
                        <p className="mt-hp-none">{t('meeting.history.preview.no_summary')}</p>
                      )}
                      <div className="mt-hp-hrow mt-hp-trhead">
                        <h3 className="mt-hp-h">{t('meeting.history.preview.transcript')}</h3>
                      </div>
                      {plines.length ? (
                        linesView
                      ) : (
                        <p className="mt-hp-none">{t('meeting.history.preview.no_lines')}</p>
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
const OpenMeeting = memo(function OpenMeeting({ d, active, onBack, uiLang }: {
  /** the app's language: a change re-renders this cached view (the back button, the tabs) */
  uiLang: string;
  d: MeetingDetail;
  active: boolean;
  onBack: () => void;
}) {
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
      diffSrc={meetingApi.diffUrl(d.diff_url)}
      video={d.video ?? null}
      live={false}
      actions={actions}
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

/** 回顾 → 分享: the transcript as Markdown or TXT, saved where the user picks (only on the host's machine). 存录屏: the
 *  meeting's screen recording, copied by the backend to where the user picks; the item stays open and busy while it
 *  copies. */
function ShareMenu({ id, transcript, video, fileName, onToast, active }: {
  id: string;
  /** the transcript has lines: 存成 Markdown / TXT */
  transcript: boolean;
  /** the meeting has a recording: 存录屏 */
  video: VideoInfo | null;
  /** 存录屏's default file name, without the extension */
  fileName: string;
  onToast: (msg: string) => void;
  active: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [recBusy, setRecBusy] = useState(false);
  const recBusyRef = useRef(false);
  const box = useRef<HTMLDivElement>(null);
  // a press anywhere else, Esc, or leaving the page closes it — not while the recording is being saved (the save
  // dialog takes the focus; the busy item stays in view until the copy is done)
  useDismiss(
    open,
    () => {
      if (!recBusyRef.current) setOpen(false);
    },
    box,
    active,
  );
  const saveRec = async () => {
    if (!video || recBusyRef.current) return;
    recBusyRef.current = true;
    setRecBusy(true);
    try {
      const ext = VIDEO_EXT[video.type ?? ''] ?? 'mp4';
      const path = await meetingApi.saveRecording(id, `${fileName}.${ext}`, ext);
      if (path) onToast(t('meeting.history.shared', { a: path }));
    } catch (e) {
      onToast(t('meeting.history.share_failed', { a: String((e as Error)?.message ?? e) }));
    } finally {
      recBusyRef.current = false;
      setRecBusy(false);
      setOpen(false);
    }
  };
  const recItem = video && (
    <div className="mt-share-group">
      <button role="menuitem" className="mt-share-rec" onClick={() => void saveRec()} disabled={recBusy} aria-busy={recBusy || undefined} data-testid="history-share-video">
        {recBusy && <span className="mw-spinner sm" aria-hidden="true" />}
        {t('meeting.history.share.video')}
      </button>
    </div>
  );
  const go = async (fmt: 'md' | 'txt') => {
    setOpen(false);
    setBusy(true);
    try {
      const path = await meetingApi.exportTranscript(id, fmt);
      if (path) onToast(t('meeting.history.shared', { a: path }));
    } catch (e) {
      onToast(t('meeting.history.share_failed', { a: String((e as Error)?.message ?? e) }));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-share" ref={box}>
      <button className="mt-btn mt-share-btn" onClick={() => setOpen((v) => !v)} disabled={busy || recBusy} aria-expanded={open} data-testid="history-share">
        <Icon name="download" size={13} />
        {t('meeting.history.share')}
      </button>
      {open && (
        <div className="mt-share-pop" role="menu">
          {transcript && (
            <>
              <button role="menuitem" onClick={() => void go('md')} data-testid="history-share-md">
                {t('meeting.history.share.md')}
              </button>
              <button role="menuitem" onClick={() => void go('txt')} data-testid="history-share-txt">
                {t('meeting.history.share.txt')}
              </button>
            </>
          )}
          {recItem}
        </div>
      )}
    </div>
  );
}
