// A Bloom meeting's post-processing as the island draws it (IslandState.postMeeting): one bar over the steps
// (save → transcript → summary), the step under way, how it ended. Pure: the tests run it without a page.
import type { PostMeeting, PostStep } from './host';

export type PostPhase = 'running' | 'done' | 'failed';

const clamp01 = (x: number) => Math.min(1, Math.max(0, x));

/** How it stands: failed once a step failed (or an error came), done once the backend says so (or every step is). */
export function postPhase(pm: PostMeeting): PostPhase {
  if (pm.steps.some((s) => s.state === 'failed') || (!!pm.error && !pm.done)) return 'failed';
  if (pm.done || (pm.steps.length > 0 && pm.steps.every((s) => s.state === 'done'))) return 'done';
  return 'running';
}

/** The bar, 0…1: the backend's `progress`, never behind the steps already done, full once done. Without a usable
 *  `progress` the steps say it (each done step a share, the running one half of its share). */
export function postFraction(pm: PostMeeting): number {
  if (postPhase(pm) === 'done') return 1;
  const n = pm.steps.length;
  const done = pm.steps.filter((s) => s.state === 'done').length;
  const running = pm.steps.some((s) => s.state === 'running') ? 0.5 : 0;
  const bySteps = n ? (done + running) / n : 0;
  const given = typeof pm.progress === 'number' && Number.isFinite(pm.progress) ? clamp01(pm.progress) : null;
  // (a stale `progress` from before the last step finished never pulls the bar back behind it)
  return given === null ? bySteps : Math.max(given, n ? done / n : 0);
}

/** The step to name: the failed one, else the one under way, else the next to come, else the last. */
export function postCurrent(pm: PostMeeting): PostStep | null {
  return (
    pm.steps.find((s) => s.state === 'failed') ??
    pm.steps.find((s) => s.state === 'running') ??
    pm.steps.find((s) => s.state === 'pending') ??
    pm.steps[pm.steps.length - 1] ??
    null
  );
}

/** "62%": whole percent, never 100 % before it is done (a bar at 99.6 % still says it is working). */
export function postPercent(pm: PostMeeting): number {
  const f = postFraction(pm);
  return postPhase(pm) === 'done' ? 100 : Math.min(99, Math.floor(f * 100));
}

/** How long a step usually takes when nobody said (s). */
const USUAL: Record<string, number> = { save: 6, transcript: 60, polish: 40, summary: 40 };
const expectedOf = (s: PostStep) => (typeof s.expected_s === 'number' && s.expected_s > 0 ? s.expected_s : USUAL[s.key] ?? 30);

/** The bar at wall time `now` (unix s), 0…1, smooth: each step's share of it is its expected length; a finished step
 *  is all of its share; the step under way (or the one waiting next, from when the one before it ended) eases on by
 *  how long it has run against how long it usually takes — 1 − e^(−1.3·t/T), never past 95 % of its share — or by its
 *  own `frac` when that is further; the steps after it add nothing. Full only once done. The island never draws it
 *  smaller than it was (a later, slower estimate doesn't pull it back). */
export function postSmooth(pm: PostMeeting, now: number): number {
  if (postPhase(pm) === 'done') return 1;
  const w = pm.steps.map(expectedOf);
  const total = w.reduce((a, b) => a + b, 0) || 1;
  let f = 0;
  let prevEnd: number | null = typeof pm.started === 'number' ? pm.started : null;
  for (let i = 0; i < pm.steps.length; i++) {
    const s = pm.steps[i];
    const share = w[i] / total;
    if (s.state === 'done') {
      f += share;
      prevEnd = typeof s.ended === 'number' ? s.ended : prevEnd;
      continue;
    }
    if (s.state === 'failed') break;
    const from = typeof s.started === 'number' ? s.started : prevEnd;
    const t = from === null ? 0 : Math.max(0, now - from);
    const eased = 0.95 * (1 - Math.exp((-1.3 * t) / w[i]));
    const own = typeof s.frac === 'number' && Number.isFinite(s.frac) ? Math.min(0.98, clamp01(s.frac)) : 0;
    f += share * Math.max(eased, own);
    break;
  }
  return Math.min(0.995, clamp01(f));
}
