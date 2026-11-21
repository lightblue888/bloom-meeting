// What Claude did, as a grey line under its bubble ("01:05 · 联网搜 TSGBench …"): the backend writes these in Chinese;
// in an English meeting (the meeting's language, see i18n.setMeetingLang) the known ones become English.
import { mi18n as i18n, mt as t } from '../i18n';

/** one grey step line (the same shape as MeetingLayout's Step) */
type Step = { t?: string; k: string; v?: string };

/** the backend writes its step labels in Chinese ("读文档", "搜索文档", "联网搜索"): in another UI language, the known
 * ones become that language's words; anything unknown stays as it came. Old meetings' steps (the older words: "读论文",
 * "搜索论文", "在论文里搜", "联网搜", "读任务板", "改稿", "没改成" …) translate the same way. */
const STEP_KEYS: Record<string, string> = {
  查看修改进度: 'meeting.ai.step.board', 对照: 'meeting.ai.step.compare', 修改: 'meeting.ai.step.edit',
  修改失败: 'meeting.ai.step.failed', 未核实: 'meeting.ai.step.unverified', 查看截图: 'meeting.ai.step.look',
  联网搜索: 'meeting.ai.step.web', 打开网页: 'meeting.ai.step.open', 查找文件: 'meeting.ai.step.glob',
  读文档: 'meeting.ai.step.read_paper', 读论文: 'meeting.ai.step.read_paper', 想法: 'meeting.ai.step.thought',
  查文献: 'meeting.ai.step.lit', 核对引用: 'meeting.ai.step.cite',
  看目录: 'meeting.ai.step.toc', 失败: 'meeting.ai.step.failed', 标注: 'meeting.ai.step.noted',
  // older meetings
  读任务板: 'meeting.ai.step.board', 改稿: 'meeting.ai.step.edit', 没改成: 'meeting.ai.step.failed',
  没核对上: 'meeting.ai.step.unverified', 看截图: 'meeting.ai.step.look', 联网搜: 'meeting.ai.step.web',
  找文件: 'meeting.ai.step.glob',
};

export function stepK(k: string): string {
  if (!k || i18n().lang === 'zh-Hans') return k;
  if (STEP_KEYS[k]) return t(STEP_KEYS[k]);
  const s = /^搜索(.+)$/.exec(k) ?? /^在(.+)里搜$/.exec(k);
  if (s) return s[1] === '文档' || s[1] === '论文' ? t('meeting.ai.step.search_paper') : t('meeting.ai.step.search_in', { f: s[1] });
  const r = /^读 (.+)$/.exec(k);
  if (r) return t('meeting.ai.step.read_file', { f: r[1] });
  // a revision's step is the whole label ("在 D 节改第 76–87 处", "检查修改"): the same words as its old place in v
  const v = stepV(k) ?? k;
  return v.charAt(0).toUpperCase() + v.slice(1);
}

/** a revision's edits, one label each ("在 D 节改第 87 处", "改第 3 到 4 处"): [where, first, last] */
const EDIT_RE = /^(在\s*(?:“[^”]*”|\S+?\s*节))?改第 (\d+)(?:(?: 到 |–)(\d+))? 处$/;
const CHECK_RE = /^(?:检查修改|检查改动)$/;

/** one alternative of a search pattern → its literal words ("3\.1\b" → "3.1", "测量工具.*误差" → "测量工具 误差") */
function literal(alt: string): string {
  let out = '';
  for (let i = 0; i < alt.length; ) {
    const c = alt[i];
    if (c === '\\' && i + 1 < alt.length) {
      const m = /^\\(?:x\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4})/.exec(alt.slice(i));
      if (m) {
        out += ' ';
        i += m[0].length;
        continue;
      }
      out += /[\p{L}\p{N}]/u.test(alt[i + 1]) ? ' ' : alt[i + 1];
      i += 2;
      continue;
    }
    if (c === '[') {
      const j = alt.indexOf(']', i + 2);
      i = j > 0 ? j + 1 : i + 1;
      out += ' ';
      continue;
    }
    if (c === '{') {
      const m = /^\{\d*,?\d*\}/.exec(alt.slice(i));
      if (m) {
        out += ' ';
        i += m[0].length;
        continue;
      }
    }
    out += '^$.*+?(){}<>'.includes(c) ? ' ' : c;
    i += 1;
  }
  return out.split(/\s+/).filter(Boolean).join(' ').replace(/^[ ,.;:\-_/'"]+|[ ,.;:\-_/'"]+$/g, '');
}

/** a search pattern → the words in it ("唯一变量|测量工具" → "唯一变量、测量工具"; "^#+" → ""), as zuhui/hands.py
 *  _pat_words does: older meetings' steps still show the pattern itself ("查找“^#+”") */
export function patWords(pat: string, most = 4): string {
  const p = pat.replace(/\(\?[a-zA-Z]+\)/g, '').replace(/\(\?(?:[:=!>]|<[=!]|P?<\w+>)/g, '(');
  const words: string[] = [];
  for (const alt of p.split(/(?<!\\)\|/)) {
    const w = literal(alt);
    if ((w.match(/[A-Za-z0-9一-鿿]/g) ?? []).length >= 2 && !words.includes(w)) words.push(w.length <= 20 ? w : w.slice(0, 20) + '…');
  }
  return words.slice(0, most).join('、') + (words.length > most ? ' 等' : '');
}

/** a pattern, not words: what the older backend put in a step ("^#|一笔数据是一条带时", "3\.1") */
const RX_SIGNS = /[\\^$|]|\.[*+?]/;
const searchWhat = (pat: string): [string, string] => {
  const w = patWords(pat);
  return w ? ['查找', w] : /\^\s*\\?#/.test(pat) ? ['看目录', ''] : ['搜索文档', ''];
};

/** a step as the backend now writes it (zuhui/hands.py grep_label / ask_step), so older meetings read the same:
 *  a search shows its words, not its pattern ("查找“^#+”" → "看目录", "搜索文档 3\.1" → "搜索文档 3.1"); a folder searched
 *  is the document ("搜索FooBench论文-bloom" → "搜索文档"); the app's own files ("读 .bloom-convert.json") don't show (null) */
export function tidyStep<T extends Step>(x: T): T | null {
  if (/^(?:读 |搜索)\./.test(x.k)) return null;
  const q = !x.v ? /^查找“(.*)”$/.exec(x.k) : null;
  if (q && RX_SIGNS.test(q[1])) {
    const [k, w] = searchWhat(q[1]);
    return { ...x, k: w ? `${k}“${w}”` : k };
  }
  const s = /^搜索(.+)$/.exec(x.k);
  if (!s) return x;
  const doc = s[1] === '文档' || s[1] === '论文' || !/\.[A-Za-z0-9]{1,6}$/.test(s[1]);
  if (!doc) return x;
  if (x.v && RX_SIGNS.test(x.v)) {
    const [k, w] = searchWhat(x.v);
    return { ...x, k: k === '看目录' ? k : '搜索文档', v: w || undefined };
  }
  return s[1] === '文档' ? x : { ...x, k: '搜索文档' };
}

/** the reviser's steps, folded the way the backend now does it (zuhui/hands.py WorkerProgress._add), so older meetings
 *  read the same: edits in a row in one section become one line ("在 D 节改第 76–87 处", a "检查修改" between them goes
 *  in too), the same step twice in a row shows once, and the app's own files in the document's folder ("读 .bloom-convert.json")
 *  don't show. Keeps the first one's time. */
export function foldSteps<T extends Step>(steps: T[]): T[] {
  const out: T[] = [];
  const edit = (x?: T) => (x && !x.v ? EDIT_RE.exec(x.k) : null);
  const joins = (p: RegExpExecArray | null, m: RegExpExecArray) =>
    !!p && (p[1] ?? '') === (m[1] ?? '') && Number(m[2]) === Number(p[3] ?? p[2]) + 1;
  for (const raw of steps) {
    const x = tidyStep(raw);
    if (!x) continue;
    const m = edit(x);
    if (m) {
      const last = out[out.length - 1];
      if (last && !last.v && CHECK_RE.test(last.k) && joins(edit(out[out.length - 2]), m)) out.pop();
      const p = edit(out[out.length - 1]);
      if (p && joins(p, m)) {
        out[out.length - 1] = { ...out[out.length - 1], k: `${p[1] ?? ''}改第 ${p[2]}–${m[3] ?? m[2]} 处` };
        continue;
      }
    } else {
      const last = out[out.length - 1];
      if (last && last.k === x.k && (last.v ?? '') === (x.v ?? '')) continue;
    }
    out.push(x);
  }
  return out;
}

const END_RE = /^(?:开始|已完成|改完了|修改失败|没改成|标注了)$/;

/** a report's steps: what the reviser did, then (when it failed) one "失败 原因" line. Older meetings had every step
 *  under "修改" ("修改 在 F 节改第 87 处", "修改 已完成") and the failure twice ("修改 修改失败", "修改失败 修改超时 420 秒"):
 *  read the same as the new ones */
export function reportSteps(steps: Step[]): Step[] {
  const flat = steps.flatMap((x): Step[] => {
    if ((x.k === '修改' || x.k === '改稿') && x.v) return END_RE.test(x.v) ? [] : [{ t: x.t, k: x.v }];
    if ((x.k === '修改失败' || x.k === '没改成') && x.v) return [{ t: x.t, k: '失败', v: x.v }];
    return [x];
  });
  return foldSteps(flat);
}

export function stepV(v: string | undefined): string | undefined {
  if (!v || i18n().lang === 'zh-Hans') return v;
  // the reviser's progress (zuhui/hands.py WorkerProgress): "在“Abstract”改第 2 处", "在 3.2 节改第 2 到 3 处", "查找“X”", "已完成"
  // (older meetings: "在论文里找“X”", "改完了", "没改成", "检查改动" …)
  const where = (w?: string) => (!w ? '' : t('meeting.ai.step.in_sec', { s: w.replace(/^在\s*/, '').replace(/\s*节$/, '').replace(/^“(.*)”$/, '$1') }));
  const prog: [RegExp, (...m: string[]) => string][] = [
    [/^(在\s*(?:“[^”]*”|\S+?\s*节))?改第 (\d+)(?: 到 |–)(\d+) 处$/, (_m, w, a, b) => t('meeting.ai.step.edit_range', { a, b }) + where(w)],
    [/^(在\s*(?:“[^”]*”|\S+?\s*节))?改第 (\d+) 处$/, (_m, w, n) => t('meeting.ai.step.edit_n', { n }) + where(w)],
    [/^(?:查找|在论文里找)“(.*)”$/, (_m, q) => t('meeting.ai.step.find_q', { q })],
    [/^(?:搜索文档|搜索论文|在论文里找)$/, () => t('meeting.ai.step.search_paper')],
    [/^(?:已完成|改完了)$/, () => t('meeting.ai.step.done')],
    [/^(?:修改失败|没改成)$/, () => t('meeting.ai.step.failed')],
    [/^(?:检查修改|检查改动)$/, () => t('meeting.ai.step.checking')],
    [/^读(?:文档|论文)$/, () => t('meeting.ai.step.read_paper')],
    [/^(?:查看截图|看截图)$/, () => t('meeting.ai.step.look')],
    [/^(?:查找文件|找文件)$/, () => t('meeting.ai.step.glob')],
    [/^查文献$/, () => t('meeting.ai.step.lit')],
    [/^核对引用$/, () => t('meeting.ai.step.cite')],
    [/^(?:重写全文|整篇重写论文)$/, () => t('meeting.ai.step.rewrite')],
    [/^开始$/, () => t('meeting.ai.step.start')],
    [/^网络错误，正在重试$/, () => t('meeting.ai.step.retry')],
  ];
  for (const [re, f] of prog) {
    const m = re.exec(v);
    if (m) return f(...(m as unknown as string[]));
  }
  return v
    .replace(/（([^（）]*)）/g, ' ($1)')
    .replace(/第 (\d+)–(\d+) 行/g, (_m, a, b) => t('meeting.ai.step.lines', { a, b }))
    .replace(/从第 (\d+) 行起/g, (_m, a) => t('meeting.ai.step.from_line', { a }))
    .replace(/^(?:文档|论文)全文/, t('meeting.ai.step.full_paper'))
    .replace(/^(?:文档|论文)目录/, t('meeting.ai.step.outline'))
    .replace(/^全文$/, t('meeting.ai.step.whole_file'))
    .replace(/，重点 /g, t('meeting.ai.step.focus'));
}
