import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ANSWER_MS, BUSY_MS, handOff, type SwapHost } from './paperSwap';

// two fake paper pages and the shell's window: what the pages say back is queued as a message to the shell
type Fn = (e: MessageEvent) => void;
function world() {
  const fns: Fn[] = [];
  const host: SwapHost = {
    addEventListener: (_t, fn) => void fns.push(fn),
    removeEventListener: (_t, fn) => void fns.splice(fns.indexOf(fn), 1),
    setTimeout: (fn, ms) => setTimeout(fn, ms) as unknown as number,
    clearTimeout: (id) => clearTimeout(id),
  };
  const say = (source: unknown, data: unknown) => [...fns].forEach((f) => f({ data, source } as MessageEvent));
  const page = (answer: (m: Record<string, unknown>, self: Window) => unknown) => {
    const got: Record<string, unknown>[] = [];
    const w = {
      postMessage: (m: Record<string, unknown>) => {
        got.push(m);
        const a = answer(m, w as unknown as Window);
        if (a) queueMicrotask(() => say(w, a));
      },
    } as unknown as Window;
    return { w, got };
  };
  return { host, say, page, fns };
}
const AT = { i: 12, text: '第三段开头', off: 0.4, d: 'p1' };

describe('handOff (a new version of the paper swaps in where you are reading)', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('asks the old page, puts the new one there, then swaps', async () => {
    const { host, page, fns } = world();
    const old = page((m) => (m.zhReadAsk ? { zhRead: { id: m.zhReadAsk, at: AT, busy: '' } } : null));
    const neu = page((m) => (m.zhGo ? { zhGone: m.id } : null));
    const swap = vi.fn();
    handOff(old.w, neu.w, swap, host);
    await vi.advanceTimersByTimeAsync(10);
    expect(neu.got).toEqual([{ zhGo: AT, id: expect.any(String) }]);
    expect(swap).toHaveBeenCalledTimes(1);
    expect(fns).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(5000);
    expect(swap).toHaveBeenCalledTimes(1);
  });

  it('waits while you scroll, select or write in the old page', async () => {
    const { host, page } = world();
    let busy = 3;
    const old = page((m) => (m.zhReadAsk ? { zhRead: { id: m.zhReadAsk, at: AT, busy: busy-- > 0 ? 'input' : '' } } : null));
    const neu = page((m) => (m.zhGo ? { zhGone: m.id } : null));
    const swap = vi.fn();
    handOff(old.w, neu.w, swap, host);
    await vi.advanceTimersByTimeAsync(BUSY_MS * 2 + 10);
    expect(swap).not.toHaveBeenCalled();
    expect(neu.got).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(BUSY_MS * 2);
    expect(old.got.filter((m) => m.zhReadAsk)).toHaveLength(4);
    expect(swap).toHaveBeenCalledTimes(1);
  });

  it('an old page that does not answer (an older backend) is swapped as before', async () => {
    const { host, page } = world();
    const old = page(() => null);
    const neu = page(() => null);
    const swap = vi.fn();
    handOff(old.w, neu.w, swap, host);
    await vi.advanceTimersByTimeAsync(ANSWER_MS - 10);
    expect(swap).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(20);
    expect(swap).toHaveBeenCalledTimes(1);
    expect(neu.got).toHaveLength(0);
  });

  it('a new page that is still loading is asked again, then swapped anyway', async () => {
    const { host, page } = world();
    const old = page((m) => (m.zhReadAsk ? { zhRead: { id: m.zhReadAsk, at: AT, busy: '' } } : null));
    const neu = page(() => null);
    const swap = vi.fn();
    handOff(old.w, neu.w, swap, host);
    await vi.advanceTimersByTimeAsync(ANSWER_MS * 3 + 10);
    expect(swap).not.toHaveBeenCalled();
    expect(neu.got.length).toBeGreaterThanOrEqual(3);
    await vi.advanceTimersByTimeAsync(ANSWER_MS * 10);
    expect(swap).toHaveBeenCalledTimes(1);
  });

  it('ignores other answers and stops when called off', async () => {
    const { host, page, say } = world();
    const old = page(() => null);
    const neu = page(() => null);
    const swap = vi.fn();
    const cancel = handOff(old.w, neu.w, swap, host);
    say(neu.w, { zhRead: { id: 'x', at: AT } });            // not the old page, not this hand-off
    say(old.w, { zhRead: { id: 'other', at: AT } });
    say(old.w, { zhGone: 'other' });
    expect(neu.got).toHaveLength(0);
    cancel();
    await vi.advanceTimersByTimeAsync(5000);
    expect(swap).not.toHaveBeenCalled();
  });
});
