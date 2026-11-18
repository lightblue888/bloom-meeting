import { describe, expect, it } from 'vitest';
import { itemSec, itemText, parseMic, splitCites } from './meetingApi';

describe('parseMic: /api/mic/stream (the mute state, who is talking)', () => {
  it('reads the mute state and the talkers', () => {
    expect(parseMic('{"muted": false, "talk": ["me", "claude"]}')).toEqual({ muted: false, talk: ['me', 'claude'] });
    expect(parseMic('{"muted": true, "talk": []}')).toEqual({ muted: true, talk: [] });
    expect(parseMic('{"talk": ["张三"]}')).toEqual({ talk: ['张三'] });
  });
  it('an older backend (a level, no talkers): nobody talking', () => {
    expect(parseMic('{"level": 0.5, "muted": false}')).toEqual({ muted: false, talk: [] });
  });
  it('ignores what it cannot read', () => {
    expect(parseMic('not json')).toBeNull();
    expect(parseMic('0.35')).toBeNull();
    expect(parseMic('null')).toBeNull();
    expect(parseMic('[0.3]')).toBeNull();
    expect(parseMic('{"muted": "yes", "talk": ["a", 3]}')).toEqual({ talk: ['a'] });
  });
});

describe('纪要 lines: older summaries hold strings, newer ones objects with a time', () => {
  it('reads the text of either', () => {
    expect(itemText('结论一')).toBe('结论一');
    expect(itemText({ text: '结论二', t: '02:10', sec: 130 })).toBe('结论二');
    expect(itemText(null)).toBe('');
  });
  it('the time only when the summary says', () => {
    expect(itemSec('结论一')).toBeNull();
    expect(itemSec({ text: 'x' })).toBeNull();
    expect(itemSec({ text: 'x', t: '02:10', sec: 130 })).toBe(130);
  });
});

describe('回顾 → 提问: the answer split at its source marks', () => {
  it('only marks with a source become links', () => {
    expect(splitCites('上次说要补 [1]，也提过 [2][9]。', [1, 2])).toEqual(['上次说要补 ', 1, '，也提过 ', 2, '[9]。']);
  });
  it('no marks: the text as it is', () => {
    expect(splitCites('往期会议里没找到', [1])).toEqual(['往期会议里没找到']);
    expect(splitCites('', [])).toEqual([]);
  });
});

describe('in the meeting (第 3 批): the guest page goes through the relay', () => {
  it('sends the same calls to the guest transport and throws the refusal code', async () => {
    const { meetingApi, setGuestTransport, liveError } = await import('./meetingApi');
    const seen: [string, unknown][] = [];
    setGuestTransport({
      models: async () => null,
      devices: async () => ({}),
      setMicMuted: async () => false,
      watchMic: () => () => undefined,
      act: async (path, body) => {
        seen.push([path, body]);
        if (path === '/api/anchor') return { ok: false, error: 'cohost_only' };
        if (path === '/api/ask') return { ok: true };
        return { ok: true, bookmark: { id: 'b1', sec: 3, t: '00:03', note: '', by: '小周', host: false } };
      },
    });
    try {
      await expect(meetingApi.ask('怎么答')).resolves.toEqual({ ok: true });
      const b = await meetingApi.bookmark('');
      expect(b.bookmark.id).toBe('b1');
      await expect(meetingApi.anchor({ text: '一段' })).rejects.toThrow('cohost_only');
      expect(seen.map((x) => x[0])).toEqual(['/api/ask', '/api/bookmark', '/api/anchor']);
      expect(seen[0][1]).toEqual({ text: '怎么答' });
      expect(liveError(new Error('cohost_only'))).not.toBe('cohost_only');   // worded for the page
      expect(liveError(new Error('something else'))).toBe('something else');
    } finally {
      setGuestTransport(null);
    }
  });
});
