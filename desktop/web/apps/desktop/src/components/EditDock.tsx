// The revision under way, at the foot of the transcript, under the newest line (2026-11-21, the user at 40:09: "奇怪，
// 为什么我看不到他的后台状态呢？"). Its steps hang under the bubble that asked for it (MeetingLayout's LiveEdit), and a
// big one runs for minutes while the talk goes on: by then that bubble was far up the column, and its 41 steps showed
// up all at once when it reported. Here: what is being revised, how many places so far, the step it is on, how long;
// the queue in a word.
//
// Its slot is there all meeting long, one height whatever is in it (two lines, one line or nothing), so nothing in the
// column moves when a revision starts or ends. It is the lines' last row, not laid over them.
import { useEffect, useState } from 'react';
import type { Board, Brain } from '../meetingApi';
import { mt as t } from '../i18n';
import { stepK, tidyStep } from './stepText';
import './edit-dock.css';

export type EditRow = NonNullable<Brain['edits']>[number];

/** the revisions being made right now: pushed with the state, or (an older backend) read off the board */
export const editsOf = (b: Brain, board: Board | null | undefined): EditRow[] =>
  b.edits ??
  (board?.tasks ?? [])
    .filter((x) => x.status === 'working')
    .map((w) => ({ id: w.id, idx: w.idx, title: w.title, started: w.progress?.started, edits: w.progress?.edits ?? 0, log: w.progress?.log, step: w.progress?.step }));

/** the step it is on, in the step list's own words ("在 D 节改第 87 处", "查找“唯一变量”"): the one the worker says it is
 *  doing, else the newest it logged */
const stepNow = (w: EditRow): string => {
  const k = w.step || w.steps?.[w.steps.length - 1]?.k || w.log?.[w.log.length - 1];
  const x = k ? tidyStep({ k }) : null;
  return x ? stepK(x.k) : '';
};

/** how long something has been going: seconds, then minutes and seconds ("7 分 5 秒", not "425 秒") */
export const elapsed = (s: number) =>
  s < 60 ? t('meeting.ai.secs', { s: String(s) }) : t('meeting.ai.mins', { m: String(Math.floor(s / 60)), s: String(s % 60) });

export interface DockText {
  /** "修改中：<title>"; nothing being revised: "下一条：<title>" (null: the board isn't known, only the count) */
  title: string | null;
  /** "已改 41 处 · 7 分 5 秒 · 在 F 节改第 41 处" */
  now: string;
  /** "1 条排队中", "2 条等文档识别完 · 1 条排队中" */
  queue: string;
  /** nothing being revised: only waiting their turn */
  wait?: boolean;
}

/** what the dock says, at the backend's time `now` (seconds); null: nothing being revised or waiting. Between two
 *  revisions (one done, the next not started) it keeps its two lines and the count keeps its place: only words change */
export function dockText(brain: Brain, board: Board | null | undefined, now: number): DockText | null {
  const edits = editsOf(brain, board);
  const w = edits[0];
  const queued = brain.queued ?? board?.counts?.queued ?? 0;
  const queue = [
    edits.length > 1 ? t('meeting.ai.dock_more', { n: String(edits.length - 1) }) : '',
    brain.waiting ? t('meeting.ai.waiting', { n: String(brain.waiting) }) : '',
    // the same words with or without one being made: the count keeps its width when one finishes
    queued ? t('meeting.ai.dock_queued', { n: String(queued) }) : '',
  ]
    .filter(Boolean)
    .join(' · ');
  if (!w) {
    if (!queue) return null;
    const next = (board?.tasks ?? []).filter((x) => x.status === 'queued').sort((a, b) => a.idx - b.idx)[0];
    return { title: next ? t('meeting.ai.dock_next', { title: next.title }) : null, now: '', queue, wait: true };
  }
  const step = stepNow(w);
  const secs = w.started ? Math.max(0, Math.round(now - w.started)) : null;
  return {
    title: t('meeting.ai.dock_working', { title: w.title }),
    now: [w.edits ? t('meeting.ai.edits', { n: String(w.edits) }) : '', secs != null ? elapsed(secs) : '', step].filter(Boolean).join(' · '),
    queue,
  };
}

/** re-render once a second: the page keeps the time itself (the backend only pushes when something changes) */
function useTick(ms = 1000) {
  const [, set] = useState(0);
  useEffect(() => {
    const h = setInterval(() => set((x) => x + 1), ms);
    return () => clearInterval(h);
  }, [ms]);
}

/** `brain` null: the meeting has ended (the page still shows it): the slot stays, empty, so the lines don't drop */
export function EditDock({ brain, board }: { brain: Brain | null; board: Board | null | undefined }) {
  useTick(1000);
  const d = brain ? dockText(brain, board, Date.now() / 1000 + (brain.skew ?? 0)) : null;
  return (
    <div className={`mt-dock${d ? ' is-on' : ''}`} data-testid="edit-dock">
      {d?.title ? (
        <div className={`mt-dock-card${d.wait ? ' is-wait' : ''}`} data-testid="edit-dock-card">
          <span className={`mt-now-dot ${d.wait ? 'is-queue' : 'is-busy'}`} aria-hidden="true" />
          <span className="mt-dock-t" data-tip={d.title} data-tip-trunc="">{d.title}</span>
          <span className="mt-dock-v" data-tip={d.now || undefined} data-tip-trunc="">{d.now}</span>
          {d.queue && <span className="mt-dock-q" data-tip={d.queue} data-tip-trunc="">{d.queue}</span>}
        </div>
      ) : d ? (
        <div className="mt-dock-card is-queue" data-testid="edit-dock-card">
          <span className="mt-now-dot is-queue" aria-hidden="true" />
          <span className="mt-dock-t" data-tip={d.queue} data-tip-trunc="">{d.queue}</span>
        </div>
      ) : null}
    </div>
  );
}
