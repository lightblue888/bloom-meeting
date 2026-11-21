// What the AI strip above the transcript says about the background (MeetingLayout's ClaudeNow): one short line, cut
// with "…" when it doesn't fit, and, pointed at, all of it in the window's own tip (Tooltip.tsx), one part a line:
// each revision under way (what, how many places, how long, the step it is on: the steps LiveEdit lists under its
// bubble), each lookup (what, how long, its last step), the counts. The card that said it at the transcript's foot is
// gone (2026-11-21, the user: "我只需要鼠标悬停在上面能展开完整状态即可").
import type { Board, Brain, Lookup } from '../meetingApi';
import { mt as t } from '../i18n';
import { stepK, stepV, tidyStep } from './stepText';

export type EditRow = NonNullable<Brain['edits']>[number];

/** the revisions being made right now: pushed with the state, or (an older backend) read off the board */
export const editsOf = (b: Brain, board: Board | null | undefined): EditRow[] =>
  b.edits ??
  (board?.tasks ?? [])
    .filter((x) => x.status === 'working')
    .map((w) => ({ id: w.id, idx: w.idx, title: w.title, started: w.progress?.started, edits: w.progress?.edits ?? 0, log: w.progress?.log, step: w.progress?.step }));

/** every lookup the background is doing (several can run at once), oldest first; an older backend sends one */
export const lookupsOf = (b: Brain | null | undefined): Lookup[] => b?.deeps ?? (b?.deep ? [b.deep] : []);

/** what is being looked up: Haiku's short name; before it has named it, the words as said, without the ums and
 *  fillers they start with */
const lookupWords = (d: Lookup) =>
  d.topic || d.q.replace(/^(?:[\s，,。.、！!？?]|嗯|呃|啊|哦|诶|欸|哎|那个|就是|然后|行|好|对|Claude)+/i, '') || d.q;

/** the same, kept short for the strip's one line */
export const lookupName = (d: Lookup) => {
  const q = lookupWords(d);
  return d.topic || q.length <= 24 ? q : q.slice(0, 24) + '…';
};

/** how long something has been going: seconds, then minutes and seconds ("7 分 5 秒", not "425 秒") */
export const elapsed = (s: number) =>
  s < 60 ? t('meeting.ai.secs', { s: String(s) }) : t('meeting.ai.mins', { m: String(Math.floor(s / 60)), s: String(s % 60) });

/** seconds from `t0` to the backend's `now`; no `t0`: `fallback` */
export const secsTo = (now: number, t0?: number | null, fallback = 0) => (t0 ? Math.max(0, Math.round(now - t0)) : fallback);

/** a step in its grey line's words ("在 D 节改第 87 处", "看目录", "联网搜索 TSGBench") */
const stepWords = (x: { k: string; v?: string } | null | undefined) => {
  const y = x ? tidyStep(x) : null;
  return y ? [stepK(y.k), stepV(y.v)].filter(Boolean).join(' ') : '';
};

/** the step a revision is on: the one the worker says it is doing, else the newest it logged */
export const editStep = (w: EditRow): string => {
  const last = w.steps?.[w.steps.length - 1];
  const logged = w.log?.[w.log.length - 1];
  return stepWords(w.step ? { k: w.step } : last ?? (logged ? { k: logged } : null));
};

/** a lookup's newest step */
const lookStep = (d: Lookup): string => stepWords(d.steps?.[d.steps.length - 1] ?? (d.step ? { k: d.step } : null));

/** what is left for after the meeting (标注): counted after the state, it doesn't make the background busy */
export const notesOf = (board: Board | null | undefined) =>
  board?.counts?.notes ?? (board?.tasks ?? []).filter((x) => x.note && x.status === 'done').length;

const queuedOf = (brain: Brain, board: Board | null | undefined) => brain.queued ?? board?.counts?.queued ?? 0;

/** the strip's one line, in parts: "查找中 2 项：问题定义写法、FinTS 次数", "修改中：T3 · 已改 1 处 · 36 秒", "1 条排队中" */
export function backLine(brain: Brain, board: Board | null | undefined, now: number): string[] {
  const back: string[] = [];
  const looks = lookupsOf(brain);
  const dp = looks[looks.length - 1];
  if (looks.length === 1) {
    back.push(
      (dp.state === 'done' ? t('meeting.ai.deep_ready', { q: lookupName(dp) }) : t('meeting.ai.deep_now', { q: lookupName(dp) })) +
        ' · ' +
        t('meeting.ai.secs', { s: String(secsTo(now, dp.t0, dp.secs)) }),
    );
  } else if (looks.length > 1) {
    const names = looks.slice(-2).map(lookupName).join('、') + (looks.length > 2 ? t('meeting.ai.and_more') : '');
    back.push(t('meeting.ai.deeps', { n: String(looks.length), names }));
  }
  for (const w of editsOf(brain, board)) {
    const bits = [t('meeting.ai.working', { title: w.title })];
    if (w.edits) bits.push(t('meeting.ai.edits', { n: String(w.edits) }));
    if (w.started) bits.push(elapsed(secsTo(now, w.started)));
    back.push(bits.join(' · '));
  }
  // revisions held back until their document (a new PDF, plain text first) is recognized in full: said before the
  // plain queue count, it's why they haven't started
  if (brain.waiting) back.push(t('meeting.ai.waiting', { n: String(brain.waiting) }));
  const queued = queuedOf(brain, board);
  if (queued) back.push(t('meeting.ai.queued', { n: String(queued) }));
  if (brain.unspoken) back.push(t('meeting.ai.unspoken', { n: String(brain.unspoken) }));
  return back;
}

/** pointed at: the whole of it, one part a line, in the strip's order. Each lookup and each revision whole, with the
 *  step it is on; then the counts. Nothing going on in the background: no tip (the strip says all there is) */
export function backTip(brain: Brain, board: Board | null | undefined, now: number): string | undefined {
  const out: string[] = [];
  for (const d of lookupsOf(brain)) {
    const done = d.state === 'done';
    const q = lookupWords(d);
    out.push(
      [done ? t('meeting.ai.deep_ready', { q }) : t('meeting.ai.deep_now', { q }), t('meeting.ai.secs', { s: String(secsTo(now, d.t0, d.secs)) }), done ? '' : lookStep(d)]
        .filter(Boolean)
        .join(' · '),
    );
  }
  for (const w of editsOf(brain, board)) {
    out.push(
      [t('meeting.ai.working', { title: w.title }), w.edits ? t('meeting.ai.edits', { n: String(w.edits) }) : '', w.started ? elapsed(secsTo(now, w.started)) : '', editStep(w)]
        .filter(Boolean)
        .join(' · '),
    );
  }
  if (brain.waiting) out.push(t('meeting.ai.waiting', { n: String(brain.waiting) }));
  const queued = queuedOf(brain, board);
  if (queued) out.push(t('meeting.ai.queued', { n: String(queued) }));
  if (brain.unspoken) out.push(t('meeting.ai.unspoken', { n: String(brain.unspoken) }));
  if (!out.length) return undefined;
  const notes = notesOf(board);
  if (notes) out.push(t('meeting.ai.notes', { n: String(notes) }));
  return out.join('\n');
}
