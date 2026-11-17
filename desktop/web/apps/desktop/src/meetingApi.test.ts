import { describe, expect, it } from 'vitest';
import { parseMic } from './meetingApi';

describe('parseMic: /api/mic/stream, the older bare level and the object with the mute state', () => {
  it('reads the older bare level', () => {
    expect(parseMic('0.35')).toEqual({ level: 0.35 });
    expect(parseMic('null')).toEqual({ level: null });
    expect(parseMic('1.7')).toEqual({ level: 1 });
    expect(parseMic('-2')).toEqual({ level: 0 });
  });
  it('reads the object with the mute state', () => {
    expect(parseMic('{"level": 0.5, "muted": false}')).toEqual({ level: 0.5, muted: false });
    expect(parseMic('{"level": null, "muted": true}')).toEqual({ level: null, muted: true });
    expect(parseMic('{"level": 0.2}')).toEqual({ level: 0.2 });
    expect(parseMic('{"muted": true}')).toEqual({ level: null, muted: true });
  });
  it('ignores what it cannot read', () => {
    expect(parseMic('not json')).toBeNull();
    expect(parseMic('"loud"')).toBeNull();
    expect(parseMic('[0.3]')).toBeNull();
    expect(parseMic('{"level": "x", "muted": "yes"}')).toEqual({ level: null });
  });
});
