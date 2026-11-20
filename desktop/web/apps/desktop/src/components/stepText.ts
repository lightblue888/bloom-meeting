// What Claude did, as a grey line under its bubble ("01:05 · 联网搜 TSGBench …"): the backend writes these in Chinese;
// in an English meeting (the meeting's language, see i18n.setMeetingLang) the known ones become English.
import { mi18n as i18n, mt as t } from '../i18n';

/** the backend writes its step labels in Chinese ("读论文", "搜索论文", "联网搜索"): in another UI language, the known
 * ones become that language's words; anything unknown stays as it came. Old meetings' steps (the older words: "在论文里搜",
 * "联网搜", "读任务板", "改稿", "没改成" …) translate the same way. */
const STEP_KEYS: Record<string, string> = {
  查看修改进度: 'meeting.ai.step.board', 对照: 'meeting.ai.step.compare', 修改: 'meeting.ai.step.edit',
  修改失败: 'meeting.ai.step.failed', 未核实: 'meeting.ai.step.unverified', 查看截图: 'meeting.ai.step.look',
  联网搜索: 'meeting.ai.step.web', 打开网页: 'meeting.ai.step.open', 查找文件: 'meeting.ai.step.glob',
  读论文: 'meeting.ai.step.read_paper', 想法: 'meeting.ai.step.thought',
  查文献: 'meeting.ai.step.lit', 核对引用: 'meeting.ai.step.cite',
  // older meetings
  读任务板: 'meeting.ai.step.board', 改稿: 'meeting.ai.step.edit', 没改成: 'meeting.ai.step.failed',
  没核对上: 'meeting.ai.step.unverified', 看截图: 'meeting.ai.step.look', 联网搜: 'meeting.ai.step.web',
  找文件: 'meeting.ai.step.glob',
};

export function stepK(k: string): string {
  if (!k || i18n().lang === 'zh-Hans') return k;
  if (STEP_KEYS[k]) return t(STEP_KEYS[k]);
  const s = /^搜索(.+)$/.exec(k) ?? /^在(.+)里搜$/.exec(k);
  if (s) return s[1] === '论文' ? t('meeting.ai.step.search_paper') : t('meeting.ai.step.search_in', { f: s[1] });
  const r = /^读 (.+)$/.exec(k);
  if (r) return t('meeting.ai.step.read_file', { f: r[1] });
  return k;
}

export function stepV(v: string | undefined): string | undefined {
  if (!v || i18n().lang === 'zh-Hans') return v;
  // the reviser's progress (zuhui/hands.py WorkerProgress): "在“Abstract”改第 2 处", "在 3.2 节改第 2 到 3 处", "查找“X”", "已完成"
  // (older meetings: "在论文里找“X”", "改完了", "没改成", "检查改动" …)
  const where = (w?: string) => (!w ? '' : t('meeting.ai.step.in_sec', { s: w.replace(/^在\s*/, '').replace(/\s*节$/, '').replace(/^“(.*)”$/, '$1') }));
  const prog: [RegExp, (...m: string[]) => string][] = [
    [/^(在\s*(?:“[^”]*”|\S+?\s*节))?改第 (\d+) 到 (\d+) 处$/, (_m, w, a, b) => t('meeting.ai.step.edit_range', { a, b }) + where(w)],
    [/^(在\s*(?:“[^”]*”|\S+?\s*节))?改第 (\d+) 处$/, (_m, w, n) => t('meeting.ai.step.edit_n', { n }) + where(w)],
    [/^(?:查找|在论文里找)“(.*)”$/, (_m, q) => t('meeting.ai.step.find_q', { q })],
    [/^(?:搜索论文|在论文里找)$/, () => t('meeting.ai.step.search_paper')],
    [/^(?:已完成|改完了)$/, () => t('meeting.ai.step.done')],
    [/^(?:修改失败|没改成)$/, () => t('meeting.ai.step.failed')],
    [/^(?:检查修改|检查改动)$/, () => t('meeting.ai.step.checking')],
    [/^读论文$/, () => t('meeting.ai.step.read_paper')],
    [/^(?:查看截图|看截图)$/, () => t('meeting.ai.step.look')],
    [/^(?:查找文件|找文件)$/, () => t('meeting.ai.step.glob')],
    [/^查文献$/, () => t('meeting.ai.step.lit')],
    [/^核对引用$/, () => t('meeting.ai.step.cite')],
    [/^(?:重写全文|整篇重写论文)$/, () => t('meeting.ai.step.rewrite')],
    [/^开始$/, () => t('meeting.ai.step.start')],
  ];
  for (const [re, f] of prog) {
    const m = re.exec(v);
    if (m) return f(...(m as unknown as string[]));
  }
  return v
    .replace(/（([^（）]*)）/g, ' ($1)')
    .replace(/第 (\d+)–(\d+) 行/g, (_m, a, b) => t('meeting.ai.step.lines', { a, b }))
    .replace(/从第 (\d+) 行起/g, (_m, a) => t('meeting.ai.step.from_line', { a }))
    .replace(/^论文全文/, t('meeting.ai.step.full_paper'))
    .replace(/^论文目录/, t('meeting.ai.step.outline'))
    .replace(/^全文$/, t('meeting.ai.step.whole_file'))
    .replace(/，重点 /g, t('meeting.ai.step.focus'));
}
