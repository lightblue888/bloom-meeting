import { describe, expect, it } from 'vitest';
import { checkCatalogs, createI18n } from '@blurt/i18n';
import { meterBars } from '../src/clock';
import EN from '../src/locales/en.json';
import ZH from '../src/locales/zh-Hans.json';
import { Pendulum, Spring } from '../src/spring';

describe('island', () => {
  it('meters the mic level', () => {
    expect(meterBars(0)).toBe(0);
    expect(meterBars(0.003)).toBe(0); // ≈ −50 dB
    expect(meterBars(0.3)).toBe(5);
    expect(meterBars(0.3, 12)).toBe(12);
  });
  it('en and zh-Hans catalogs match (K10)', () => {
    expect(checkCatalogs(EN, ZH)).toEqual([]);
    for (const k of Object.keys(EN)) expect(EN[k as keyof typeof EN].trim(), k).not.toBe('');
  });
  it('every job type and step the island names has words', () => {
    const i = createI18n({ en: EN, 'zh-Hans': ZH }, 'zh-Hans');
    for (const k of ['job.type.process', 'job.step.write', 'session.waiting_input', 'project.ai_sort', 'project.open_folder'])
      expect(i.has(k), k).toBe(true);
    expect(i.plural('island.open_inbox', 5)).toBe('5 条需要你看');
    expect(createI18n({ en: EN }, 'en').plural('island.open_inbox', 1)).toBe('1 item needs you');
  });
});

describe('motion', () => {
  it('spring settles on its target', () => {
    const s = new Spring(-1);
    s.target = 0;
    for (let i = 0; i < 120; i++) s.step(1 / 60);
    expect(s.resting).toBe(true);
  });
  it('pendulum swings then rests; holding damps it faster', () => {
    const a = new Pendulum();
    const b = new Pendulum();
    a.kick(2.5);
    b.kick(2.5);
    let crossed = false;
    for (let i = 0; i < 30; i++) {
      a.step(1 / 60);
      b.step(1 / 60, true);
      if (a.angle < 0) crossed = true;
    }
    expect(crossed).toBe(true); // it swings back past the rest position
    expect(Math.abs(b.velocity)).toBeLessThan(Math.abs(a.velocity) + 1e-9);
    for (let i = 0; i < 60 * 8; i++) a.step(1 / 60);
    expect(a.resting).toBe(true);
  });
});
