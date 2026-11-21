import { afterEach, describe, expect, it } from 'vitest';
import { setMeetingLang } from './i18n';
import { foldSteps, patWords, reportSteps, stepK, tidyStep } from './components/stepText';

afterEach(() => setMeetingLang('zh-Hans'));

describe('foldSteps', () => {
  it('folds edits in a row in one section, keeps the first time', () => {
    const steps = [
      { t: '36:50', k: '在 D 节改第 66 处' },
      { t: '36:52', k: '在 D 节改第 67 处' },
      { t: '36:55', k: '在 D 节改第 68–70 处' },
      { t: '37:01', k: '在 E 节改第 71 处' },
      { t: '37:02', k: '在 E 节改第 72 处' },
    ];
    expect(foldSteps(steps)).toEqual([
      { t: '36:50', k: '在 D 节改第 66–70 处' },
      { t: '37:01', k: '在 E 节改第 71–72 处' },
    ]);
  });

  it('takes in a check between two edits of one section; leaves other sections and gaps alone', () => {
    expect(foldSteps([{ k: '在 3.2 节改第 1 处' }, { k: '检查修改' }, { k: '在 3.2 节改第 2 处' }, { k: '检查修改' }])).toEqual([
      { k: '在 3.2 节改第 1–2 处' },
      { k: '检查修改' },
    ]);
    expect(foldSteps([{ k: '在 3.2 节改第 1 处' }, { k: '在 3.1 节改第 2 处' }, { k: '在 3.2 节改第 3 处' }]).map((x) => x.k)).toEqual([
      '在 3.2 节改第 1 处',
      '在 3.1 节改第 2 处',
      '在 3.2 节改第 3 处',
    ]);
  });

  it('shows the same step once and hides the app’s own files', () => {
    expect(foldSteps([{ k: '读文档' }, { k: '读文档' }, { k: '读 .bloom-convert.json' }, { k: '查看截图' }]).map((x) => x.k)).toEqual([
      '读文档',
      '查看截图',
    ]);
  });
});

describe('reportSteps', () => {
  it('reads an old meeting’s report like a new one', () => {
    const old = [
      { k: '修改', v: '查找“^#+”' },
      { k: '修改', v: '在 F 节改第 87 处' },
      { k: '修改', v: '在 F 节改第 88 处' },
      { k: '修改', v: '检查修改' },
      { k: '修改', v: '已完成' },
    ];
    expect(reportSteps(old)).toEqual([{ k: '看目录' }, { k: '在 F 节改第 87–88 处' }, { k: '检查修改' }]);
  });

  it('says a failure once', () => {
    const old = [
      { k: '修改', v: '读文档' },
      { k: '修改', v: '读 .bloom-convert.json' },
      { k: '修改', v: '修改失败' },
      { k: '修改失败', v: '修改超时 420 秒' },
    ];
    expect(reportSteps(old)).toEqual([{ k: '读文档' }, { k: '失败', v: '修改超时 420 秒' }]);
    const now = [{ t: '58:40', k: '读文档' }, { t: '58:50', k: '失败', v: '修改超时 420 秒' }];
    expect(reportSteps(now)).toEqual(now);
  });
});

describe('patWords / tidyStep', () => {
  it('turns a pattern into its words, like the backend', () => {
    const cases: [string, string][] = [
      ['^#+', ''],
      [String.raw`3\.1`, '3.1'],
      [String.raw`3\.1\b`, '3.1'],
      ['唯一变量|测量工具', '唯一变量、测量工具'],
      ['^#|一笔数据是一条带时', '一笔数据是一条带时'],
      [String.raw`(?:3\.1|3\.2)`, '3.1、3.2'],
      ['测量工具.*误差', '测量工具 误差'],
      ['冒号[：:]', '冒号'],
      [String.raw`\d+`, ''],
      ['(?i)experiment|实验|result|outperform|baseline', 'experiment、实验、result、outperform 等'],
    ];
    for (const [pat, want] of cases) expect(patWords(pat), pat).toBe(want);
  });

  it('reads an old meeting’s search steps', () => {
    expect(tidyStep({ k: '查找“^#+”' })).toEqual({ k: '看目录' });
    expect(tidyStep({ k: '查找“^#|一笔数据是一条带时”' })).toEqual({ k: '查找“一笔数据是一条带时”' });
    expect(tidyStep({ k: String.raw`查找“3\.1”` })).toEqual({ k: '查找“3.1”' });
    expect(tidyStep({ t: '01:00', k: '搜索文档', v: '^#+' })).toEqual({ t: '01:00', k: '看目录', v: undefined });
    expect(tidyStep({ k: '搜索FooBench论文_合稿第1249版-bloom', v: '唯一变量、测量工具' })).toEqual({ k: '搜索文档', v: '唯一变量、测量工具' });
    expect(tidyStep({ k: '搜索notes.md', v: '基线' })).toEqual({ k: '搜索notes.md', v: '基线' });
    expect(tidyStep({ k: '读 .bloom-convert.json', v: '全文' })).toBeNull();
    // new steps stay as they are
    for (const x of [{ k: '查找“式 (1)”' }, { k: '查找“[12]”' }, { k: '看目录' }, { k: '搜索文档', v: '3.1' }, { k: '在 D 节改第 66–70 处' }])
      expect(tidyStep(x)).toEqual(x);
  });
});

describe('stepK', () => {
  it('leaves Chinese as it is', () => {
    setMeetingLang('zh-Hans');
    expect(stepK('在 D 节改第 66–70 处')).toBe('在 D 节改第 66–70 处');
    expect(stepK('看目录')).toBe('看目录');
  });

  it('translates a revision’s steps in an English meeting', () => {
    setMeetingLang('en');
    expect(stepK('在 D 节改第 66–70 处')).toBe('Edits 66–70 in “D”');
    expect(stepK('改第 3 到 4 处')).toBe('Edits 3–4');
    expect(stepK('检查修改')).toBe('Checking the edit');
    expect(stepK('看目录')).toBe('Read the outline');
    expect(stepK('失败')).toBe('Not revised');
    expect(stepK('查找“3.1”')).toBe('Searching the paper for “3.1”');
  });
});
