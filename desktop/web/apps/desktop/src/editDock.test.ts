// The revision under way at the transcript's foot (components/EditDock.tsx): what its two lines say.
import { beforeAll, describe, expect, it } from 'vitest';
import { setLang } from './i18n';
import { dockText, elapsed } from './components/EditDock';
import type { Board, Brain } from './meetingApi';

const NOW = 1_800_000_000;
const brain = (b: Partial<Brain>): Brain => ({ front: 'listening', ...b });

describe('EditDock: the revision under way, at the foot of the transcript', () => {
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
    const d = dockText(
      brain({
        edits: [{ id: 'T20', idx: 20, title: '全文去掉多余冒号分号', started: NOW - 425, edits: 41, log: ['读文档', '在 3.2 节改第 40 处', '在 F 节改第 41 处'] }],
        queued: 1,
      }),
      null,
      NOW,
    );
    expect(d).toEqual({ title: '修改中：全文去掉多余冒号分号', now: '已改 41 处 · 7 分 5 秒 · 在 F 节改第 41 处', queue: '1 条排队中' });
  });
  it('the step it is on: the one the worker says, in the step list\'s words', () => {
    const e = { id: 'T20', idx: 20, title: 'X', started: NOW - 5, edits: 87 };
    // the worker's own "now" over the folded log
    expect(dockText(brain({ edits: [{ ...e, step: '在 D 节改第 87 处', log: ['在 D 节改第 76–87 处'] }] }), null, NOW)?.now).toBe('已改 87 处 · 5 秒 · 在 D 节改第 87 处');
    // no "now": the newest step
    expect(dockText(brain({ edits: [{ ...e, steps: [{ t: '36:50', k: '读文档' }, { t: '36:58', k: '在 D 节改第 76–87 处' }] }] }), null, NOW)?.now).toBe('已改 87 处 · 5 秒 · 在 D 节改第 76–87 处');
    // an older backend's search pattern reads as what it looks for
    expect(dockText(brain({ edits: [{ ...e, log: ['查找“^#+”'] }] }), null, NOW)?.now).toBe('已改 87 处 · 5 秒 · 看目录');
  });
  it('just started: no "已改 0 处", no step yet', () => {
    const d = dockText(brain({ edits: [{ id: 'T3', idx: 3, title: '统一图 3', started: NOW - 4, edits: 0, log: [] }], queued: 0 }), null, NOW);
    expect(d).toEqual({ title: '修改中：统一图 3', now: '4 秒', queue: '' });
  });
  it('only waiting: the next one by name, how many (the count where it was)', () => {
    const board = {
      counts: { queued: 2 },
      tasks: [
        { id: 'T22', idx: 22, title: '后台状态', remark: '', status: 'queued', created: '' },
        { id: 'T21', idx: 21, title: '全文扫冒号分号只换标点', remark: '', status: 'queued', created: '' },
        { id: 'T20', idx: 20, title: '全文去掉多余冒号分号', remark: '', status: 'done', created: '' },
      ],
    } as unknown as Board;
    expect(dockText(brain({ edits: [], queued: 2 }), board, NOW)).toEqual({ title: '下一条：全文扫冒号分号只换标点', now: '', queue: '2 条排队中', wait: true });
    // the board not known: the count alone
    expect(dockText(brain({ edits: [], queued: 3 }), null, NOW)).toEqual({ title: null, now: '', queue: '3 条排队中', wait: true });
    expect(dockText(brain({ edits: [], queued: 1, waiting: 2 }), null, NOW)?.queue).toBe('2 条等文档识别完 · 1 条排队中');
  });
  it('two at once: the first, and how many more', () => {
    const e = (id: string, idx: number) => ({ id, idx, title: id, started: NOW - 10, edits: 1, log: [] });
    expect(dockText(brain({ edits: [e('T1', 1), e('T2', 2)], queued: 0 }), null, NOW)?.queue).toBe('还有 1 条在改');
  });
  it('nothing under way: empty', () => {
    expect(dockText(brain({ edits: [], queued: 0, waiting: 0 }), null, NOW)).toBeNull();
  });
  it('an older backend that pushes no edits: read off the board', () => {
    const board = {
      counts: { queued: 2 },
      tasks: [{ id: 'T2', idx: 2, title: '改写 3.2 节', remark: '', status: 'working', created: '', progress: { edits: 2, started: NOW - 90, log: ['在 3.2 节改第 2 处'] } }],
    } as unknown as Board;
    expect(dockText(brain({}), board, NOW)).toEqual({ title: '修改中：改写 3.2 节', now: '已改 2 处 · 1 分 30 秒 · 在 3.2 节改第 2 处', queue: '2 条排队中' });
  });
});
