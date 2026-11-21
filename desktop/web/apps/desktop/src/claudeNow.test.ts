// The AI strip above the transcript (components/claudeNow.ts): its one line, and the whole of it on hover, one part a
// line. The time and step words came from the card that used to sit at the transcript's foot (EditDock, removed
// 2026-11-21): the same words, now in the strip's tip.
import { beforeAll, describe, expect, it } from 'vitest';
import { setLang } from './i18n';
import { backLine, backTip, editsOf, elapsed } from './components/claudeNow';
import { splitClaude } from './components/MeetingLayout';
import type { Board, Brain, Line } from './meetingApi';

const NOW = 1_800_000_000;
const brain = (b: Partial<Brain>): Brain => ({ front: 'listening', ...b });
const lines = (b: Partial<Brain>, board: Board | null = null) => backTip(brain(b), board, NOW)?.split('\n') ?? [];

describe('the AI strip: what the background is doing', () => {
  beforeAll(() => {
    setLang('zh-Hans');
  });
  it('minutes once past a minute', () => {
    expect(elapsed(0)).toBe('0 秒');
    expect(elapsed(59)).toBe('59 秒');
    expect(elapsed(60)).toBe('1 分 0 秒');
    expect(elapsed(425)).toBe('7 分 5 秒');
  });
  it('a big one far into its work: title, places so far, time, the step it is on; the queue', () => {
    const b = { edits: [{ id: 'T20', idx: 20, title: '全文去掉多余冒号分号', started: NOW - 425, edits: 41, log: ['读文档', '在 3.2 节改第 40 处', '在 F 节改第 41 处'] }], queued: 1 };
    expect(lines(b)).toEqual(['修改中：全文去掉多余冒号分号 · 已改 41 处 · 7 分 5 秒 · 在 F 节改第 41 处', '1 条排队中']);
    // the strip's own line: no step (it ends in "…" soon enough)
    expect(backLine(brain(b), null, NOW)).toEqual(['修改中：全文去掉多余冒号分号 · 已改 41 处 · 7 分 5 秒', '1 条排队中']);
  });
  it("the step it is on: the one the worker says, in the step list's words", () => {
    const e = { id: 'T20', idx: 20, title: 'X', started: NOW - 5, edits: 87 };
    // the worker's own "now" over the folded log
    expect(lines({ edits: [{ ...e, step: '在 D 节改第 87 处', log: ['在 D 节改第 76–87 处'] }] })).toEqual(['修改中：X · 已改 87 处 · 5 秒 · 在 D 节改第 87 处']);
    // no "now": the newest step
    expect(lines({ edits: [{ ...e, steps: [{ t: '36:50', k: '读文档' }, { t: '36:58', k: '在 D 节改第 76–87 处' }] }] })).toEqual(['修改中：X · 已改 87 处 · 5 秒 · 在 D 节改第 76–87 处']);
    // an older backend's search pattern reads as what it looks for
    expect(lines({ edits: [{ ...e, log: ['查找“^#+”'] }] })).toEqual(['修改中：X · 已改 87 处 · 5 秒 · 看目录']);
  });
  it('just started: no "已改 0 处", no step yet', () => {
    expect(lines({ edits: [{ id: 'T3', idx: 3, title: '统一图 3', started: NOW - 4, edits: 0, log: [] }], queued: 0 })).toEqual(['修改中：统一图 3 · 4 秒']);
  });
  it('two at once: one line each', () => {
    const e = (id: string, idx: number) => ({ id, idx, title: id, started: NOW - 10, edits: 1, log: [`在 ${idx}.1 节改第 1 处`] });
    expect(lines({ edits: [e('T1', 1), e('T2', 2)], queued: 0 })).toEqual(['修改中：T1 · 已改 1 处 · 10 秒 · 在 1.1 节改第 1 处', '修改中：T2 · 已改 1 处 · 10 秒 · 在 2.1 节改第 1 处']);
  });
  it('each lookup whole, with its newest step; a found one waits to be said', () => {
    const tip = lines({
      deeps: [
        { id: 1, q: '嗯那个 TSGBench 的问题定义是怎么写的，和我们的写法比一下哪个更清楚', topic: null, secs: 14, t0: NOW - 14, state: 'running', steps: [{ k: '读文档' }, { k: '联网搜索', v: 'TSGBench problem definition' }] },
        { id: 2, q: 'FinTS 在表 3 里出现了几次', topic: 'FinTS 次数', secs: 6, t0: NOW - 6, state: 'done', steps: [{ k: '读文档' }] },
      ],
      unspoken: 1,
    });
    expect(tip).toEqual([
      // the words as said, without the ums, not cut
      '查找中：TSGBench 的问题定义是怎么写的，和我们的写法比一下哪个更清楚 · 14 秒 · 联网搜索 TSGBench problem definition',
      '已查到，待回答：FinTS 次数 · 6 秒',
      '1 项已查到，未说出',
    ]);
    // the strip's own line: both by their short names
    expect(backLine(brain({ deeps: [{ id: 1, q: '嗯那个 TSGBench 的问题定义是怎么写的，和我们的写法比一下哪个更清楚', secs: 14, t0: NOW - 14, state: 'running' }] }), null, NOW)).toEqual([
      '查找中：TSGBench 的问题定义是怎么写的，和我们的… · 14 秒',
    ]);
  });
  it('the counts: waiting on their document, queued, notes for after the meeting', () => {
    const board = { counts: { queued: 2, notes: 3 }, tasks: [] } as unknown as Board;
    expect(lines({ edits: [], waiting: 2 }, board)).toEqual(['2 条等文档识别完', '2 条排队中', '会后改 3 条']);
  });
  it('nothing under way: no tip (notes alone are in the strip already)', () => {
    expect(backTip(brain({ edits: [], queued: 0, waiting: 0 }), { counts: { notes: 2 }, tasks: [] } as unknown as Board, NOW)).toBeUndefined();
  });
  it('an older backend that pushes no edits: read off the board', () => {
    const board = {
      counts: { queued: 2 },
      tasks: [{ id: 'T2', idx: 2, title: '改写 3.2 节', remark: '', status: 'working', created: '', progress: { edits: 2, started: NOW - 90, log: ['在 3.2 节改第 2 处'] } }],
    } as unknown as Board;
    expect(editsOf(brain({}), board).map((w) => w.id)).toEqual(['T2']);
    expect(lines({}, board)).toEqual(['修改中：改写 3.2 节 · 已改 2 处 · 1 分 30 秒 · 在 3.2 节改第 2 处', '2 条排队中']);
  });
});

describe('a hand-over to the background (#deep): one step saying what it thinks about', () => {
  beforeAll(() => {
    setLang('zh-Hans');
  });
  const deep = (x: Partial<Line>): Line => ({ t: '10:54', sec: 654, who: 'student', tag: '#deep', lat: 1.2, text: '好，这个我交给后台仔细想一想。', ...x });
  const steps = (l: Line) => splitClaude(l).flatMap((p) => p.shownSteps ?? []).map((s) => [s.k, s.v ?? ''].join(' ').trim());
  it('its own step names the question: that line only, no "派任务 深入查找" after it', () => {
    expect(steps(deep({ steps: [{ t: '10:54', k: '深入思考', v: '要不要在第三章把表二的大类都介绍一遍' }] }))).toEqual(['深入思考 要不要在第三章把表二的大类都介绍一遍']);
  });
  it('no question in its steps: "交给后台", with the question when the tag carries one', () => {
    expect(steps(deep({ steps: [{ t: '10:54', k: '对照', v: '文档全文，重点 3.2' }] }))).toEqual(['对照 文档全文，重点 3.2', '交给后台']);
    expect(steps(deep({ tag: '#deep 表 2 少了哪个基线' }))).toEqual(['交给后台 表 2 少了哪个基线']);
  });
  it('an old line (no lat): the answer in the same line, no hand-over step', () => {
    expect(steps(deep({ lat: undefined, steps: [{ k: '读文档' }] }))).toEqual(['读文档']);
  });
});
