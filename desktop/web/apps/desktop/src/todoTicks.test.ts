import { describe, expect, it } from 'vitest';
import { ticksToMove, todoKey } from './todoTicks';

describe('修改清单 ticks move from the browser to carry.json', () => {
  it('moves the ticks carry.json does not have yet', () => {
    expect(ticksToMove(['T1', 'T2', 'T3'], ['note:T2', 'todo:abc'])).toEqual(['T1', 'T3']);
  });
  it('keeps each once and drops what is not a task id', () => {
    expect(ticksToMove(['T1', 'T1', 7, null, '../x', 'a b'], [])).toEqual(['T1']);
  });
  it('nothing to move when there was nothing (or garbage) stored', () => {
    expect(ticksToMove(null, [])).toEqual([]);
    expect(ticksToMove({ T1: true }, [])).toEqual([]);
    expect(ticksToMove(['T4'], ['note:T4'])).toEqual([]);
  });
  it('the old key is per meeting', () => {
    expect(todoKey('20261118-100000')).toBe('bloom-todos-20261118-100000');
  });
});
