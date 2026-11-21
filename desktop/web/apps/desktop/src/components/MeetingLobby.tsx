// 会议 before a meeting: the lobby. One panel for the next meeting: on the left its name (large, editable in place),
// the paper on the desk (its size and when it was changed, so it's clearly the right one and the right version), and
// 开始会议, which stays off until what a meeting needs is there (the transcription model; with a paper, a model source
// too) and says what's missing with the fix right beside it. No paper is not missing: that meeting keeps only the
// transcript (mode "chat", the AI stays out); on the right, past a hairline, where everything stands (the two
// models, transcription, microphone, voice) and the invite link. Below, the recent meetings (a click opens one in 回顾). Beside them, on a second tab of the same card, what the last
// meetings on this paper left undone (their to-dos, notes, edits nobody accepted): ticked here, saved with the meeting
// it came from; the meeting about to start carries the rest. The meeting page itself (MeetingLayout) comes up once the
// meeting starts; the lobby never shows the last meeting's transcript.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type DragEvent, type ReactNode } from 'react';
import { Icon } from '@blurt/icons';
import { i18n, t } from '../i18n';
import { cleanTitle, fmtDay, fmtDuration, fmtWhen, meetingApi, type CarryItem, type Current, type Invite, type MeetingBrief, type UpdateStatus } from '../meetingApi';
import { nav } from '../nav';
import { relayApi, setupApi } from '../setupApi';
import { useReadiness, type Readiness } from './readiness';
import './setup.css';

interface DropProps {
  onDragOver: (e: DragEvent) => void;
  onDragLeave: (e: DragEvent) => void;
  onDrop: (e: DragEvent) => void;
}

export function MeetingLobby({ cur, paperName, busy, importText, notice, title, onTitle, onStart, onPickPaper, onClearPaper, dropProps, over, active, onHistory }: {
  cur: Current | null;
  /** the paper just imported, before the next poll says so */
  paperName: string | null;
  busy: 'start' | 'stop' | 'paper' | null;
  importText: string | null;
  notice: ReactNode;
  title: string;
  onTitle: (title: string) => void;
  onStart: () => void;
  onPickPaper: () => void;
  /** 移除: no paper for the next meeting (then it keeps only the transcript); null while that can't be done */
  onClearPaper?: (() => void) | null;
  dropProps: DropProps;
  over: boolean;
  active: boolean;
  /** opens the paper's saved versions (历史版本); null when there's no paper to look back on */
  onHistory?: (() => void) | null;
}) {
  const r = useReadiness(active);
  const paper = cur?.paper?.exists ? cur.paper : null;
  // the chosen paper's file is gone (moved, renamed, deleted): not "no paper" — started now, the meeting would keep only
  // the transcript while you think the AI is there to edit (2026-11-19 e2e). Its name stays, 开始会议 waits for 更换 / 移除
  const missing = !paper && !!cur?.paper?.path && !cur.paper.exists;
  const hasPaper = !!paper || !!paperName || missing;
  const name = paperName ?? paper?.name ?? (missing ? cur?.paper?.name : '') ?? '';
  // no paper is not missing: the meeting then keeps only the transcript and the AI stays out, so it needs no model
  // source either (2026-11-19, the user). A paper whose file is gone is: said beside 开始会议 too, with 更换
  const needs: NeedKind[] = [...(missing ? (['paper'] as const) : []), ...r.blockers.filter((b) => hasPaper || b !== 'provider')];
  const canStart = !needs.length && busy !== 'start' && busy !== 'paper';
  const lang = i18n().lang;

  // the recent meetings, newest first (every 15 s while shown: one may still be finishing)
  const [recent, setRecent] = useState<MeetingBrief[] | null>(null);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = () =>
      meetingApi.meetings().then(
        (l) => alive && setRecent(Array.isArray(l) ? l.filter((m) => m.state !== 'live').sort((a, b) => (b.started ?? '').localeCompare(a.started ?? '')) : []),
        () => {},
      );
    void load();
    const h = window.setInterval(load, 15_000);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [active]);
  // what the last meetings on this paper left undone; a tick stays on screen (struck through) until the lobby is
  // opened again, so nothing moves under the pointer
  const [carry, setCarry] = useState<CarryItem[] | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(() => new Set());
  const [carryErr, setCarryErr] = useState<string | null>(null);
  const [tab, setTab] = useState<'recent' | 'carry' | null>(null);
  const [page, setPage] = useState(0);
  const paperKey = paper?.path ?? '';
  useEffect(() => {
    if (!active) return;
    let alive = true;
    meetingApi.carry().then(
      (r) => {
        if (!alive) return;
        const items = Array.isArray(r?.items) ? r.items : [];
        setCarry(items);
        setTicked(new Set());
        setPage(0);
        // the first time: the left-over items first when there are any
        setTab((x) => x ?? (items.length ? 'carry' : 'recent'));
      },
      () => alive && setCarry((c) => c ?? []),
    );
    return () => {
      alive = false;
    };
  }, [active, paperKey]);
  const errTimer = useRef<number | undefined>(undefined);
  const tick = async (it: CarryItem) => {
    const on = !ticked.has(it.id);
    const flip = (want: boolean) =>
      setTicked((s) => {
        const n = new Set(s);
        if (want) n.add(it.id);
        else n.delete(it.id);
        return n;
      });
    flip(on);
    try {
      await meetingApi.tickCarry(it.id, on);
    } catch (e) {
      flip(!on);
      const c = String((e as Error)?.message ?? e);
      setCarryErr(t('lobby.carry.failed', { a: c === 'not_found' ? t('lobby.carry.err.not_found') : c }));
      window.clearTimeout(errTimer.current);
      errTimer.current = window.setTimeout(() => setCarryErr(null), 4000);
    }
  };
  useEffect(() => () => window.clearTimeout(errTimer.current), []);
  const view: 'recent' | 'carry' = tab ?? 'recent';
  const openN = (carry ?? []).filter((x) => !ticked.has(x.id)).length;

  // as many rows as fit in the window's height (the page itself never scrolls at its usual size): recent meetings
  // as many as fit (全部 has the rest), left-over items as many as fit, the rest on the next page
  const listBox = useRef<HTMLDivElement>(null);
  const [boxH, setBoxH] = useState(LIST_TOP + 5 * ROW_H);
  const fit = Math.max(1, Math.floor((boxH - LIST_TOP) / ROW_H));
  const rows = Math.max(1, Math.floor((boxH - LIST_TOP) / CARRY_ROW_H));
  const measure = useCallback(() => {
    const el = listBox.current;
    if (el) setBoxH(el.clientHeight);
  }, []);
  const pages = Math.max(1, Math.ceil((carry?.length ?? 0) / rows));
  const pg = Math.min(page, pages - 1);
  // measured after every render that can change the space, and whenever the window or the card changes size
  useLayoutEffect(measure, [measure, recent, busy, notice, view]);
  useEffect(() => {
    const el = listBox.current;
    window.addEventListener('resize', measure);
    const ro = el ? new ResizeObserver(measure) : null;
    if (el) ro?.observe(el);
    return () => {
      window.removeEventListener('resize', measure);
      ro?.disconnect();
    };
  }, [measure]);
  const [invite, setInvite] = useState<Invite | null>(null);
  const inviteLoad = useRef<() => Promise<unknown>>(() => Promise.resolve());
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = () => meetingApi.invite().then((x) => alive && setInvite(x), () => alive && setInvite({ ok: false }));
    void load();
    inviteLoad.current = load;
    const h = window.setInterval(load, 30_000);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [active]);
  const reloadInvite = useCallback(() => void inviteLoad.current(), []);

  return (
    <div className={`lb ${over ? 'is-over' : ''}`} {...dropProps} data-testid="lobby">
      <div className="lb-inner">
        <div className="lb-top">
          <section className="lb-new" aria-labelledby="lb-new-h">
            <div className="lb-new-h">
              <h2 id="lb-new-h" className="lb-kicker">{t('lobby.new')}</h2>
              {/* a notice (a start that failed, a paper that didn't import) sits on the heading's line: nothing below moves */}
              {notice && <div className="lb-notice">{notice}</div>}
            </div>
            <input
              className="lb-title"
              value={title}
              maxLength={60}
              spellCheck={false}
              placeholder={cleanTitle(name) || t('lobby.title_ph')}
              title={title || cleanTitle(name) || undefined}
              aria-label={t('lobby.title')}
              onChange={(e) => onTitle(e.target.value)}
              data-testid="lobby-title"
            />
            <div className="lb-field">
              <span className="lb-label">{t('lobby.paper')}</span>
              {busy === 'paper' ? (
                <div className="lb-paper is-busy">
                  <span className="mw-spinner" aria-hidden="true" />
                  <div className="lb-paper-text">
                    <div className="lb-paper-name">{t('meeting.paper.importing')}</div>
                    <div className="lb-paper-meta lb-fixed-line">{importText ?? ''}</div>
                  </div>
                </div>
              ) : (
                // the same row with a paper or without one (then the meeting keeps only the transcript): 选择 and 更换
                // share one button at the row's end, as wide as the longer word; 移除 and 历史版本 come in to its left
                <div className={`lb-paper ${hasPaper ? '' : 'is-none'}`} data-testid={hasPaper ? 'lobby-paper' : 'lobby-no-paper'}>
                  {/* without a paper the two lines keep their room (empty) and 未选择 sits over them, centred: the row is
                      as high either way, so 开始会议 under it never moves */}
                  <div className="lb-paper-text">
                    <div className="lb-paper-name" title={hasPaper ? name : undefined} aria-hidden={!hasPaper || undefined}>{hasPaper ? name : ' '}</div>
                    {/* a new PDF comes in as plain text first: while its formulas and tables are recognized, the same line
                        says so in place (no row grows), then goes back to its size and date */}
                    <div className={`lb-paper-meta ${missing ? 'is-err' : ''}`} aria-hidden={!hasPaper || undefined}
                      data-testid={missing ? 'lobby-paper-missing' : paper?.pending_full ? 'lobby-paper-recognizing' : undefined}>
                      {missing
                        ? t('lobby.paper_missing')
                        : paper?.pending_full
                        ? t('lobby.paper_recognizing')
                        : hasPaper
                        ? [
                            paper?.words ? t('lobby.words', { a: i18n().formatNumber(paper.words, { notation: 'compact', maximumFractionDigits: 1 }) }) : null,
                            paper?.modified ? t('lobby.changed', { a: fmtWhen(paper.modified.replace(' ', 'T'), lang) }) : null,
                          ]
                            .filter(Boolean)
                            .join(' · ')
                        : ''}
                    </div>
                    {!hasPaper && <div className="lb-paper-none">{t('lobby.paper_none')}</div>}
                  </div>
                  <span className="lb-paper-acts">
                    {/* the short word here (English "History"): with 移除 beside it the paper's line keeps its room at 1024 px */}
                    {hasPaper && !missing && onHistory && <button className="link" onClick={onHistory} data-testid="lobby-paper-history">{t('lobby.paper_history')}</button>}
                    {hasPaper && (
                      <button className="link" onClick={onClearPaper ?? undefined} disabled={!onClearPaper || busy === 'start'} data-testid="lobby-clear-paper">
                        {t('lobby.paper_clear')}
                      </button>
                    )}
                    <button className="lb-paper-change" onClick={onPickPaper} data-testid={hasPaper ? 'lobby-change-paper' : 'lobby-pick-paper'}>
                      <span className="lb-swap">
                        <span className={hasPaper ? '' : 'is-off'} aria-hidden={!hasPaper || undefined}>{t('lobby.change')}</span>
                        <span className={hasPaper ? 'is-off' : ''} aria-hidden={hasPaper || undefined}>{t('lobby.fix.paper')}</span>
                      </span>
                    </button>
                  </span>
                </div>
              )}
            </div>
            <div className="lb-start-row">
              <button className="lb-start" disabled={!canStart} onClick={onStart} data-testid="lobby-start">
                {/* the spinner floats over the label's left: the button keeps its width while it starts */}
                {busy === 'start' && <span className="mw-spinner" aria-hidden="true" />}
                <span>{t('lobby.start')}</span>
              </button>
              <div className="lb-needs" aria-live="polite">
                {needs.map((n) => (
                  <Need key={n} n={n} r={r} onPickPaper={onPickPaper} />
                ))}
              </div>
            </div>
          </section>

          <div className="lb-side">
            <section aria-labelledby="lb-ready-h">
              <div className="lb-side-h">
                <h2 id="lb-ready-h" className="lb-kicker">{t('lobby.ready')}</h2>
                <UpdateLine active={active} />
                <button className="link" onClick={() => nav({ to: 'settings' })}>{t('lobby.settings')}</button>
              </div>
              <Status r={r} hasPaper={hasPaper} />
            </section>
            <section className="lb-inv" aria-labelledby="lb-inv-h">
              <InviteLine invite={invite} onChange={reloadInvite} />
            </section>
          </div>
        </div>

        <section className="lb-recent" aria-labelledby={view === 'carry' ? 'lb-carry-h' : 'lb-recent-h'}>
          <div className="lb-card-hrow">
            <div className="lb-tabs" role="tablist">
              <button role="tab" id="lb-recent-h" className="lb-tab" aria-selected={view === 'recent'} onClick={() => setTab('recent')} data-testid="lobby-tab-recent">
                {t('lobby.recent')}
              </button>
              <button role="tab" id="lb-carry-h" className="lb-tab" aria-selected={view === 'carry'} onClick={() => setTab('carry')} data-testid="lobby-tab-carry">
                {t('lobby.carry')}
                {/* the count keeps its room when it is 0: ticking the last one doesn't move the tab's edge */}
                <span className={`lb-tab-n ${openN ? '' : 'is-ghost'}`} aria-hidden={!openN || undefined}>{i18n().formatNumber(openN)}</span>
              </button>
            </div>
            {view === 'recent'
              ? !!recent?.length && <button className="link" onClick={() => nav({ to: 'history' })}>{t('lobby.all')}</button>
              : pages > 1 && (
                  <span className="lb-pager">
                    <button className="lb-pager-b" disabled={pg === 0} onClick={() => setPage(pg - 1)} aria-label={t('lobby.carry.prev')} data-testid="lobby-carry-prev">
                      <Icon name="chevron-left" size={14} />
                    </button>
                    <span className="lb-pager-n lb-num">{`${pg + 1}/${pages}`}</span>
                    <button className="lb-pager-b" disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)} aria-label={t('lobby.carry.next')} data-testid="lobby-carry-next">
                      <Icon name="chevron-right" size={14} />
                    </button>
                  </span>
                )}
          </div>
          <div className="lb-list-box" ref={listBox}>
          {/* nothing (yet) in the list: said in the middle of the empty box, not stuck under the tabs */}
          {view === 'carry' ? (
            carry === null ? (
              <div className="lb-empty lb-list-empty">…</div>
            ) : !carry.length ? (
              <div className="lb-empty lb-list-empty">{t('lobby.carry_none')}</div>
            ) : (
              <ul className="lb-list" data-testid="lobby-carry">
                {carry.slice(pg * rows, pg * rows + rows).map((it) => {
                  const on = ticked.has(it.id);
                  return (
                    <li key={it.id}>
                      <div className={`lb-carry ${on ? 'is-done' : ''}`}>
                        <label className="lb-carry-main">
                          <input type="checkbox" checked={on} onChange={() => void tick(it)} data-testid="lobby-carry-tick" />
                          <span className="lb-carry-t" title={it.text}>{it.text}</span>
                        </label>
                        <span className="lb-meeting-m lb-carry-who" title={it.who ?? undefined}>{it.who ?? ''}</span>
                        <span className={`lb-carry-kind is-${it.kind}`}>{t(`lobby.carry.kind.${it.kind}`)}</span>
                        <button className="lb-carry-day" title={cleanTitle(it.title) || undefined}
                          onClick={() => nav({ to: 'history', meeting: it.mid, at: it.sec ?? null })} data-testid="lobby-carry-open">
                          {fmtDay(it.started, lang)}
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )
          ) : recent === null ? (
            <div className="lb-empty lb-list-empty">…</div>
          ) : !recent.length ? (
            <div className="lb-empty lb-list-empty">{t('lobby.none')}</div>
          ) : (
            <ul className="lb-list">
              {recent.slice(0, fit).map((m) => (
                <li key={m.id}>
                  <button className="lb-meeting" onClick={() => nav({ to: 'history', meeting: m.id })} data-testid="lobby-recent">
                    <span className="lb-meeting-d lb-num">{fmtWhen(m.started, lang)}</span>
                    <span className="lb-meeting-t" title={cleanTitle(m.title)}>{cleanTitle(m.title) || t('lobby.untitled')}</span>
                    <span className="lb-meeting-m lb-num">{m.duration_s ? fmtDuration(m.duration_s) : ''}</span>
                    <span className="lb-meeting-m lb-num">{m.n_tasks ? t('lobby.edits', { a: m.n_done, b: m.n_tasks }) : ''}</span>
                    {/* only what's unusual: still being written up, or no minutes */}
                    <span className={`lb-meeting-tag ${m.processing ? 'is-busy' : ''}`}>{m.processing ? t('lobby.processing') : m.has_summary ? '' : t('lobby.no_summary')}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {/* a tick that didn't save: said over the list's corner for a few seconds (nothing moves) */}
          {carryErr && view === 'carry' && <div className="lb-carry-err" role="alert">{carryErr}</div>}
          </div>
        </section>
      </div>
      {over && (
        <div className="lb-veil" aria-hidden="true">
          {t(hasPaper ? 'meeting.paper.drop_new' : 'lobby.drop_here')}
        </div>
      )}
    </div>
  );
}

type NeedKind = 'paper' | 'provider' | 'asr';

/** what keeps 开始会议 off, each with its fix: the same red as its 准备情况 row, the same link as 更换 there */
function Need({ n, r, onPickPaper }: { n: NeedKind; r: Readiness; onPickPaper: () => void }) {
  if (n === 'paper') {
    return (
      <span className="lb-need" data-testid="lobby-need-paper">
        <Icon name="warning" size={13} />
        <span>{t('lobby.paper_missing')}</span>
        <button className="link" onClick={onPickPaper}>{t('lobby.change')}</button>
      </span>
    );
  }
  if (n === 'asr' && r.speech.dl) {
    // coming down: the model's name, a bar and the percent, on the one line the need had
    const dl = r.speech.dl;
    const pct = dl.total_mb > 0 ? Math.min(100, Math.round((dl.done_mb / dl.total_mb) * 100)) : null;
    return (
      <span className="lb-need is-dl" title={t('lobby.need.asr_dl')}>
        <span className="lb-need-t">{t('lobby.need.asr_dl_short')}</span>
        <progress max={100} value={pct ?? undefined} aria-label={t('lobby.need.asr_dl')} />
        <span className="lb-need-pct">{pct == null ? '…' : `${pct}%`}</span>
      </span>
    );
  }
  const sel = r.speech.models?.selected || r.speech.models?.recommended;
  return (
    <span className="lb-need">
      <Icon name="warning" size={13} />
      <span>{t(`lobby.need.${n}`)}</span>
      {n === 'provider' && <button className="link" onClick={() => nav({ to: 'settings', link: 'general/source' })}>{t('lobby.fix.provider')}</button>}
      {n === 'asr' && sel && <button className="link" onClick={() => void r.speech.download(sel)}>{t('lobby.fix.asr')}</button>}
    </span>
  );
}

/** a recent meeting's row (setup.css .lb-meeting min-height + its border) */
const ROW_H = 45;
/** a left-over item's row (setup.css .lb-carry min-height + its border): lower, so a small window still shows a few */
const CARRY_ROW_H = 37;
/** the list's top margin (setup.css .lb-list) */
const LIST_TOP = 8;

function Status({ r, hasPaper }: { r: Readiness; hasPaper: boolean }) {
  const [say, setSay] = useState<string | null>(null);
  const test = async () => {
    setSay(t('lobby.voice_testing'));
    try {
      const x = await setupApi.testVoice();
      setSay(x.ok ? null : t(`lobby.voice_err.${x.error ?? 'failed'}`));
    } catch {
      setSay(t('lobby.voice_err.failed'));
    }
  };
  const pv = r.provider;
  // no model source: in the way only with a paper (red); without one that meeting needs no model (grey, 2026-11-20)
  const model = !pv ? 'wait' : pv.kind === 'unset' ? (hasPaper ? 'block' : 'off') : 'ok';
  const row = (label: string, value: ReactNode, state: 'ok' | 'warn' | 'block' | 'off' | 'wait', act?: ReactNode) => (
    <div className={`lb-st is-${state}`}>
      <span className="lb-st-dot" aria-hidden="true" />
      <span className="lb-st-l">{label}</span>
      <span className="lb-st-v">{value}</span>
      <span className="lb-st-a">{act}</span>
    </div>
  );
  const voice = r.voiceEngine === undefined ? '…' : r.voiceEngine === null ? t('ob.voice_off') : t(`ob.voice.${r.voiceEngine}`);
  // every row's 更换 opens Settings at the row that changes it (flashed there). The models: their own rows under Model
  // source (Claude Code's login, an API with model fields); no source yet, or Anthropic's API (fixed models): the source
  const change = (link: string, id: string) => (
    <button className="link" onClick={() => nav({ to: 'settings', link })} data-testid={`lobby-change-${id}`}>{t('lobby.change')}</button>
  );
  const modelLink = (rowId: string) => (pv && pv.kind !== 'unset' && pv.kind !== 'anthropic' ? `general/${rowId}` : 'general/source');
  const mic = r.mic;
  const micName = mic?.off ? t('lobby.mic_off') : mic?.mic || (!mic || mic.probing ? '…' : t('ob.mic_none'));
  return (
    <div className="lb-status" data-testid="lobby-status">
      {/* two models: the quick one answers in the meeting, the stronger one edits the paper (Settings → Model source) */}
      {row(t('lobby.st.fast'),
        <span className="lb-trunc" title={r.modelFast ?? undefined}>{!pv ? '…' : pv.kind === 'unset' ? t('ob.unset') : r.modelFast ?? (pv.label || '…')}</span>,
        model,
        change(modelLink('fast_model'), 'fast'))}
      {row(t('lobby.st.main'),
        <span className="lb-trunc" title={r.model ?? undefined}>{!pv ? '…' : pv.kind === 'unset' ? t('ob.unset') : r.model ?? (pv.label || '…')}</span>,
        model,
        change(modelLink('main_model'), 'main'))}
      {/* coming down: not red (nothing to fix), as its line beside 开始会议 */}
      {row(t('lobby.st.asr'),
        <span className="lb-trunc">{r.asr === 'ok' ? r.asrName : r.asr === 'downloading' ? t('ob.downloading') : r.asr === 'missing' ? t('ob.not_downloaded') : '…'}</span>,
        r.asr === 'ok' ? 'ok' : r.asr === 'missing' ? 'block' : 'wait',
        change('speech/models', 'asr'))}
      {/* the one picked in Settings → Speech → Microphone (the system's default: its name); picked but not plugged in: amber */}
      {row(t('lobby.st.mic'),
        <span className="lb-trunc" title={mic?.missing && mic.mic ? t('lobby.mic_missing', { a: mic.mic }) : mic?.mic || undefined}>{micName}</span>,
        mic?.mic ? (mic.missing ? 'warn' : 'ok') : !mic || mic.probing ? 'wait' : 'warn',
        change('speech/mic', 'mic'))}
      {/* 试听 beside 更换: both always there (试听 hidden while there's no voice), so the cell keeps its width */}
      {row(t('lobby.st.voice'),
        <span className="lb-trunc">{say ?? voice}</span>,
        r.voiceEngine === undefined ? 'wait' : 'ok',
        <span className="lb-st-two">
          <button className="link" onClick={() => void test()} disabled={!r.voiceEngine} style={r.voiceEngine ? undefined : { visibility: 'hidden' }}
            data-testid="lobby-voice-test">{t('lobby.voice_test')}</button>
          {change('speech/voice', 'voice')}
        </span>)}
    </div>
  );
}

/** /api/update 回的错误代码。别的是安装日志的最后一行（多半是英文原句）：界面上写"未知"，原句放在悬停里 */
const UPDATE_ERRORS = ['download', 'launch', 'timeout', 'failed', 'live', 'processing', 'none'];
const updateError = (e: string | null | undefined) => {
  const c = e || 'failed';
  return t('lobby.update.failed', { a: t(`lobby.update.err.${UPDATE_ERRORS.includes(c) ? c : 'failed'}`) });
};
/** the raw reason, for the hover: only when it isn't one of the codes above */
const updateRaw = (e: string | null | undefined) => (e && !UPDATE_ERRORS.includes(e) ? e : undefined);

/** 新版本（zuhui/updates.py）：在"准备情况"这一行、标题和"设置"中间。这一格不管有没有字都占住这一行剩下的宽度，
 *  旁边的东西不动。有新版：新版本 0.6.2 + 更新；点了：正在更新（Bloom 会关掉，装完自己打开）；失败：更新失败：原因 + 重试。
 *  从源码跑的（dev）、没有新版、开会中：什么都不显示 */
function UpdateLine({ active }: { active: boolean }) {
  const [st, setSt] = useState<UpdateStatus | null>(null);
  const [sending, setSending] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const saidTimer = useRef<number | undefined>(undefined);
  const updating = st?.state === 'updating';
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = () => meetingApi.update().then((x) => alive && setSt(x), () => {});
    void load();
    // 后端在后台线程里查 GitHub：第一次的结果几秒后才有，5 秒后再问一次；正在更新时问得勤一些
    const first = window.setTimeout(load, 5000);
    const h = window.setInterval(load, updating ? 5000 : 60_000);
    return () => {
      alive = false;
      clearTimeout(first);
      clearInterval(h);
    };
  }, [active, updating]);
  useEffect(() => () => window.clearTimeout(saidTimer.current), []);
  const go = async () => {
    setSending(true);
    setSaid(null);
    try {
      setSt(await meetingApi.startUpdate());
    } catch (e) {
      // 被拒了（刚开了会、会后整理没完）：在同一格里显示几秒
      setSaid(updateError(String((e as Error)?.message ?? e)));
      window.clearTimeout(saidTimer.current);
      saidTimer.current = window.setTimeout(() => setSaid(null), 4000);
    } finally {
      setSending(false);
    }
  };
  const ver = (v: string | null | undefined) => (v ?? '').replace(/^v(?=\d)/, '');
  const shown = !!st && st.busy !== 'live' && (st.state === 'available' || st.state === 'updating' || st.state === 'failed');
  // a capsule in the row's own height (2026-11-20, the user: "更新的那个提醒 好小啊"): the version in the accent colour and a
  // solid 更新; blue while it updates, red when it failed
  let body: ReactNode = null;
  let kind = '';
  if (shown && (updating || sending)) {
    kind = 'is-busy';
    body = (
      <>
        <span className="mw-spinner sm" aria-hidden="true" />
        <span className="lb-upd-t">{t('lobby.update.running')}</span>
      </>
    );
  } else if (shown) {
    const failed = st!.state === 'failed';
    const text = said ?? (failed ? updateError(st!.error) : t('lobby.update.new', { a: ver(st!.latest) }));
    const wait = st!.busy === 'processing';
    kind = failed || said ? 'is-err' : 'is-new';
    body = (
      <>
        <span className="lb-upd-t" title={(!said && failed && updateRaw(st!.error)) || text}>{text}</span>
        <button className="lb-upd-b" disabled={wait} title={wait ? t('lobby.update.err.processing') : undefined} onClick={() => void go()} data-testid="lobby-update-go">
          {t(failed ? 'lobby.update.retry' : 'lobby.update.go')}
        </button>
      </>
    );
  }
  return (
    <div className="lb-upd" aria-live="polite" data-testid="lobby-update">
      {body && <span className={`lb-upd-chip ${kind}`}>{body}</span>}
    </div>
  );
}

function InviteLine({ invite, onChange }: { invite: Invite | null; onChange: () => void }) {
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const head = (right?: ReactNode) => (
    <div className="lb-side-h">
      <h2 id="lb-inv-h" className="lb-kicker">{t('lobby.invite')}</h2>
      {right}
    </div>
  );
  if (!invite) return <>{head()}<div className="lb-empty">…</div></>;
  if (!invite.ok || !invite.url || invite.relay === false) {
    return (
      <>
        {head()}
        <div className="lb-empty lb-invite-off">
          <span>{t('lobby.invite_off')}</span>
          <button className="link" onClick={() => nav({ to: 'settings', link: 'general/relay_mode' })} data-testid="lobby-relay-setup">{t('lobby.fix.provider')}</button>
        </div>
      </>
    );
  }
  const arm = async (on: boolean) => {
    setBusy(true);
    try {
      await relayApi.setInvite(on);
      onChange();
    } catch {
      /* the next poll says where it stands */
    } finally {
      setBusy(false);
    }
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(invite.url!);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* the field is selectable: copy by hand */
    }
    if (invite.public && !invite.armed) void arm(true);   // 发了链接就是要别人进来
  };
  return (
    <>
      {head(invite.public && (
        <label className="lb-invite-on">
          <span>{t('lobby.allow_join')}</span>
          <button type="button" role="switch" aria-checked={!!invite.armed} aria-label={t('lobby.allow_join')} disabled={busy}
            className={`st-switch ${invite.armed ? 'on' : ''}`} onClick={() => void arm(!invite.armed)} data-testid="lobby-allow-join">
            <span />
          </button>
        </label>
      ))}
      <div className="lb-invite">
        <input className="lb-input lb-mono" readOnly value={invite.url} onFocus={(e) => e.currentTarget.select()} aria-label={t('lobby.invite')} />
        <button onClick={() => void copy()} data-testid="lobby-copy-invite">
          <Icon name={copied ? 'check' : 'copy'} size={14} />
          <span className="lb-copy-t">{t(copied ? 'lobby.copied' : 'lobby.copy')}</span>
        </button>
      </div>
    </>
  );
}
