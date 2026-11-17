// Dev (the rail, under 回顾): the simulated meetings Bloom is tested with (scratch_ab/sim/sim_meeting.py), newest first,
// grouped by round. Read-only: the backend reads each run's own folder (/api/dev/runs), never the real meeting library.
// Laid out like 回顾 — the list on the left, the chosen run on the right (Claude's review of it, the transcript) — and
// 打开 shows the run on the meeting page's own layout: live while it runs (the transcript follows, the status strip on
// top), the review's tabs after (修订对比 from the paper's master copy, the review in 纪要).
import { memo, useCallback, useEffect, useMemo, useState } from 'react';
import { Icon } from '@blurt/icons';
import { i18n, t, setMeetingLang } from '../i18n';
import { fmtDuration, fmtWhen, meetingApi, type Brain, type DevDetail, type DevRun, type Participant } from '../meetingApi';
import { MeetingLayout, Transcript } from './MeetingLayout';
import { keep } from './useDismiss';

const sig = (r: DevRun | undefined) => (r ? [r.n_lines, r.state, r.processing, r.has_summary, r.match, r.duration_s].join('|') : '');
const NOBODY: Participant[] = [];

/** a run's group: "<paper> 第 N 轮", or the paper alone for runs without a round */
const groupOf = (r: DevRun) => (r.round ? t('meeting.dev.round', { p: r.paper, n: String(r.round) }) : t('meeting.dev.no_round', { p: r.paper }));

/** a run's title: the backend writes the scenario in Chinese ("乱测 · 小周"); other languages get the scenario from its
 * code, the role-played names stay as they are */
const titleOf = (r: DevRun) => {
  if (i18n().lang === 'zh-Hans' || !r.scenario) return r.title;
  const who = r.title.includes(' · ') ? r.title.slice(r.title.indexOf(' · ')) : '';
  return t(`meeting.dev.scen.${r.scenario}`) + who;
};

/** the opened run's title ("GIFT-Eval · 乱测 · 小周"): the same scenario words, found by their Chinese name */
const SCEN_CODE: Record<string, string> = { 两人会: 'two', 三人会: 'three', 导师在场: 'teacher', 乱测: 'monkey' };
const meetingTitle = (title: string | undefined) =>
  !title || i18n().lang === 'zh-Hans' ? title : title.split(' · ').map((p) => (SCEN_CODE[p] ? t(`meeting.dev.scen.${SCEN_CODE[p]}`) : p)).join(' · ');

export function DevMeetings({ active = true }: { active?: boolean }) {
  const [list, setListRaw] = useState<DevRun[] | null>(null);
  const setList = useCallback((l: DevRun[]) => setListRaw((prev) => keep(prev, l)), []);
  const [sel, setSel] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [details, setDetailsRaw] = useState<Record<string, DevDetail>>({});
  const setDetails = useCallback((f: (x: Record<string, DevDetail>) => Record<string, DevDetail>) => setDetailsRaw((x) => keep(x, f(x))), []);
  const lang = i18n().lang;

  // the list: every 3 s while a run is going, else every 10 s (only while this page is on screen)
  const anyLive = (list ?? []).some((r) => r.state === 'live' || r.processing);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = () =>
      meetingApi.devRuns().then(
        (l) => alive && setList(Array.isArray(l) ? l : []),
        () => alive && setListRaw((x) => x ?? []),
      );
    void load();
    const h = setInterval(load, anyLive ? 3000 : 10_000);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [active, anyLive, setList]);

  const refresh = useCallback(async (id: string) => {
    try {
      const d = await meetingApi.devRun(id);
      if (d.brain && typeof d.brain.now === 'number') d.brain = { ...d.brain, skew: d.brain.now - Date.now() / 1000 } as Brain;
      setDetails((x) => ({ ...x, [id]: d }));
      return d;
    } catch {
      return null;
    }
  }, [setDetails]);

  const rows = list ?? [];
  const picked = sel && rows.some((r) => r.id === sel) ? sel : (rows[0]?.id ?? null);
  const pickedRun = rows.find((r) => r.id === picked);
  const pickedSig = sig(pickedRun);
  useEffect(() => {
    if (picked && active) void refresh(picked);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picked, pickedSig, active]);
  // the run on screen while it is going: its transcript and status every 1.5 s
  const watch = open ?? picked;
  const watchLive = !!watch && (rows.find((r) => r.id === watch)?.state === 'live');
  useEffect(() => {
    if (!watch || !watchLive || !active) return;
    const h = setInterval(() => void refresh(watch), 1500);
    return () => clearInterval(h);
  }, [watch, watchLive, active, refresh]);

  const groups = useMemo(() => {
    const out: { key: string; runs: DevRun[] }[] = [];
    for (const r of rows) {
      const k = groupOf(r);
      const g = out.find((x) => x.key === k);
      if (g) g.runs.push(r);
      else out.push({ key: k, runs: [r] });
    }
    return out;
  }, [rows]);

  const d = picked ? details[picked] : undefined;
  const people = d?.participants ?? NOBODY;
  const pickedLive = pickedRun?.state === 'live';

  const opened = open && details[open];
  // the preview's transcript speaks the run's language (an English simulated meeting: English cards and steps); the list
  // around it stays in the app's language. An opened run's meeting page sets it itself
  if (active && !opened) setMeetingLang(d?.meeting?.lang ?? null);
  // the run picked in the list, after a short pause: its meeting page (and the paper) is mounted hidden and loads behind
  // the preview, so 打开 shows it at once (the paper's formulas can take seconds to typeset)
  const [warm, setWarm] = useState<string | null>(null);
  useEffect(() => {
    if (!picked || !details[picked] || !active) return;
    const h = setTimeout(() => setWarm(picked), 600);
    return () => clearTimeout(h);
  }, [picked, !!(picked && details[picked]), active]);
  const layerId = open ?? warm;
  return (
    <div className="mt-hstack">
      <div className={`mt-hlayer ${opened ? 'is-off' : ''}`} aria-hidden={!!opened || undefined}>
        <div className="mt-root mt-history mt-dev" data-testid="dev-meetings">
          <header className="mt-head">
            <div className="mt-head-main">
              <h1 className="mt-title">{t('meeting.dev.title')}</h1>
              <div className="mt-meta">
                {list && <span className="mt-meta-item">{t('meeting.history.count', { n: i18n().formatNumber(list.length) })}</span>}
              </div>
            </div>
          </header>
          <div className="mt-history-body">
            {!list ? (
              <div className="mt-empty">
                <span className="mw-spinner" aria-hidden="true" />
              </div>
            ) : rows.length === 0 ? (
              <div className="mt-empty">
                <Icon name="code" size={28} />
                <p>{t('meeting.dev.empty')}</p>
              </div>
            ) : (
              <div className="mt-hsplit">
                <ul className="mt-hlist" role="listbox" aria-label={t('meeting.dev.title')}>
                  {groups.map((g) => (
                    <li key={g.key} className="mt-hgroup">
                      <div className="mt-hgroup-h">
                        <span>{g.key}</span>
                        {g.runs[0]?.note && !/^第\s*\d+\s*轮$/.test(g.runs[0].note) && g.runs.every((r) => r.note === g.runs[0].note) ? (
                          <span className="mt-hgroup-note">{g.runs[0].note.replace(/^.*?第\s*\d+\s*轮[：:]\s*/, '')}</span>
                        ) : null}
                      </div>
                      <ul>
                        {g.runs.map((r) => (
                          <li key={r.id} className="mt-hitem">
                            <button
                              className={`mt-hrow ${r.id === picked ? 'is-sel' : ''}`}
                              role="option"
                              aria-selected={r.id === picked}
                              onClick={() => setSel(r.id)}
                              onDoubleClick={() => void refresh(r.id).then((x) => x && setOpen(r.id))}
                              data-testid={`dev-${r.id}`}
                            >
                              <span className={`mt-state-dot is-${r.state ?? 'ended'}`} aria-hidden="true" />
                              <span className="mt-hrow-main">
                                <span className="mt-hrow-title">{titleOf(r)}</span>
                                <span className="mt-hrow-sub">
                                  <span>{fmtWhen(r.started, lang)}</span>
                                  <span>{fmtDuration(r.duration_s)}</span>
                                  <RunState r={r} />
                                </span>
                              </span>
                            </button>
                          </li>
                        ))}
                      </ul>
                    </li>
                  ))}
                </ul>
                <section className="mt-hpreview mt-dev-pv" aria-live="polite" data-testid="dev-preview">
                  {pickedRun && (
                    <>
                      <div className="mt-hp-head">
                        <div className="mt-hp-titles">
                          <h2 className="mt-hp-title">{`${pickedRun.paper} · ${titleOf(pickedRun)}`}</h2>
                          <div className="mt-hrow-sub">
                            <span>{fmtWhen(pickedRun.started, lang)}</span>
                            <span>{fmtDuration(pickedRun.duration_s)}</span>
                            <RunState r={pickedRun} />
                            {pickedRun.effort ? <span>{t('meeting.dev.effort', { e: pickedRun.effort })}</span> : null}
                          </div>
                        </div>
                        <div className="mt-hp-acts">
                          <button className="mt-btn is-primary" onClick={() => void refresh(pickedRun.id).then((x) => x && setOpen(pickedRun.id))} data-testid="dev-open">
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
                        <>
                        <div className="mt-hp-body mt-dev-review">
                          <h3 className="mt-hp-h">{t('meeting.dev.review')}</h3>
                          {d.summary?.abstract ? (
                            <>
                              <p className="mt-hp-abstract">{d.summary.abstract}</p>
                              {(d.summary.conclusions?.length ?? 0) > 0 && (
                                <ul className="mt-hp-points">
                                  {d.summary.conclusions.slice(0, 5).map((c, i) => (
                                    <li key={i}>{c}</li>
                                  ))}
                                </ul>
                              )}
                            </>
                          ) : (
                            <p className="mt-hp-none">
                              {pickedRun.state === 'live' ? t('meeting.dev.review_live') : pickedRun.processing ? t('meeting.dev.review_running') : t('meeting.dev.review_none')}
                            </p>
                          )}
                        </div>
                        {/* the meeting page's own transcript: bubbles, 随口 / 思考 heads, the steps under them; the status strip while it runs */}
                        <Transcript
                          bare
                          uiLang={i18n().lang}
                          lines={d.transcript}
                          partial={pickedLive ? d.partial ?? null : null}
                          people={people}
                          follow={pickedLive}
                          brain={pickedLive ? d.brain ?? null : null}
                          board={pickedLive ? d.board : null}
                        />
                        </>
                      )}
                    </>
                  )}
                </section>
              </div>
            )}
          </div>
        </div>
      </div>
      {layerId && details[layerId] && (
        <div className={`mt-hlayer ${opened ? '' : 'is-off'}`} aria-hidden={!opened || undefined}>
          <OpenRun key={layerId} d={details[layerId]} active={active && !!opened} onBack={() => setOpen(null)} uiLang={i18n().lang} />
        </div>
      )}
    </div>
  );
}

/** a run's state after its time and length: 在开 / 评审中 / 中断 / 标签对上 a/b */
function RunState({ r }: { r: DevRun }) {
  if (r.state === 'live') return <span className="mt-dev-tag is-live">{t('meeting.dev.live')}</span>;
  if (r.processing) return <span className="mt-dev-tag">{t('meeting.dev.wrapping')}</span>;
  if (r.stopped) return <span className="mt-dev-tag is-stopped">{t('meeting.dev.stopped')}</span>;
  if (r.total) return <span className="mt-dev-tag">{t('meeting.dev.match', { a: String(r.match ?? 0), b: String(r.total) })}</span>;
  return null;
}

/** one run on the meeting page's layout: live while it runs, the review's tabs after */
const OpenRun = memo(function OpenRun({ d, active, onBack, uiLang }: { d: DevDetail; active: boolean; onBack: () => void; uiLang: string }) {
  const live = d.meeting?.state === 'live';
  const actions = useMemo(
    () => (
      <button className="mt-btn" onClick={onBack} data-testid="dev-back">
        <Icon name="arrow-left" size={13} />
        {t('meeting.history.back')}
      </button>
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [onBack, uiLang],
  );
  // the diff page again when a revision lands (the done count is in its address)
  const done = d.board?.tasks?.filter((x) => x.status === 'done').length ?? 0;
  const diffSrc = meetingApi.diffUrl(d.diff_url ? `${d.diff_url}?v=${done}` : null);
  const meeting = useMemo(() => (d.meeting ? { ...d.meeting, title: meetingTitle(d.meeting.title) ?? d.meeting.title } : d.meeting), [d.meeting]);
  return (
    <MeetingLayout
      review={!live}
      active={active}
      meeting={meeting}
      transcript={d.transcript}
      partial={live ? d.partial ?? null : null}
      people={d.participants ?? NOBODY}
      board={d.board}
      brain={live ? d.brain ?? null : null}
      summary={d.summary}
      diffSrc={diffSrc}
      video={null}
      live={live}
      actions={actions}
    />
  );
});
