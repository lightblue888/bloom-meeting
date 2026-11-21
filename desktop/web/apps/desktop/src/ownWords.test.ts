import { describe, expect, it } from 'vitest';
import { ownWords } from './ownWords';

// made-up remarks shaped like the meeting's: spoken words, then the AI's said-back with brackets of its own
const SAID = '那这一段你就，嗯，你改吧你改吧，就是那个区间的写法，有没有打断你，你改吧。';

describe('ownWords', () => {
  it('drops the said-back, brackets inside it included', () => {
    expect(ownWords(`${SAID}（当场复述的改法：好，那我改。2 节第（2）段那个“区间 1.20 至 3.45”，改成标准写法，记下了。）`)).toBe(SAID);
  });
  it('drops the older form and a supplement, keeps the other notes', () => {
    expect(ownWords('把表 2 的标题改一下（Claude 当场复述的改法：改成“各城市误差（毫米）”。）（又补充了：单位也写上）（当场复述的补充：好。）')).toBe(
      '把表 2 的标题改一下（又补充了：单位也写上）',
    );
  });
  it('cut short: to the end', () => {
    expect(ownWords('改一下（当场复述的改法：2 节第（2…')).toBe('改一下');
  });
  it('nothing to drop', () => {
    expect(ownWords(SAID)).toBe(SAID);
    expect(ownWords(null)).toBe('');
  });
});
