import { describe, expect, it } from 'vitest';
import type { PostMeeting } from '../src/host';
import { postCurrent, postFraction, postPercent, postPhase, postSmooth } from '../src/postmeeting';

const pm = (states: string[], extra: Partial<PostMeeting> = {}): PostMeeting => ({
  meeting: '组会',
  steps: (['save', 'transcript', 'summary'] as const).map((key, i) => ({ key, label: ['保存录屏', '转写', '纪要'][i], state: states[i] as never })),
  progress: 0,
  done: false,
  error: null,
  ...extra,
});

describe('post-meeting progress', () => {
  it('names the step under way and fills by the backend progress', () => {
    const p = pm(['done', 'running', 'pending'], { progress: 0.55 });
    expect(postPhase(p)).toBe('running');
    expect(postCurrent(p)?.label).toBe('转写');
    expect(postFraction(p)).toBeCloseTo(0.55);
    expect(postPercent(p)).toBe(55);
  });

  it('never lags behind the steps already done', () => {
    const p = pm(['done', 'done', 'running'], { progress: 0.1 });
    expect(postFraction(p)).toBeCloseTo(2 / 3);
  });

  it('falls back to the steps when progress is missing', () => {
    const p = pm(['done', 'running', 'pending'], { progress: Number.NaN });
    expect(postFraction(p)).toBeCloseTo(1.5 / 3);
  });

  it('is full once done (by the flag or by every step)', () => {
    expect(postFraction(pm(['done', 'done', 'done'], { progress: 0.9 }))).toBe(1);
    expect(postPercent(pm(['done', 'done', 'running'], { done: true, progress: 0.97 }))).toBe(100);
    expect(postPhase(pm(['done', 'done', 'done']))).toBe('done');
  });

  it('never says 100 % while still working', () => {
    expect(postPercent(pm(['done', 'done', 'running'], { progress: 0.999 }))).toBe(99);
  });

  it('a failed step wins: it is the one named', () => {
    const p = pm(['done', 'failed', 'pending'], { error: 'ASR 超时', progress: 0.4 });
    expect(postPhase(p)).toBe('failed');
    expect(postCurrent(p)?.key).toBe('transcript');
  });

  it('an error with no failed step still reads as failed until done', () => {
    expect(postPhase(pm(['done', 'running', 'pending'], { error: 'x' }))).toBe('failed');
  });
});

describe('postSmooth (the bar eased on by the clock)', () => {
  const four = (states: string[], started: (number | null)[], ended: (number | null)[] = [], frac: (number | null)[] = []): PostMeeting => ({
    meeting: '组会',
    steps: ['save', 'transcript', 'polish', 'summary'].map((key, i) => ({
      key,
      label: key,
      state: states[i] as never,
      started: started[i] ?? null,
      ended: ended[i] ?? null,
      expected_s: [6, 60, 40, 40][i],
      frac: frac[i] ?? null,
    })),
    progress: 0,
    done: false,
    error: null,
    started: 1000,
  });

  it('starts at nothing, moves with time, never full while working', () => {
    const p = four(['running', 'pending', 'pending', 'pending'], [1000]);
    expect(postSmooth(p, 1000)).toBe(0);
    const a = postSmooth(p, 1003);
    const b = postSmooth(p, 1010);
    expect(a).toBeGreaterThan(0);
    expect(b).toBeGreaterThan(a);
    // the save step alone can't take more than its share (6 of 146 s)
    expect(postSmooth(p, 5000)).toBeLessThan(6 / 146);
  });

  it('a done step is its whole share; the next one eases on from when it ended', () => {
    const p = four(['done', 'pending', 'pending', 'pending'], [1000], [1006]);
    expect(postSmooth(p, 1006)).toBeCloseTo(6 / 146, 5);
    expect(postSmooth(p, 1036)).toBeGreaterThan(6 / 146);
  });

  it('a step\'s own frac wins when it is further', () => {
    const p = four(['done', 'running', 'pending', 'pending'], [1000, 1006], [1006], [null, 0.9]);
    expect(postSmooth(p, 1007)).toBeCloseTo((6 + 0.9 * 60) / 146, 3);
  });

  it('full only once done, and never 100 % before', () => {
    expect(postSmooth(four(['done', 'done', 'done', 'running'], [1, 2, 3, 4], [2, 3, 4]), 1e9)).toBeLessThan(1);
    expect(postSmooth(four(['done', 'done', 'done', 'done'], [], []), 0)).toBe(1);
  });
});
