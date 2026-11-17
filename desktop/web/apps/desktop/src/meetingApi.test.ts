import { describe, expect, it } from 'vitest';
import { parseMic } from './meetingApi';

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
