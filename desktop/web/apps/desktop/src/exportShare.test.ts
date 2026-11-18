// 第 5 批 导出和分享: the save dialog's default names and the reasons in "保存失败：原因"
import { describe, expect, it } from 'vitest';
import { exportName, exportReason } from './meetingApi';

describe('exportName: "2026-11-18 组会 纪要.docx"', () => {
  it('date first, then the meeting, then what it is', () => {
    expect(exportName('第三章 实验设计', '2026-11-18T14:30:00', '纪要', 'docx')).toBe('2026-11-18 第三章 实验设计 纪要.docx');
    expect(exportName('Weekly', '2026-01-05T09:00:00', 'Meeting pack', 'html')).toBe('2026-01-05 Weekly Meeting pack.html');
  });
  it('drops the date an older title carried, and what Windows refuses in a name', () => {
    expect(exportName('组会 · 11月11日 05:03', '2026-11-11T05:03:00', '纪要', 'pdf')).toBe('2026-11-11 组会 纪要.pdf');
    expect(exportName('a/b:c*d?"e"<f>|g', '2026-11-18T10:00:00', '纪要', 'docx')).toBe('2026-11-18 a b c d e f g 纪要.docx');
    expect(exportName('ends with dots...', '2026-11-18T10:00:00', '纪要', 'docx')).toBe('2026-11-18 ends with dots 纪要.docx');
  });
  it('no start time: no date', () => {
    expect(exportName('组会', null, '纪要', 'docx')).toBe('组会 纪要.docx');
    expect(exportName('组会', 'not a date', '纪要', 'docx')).toBe('组会 纪要.docx');
  });
});

describe('exportReason', () => {
  it('a code the app knows: its words; anything else: what the backend said', () => {
    expect(exportReason({ code: 'no_summary' })).not.toBe('no_summary');
    expect(exportReason({ code: 'zzz', detail: '磁盘已满' })).toBe('磁盘已满');
    expect(exportReason(new Error('HTTP 500'))).toBe('HTTP 500');
  });
});
