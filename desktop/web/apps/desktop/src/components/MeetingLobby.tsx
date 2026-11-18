// 会议 before a meeting: the lobby. Left, the next meeting — its name, the paper on the desk (its size, when it was
// changed, its first headings, so it's clearly the right one and the right version), and 开始会议, which stays off
// until what a meeting needs is there (a model source, the transcription model, a paper) and says what's missing with
// the fix right beside it. Right, where everything stands (model, transcription, microphone, voice), the invite link,
// and the recent meetings (a click opens one in 回顾). The meeting page itself (MeetingLayout) comes up once the
// meeting starts; the lobby never shows the last meeting's transcript.
import { useEffect, useMemo, useState, type DragEvent, type ReactNode } from 'react';
import { Icon } from '@blurt/icons';
import { i18n, t } from '../i18n';
import { avatarUrl, cleanTitle, fmtDuration, fmtWhen, meetingApi, type Current, type Invite, type MeetingBrief } from '../meetingApi';
import { nav } from '../nav';
import { setupApi } from '../setupApi';
import { Flower, HOST_COLOR, nameColor } from './Flower';
import { BloomMark } from './MeetingLayout';
import { useReadiness, type Readiness } from './readiness';
import './setup.css';

interface DropProps {
  onDragOver: (e: DragEvent) => void;
  onDragLeave: (e: DragEvent) => void;
  onDrop: (e: DragEvent) => void;
}

export function MeetingLobby({ cur, paperName, busy, importText, notice, title, onTitle, onStart, onPickPaper, dropProps, over, active }: {
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
  dropProps: DropProps;
  over: boolean;
  active: boolean;
}) {
  const r = useReadiness(active);
  const paper = cur?.paper?.exists ? cur.paper : null;
  const hasPaper = !!paper || !!paperName;
  const name = paperName ?? paper?.name ?? '';
  const needs: ('provider' | 'asr' | 'paper')[] = [...r.blockers, ...(hasPaper ? [] : ['paper' as const])];
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
  const [invite, setInvite] = useState<Invite | null>(null);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = () => meetingApi.invite().then((x) => alive && setInvite(x), () => alive && setInvite({ ok: false }));
    void load();
    const h = window.setInterval(load, 30_000);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [active]);

  return (
    <div className={`lb ${over ? 'is-over' : ''}`} {...dropProps} data-testid="lobby">
      <div className="lb-inner">
        <Greeting r={r} />
        <div className="lb-grid">
          <section className="lb-card lb-new" aria-labelledby="lb-new-h">
            <h2 id="lb-new-h" className="lb-card-h">{t('lobby.new')}</h2>
            {notice && <div className="lb-notice">{notice}</div>}
            <label className="lb-field">
              <span className="lb-label">{t('lobby.title')}</span>
              <input
                className="lb-input"
                value={title}
                maxLength={60}
                spellCheck={false}
                placeholder={cleanTitle(name) || t('lobby.title_ph')}
                onChange={(e) => onTitle(e.target.value)}
                data-testid="lobby-title"
              />
            </label>
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
              ) : hasPaper ? (
                <div className="lb-paper" data-testid="lobby-paper">
                  <Icon name="document" size={22} />
                  <div className="lb-paper-text">
                    <div className="lb-paper-name" title={name}>{name}</div>
                    <div className="lb-paper-meta">
                      {[
                        paper?.words ? t('lobby.words', { a: i18n().formatNumber(paper.words, { notation: 'compact', maximumFractionDigits: 1 }) }) : null,
                        paper?.modified ? t('lobby.changed', { a: fmtWhen(paper.modified.replace(' ', 'T'), lang) }) : null,
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                    </div>
                    {!!paper?.outline?.length && (
                      <ol className="lb-outline">
                        {paper.outline.slice(0, 6).map((h, i) => (
                          <li key={i} title={h}>{h}</li>
                        ))}
                      </ol>
                    )}
                  </div>
                  <button className="lb-paper-change" onClick={onPickPaper} data-testid="lobby-change-paper">{t('lobby.change')}</button>
                </div>
              ) : (
                <button className="lb-drop" onClick={onPickPaper} data-testid="lobby-pick-paper">
                  <Icon name="document" size={28} />
                  <span className="lb-drop-t">{t('lobby.drop')}</span>
                  <span className="lb-drop-s">{t('lobby.formats')}</span>
                </button>
              )}
            </div>
            <div className="lb-start-row">
              <button className="lb-start" disabled={!canStart} onClick={onStart} data-testid="lobby-start">
                {busy === 'start' ? <span className="mw-spinner" aria-hidden="true" /> : <BloomMark size={22} />}
                <span>{t('lobby.start')}</span>
              </button>
              <div className="lb-needs" aria-live="polite">
                {needs.map((n) => (
                  <Need key={n} n={n} r={r} onPickPaper={onPickPaper} />
                ))}
                {!needs.length && (
                  <span className="lb-need is-ok">
                    <Icon name="check" size={13} />
                    <span>{t('lobby.all_set')}</span>
                  </span>
                )}
              </div>
            </div>
          </section>

          <div className="lb-side">
            <section className="lb-card" aria-labelledby="lb-ready-h">
              <div className="lb-card-hrow">
                <h2 id="lb-ready-h" className="lb-card-h">{t('lobby.ready')}</h2>
                <button className="link" onClick={() => nav({ to: 'settings' })}>{t('lobby.settings')}</button>
              </div>
              <Status r={r} />
            </section>
            <section className="lb-card" aria-labelledby="lb-inv-h">
              <h2 id="lb-inv-h" className="lb-card-h">{t('lobby.invite')}</h2>
              <InviteLine invite={invite} />
            </section>
          </div>
        </div>

        <section className="lb-card lb-recent" aria-labelledby="lb-recent-h">
          <div className="lb-card-hrow">
            <h2 id="lb-recent-h" className="lb-card-h">{t('lobby.recent')}</h2>
            {!!recent?.length && <button className="link" onClick={() => nav({ to: 'history' })}>{t('lobby.all')}</button>}
          </div>
          {recent === null ? (
            <div className="lb-empty">…</div>
          ) : !recent.length ? (
            <div className="lb-empty">{t('lobby.none')}</div>
          ) : (
            <ul className="lb-list">
              {recent.slice(0, 5).map((m) => (
                <li key={m.id}>
                  <button className="lb-meeting" onClick={() => nav({ to: 'history', meeting: m.id })} data-testid="lobby-recent">
                    <span className="lb-meeting-t" title={cleanTitle(m.title)}>{cleanTitle(m.title) || t('lobby.untitled')}</span>
                    <span className="lb-meeting-m">{fmtWhen(m.started, lang)}</span>
                    <span className="lb-meeting-m lb-num">{m.duration_s ? fmtDuration(m.duration_s) : ''}</span>
                    <span className="lb-meeting-m lb-num">{m.n_tasks ? t('lobby.edits', { a: m.n_done, b: m.n_tasks }) : ''}</span>
                    <span className="lb-meeting-tag">{m.processing ? t('lobby.processing') : m.has_summary ? t('lobby.summary') : ''}</span>
                    <Icon name="chevron-right" size={14} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      {over && (
        <div className="lb-veil" aria-hidden="true">
          <Icon name="document" size={28} />
          {t(hasPaper ? 'meeting.paper.drop_new' : 'lobby.drop_here')}
        </div>
      )}
    </div>
  );
}

function Greeting({ r }: { r: Readiness }) {
  const p = r.profile;
  const h = new Date().getHours();
  const part = h < 5 ? 'evening' : h < 11 ? 'morning' : h < 13 ? 'noon' : h < 18 ? 'afternoon' : 'evening';
  const date = useMemo(() => new Date().toLocaleDateString(i18n().locale, { month: 'long', day: 'numeric', weekday: 'long' }), []);
  return (
    <header className="lb-head">
      <span className="lb-avatar">{p?.avatar ? <img src={avatarUrl(p.avatar)} alt="" draggable={false} /> : <Flower color={p?.color || HOST_COLOR} />}</span>
      <div className="lb-hello">
        <h1>
          {t(`lobby.hello.${part}`)}
          {p?.name && <>{t('lobby.hello_sep')}<span className="lb-name" style={{ color: nameColor(p.color) }}>{p.name}</span></>}
        </h1>
        <div className="lb-date">{date}</div>
      </div>
    </header>
  );
}

/** what keeps 开始会议 off, each with its fix */
function Need({ n, r, onPickPaper }: { n: 'provider' | 'asr' | 'paper'; r: Readiness; onPickPaper: () => void }) {
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
      {n === 'paper' && <button className="link" onClick={onPickPaper}>{t('lobby.fix.paper')}</button>}
    </span>
  );
}

function Status({ r }: { r: Readiness }) {
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
  const row = (icon: 'cpu' | 'waveform' | 'mic' | 'character-bubble', label: string, value: ReactNode, state: 'ok' | 'warn' | 'block' | 'wait', act?: ReactNode) => (
    <div className={`lb-st is-${state}`}>
      <span className="lb-st-dot" aria-hidden="true" />
      <Icon name={icon} size={15} />
      <span className="lb-st-l">{label}</span>
      <span className="lb-st-v">{value}</span>
      <span className="lb-st-a">{act}</span>
    </div>
  );
  const voice = r.voiceEngine === undefined ? '…' : r.voiceEngine === null ? t('ob.voice_off') : t(`ob.voice.${r.voiceEngine}`);
  return (
    <div className="lb-status" data-testid="lobby-status">
      {row('cpu', t('lobby.st.model'),
        <span className="lb-trunc" title={pv?.main || ''}>{!pv ? '…' : pv.kind === 'unset' ? t('ob.unset') : pv.label}</span>,
        !pv ? 'wait' : pv.kind === 'unset' ? 'block' : 'ok',
        <button className="link" onClick={() => nav({ to: 'settings', link: 'general/source' })}>{t('lobby.change')}</button>)}
      {row('waveform', t('lobby.st.asr'),
        <span className="lb-trunc">{r.asr === 'ok' ? r.asrName : r.asr === 'downloading' ? t('ob.downloading') : r.asr === 'missing' ? t('ob.not_downloaded') : '…'}</span>,
        r.asr === 'ok' ? 'ok' : r.asr === 'unknown' ? 'wait' : 'block',
        <button className="link" onClick={() => nav({ to: 'settings', link: 'speech' })}>{t('lobby.change')}</button>)}
      {row('mic', t('lobby.st.mic'),
        <span className="lb-trunc" title={r.mic?.mic || ''}>{r.mic?.mic || (!r.mic || r.mic.probing ? '…' : t('ob.mic_none'))}</span>,
        r.mic?.mic ? 'ok' : !r.mic || r.mic.probing ? 'wait' : 'warn')}
      {row('character-bubble', t('lobby.st.voice'),
        <span className="lb-trunc">{say ?? voice}</span>,
        r.voiceEngine === undefined ? 'wait' : 'ok',
        r.voiceEngine ? <button className="link" onClick={() => void test()}>{t('lobby.voice_test')}</button> : <button className="link" onClick={() => nav({ to: 'settings', link: 'speech' })}>{t('lobby.change')}</button>)}
    </div>
  );
}

function InviteLine({ invite }: { invite: Invite | null }) {
  const [copied, setCopied] = useState(false);
  if (!invite) return <div className="lb-empty">…</div>;
  if (!invite.ok || !invite.url || invite.relay === false) return <div className="lb-empty">{t('lobby.invite_off')}</div>;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(invite.url!);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* the field is selectable: copy by hand */
    }
  };
  return (
    <div className="lb-invite">
      <input className="lb-input lb-mono" readOnly value={invite.url} onFocus={(e) => e.currentTarget.select()} aria-label={t('lobby.invite')} />
      <button onClick={() => void copy()} data-testid="lobby-copy-invite">
        <Icon name={copied ? 'check' : 'copy'} size={14} />
        <span className="lb-copy-t">{t(copied ? 'lobby.copied' : 'lobby.copy')}</span>
      </button>
    </div>
  );
}
