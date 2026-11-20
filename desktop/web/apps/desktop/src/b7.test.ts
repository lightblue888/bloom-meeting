// 第 7 批: what the page works out by itself — 读法's "written = said" lines (the backend's voice_prefs._clean_pron).
import { describe, expect, it } from 'vitest';
import { parsePron, pronText } from './components/PronSettings';

describe('parsePron: 读法 one per line', () => {
  it('any of the separators; the later reading of the same word wins', () => {
    const { pairs, bad } = parsePron('BERT = 伯特\nLaTeX → 雷泰克\n\nIT,爱踢\nGPT-4o -> 鸡皮提四欧\n张老师\t张老湿\nBERT = 波特');
    expect(bad).toBe(0);
    expect(pairs).toEqual([
      { w: 'BERT', r: '波特' },
      { w: 'LaTeX', r: '雷泰克' },
      { w: 'IT', r: '爱踢' },
      { w: 'GPT-4o', r: '鸡皮提四欧' },
      { w: '张老师', r: '张老湿' },
    ]);
    expect(pronText(pairs.slice(0, 2))).toBe('BERT = 波特\nLaTeX = 雷泰克');
  });
  it('a line without both sides is bad', () => {
    expect(parsePron('只有写法\n= 只有读法\na = b').bad).toBe(2);
  });
});
