// THE LIST: every screen / state the audit visits. To audit a new screen (the onboarding wizard, the meeting lobby…)
// add one entry: a name, how to get there from a fresh window, the selector that says it's there, and (optionally)
// a fixture tweak and the state changes to watch for layout shifts. See README.md.
//
// Each entry runs in both languages; each run is checked at every size × theme (lib/audit.ts SIZES, THEMES), and its
// transitions run at SHIFT_SIZES. `world` starts as fixtures/world.ts baseWorld(lang, stress): a live meeting with
// three people, a transcript, a task board, six past meetings, a DeepSeek model source.
import type { Page } from '@playwright/test';
import { chatLive, chatPast, ended, firstRun, fresh, idle, isoAgo, noPaper, NOW_S, paperMissing, processing, STRESS, withCarry, withComments, withVersions, type Lang, type World } from './fixtures/world';
import type { Summary, UpdateStatus } from '../src/meetingApi';
import type { UpdWorld } from './lib/mock-backend';
import { PAPER_LIVE_SCREENS } from './paper-live';

export interface Ctx {
  page: Page;
  lang: Lang;
  /** the mocked backend's state: change it, then waitPoll() (main window) or bumpRelay() (guest page) */
  world: World;
  /** push one message on a server-sent-events stream ('mic' = /api/mic/stream, 'brain' = /api/brain/stream) */
  push: (stream: 'mic' | 'brain', data: unknown) => Promise<void>;
  /** wait for the app's next GET /api/current (it polls every 1.5 s) and let the page settle */
  waitPoll: () => Promise<void>;
  /** the guest page: tell its next relay poll that the state (and the transcript) changed, and wait for it */
  bumpRelay: (lines?: boolean) => Promise<void>;
  settle: (ms?: number) => Promise<void>;
  /** "中文 / English": the fixture's own words in the run's language */
  T: (zh: string, en: string) => string;
}

export interface Transition {
  name: string;
  /** what the user (or the backend) does */
  act: (c: Ctx) => Promise<void>;
  /** selectors that are *supposed* to change (they and their insides are not reported as shifted) */
  expect?: string[];
  /** what a human should know when reading a finding for this transition */
  note?: string;
  /** the movement is a decision on record (say whose / where): its shifts are listed as info, not as problems */
  byDesign?: string;
}

export interface Screen {
  name: string;
  /** one line for the report */
  what: string;
  page: 'main' | 'guest';
  /** the longest names / titles / model names, many people, a very long transcript line */
  stress?: boolean;
  world?: (w: World) => World | void;
  /** from a freshly loaded window to the state (clicks, hovers, typing) */
  go?: (c: Ctx) => Promise<void>;
  /** visible once the state is reached */
  ready: string;
  /** done again after every resize / theme switch (a hover card closes when the window changes) */
  reapply?: (c: Ctx) => Promise<void>;
  transitions?: Transition[];
  /** selectors left out of the geometry checks */
  ignore?: string[];
  /** only check inside this element (a modal over the page: what's behind it is checked by its own screens) */
  scope?: string;
  /** this state refuses network on purpose (backend down): failed requests are not findings */
  expectNetErrors?: boolean;
  /** only these sizes (e.g. a hover card); default: all */
  sizes?: string[];
}

// ------------------------------------------------------------------------------------------------ helpers
/** 大厅：有新版本（GET /api/update，lib/mock-backend.ts）。失败的原因是更新日志的最后一行，算照原样显示的内容
 *  （太长时省略号） */
const withUpdate = (u: Partial<UpdateStatus>) => (w: World): World => {
  idle(w);
  (w as UpdWorld).update = { state: 'available', current: 'v0.6.1', latest: 'v0.6.2', busy: null, ...u };
  if (u.error) w.userStrings.push(u.error);
  return w;
};
/** in a meeting: the host types a line 20 s after saying one, then a guest types one (the box under the transcript) */
const withTyped = (w: World): World => {
  const T = (zh: string, en: string) => (w.lang === 'zh' ? zh : en);
  const c = w.current;
  const guest = c.participants!.find((p) => p.role === 'guest')?.name ?? T('参会者', 'Guest');
  const host = c.transcript.filter((l) => l.who === 'teacher').at(-1)!;
  const mine = T('表 3 的单位写错了，应该是毫秒。', 'The unit in table 3 is wrong: it should be milliseconds.');
  const theirs = T('附录 B 的公式编号也要顺一下。', 'The equation numbers in appendix B need fixing too.');
  c.transcript.push(
    { t: fmtSec(host.sec + 20), sec: host.sec + 20, who: 'teacher', text: mine, typed: true },
    { t: fmtSec(host.sec + 31), sec: host.sec + 31, who: 'guest', name: guest, text: theirs, typed: true },
  );
  c.partial = null;
  w.userStrings.push(mine, theirs);
  return w;
};
const fmtSec = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
/** Claude Code's own login (Settings → Model source): its models as /api/models has them, the conversation model with
 *  one choice; nothing picked in the lobby's world otherwise */
const withClaude = (w: World): World => {
  idle(w);
  Object.assign(w.provider, { kind: 'claude', base_url: '', fast: '', main: '', key_set: false, key_tail: '', active: false, label: '' });
  w.models = {
    dialog: { current: 'claude-haiku-5-5', options: [{ id: 'claude-haiku-5-5', label: 'Claude Haiku 5.5' },
      { id: 'claude-sonnet-5-5', label: 'Claude Sonnet 5.5' }, { id: 'claude-opus-5-5', label: 'Claude Opus 5.5' }] },
    edit: { current: 'claude-opus-5-5', options: [{ id: 'claude-opus-5-5', label: 'Claude Opus 5.5' }, { id: 'claude-opus-4-6[1m]', label: 'Claude Opus 4.6' }] },
    effort: { current: 'xhigh', options: [['low', 'Low'], ['medium', 'Medium'], ['high', 'High'], ['xhigh', 'Extra'], ['max', 'Max']].map(([id, label]) => ({ id, label })) },
    tts: 'CosyVoice 3',
    asr: 'Fun-ASR Nano',
  };
  w.current.ready = { ...w.current.ready!, models: { fast: 'Claude Haiku 5.5', hands: 'Claude Opus 5.5 · Extra', tts: 'CosyVoice 3', asr: 'Fun-ASR Nano' } };
  w.usageMonth = { ...w.usageMonth, mode: 'claude', models: [], tokens: 0, costs: {} };
  w.userStrings.push('Claude Haiku 5.5', 'Claude Sonnet 5.5', 'Claude Opus 5.5', 'Claude Opus 4.6');
  return w;
};
/** Settings → Speech → Microphone picked a USB microphone that isn't plugged in now */
const withMicMissing = (w: World): World => {
  const name = w.stress ? 'USB Microphone (Blue Yeti X Professional Condenser for Streaming and Podcasting)' : 'USB Microphone (Blue Yeti)';
  Object.assign(w.micChoice, { mic: name, missing: true });
  Object.assign(w.devices, { mic: name, missing: true });
  w.current.ready = { ...w.current.ready!, devices: { ...w.current.ready!.devices, mic: name, missing: true } };
  w.userStrings.push(name);
  return w;
};
const goHistory = async (c: Ctx) => {
  await c.page.getByTestId('nav-history').click();
  await c.page.getByTestId('history-preview').waitFor();
};
const goSettings = async (c: Ctx) => {
  await c.page.getByTestId('nav-settings').click();
  await c.page.getByTestId('settings').waitFor();
};
const hover = (testid: string) => async (c: Ctx) => {
  // move away first: re-entering is what opens the card
  await c.page.mouse.move(2, 2);
  await c.page.getByTestId(testid).first().hover();
  await c.settle(200);
};
const openMeeting = async (c: Ctx) => {
  await goHistory(c);
  await c.page.getByTestId('history-open').click();
  await c.page.locator('[data-testid="meeting-history"] ~ *, .mt-hlayer:not(.is-off) [data-testid="meeting"]').first().waitFor();
};
const scrollTo = (sel: string) => async (c: Ctx) => {
  await c.page.locator(sel).first().evaluate((el) => el.scrollIntoView({ block: 'start' }));
  await c.settle(150);
};
const joinGuest = async (c: Ctx) => {
  await c.page.getByTestId('guest-name').fill(c.world.stress ? 'Alexandra Konstantinopoulou-Wang' : c.T('赵同学', 'Sam Zhao'));
  await c.page.getByTestId('guest-go').click();
  await c.page.getByTestId('meeting').waitFor();
};
/** the guest who joins (joinGuest's name) is a co-host: the host set them so in Bloom (the relay state says so) */
const asCohost = (w: World) => {
  const name = w.stress ? 'Alexandra Konstantinopoulou-Wang' : w.lang === 'zh' ? '赵同学' : 'Sam Zhao';
  const ps = w.relay.state!.participants ?? [];
  const hit = ps.find((p) => p.name === name);
  if (hit) hit.rank = 'cohost';
  else ps.push({ id: 'g-co', name, role: 'guest', rank: 'cohost', color: '#22d3ee' });
  w.relay.state!.participants = ps;
};
const brainWith = (c: Ctx, patch: Record<string, unknown>) => ({ ...c.world.current.brain, now: NOW_S, ...patch });
/** 2026-11-21: a big revision under way (全文去掉多余冒号分号: 41 places in 7 minutes) while the talk goes on. The bubble
 *  that asked for it is far up the column; the AI strip above the transcript says how far it is, all of it on hover
 *  (MeetingLayout's ClaudeNow, claudeNow.ts). The worker's steps come in Chinese as the backend writes them
 *  ("在 3.2 节改第 12 处"; an English meeting translates them) */
const LONG_SECS = ['2.2', '3.1', '3.2', '4.1', '5.5', 'F'];
const longSec = (i: number) => LONG_SECS[Math.min(LONG_SECS.length - 1, Math.floor(i / 7))];
const longLog = (n: number) => Array.from({ length: n }, (_, i) => `在 ${longSec(i)} 节改第 ${i + 1} 处`);
const longRow = (w: World, n: number) => ({
  id: 'T2', idx: 2, title: w.current.board!.tasks.find((x) => x.id === 'T2')!.title, started: NOW_S - 425, edits: n, log: longLog(n),
});
const withLongEdit = (w: World): World => {
  const c = w.current;
  const T = (zh: string, en: string) => (w.lang === 'zh' ? zh : en);
  const title = w.stress
    ? T('全文扫冒号分号只换标点，结论段整段跳过，图注表注、符号表、参考文献和公式里的冒号保留', 'Colons and semicolons throughout: punctuation only, skip the conclusion, keep those in captions, the symbol table, references and formulas')
    : T('全文去掉多余冒号分号，跳过引言、摘要、2.1 和结论', 'Remove extra colons and semicolons, except the introduction, abstract, 2.1 and conclusion');
  const t2 = c.board!.tasks.find((x) => x.id === 'T2')!;
  Object.assign(t2, { title, section: null, created: isoAgo(427), started: isoAgo(425), progress: { edits: 41, started: NOW_S - 425, updated: NOW_S - 3, log: longLog(41) } });
  const g1 = c.participants!.find((p) => p.role === 'guest')?.name ?? 'Guest';
  const fast = c.brain?.front_model;
  const at = (sec: number) => `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
  const say = (sec: number, who: 'teacher' | 'student' | 'guest', zh: string, en: string, x: Record<string, unknown> = {}) => ({
    t: at(sec), sec, who, ...(who === 'guest' ? { name: g1 } : {}), ...(who === 'student' ? { tag: '#chat', lat: 1.0, model: fast } : {}), text: T(zh, en), ...x,
  });
  c.transcript = [
    ...c.transcript.filter((l) => l.sec < 1198),
    say(798, 'teacher', '全文扫一遍，把不该有的冒号和分号拆掉，引言、摘要、2.1 节和结论除外。', 'Go through the whole paper and take out the colons and semicolons that shouldn’t be there, except the introduction, abstract, 2.1 and conclusion.'),
    say(805, 'student', '好，全文扫一遍，引言、摘要、2.1 和结论不动。我记下了，交给后台改。', 'Sure, the whole paper, leaving the introduction, abstract, 2.1 and conclusion alone. Noted, the background will do it.', { tag: '#edit', lat: 1.1, title }),
    say(838, 'guest', '3.2 节的数据划分是按时间切的吗？', 'Is the data split in 3.2 by time?'),
    say(846, 'student', '是按时间切的，训练集到 2023 年底，之后是测试集。', 'Yes, by time: training up to the end of 2023, testing after that.'),
    say(910, 'teacher', '那这一点要在 3.2 节写清楚。', 'Then 3.2 has to say so.'),
    say(940, 'guest', '表 3 的单位也要统一一下。', 'Table 3 needs the same units too.'),
    say(982, 'teacher', '对，表 3 和图 3 用同一套单位。', 'Right, table 3 and figure 3 in the same units.'),
    say(1025, 'student', '好，等冒号分号那条改完我再一起看表 3。', 'OK, I’ll look at table 3 once the colons and semicolons are done.'),
    say(1092, 'guest', '消融实验会后补吗？', 'Is the ablation for after the meeting?'),
    say(1110, 'teacher', '会后补，这次先把正文理顺。', 'After the meeting. Today we get the text in order.'),
    say(1160, 'guest', '引言最后一段我觉得也可以短一点。', 'I think the last paragraph of the introduction could be shorter too.'),
    say(1172, 'teacher', '那个下次再说。现在改到哪里了？', 'Next time. How far has it got?'),
    say(1180, 'student', '冒号分号那条改到附录 F 了，已经改了 41 处。', 'The colons and semicolons are at appendix F now: 41 places so far.', { tag: '#status' }),
    say(1220, 'teacher', '好，第三节的实验设置我们再看一下，尤其是数据划分。', "Good. Let's look at the setup in section 3 again, the data split in particular."),
  ] as typeof c.transcript;
  c.brain = { ...c.brain!, edits: [longRow(w, 41)], queued: 1 };
  // the title and the step are what the board and the paper say: theirs, cut with "…" on purpose (whole on hover)
  w.userStrings.push(title, ...longLog(45), ...longLog(45).map((_, i) => `edit ${i + 1} in “${longSec(i)}”`));
  return w;
};
/** nothing being revised, three waiting their turn */
const withQueueOnly = (w: World): World => {
  withLongEdit(w);
  const c = w.current;
  const t2 = c.board!.tasks.find((x) => x.id === 'T2')!;
  Object.assign(t2, { status: 'queued', started: null, progress: null });
  c.board!.counts = { ...c.board!.counts, working: 0, queued: 3 };
  c.brain = { ...c.brain!, edits: [], queued: 3 };
  c.ready = { ...c.ready!, working: 0, queued: 3 };
  return w;
};
/** only the AI strip's words change: the strip keeps its place and size */
const STRIP_WORDS = ['[data-testid="claude-now"] .mt-now-row *'];
/** a new line: the lines move up as always (turns, marks, system lines) */
const LINES_MOVE =['.mt-right-n', '[data-testid="transcript"] section', '[data-testid="transcript"] section *', '[data-testid="transcript-mark"]',
  '[data-testid="transcript-mark"] *', '[data-testid="transcript"] .mt-sysline'];
const newLine = async (c: Ctx) => {
  c.world.current.transcript.push({ t: '20:31', sec: 1231, who: 'teacher', text: c.T('那第三节的实验设置我们再统一一下表述。', "So let's word the setup in section 3 the same way throughout.") });
  await c.waitPoll();
};
/** a big revision starts, goes on and ends while the talk goes on: only the strip's words change, and nothing shows up
 *  at (or goes from) the transcript's foot */
const longEditTransitions: Transition[] = [
  {
    name: 'the revision goes on (its count, step and time change)',
    act: (c) => c.push('brain', brainWith(c, { edits: [longRow(c.world, 42)], queued: 1 })),
    expect: STRIP_WORDS,
    note: 'only the words in the AI strip change: the strip and the transcript keep their places',
  },
  {
    name: 'it finishes, the next one waits its turn',
    act: (c) => c.push('brain', brainWith(c, { edits: [], queued: 1 })),
    expect: STRIP_WORDS,
  },
  {
    name: 'the queue empties',
    act: (c) => c.push('brain', brainWith(c, { edits: [], queued: 0 })),
    expect: STRIP_WORDS,
    note: 'nothing at the foot of the transcript moves',
  },
  {
    name: 'the next revision starts',
    act: (c) => {
      const t3 = c.world.current.board!.tasks.find((x) => x.id === 'T3')!;
      return c.push('brain', brainWith(c, { edits: [{ id: 'T3', idx: 3, title: t3.title, started: NOW_S - 2, edits: 0, log: ['读文档'] }], queued: 0 }));
    },
    expect: STRIP_WORDS,
  },
  {
    name: 'a new line comes in',
    act: newLine,
    expect: [...STRIP_WORDS, ...LINES_MOVE],
    note: 'the lines move up as always',
  },
];
/** scrolled up (by the wheel, as a person does: a scroll nobody asked for is pinned back) to the bubble that asked for
 *  the big revision: its own step list */
const toLiveEdit = async (c: Ctx) => {
  const box = c.page.getByTestId('transcript');
  await box.hover({ position: { x: 60, y: 60 } });
  for (let i = 0; i < 40; i++) {
    const r = await c.page.getByTestId('live-edit').first().boundingBox();
    const b = await box.boundingBox();
    if (r && b && r.y >= b.y + 40) break;
    await c.page.mouse.wheel(0, -160);
    await c.settle(60);
  }
  await c.settle(250);
};
/** a point on the AI strip: its first row (the front's words) or its second (the background's), a share of its width */
const stripAt = async (c: Ctx, row: 1 | 2, fx = 0.6) => {
  const b = (await c.page.getByTestId('claude-now').first().boundingBox())!;
  return { x: b.x + b.width * fx, y: b.y + (row === 1 ? b.height * 0.3 : b.height * 0.72) };
};
/** pointing at the AI strip (anywhere on it: the strip itself is the target): the whole status in the window's own
 *  tip (Tooltip.tsx), one part a line */
const openNowTip = async (c: Ctx) => {
  await c.page.mouse.move(2, 2);
  const p = await stripAt(c, 1);
  await c.page.mouse.move(p.x, p.y);
  await c.page.locator('.mw-tip.is-lines').waitFor();
  await c.settle(150);
};
const tipWords = (c: Ctx) => c.page.locator('.mw-tip').textContent({ timeout: 800 }).catch(() => null);
const tipBox = (c: Ctx) => c.page.locator('.mw-tip').boundingBox({ timeout: 800 }).catch(() => null);
const sameBox = (a: { width: number; height: number } | null, b: { width: number; height: number } | null) =>
  !!a && !!b && Math.abs(a.width - b.width) < 0.5 && Math.abs(a.height - b.height) < 0.5;
const tipTransitions: Transition[] = [
  {
    name: 'the revision goes on while the tip is open (it keeps its words and size)',
    act: async (c) => {
      const before = await tipWords(c);
      const box = await tipBox(c);
      await c.push('brain', brainWith(c, { edits: [longRow(c.world, 42)], queued: 1 }));
      await c.settle(1300);
      const after = await tipWords(c);
      if (after == null) throw new Error('the tip closed when the progress changed');
      if (after !== before) throw new Error(`the tip's words changed under the pointer: "${before}" → "${after}"`);
      if (!sameBox(box, await tipBox(c))) throw new Error("the tip's size changed under the pointer");
    },
    expect: [...STRIP_WORDS, '.mw-tip'],
  },
  {
    name: 'the pointer moves along the strip to the background row (the same tip, it doesn\'t close and open again)',
    act: async (c) => {
      const before = await tipWords(c);
      const p = await stripAt(c, 2, 0.75);
      await c.page.mouse.move(p.x, p.y, { steps: 6 });
      await c.settle(120);
      const after = await tipWords(c);
      if (after == null) throw new Error('the tip closed while the pointer moved along the strip');
      if (after !== before) throw new Error(`another tip opened on the strip: "${before}" → "${after}"`);
    },
    expect: ['.mw-tip'],
  },
  {
    name: 'a new line comes in while the tip is open (the transcript scrolls itself; the tip stays)',
    act: async (c) => {
      await newLine(c);
      if ((await tipWords(c)) == null) throw new Error('the tip closed when the transcript scrolled to the new line');
    },
    expect: [...STRIP_WORDS, ...LINES_MOVE, '.mw-tip'],
  },
];
/** everything at once in the background: two lookups (one found, not said yet), two revisions, two waiting on their
 *  document, three queued, notes for after the meeting: the tip has a line for each */
const withBusyBack = (w: World): World => {
  withLongEdit(w);
  const c = w.current;
  const T = (zh: string, en: string) => (w.lang === 'zh' ? zh : en);
  const t3 = c.board!.tasks.find((x) => x.id === 'T3')!;
  Object.assign(t3, { status: 'working', started: isoAgo(50), progress: { edits: 3, started: NOW_S - 50, updated: NOW_S - 4, log: ['读文档', '在 4.3 节改第 3 处'] } });
  const q1 = w.stress
    ? T('TSGBench 的问题定义是怎么写的，和我们 2.1 节的写法比一下哪个更清楚，顺便看看它有没有把生成和预测分开说', 'how TSGBench words its problem definition, compared with our 2.1, and whether it keeps generation and forecasting apart')
    : T('TSGBench 的问题定义是怎么写的', 'how TSGBench words its problem definition');
  const topic1 = T('问题定义写法', 'problem definition wording');
  const step1 = w.stress ? 'TSGBench: A Benchmark for Time Series Generation — problem definition and evaluation protocol' : 'TSGBench problem definition';
  c.brain = {
    ...c.brain!,
    front: 'thinking',
    deeps: [
      { id: 1, q: q1, topic: topic1, secs: 14, t0: NOW_S - 14, last: NOW_S - 2, sec0: 1210, state: 'running', steps: [{ k: '读文档' }, { k: '联网搜索', v: step1 }], model: c.brain?.deep_model },
      { id: 2, q: T('FinTS 在表 3 里出现了几次', 'how often FinTS appears in table 3'), topic: T('FinTS 次数', 'FinTS count'), secs: 6, t0: NOW_S - 6, last: NOW_S - 1, sec0: 1220, state: 'done', steps: [{ k: '读文档' }], model: c.brain?.deep_model, quick: true },
    ],
    edits: [longRow(w, 41), { id: 'T3', idx: 3, title: t3.title, started: NOW_S - 50, edits: 3, log: ['读文档', '在 4.3 节改第 3 处'] }],
    waiting: 2,
    queued: 3,
    unspoken: 1,
  };
  w.userStrings.push(q1, topic1, step1, T('FinTS 次数', 'FinTS count'), t3.title);
  return w;
};
/** the tip says every part, one a line: each revision with its step, each lookup with its question */
const tipHasAll = async (c: Ctx) => {
  const lines = await c.page.locator('.mw-tip .mw-tip-l').allTextContents();
  const want = [c.T('在 F 节改第 41 处', 'Edit 41 in “F”'), c.T('在 4.3 节改第 3 处', 'Edit 3 in “4.3”'), c.T('问题定义写法', 'problem definition wording'),
    c.T('2 条等文档识别完', '2 waiting on the paper'), c.T('3 条排队中', '3 more queued')];
  for (const w of want) if (!lines.some((l) => l.includes(w))) throw new Error(`the tip doesn't say "${w}": ${JSON.stringify(lines)}`);
  if (lines.length !== 8) throw new Error(`the tip has ${lines.length} lines, not one a part (8): ${JSON.stringify(lines)}`);
};
const openNotes = async (c: Ctx) => {
  await openMeeting(c);
  await c.page.getByTestId('meeting-tab-notes').click();
  await c.page.locator('.mt-hlayer:not(.is-off) .mt-sum-title').first().waitFor();
};
/** the 纪要 as older meetings saved it: plain strings, no times, no one-per-person part, no format */
const oldSummary = (w: World): Summary => {
  const T = (zh: string, en: string) => (w.lang === 'zh' ? zh : en);
  return {
    title: T('FooBench 论文合稿 · 组会纪要', 'FooBench paper draft · meeting notes'),
    speakers: w.details['m-001'].summary!.speakers,
    abstract: w.details['m-001'].summary!.abstract,
    conclusions: [T('表 2 补上 TimeGAN', 'Add TimeGAN to table 2'), T('消融实验会后补', 'Ablation after the meeting')],
    sections: [{ title: T('实验设置', 'Experimental setup'), cards: [{ title: T('基线', 'Baselines'), bullets: [T('表 2 少了 TimeGAN，已补', 'Table 2 missed TimeGAN: added')] }] }],
    todos: [T('图 3 坐标轴单位统一', 'Same axis units in figure 3')],
  };
};
const askPast = async (c: Ctx) => {
  await goHistory(c);
  await c.page.getByTestId('history-ask').click();
  const q = c.world.stress ? '问'.repeat(300) : c.T('上次老师对第三章的消融实验怎么说的？', 'What was said about the chapter 3 ablation last time?');
  await c.page.getByTestId('history-ask-input').fill(q);
  await c.page.getByTestId('history-ask-go').click();
};
const goSummarySettings = async (c: Ctx) => {
  await goSettings(c);
  await scrollTo('[data-setting="general.summary"]')(c);
};
/** 第 6 批: the paper menu → 历史版本 */
const openPaperHistory = async (c: Ctx) => {
  // in a meeting: the paper menu → 历史版本; after it (the lobby): 历史版本 beside the paper
  const lobby = c.page.getByTestId('lobby-paper-history');
  if (await lobby.isVisible().catch(() => false)) await lobby.click();
  else {
    await c.page.getByTestId('paper-menu').click();
    await c.page.getByTestId('paper-history').click();
  }
  await c.page.locator('.ph-row').first().waitFor();
  await c.settle(400);
};
/** 第 5 批: 回顾's 保存 menu open on the first meeting */
const openShareMenu = async (c: Ctx) => {
  await goHistory(c);
  await c.page.getByTestId('history-share').click();
  await c.page.getByTestId('history-share-menu').waitFor();
};
const goAutosaveSettings = async (c: Ctx) => {
  await goSettings(c);
  await scrollTo('[data-setting="general.autosave"]')(c);
};

/** the lobby with 16 left-over items: more than one page at every window size */
const withCarryPages = (w: World): World => {
  withCarry(w);
  const base = w.carry;
  w.carry = Array.from({ length: 16 }, (_, i) => {
    const x = base[i % base.length];
    return { ...x, id: `${x.id}-${i}`, text: x.text + (i >= base.length ? ` (${i + 1})` : '') };
  });
  w.userStrings.push(...w.carry.map((x) => x.text));
  return w;
};
/** the left-over list's pager ("‹ 1/2 ›"): both arrows the same size and stroke in the same colour, the one that can't
 *  go on only fainter and still plain to see, the same room either side of the page number */
const pagerEven = async (c: Ctx) => {
  const m = await c.page.locator('.lb-pager').first().evaluate((p) => {
    const [a, n, b] = [...p.children] as HTMLElement[];
    const r = (e: Element) => e.getBoundingClientRect();
    const svg = (e: Element) => e.querySelector('svg')!;
    const cs = (e: Element) => getComputedStyle(e);
    const range = document.createRange();
    range.selectNodeContents(n);
    const tr = range.getBoundingClientRect();
    return {
      btn: [r(a).width, r(a).height, r(b).width, r(b).height],
      icon: [r(svg(a)).width, r(svg(a)).height, r(svg(b)).width, r(svg(b)).height],
      stroke: [cs(svg(a)).strokeWidth, cs(svg(b)).strokeWidth],
      color: [cs(a).color, cs(b).color],
      opacity: [Number(cs(a).opacity), Number(cs(b).opacity)],
      disabled: [(a as HTMLButtonElement).disabled, (b as HTMLButtonElement).disabled],
      gaps: [r(n).left - r(a).right, r(b).left - r(n).right],
      text: [tr.left - r(n).left, r(n).right - tr.right],
    };
  });
  const near = (x: number, y: number, d = 0.6) => Math.abs(x - y) <= d;
  const bad: string[] = [];
  if (!near(m.btn[0], m.btn[2]) || !near(m.btn[1], m.btn[3])) bad.push(`buttons ${m.btn.join(',')}`);
  if (!near(m.icon[0], m.icon[2]) || !near(m.icon[1], m.icon[3])) bad.push(`arrows ${m.icon.join(',')}`);
  if (m.stroke[0] !== m.stroke[1]) bad.push(`stroke ${m.stroke.join(' / ')}`);
  if (m.color[0] !== m.color[1]) bad.push(`colour ${m.color.join(' / ')}`);
  for (let i = 0; i < 2; i++) if (m.opacity[i] < (m.disabled[i] ? 0.35 : 0.99)) bad.push(`opacity ${m.opacity[i]} (disabled ${m.disabled[i]})`);
  if (!near(m.gaps[0], m.gaps[1])) bad.push(`gaps either side of the number ${m.gaps.join(' / ')}`);
  if (!near(m.text[0], m.text[1], 1)) bad.push(`the number off centre ${m.text.join(' / ')}`);
  if (bad.length) throw new Error(`the pager isn't even: ${bad.join('; ')}`);
};

/** the state changes on the live meeting page that must not move anything else */
const liveTransitions: Transition[] = [
  {
    name: 'mic-muted (push)',
    act: (c) => c.push('mic', { muted: true, talk: [] }),
    expect: ['[data-testid="mic-muted"]', '.mt-mic-off', '[data-testid="mic-mute"]'],
    note: 'the backend says the host muted (e.g. by voice): the crossed-out mic appears on the avatar',
  },
  {
    name: 'mic-unmuted (push)',
    act: (c) => c.push('mic', { muted: false, talk: [] }),
    expect: ['[data-testid="mic-muted"]', '.mt-mic-off', '[data-testid="mic-mute"]'],
  },
  {
    name: 'speaking ring',
    act: (c) => c.push('mic', { muted: false, talk: ['me', 'claude'] }),
    note: 'the host and the AI talk: rings around their avatars',
  },
  {
    name: 'ai thinking',
    act: (c) => c.push('brain', brainWith(c, { front: 'thinking' })),
    expect: ['[data-testid="claude-now"] .mt-now-row *'],
  },
  {
    name: 'ai speaking + two lookups',
    act: (c) =>
      c.push(
        'brain',
        brainWith(c, {
          front: 'speaking',
          deeps: [
            { id: 1, q: c.T('TSGBench 的问题定义是怎么写的', 'how TSGBench words its problem definition'), topic: c.T('问题定义写法', 'problem definition wording'), secs: 14, t0: NOW_S - 14, last: NOW_S - 2, sec0: 1210, state: 'running', steps: [{ k: c.T('搜论文', 'Searched the paper'), v: 'problem definition' }], model: c.world.current.brain?.deep_model },
            { id: 2, q: c.T('FinTS 在表 3 里出现了几次', 'how often FinTS appears in table 3'), topic: c.T('FinTS 次数', 'FinTS count'), secs: 6, t0: NOW_S - 6, last: NOW_S - 1, sec0: 1220, state: 'running', steps: [], model: c.world.current.brain?.deep_model, quick: true },
          ],
          queued: 2,
          unspoken: 1,
        }),
      ),
    expect: ['[data-testid="claude-now"] .mt-now-row *', '[data-testid="live-deep"]', '[data-testid="transcript"] *'],
    note: 'the strip above the transcript gets longer words; the lookups hang under the transcript (lines inside the list may move; the list itself and the strip must not)',
  },
  {
    name: 'ai status busy',
    act: async (c) => {
      c.world.current.ready = { ...c.world.current.ready!, working: 2, queued: 3, fast_online: false };
      await c.waitPoll();
    },
    expect: ['.mt-status-dot'],
    note: "the AI's status dot changes colour (one part offline, two edits running)",
  },
  {
    name: 'new transcript line',
    act: async (c) => {
      const t = c.world.current.transcript;
      t.push({ t: '20:31', sec: 1231, who: 'teacher', text: c.T('那第三节的实验设置我们再统一一下表述。', "So let's word the setup in section 3 the same way throughout.") });
      c.world.current.partial = { text: c.T('还有图 3', 'And figure 3'), t: '20:33' };
      await c.waitPoll();
    },
    expect: ['.mt-right-n', '[data-testid="transcript"] *'],
  },
  {
    name: 'co-host reminder appears',
    act: async (c) => {
      const g = c.world.current.participants!.find((p) => p.role === 'guest');
      c.world.current.role_suggest = [{ name: g?.name ?? 'Emma' }];
      await c.waitPoll();
    },
    expect: ['[data-testid="role-suggest"]'],
    note: '"X was a co-host last time · Restore" shows in the header',
  },
  {
    name: 'guest joins (3 → 4 people)',
    act: async (c) => {
      const ps = c.world.current.participants!;
      ps.push({ id: 'g9', name: c.T('新来的同学', 'New Student'), role: 'guest', rank: 'participant', color: '#22d3ee' });
      await c.waitPoll();
    },
    expect: ['[data-testid="meeting-people"]'],
    note: 'more than three people fold into a stack of avatars',
  },
  {
    name: 'error notice appears',
    act: async (c) => {
      // outside the app (a plain browser) 散会 answers "only in the app": a real notice, shown the same way as any error
      await c.page.getByTestId('meeting-end').click();
    },
    expect: ['.mt-notice'],
    note: 'an error line appears between the header and the columns',
  },
  {
    name: 'meeting ends',
    act: async (c) => {
      c.world.current.meeting = { ...c.world.current.meeting!, state: 'ended', ended: isoAgo(0) };
      c.world.current.brain = null;
      c.world.current.partial = null;
      await c.waitPoll();
    },
    expect: ['.mt-head-acts', '.mt-acts-slot', '[data-testid="claude-now"] *', '.mt-meta', '.mt-state', '.mt-notice', '[data-testid="transcript"] *'],
    note: 'the end button turns into 新会议; the strip says "no meeting" (owner, 2026-11-18: nothing in the bar may move)',
  },
];

/** 会中交互 (第 3 批): what the meeting's tools do, none of which may move anything else */
const toolTransitions: Transition[] = [
  {
    name: 'type a 500-character question',
    act: async (c) => {
      const q = (c.lang === 'zh' ? '第三节的实验设置怎么改？' : 'How should section 3 change? ').repeat(60).slice(0, 500);
      c.world.userStrings.push(q.trim());
      await c.page.getByTestId('ask-input').fill(q);
    },
    expect: ['[data-testid="ask-input"]', '[data-testid="ask-send"] *'],
  },
  {
    name: 'send it (the line comes in, marked typed)',
    act: async (c) => {
      await c.page.getByTestId('ask-send').click();
      await c.waitPoll();
    },
    expect: ['[data-testid="ask-input"]', '[data-testid="ask-send"] *', '.mt-right-n', '[data-testid="transcript"] *'],
  },
  {
    name: 'mark this moment (a note card over the box; the mark joins the transcript)',
    act: async (c) => {
      await c.page.getByTestId('bookmark').click();
      await c.page.getByTestId('bookmark-note').waitFor();
      await c.waitPoll();
    },
    expect: ['[data-testid="bookmark-note"]', '[data-testid="bookmark-note"] *', '[data-testid="transcript"] *', '.mt-right-n'],
  },
  {
    name: 'save a 100-character note',
    act: async (c) => {
      c.world.userStrings.push('注'.repeat(100));
      await c.page.getByTestId('bookmark-note-input').fill('注'.repeat(100));
      await c.page.getByTestId('bookmark-note-save').click();
      await c.waitPoll();
    },
    expect: ['[data-testid="bookmark-note"]', '[data-testid="transcript"] *'],
  },
  {
    name: 'a passage is selected in the paper (its chip floats over the box)',
    act: async (c) => {
      c.world.current.anchor = { text: c.T('我们提出了一种全新的方法。', 'We propose an entirely new method.'), section: '1', by: null, at: NOW_S, left_s: 55 };
      await c.waitPoll();
    },
    expect: ['[data-testid="anchor-chip"]', '[data-testid="anchor-chip"] *'],
  },
  {
    name: 'nobody has spoken for 10 minutes (the reminder floats over the paper)',
    act: async (c) => {
      c.world.current.quiet_s = 620;
      await c.waitPoll();
    },
    expect: ['[data-testid="quiet-note"]', '[data-testid="quiet-note"] *'],
  },
  {
    name: 'the meeting ends (the box stays, switched off)',
    act: async (c) => {
      c.world.current.meeting = { ...c.world.current.meeting!, state: 'ended', ended: isoAgo(0) };
      c.world.current.brain = null;
      c.world.current.partial = null;
      await c.waitPoll();
    },
    expect: ['.mt-head-acts', '.mt-acts-slot', '[data-testid="claude-now"] *', '.mt-meta', '.mt-state', '[data-testid="transcript"] *', '[data-testid="quiet-note"]', '.mt-compose *'],
    note: 'the box keeps its place and size; what is in it is switched off',
  },
];

/** a meeting without a paper: what comes and goes on its one column must not move anything else */
const chatTransitions: Transition[] = [
  {
    name: 'new transcript line',
    act: async (c) => {
      c.world.current.transcript.push({ t: '20:31', sec: 1231, who: 'teacher', text: c.T('下周把实验数据整理好发群里。', "Next week, put the experiment data in the group chat.") });
      c.world.current.partial = { text: c.T('还有', 'And'), t: '20:33' };
      await c.waitPoll();
    },
    expect: ['.mt-right-n', '[data-testid="transcript"] *'],
  },
  {
    name: 'mark this moment (a note card over the box; the mark joins the transcript)',
    act: async (c) => {
      await c.page.getByTestId('bookmark').click();
      await c.page.getByTestId('bookmark-note').waitFor();
      await c.waitPoll();
    },
    expect: ['[data-testid="bookmark-note"]', '[data-testid="bookmark-note"] *', '[data-testid="transcript"] *', '.mt-right-n'],
  },
  {
    name: 'co-host reminder appears',
    act: async (c) => {
      await c.page.keyboard.press('Escape');
      const g = c.world.current.participants!.find((p) => p.role === 'guest');
      c.world.current.role_suggest = [{ name: g?.name ?? 'Emma' }];
      await c.waitPoll();
    },
    expect: ['[data-testid="role-suggest"]', '[data-testid="bookmark-note"]'],
  },
  {
    name: 'nobody has spoken for 10 minutes (the reminder floats)',
    act: async (c) => {
      c.world.current.quiet_s = 620;
      await c.waitPoll();
    },
    expect: ['[data-testid="quiet-note"]', '[data-testid="quiet-note"] *'],
  },
  {
    name: 'error notice appears',
    act: async (c) => {
      await c.page.getByTestId('meeting-end').click();
    },
    expect: ['.mt-notice'],
    note: 'outside the app 散会 answers "only in the app": the notice shows in the reminders\' slot at the page\'s foot',
  },
  {
    name: 'meeting ends',
    act: async (c) => {
      c.world.current.meeting = { ...c.world.current.meeting!, state: 'ended', ended: isoAgo(0) };
      c.world.current.partial = null;
      await c.waitPoll();
    },
    expect: ['.mt-head-acts', '.mt-acts-slot', '.mt-meta', '.mt-state', '.mt-notice', '[data-testid="transcript"] *', '[data-testid="quiet-note"]', '.mt-compose *'],
    note: '散会 turns into 新会议; the box stays, switched off',
  },
];

// ------------------------------------------------------------------------------------------------ THE LIST
export const SCREENS: Screen[] = [
  // ---------------------------------------------------------------- 会议 (the meeting page)
  {
    name: 'meeting-connecting',
    what: 'Meeting page while the backend is not answering yet ("Connecting…")',
    page: 'main',
    world: (w) => {
      w.backendUp = false;
    },
    ready: '[data-testid="meeting-down"]',
    expectNetErrors: true,
  },
  // ---------------------------------------------------------------- first-run guide (Onboarding.tsx)
  ...(['me', 'model', 'listen', 'voice', 'done'] as const).map(
    (step): Screen => ({
      name: `onboarding-${step}`,
      what: `First-run guide, step "${step}" (fresh install)`,
      page: 'main',
      world: firstRun,
      go: async (c) => {
        await c.page.getByTestId('onboarding').waitFor();
        await c.page.getByTestId(`ob-step-${step}`).click();
      },
      ready: `[data-testid="ob-step-${step}"][aria-current="step"]`,
      scope: '[data-testid="onboarding"]',
    }),
  ),
  {
    name: 'onboarding-done-stress',
    what: 'First-run guide, last step, with the longest names and model names',
    page: 'main',
    stress: true,
    world: (w) => {
      w.setup = { ...w.setup, done: false, skipped: false };
    },
    go: async (c) => {
      await c.page.getByTestId('onboarding').waitFor();
      await c.page.getByTestId('ob-step-done').click();
    },
    ready: '[data-testid="ob-summary"]',
    scope: '[data-testid="onboarding"]',
  },
  // ---------------------------------------------------------------- 会议 before a meeting: the lobby (MeetingLobby.tsx)
  {
    name: 'lobby-fresh',
    what: 'Lobby on a fresh install (guide skipped): no paper, no model source, no speech model',
    page: 'main',
    world: fresh,
    ready: '[data-testid="lobby-pick-paper"]',
    transitions: [
      {
        name: 'download the speech model (need → progress)',
        act: async (c) => {
          await c.page.locator('.lb-need button.link', { hasText: c.T('下载', 'Download') }).click();
          await c.page.locator('.lb-need.is-dl').first().waitFor();
        },
        expect: ['.lb-need', '.lb-needs', '.lb-st', '.lb-need.is-dl *'],
        note: 'the "missing" line under 开始会议 turns into a progress bar',
      },
      {
        name: 'speech model ready (its need goes away)',
        act: async (c) => {
          await c.page.locator('.lb-need.is-dl').first().waitFor({ state: 'detached', timeout: 15_000 });
        },
        expect: ['.lb-needs', '.lb-need', '.lb-st'],
      },
    ],
  },
  {
    name: 'lobby',
    what: 'Lobby, everything ready: a paper on the desk, model source, speech model',
    page: 'main',
    world: idle,
    ready: '[data-testid="lobby-paper"]',
    transitions: [
      {
        name: 'type a 60-char title',
        act: async (c) => {
          await c.page.getByTestId('lobby-title').fill(c.lang === 'zh' ? STRESS.titleZh : STRESS.titleEn);
        },
        expect: ['[data-testid="lobby-title"]'],
      },
      {
        // 准备情况 → 声音: 试听 beside 更换 (the cell keeps its width); the voice row's text says it's playing
        name: 'play the voice (试听; only the voice row’s text changes)',
        act: async (c) => {
          await c.page.getByTestId('lobby-voice-test').click();
          await c.settle(400);
        },
        expect: ['[data-testid="lobby-status"] .lb-st-v', '[data-testid="lobby-status"] .lb-st-v *'],
      },
      {
        // 第 5 批: the last meeting's 散会后自动保存 failed (its folder is gone): said once, in the toast
        name: 'auto-save after the meeting fails (a toast)',
        act: async (c) => {
          c.world.current.autosave = {
            meeting: 'm-001', title: 'FooBench', at: NOW_S - 30 + Math.random(), ok: false, error: 'not_dir', detail: 'D:\\notes',
            dir: 'D:\\notes', files: [], failed: ['*'],
          };
          await c.waitPoll();
          await c.page.getByTestId('toast').waitFor();
        },
        expect: ['[data-testid="toast"]'],
        note: 'the failure floats at the foot of the window; nothing on the lobby moves',
      },
      {
        name: 'remove the paper (back to 未选择; 开始会议 stays on)',
        act: async (c) => {
          await c.page.getByTestId('lobby-clear-paper').click();
          await c.page.getByTestId('lobby-no-paper').waitFor();
          await c.page.locator('[data-testid="lobby-start"]:not([disabled])').waitFor();
        },
        expect: ['.lb-paper', '.lb-paper *', '[data-testid="lobby-title"]', '[data-testid="toast"]'],
        note: 'the paper row keeps its height; 选择 takes 更换\u2019s place at the row\u2019s end (one button, as wide as the longer word)',
      },
    ],
  },
  {
    // every 准备情况 row has 更换 (the voice row 试听 too); the picked microphone isn't plugged in: an amber dot
    name: 'lobby-claude-mic-missing',
    what: 'Lobby with Claude Code’s login (its model names) and a picked microphone that isn’t plugged in',
    page: 'main',
    world: (w) => withMicMissing(withClaude(w)),
    ready: '[data-testid="lobby-change-mic"]',
  },
  {
    name: 'lobby-no-paper',
    what: 'Lobby with no paper picked: 未选择 + 选择 in the paper row, 开始会议 on (that meeting keeps only the transcript)',
    page: 'main',
    world: noPaper,
    go: async (c) => {
      await c.page.getByTestId('lobby-no-paper').waitFor();
    },
    ready: '[data-testid="lobby-start"]:not([disabled])',
    transitions: [
      {
        name: 'a paper comes in (未选择 → its name; nothing else moves)',
        act: async (c) => {
          const T = c.T;
          const name = 'FooBench.md';
          Object.assign(c.world.current, {
            mode: 'paper',
            paper: { path: `C:\\Users\\maya\\Documents\\${name}`, name, exists: true, kind: 'md', words: 8231, id: 'p1', modified: '2026-11-18 09:40:12', bytes: 61234 },
            papers: [{ id: 'p1', name }, { id: 'p2', name: T('申报书.docx', 'proposal.docx') }],
            active_paper: 'p1',
          });
          await c.waitPoll();
          await c.page.getByTestId('lobby-paper').waitFor();
        },
        expect: ['.lb-paper', '.lb-paper *', '[data-testid="lobby-title"]'],
        note: 'the row keeps its height: the name and its line come in, 移除 and 历史版本 to the left of 更换',
      },
    ],
  },
  {
    name: 'lobby-paper-missing',
    what: 'Lobby whose chosen paper file is gone: its name, 文件找不到了 in the warning colour, 开始会议 off, 更换 and 移除',
    page: 'main',
    world: paperMissing,
    go: async (c) => {
      await c.page.getByTestId('lobby-paper-missing').waitFor();
    },
    ready: '[data-testid="lobby-start"][disabled]',
  },
  {
    // a new PDF came in as plain text first; its formulas and tables are still being recognized in the background
    // (zuhui/pdf_full.py): the line under its name says so in place, then goes back to its size and date
    name: 'lobby-paper-recognizing',
    what: 'Lobby right after a new PDF came in: 正在识别公式和表格… in the line under its name, everything else as usual',
    page: 'main',
    world: (w) => {
      idle(w);
      w.current.paper = { ...w.current.paper!, pending_full: true };
      return w;
    },
    go: async (c) => {
      await c.page.getByTestId('lobby-paper-recognizing').waitFor();
    },
    ready: '[data-testid="lobby-paper"]',
    transitions: [
      {
        name: 'recognized (the line goes back to its size and date; nothing moves)',
        act: async (c) => {
          c.world.current.paper = { ...c.world.current.paper!, pending_full: false, words: 9120 };
          await c.waitPoll();
          await c.page.getByTestId('lobby-paper-recognizing').waitFor({ state: 'detached' });
        },
        expect: ['.lb-paper-meta'],
        note: 'the same line, the same height: only its words change',
      },
    ],
  },
  {
    name: 'lobby-no-paper-stress',
    what: 'Lobby with no paper picked, longest names, 40 past meetings',
    page: 'main',
    stress: true,
    world: noPaper,
    go: async (c) => {
      await c.page.getByTestId('lobby-no-paper').waitFor();
    },
    ready: '[data-testid="lobby-start"]:not([disabled])',
  },
  {
    name: 'lobby-stress',
    what: 'Lobby with a 24-char name, long paper name and headings, long model names, 40 past meetings',
    page: 'main',
    stress: true,
    world: idle,
    go: async (c) => {
      await c.page.getByTestId('lobby-paper').waitFor();
      await c.page.getByTestId('lobby-title').fill(c.lang === 'zh' ? STRESS.titleZh : STRESS.titleEn);
    },
    ready: '[data-testid="lobby-paper"]',
  },
  {
    name: 'lobby-carry',
    what: 'Lobby with what the last meetings on this paper left undone (the second tab of the recent card)',
    page: 'main',
    world: withCarry,
    ready: '[data-testid="lobby-carry"]',
    transitions: [
      {
        name: 'tick a left-over item (struck through, the count drops, nothing moves)',
        act: async (c) => {
          await c.page.getByTestId('lobby-carry-tick').first().check();
          await c.page.locator('.lb-carry.is-done').first().waitFor();
        },
        expect: ['.lb-carry.is-done', '.lb-carry.is-done *', '.lb-tab-n'],
      },
      {
        name: 'switch to the recent meetings',
        act: async (c) => {
          await c.page.getByTestId('lobby-tab-recent').click();
          await c.page.getByTestId('lobby-recent').first().waitFor();
        },
        expect: ['.lb-list-box', '.lb-list-box *', '.lb-card-hrow > .link', '.lb-pager', '.lb-tab'],
        note: 'the list box keeps its place and size; only what is in it changes',
      },
    ],
  },
  {
    name: 'lobby-carry-stress',
    what: 'Lobby: 23 long left-over items (pages), long names, 40 past meetings',
    page: 'main',
    stress: true,
    world: withCarry,
    ready: '[data-testid="lobby-carry"]',
    transitions: [
      {
        name: 'next page of left-over items',
        act: async (c) => {
          await c.page.getByTestId('lobby-carry-next').click();
          await c.settle(200);
        },
        expect: ['.lb-list-box *', '.lb-pager-n', '.lb-pager *'],
      },
    ],
  },
  {
    // 2026-11-21 (the user: "大小都不等 怎么老是这种不对称的问题"): the arrow at the end used to be all but invisible
    name: 'lobby-carry-pages',
    what: 'Lobby: 16 left-over items over pages; the pager’s two arrows alike (the one at the end only fainter), the page in the middle',
    page: 'main',
    world: withCarryPages,
    go: async (c) => {
      await c.page.locator('.lb-pager').waitFor();
      await pagerEven(c);
    },
    ready: '[data-testid="lobby-carry"]',
    transitions: [
      {
        name: 'to the last page (the other arrow goes faint; nothing moves)',
        act: async (c) => {
          const next = c.page.getByTestId('lobby-carry-next');
          for (let i = 0; i < 20 && (await next.isEnabled()); i++) await next.click();
          await c.settle(200);
          await pagerEven(c);
        },
        expect: ['.lb-list-box *'],
      },
      {
        name: 'back to the first page',
        act: async (c) => {
          const prev = c.page.getByTestId('lobby-carry-prev');
          for (let i = 0; i < 20 && (await prev.isEnabled()); i++) await prev.click();
          await c.settle(200);
          await pagerEven(c);
        },
        expect: ['.lb-list-box *'],
      },
    ],
  },
  // "准备情况"那一行的新版本（zuhui/updates.py）：有新版 → 正在更新，以及更新失败
  {
    name: 'lobby-update',
    what: 'Lobby: a newer version is out (新版本 0.6.2 + 更新 between 准备情况 and 设置)',
    page: 'main',
    world: withUpdate({ state: 'available' }),
    ready: '[data-testid="lobby-update-go"]',
    transitions: [
      {
        name: 'click 更新 (→ 正在更新; nothing beside it moves)',
        act: async (c) => {
          await c.page.getByTestId('lobby-update-go').click();
          await c.page.locator('.lb-upd .mw-spinner').waitFor();
        },
        expect: ['.lb-upd', '.lb-upd *'],
      },
    ],
  },
  {
    name: 'lobby-updating',
    what: 'Lobby while the update runs (正在更新; Bloom closes and opens again by itself)',
    page: 'main',
    world: withUpdate({ state: 'updating' }),
    ready: '.lb-upd .mw-spinner',
  },
  {
    name: 'lobby-update-failed',
    what: 'Lobby after a failed update: 更新失败：<the update log’s last line> + 重试, long reason cut with "…"',
    page: 'main',
    world: withUpdate({ state: 'failed', error: 'the desktop installer exited with 2: C:\\Users\\maya\\AppData\\Local\\Bloom\\bloom.exe is in use' }),
    ready: '[data-testid="lobby-update-go"]',
  },
  {
    name: 'meeting-live',
    what: 'In a meeting: three people, transcript, the AI strip, a revision under way',
    page: 'main',
    ready: '[data-testid="claude-now"] .mt-now-dot',
    transitions: liveTransitions,
  },
  {
    name: 'meeting-live-stress',
    what: 'In a meeting, longest names (24/32 chars), 60-char title, long model names, 14 people, a very long line',
    page: 'main',
    stress: true,
    ready: '[data-testid="claude-now"] .mt-now-dot',
    transitions: liveTransitions.filter((t) => !/guest joins/.test(t.name)),
  },
  {
    // a PDF added during the meeting came in as plain text first (zuhui/pdf_full.py): its revisions stay queued until
    // the full version replaces it, and the AI strip says how many are waiting for it
    name: 'meeting-waiting-doc',
    what: 'In a meeting: 2 revisions waiting for their document to finish loading, nothing else being edited (AI strip)',
    page: 'main',
    world: (w) => {
      const c = w.current;
      c.brain = { ...c.brain!, edits: [], queued: 0, waiting: 2 };
    },
    go: async (c) => {
      await c.page.getByTestId('claude-now').getByText(c.T('2 条等文档识别完', '2 waiting on the paper')).waitFor();
    },
    ready: '[data-testid="claude-now"] .mt-now-dot',
    transitions: [
      {
        // the longest the row gets: a revision on another paper runs meanwhile, one more queued behind it
        name: 'another revision starts meanwhile (the waiting part comes right after it)',
        act: async (c) => {
          // the same revision the board already shows under its bubble (same steps): only the strip's words change
          const w = c.world.current.board!.tasks.find((x) => x.status === 'working')!;
          const edit = { id: w.id, idx: w.idx, title: w.title, started: w.progress?.started ?? NOW_S - 36, edits: w.progress?.edits ?? 1, log: w.progress?.log };
          await c.push('brain', brainWith(c, { edits: [edit], queued: 1, waiting: 2 }));
          await c.page.locator('[data-testid="claude-now"] .mt-now-v', { hasText: w.title }).waitFor();
        },
        expect: ['[data-testid="claude-now"] .mt-now-row *'],
      },
      {
        name: 'document recognized (the waiting part goes; the strip keeps its size)',
        act: async (c) => {
          await c.push('brain', brainWith(c, { queued: 3, waiting: 0 }));
          await c.page.getByTestId('claude-now').getByText(c.T('2 条等文档识别完', '2 waiting on the paper')).waitFor({ state: 'detached' });
        },
        expect: ['[data-testid="claude-now"] .mt-now-row *'],
        note: 'only the words in the background row change',
      },
    ],
  },
  {
    name: 'meeting-live-empty',
    what: 'A meeting that just started: nobody but host and AI, no lines yet, no board',
    page: 'main',
    world: (w) => {
      const c = w.current;
      c.transcript = [];
      c.partial = null;
      c.participants = c.participants!.filter((p) => p.id === 'me' || p.id === 'claude');
      c.board = { counts: {}, tasks: [] };
      c.brain = { ...c.brain!, edits: [], deeps: [], queued: 0 };
      c.meeting = { ...c.meeting!, started: isoAgo(5), duration_s: 5 };
    },
    ready: '[data-testid="claude-now"] .mt-now-dot',
  },
  {
    // 2026-11-21 (the user: "你这个文字的tag 放在名字旁边吧"): a typed line's "文字" sits by the name, on the time's row;
    // the host's typed line right after a said one (20 s apart) is a turn of its own
    name: 'meeting-typed',
    what: 'In a meeting: the host and a guest typed a line each (the tag by the name, not in the bubble)',
    page: 'main',
    world: withTyped,
    ready: '[data-testid="transcript"] [data-testid="transcript-typed"]',
  },
  {
    name: 'meeting-typed-stress',
    what: 'Typed lines with the longest names (the tag stays by the name)',
    page: 'main',
    stress: true,
    world: withTyped,
    ready: '[data-testid="transcript"] [data-testid="transcript-typed"]',
  },
  {
    // 2026-11-21 (the user, 40:09: "为什么我看不到他的后台状态呢？"; then "为什么你把本应该出现在上面的状态栏的东西放在了
    // 下面": the card at the transcript's foot went, the AI strip above says it, all of it on hover)
    name: 'meeting-long-edit',
    what: 'In a meeting: a big revision under way (41 places, 7 minutes) whose bubble is far up, one more queued: the AI strip says it, nothing at the transcript\'s foot',
    page: 'main',
    world: withLongEdit,
    go: async (c) => {
      await c.page.getByTestId('claude-now-back').getByText(c.T('已改 41 处', 'Edits so far: 41'), { exact: false }).waitFor();
    },
    ready: '[data-testid="claude-now"] .mt-now-dot',
    transitions: longEditTransitions,
  },
  {
    name: 'meeting-long-edit-stress',
    what: 'The big revision with the longest title, names and model names (the strip ends in "…")',
    page: 'main',
    stress: true,
    world: withLongEdit,
    ready: '[data-testid="claude-now"] .mt-now-dot',
    transitions: longEditTransitions,
  },
  {
    // more than 5 steps fold under "查看全部"; the "正在修改 · 已改 N 处 · 时间" line stays first (it used to fold away too)
    name: 'meeting-edit-steps',
    what: 'Scrolled up to the bubble that asked for the big revision: its live steps folded under 查看全部, its progress line first',
    page: 'main',
    world: withLongEdit,
    go: async (c) => {
      await toLiveEdit(c);
      const first = await c.page.getByTestId('live-edit').locator('.mt-steps > *').first().textContent();
      if (!first?.includes(c.T('已改 41 处', 'Edits so far: 41'))) throw new Error(`the progress line isn't first under the bubble: "${first}"`);
    },
    reapply: toLiveEdit,
    ready: '[data-testid="live-edit"] [data-testid="steps-more"]',
  },
  {
    name: 'meeting-edit-queue',
    what: 'In a meeting: three revisions waiting their turn, none being made (the AI strip counts them)',
    page: 'main',
    world: withQueueOnly,
    go: async (c) => {
      await c.page.getByTestId('claude-now-back').getByText(c.T('3 条排队中', '3 more queued')).waitFor();
    },
    ready: '[data-testid="claude-now"] .mt-now-dot',
    transitions: [
      {
        name: 'one starts',
        act: (c) => c.push('brain', brainWith(c, { edits: [{ ...longRow(c.world, 0), started: NOW_S - 1, log: ['读文档'] }], queued: 2 })),
        expect: STRIP_WORDS,
      },
    ],
  },
  {
    // the user: "鼠标悬停在状态栏附近，会有完整状态，但是是一直闪烁的…是 html 原生页面"; then (2026-11-21) "我只需要鼠标悬停在
    // 上面能展开完整状态即可": the strip itself is the target, the tip has every part with the step it is on
    name: 'meeting-now-tip',
    what: "Pointing at the AI strip: the whole status in the window's own tip, one part a line with the step each is on, steady while it ticks",
    page: 'main',
    world: withLongEdit,
    go: openNowTip,
    reapply: openNowTip,
    ready: '.mw-tip.is-lines',
    transitions: tipTransitions,
  },
  {
    name: 'meeting-now-tip-stress',
    what: "The AI strip's tip with the longest title and steps: a part too long for a line goes on under itself, indented",
    page: 'main',
    stress: true,
    world: withBusyBack,
    go: openNowTip,
    reapply: openNowTip,
    ready: '.mw-tip.is-lines',
    transitions: tipTransitions,
  },
  {
    name: 'meeting-now-tip-all',
    what: "The AI strip's tip with everything at once: two lookups, two revisions, waiting, queued, not said yet, notes",
    page: 'main',
    world: withBusyBack,
    go: async (c) => {
      await openNowTip(c);
      await tipHasAll(c);
    },
    reapply: openNowTip,
    ready: '.mw-tip.is-lines',
    transitions: tipTransitions,
  },
  {
    name: 'meeting-ai-card',
    what: "The AI's hover card: models, effort, speech parts",
    page: 'main',
    ready: '.mt-person.is-claude .mt-card-pop, [data-testid="claude-status"] .mt-card-pop',
    go: hover('claude-status'),
    reapply: hover('claude-status'),
    transitions: [
      {
        name: 'effort slider opens',
        act: async (c) => {
          await c.page.getByTestId('model-effort-chip').click();
          await c.page.getByTestId('effort-pane').waitFor();
        },
        expect: ['[data-testid="effort-pane"]', '[data-testid="model-effort-chip"]'],
        note: 'the slider is laid over the last two rows: the card must not grow',
      },
    ],
  },
  {
    name: 'meeting-ai-card-stress',
    what: "The AI's hover card with long custom model names",
    page: 'main',
    stress: true,
    ready: '[data-testid="claude-status"] .mt-card-pop',
    go: hover('claude-status'),
    reapply: hover('claude-status'),
  },
  {
    name: 'meeting-my-card',
    what: 'Your own hover card: microphone, speaker, mute',
    page: 'main',
    ready: '[data-testid="mic-mute"]',
    go: hover('person-me'),
    reapply: hover('person-me'),
    transitions: [
      {
        name: 'mute from the card',
        act: async (c) => {
          await c.page.getByTestId('mic-mute').click();
          await c.page.getByTestId('mic-muted').waitFor();
        },
        expect: ['[data-testid="mic-mute"]', '[data-testid="mic-muted"]', '.mt-mic-off'],
        note: 'the button label changes (Mute ↔ Unmute) and the badge appears on the avatar',
      },
    ],
  },
  {
    name: 'meeting-people-list',
    what: '14 people: the avatar stack and its list (longest names)',
    page: 'main',
    stress: true,
    ready: '[data-testid="people-stack"] .mt-card-pop',
    go: hover('people-stack'),
    reapply: hover('people-stack'),
  },
  {
    name: 'meeting-invite',
    what: 'The invite popover (link, copy)',
    page: 'main',
    ready: '[data-testid="meeting-invite-url"]',
    go: async (c) => {
      await c.page.getByTestId('meeting-invite').click();
    },
  },
  {
    name: 'meeting-paper-menu',
    what: 'The papers menu (two papers, add)',
    page: 'main',
    stress: true,
    ready: '.mw-menu',
    go: async (c) => {
      await c.page.getByTestId('paper-menu').click();
    },
  },
  // ---------------------------------------------------------------- 第 6 批: the paper menu's 历史版本
  {
    name: 'meeting-paper-history',
    what: 'The paper menu → 历史版本 in a meeting: versions on the left, the diff of two on the right; 恢复 off (a meeting is on)',
    page: 'main',
    world: (w) => void withComments(withVersions(w)),
    go: openPaperHistory,
    ready: '[data-testid="paper-history"] .ph-row',
    scope: '.ph-dlg',
    ignore: ['[data-testid="ph-diff"]'],
    transitions: [
      {
        name: 'pick an older version (the diff loads, the list and the buttons stay)',
        act: async (c) => {
          await c.page.locator('.ph-row').nth(3).click();
          await c.settle(400);
        },
        expect: ['.ph-row', '.ph-cmp-v', '.ph-wait', '[data-testid="ph-diff"]'],
      },
    ],
  },
  {
    name: 'meeting-paper-history-stress',
    what: '历史版本 with 120 versions and very long meeting names',
    page: 'main',
    stress: true,
    world: (w) => void withVersions(w),
    go: openPaperHistory,
    ready: '[data-testid="paper-history"] .ph-row',
    scope: '.ph-dlg',
    ignore: ['[data-testid="ph-diff"]'],
  },
  {
    name: 'meeting-paper-history-restore',
    what: '历史版本 after the meeting: 恢复到这一版 → 确定恢复 (same width) → restored',
    page: 'main',
    world: (w) => {
      ended(w);
      withVersions(w);
    },
    go: openPaperHistory,
    ready: '[data-testid="paper-history"] .ph-row',
    scope: '.ph-dlg',
    ignore: ['[data-testid="ph-diff"]'],
    transitions: [
      {
        name: 'pick an old version, press 恢复到这一版 (it asks in place, same width)',
        act: async (c) => {
          await c.page.locator('.ph-row').nth(4).click();
          await c.settle(300);
          await c.page.getByTestId('ph-restore').click();
          await c.settle(200);
        },
        expect: ['.ph-row', '.ph-cmp-v', '.ph-wait', '[data-testid="ph-diff"]', '[data-testid="ph-restore"] *', '.ph-msg'],
      },
    ],
  },
  {
    name: 'meeting-ended',
    what: 'The meeting page right after the meeting ended (it stays until 新会议)',
    page: 'main',
    go: async (c) => {
      await c.page.locator('[data-testid="claude-now"] .mt-now-dot').first().waitFor();
      ended(c.world);
      await c.waitPoll();
    },
    ready: '[data-testid="meeting-new"]',
  },
  // ---------------------------------------------------------------- a meeting without a paper (mode "chat")
  {
    name: 'meeting-chat',
    what: 'In a meeting without a paper: the transcript is the page; no AI among the people, no AI strip, 标记 in the top bar',
    page: 'main',
    world: chatLive,
    ready: '.mt-root.is-chat .mt-head [data-testid="bookmark"]',
    transitions: chatTransitions,
  },
  {
    name: 'meeting-chat-stress',
    what: 'In a meeting without a paper, longest names, 14 people, a very long line',
    page: 'main',
    stress: true,
    world: chatLive,
    ready: '.mt-root.is-chat .mt-head [data-testid="bookmark"]',
  },
  // ---------------------------------------------------------------- 会中交互 (第 3 批): the box, what floats
  {
    name: 'meeting-tools',
    what: 'In a meeting: the box to type to the AI, two bookmarks in the transcript',
    page: 'main',
    ready: '[data-testid="composer"]',
    transitions: toolTransitions,
  },
  {
    name: 'meeting-tools-stress',
    what: 'In a meeting: a 100-char bookmark note, longest names',
    page: 'main',
    stress: true,
    ready: '[data-testid="composer"]',
    transitions: toolTransitions,
  },
  {
    name: 'meeting-anchor',
    what: 'A passage selected in the paper: the chip over the box (it clears itself)',
    page: 'main',
    stress: true,
    world: (w) => {
      const text = (w.lang === 'zh' ? '我们提出了一种全新的方法，在九个公开数据集上全面超过已有的基线。' : 'We propose an entirely new method that beats every baseline on nine public datasets. ').repeat(8);
      w.current.anchor = { text, section: '1 Introduction', by: null, at: NOW_S, left_s: 40 };
      w.userStrings.push(text);
    },
    ready: '[data-testid="anchor-chip"]',
  },
  // 论文区 with the real paper page in it (2026-11-21): 「引用」 after a selection, the comment box, staying put when an
  // edit lands (paper-live.ts)
  ...PAPER_LIVE_SCREENS,
  {
    name: 'meeting-quiet',
    what: 'Nobody has spoken for 10 minutes: the host is reminded (散会 there; nothing ends by itself)',
    page: 'main',
    world: (w) => {
      w.current.quiet_s = 640;
    },
    ready: '[data-testid="quiet-note"]',
    transitions: [
      {
        name: 'close the reminder',
        act: async (c) => {
          await c.page.getByTestId('quiet-close').click();
          await c.settle(200);
        },
        expect: ['[data-testid="quiet-note"]'],
      },
    ],
  },
  // ---------------------------------------------------------------- 回顾 (history)
  {
    name: 'history',
    what: 'History: the list and the chosen meeting (summary, transcript)',
    page: 'main',
    go: goHistory,
    ready: '[data-testid="history-preview"] .mt-hp-title',
    transitions: [
      {
        name: 'pick another meeting',
        act: async (c) => {
          await c.page.getByTestId('history-m-002').click();
          await c.settle(300);
        },
        expect: ['[data-testid="history-preview"]', '.mt-hrow'],
      },
    ],
  },
  {
    name: 'history-stress',
    what: 'History with 40 meetings and 60-char titles',
    page: 'main',
    stress: true,
    go: goHistory,
    ready: '[data-testid="history-preview"] .mt-hp-title',
  },
  {
    name: 'history-empty',
    what: 'History with no meetings yet',
    page: 'main',
    world: (w) => {
      w.meetings = [];
      w.details = {};
    },
    go: async (c) => {
      await c.page.getByTestId('nav-history').click();
    },
    ready: '[data-testid="meeting-history"] .mt-empty',
  },
  // 第 8 批: no meeting of the user's own yet: the sample meeting that comes with Bloom (tagged 示例, can be deleted)
  {
    name: 'history-sample',
    what: 'History with only the sample meeting (tag 示例; 提问 and 阶段汇总 off); delete asks first',
    page: 'main',
    world: (w) => {
      const first = w.meetings[0];
      const d = w.details[first.id];
      w.meetings = [{ ...first, id: 'sample', title: w.lang === 'zh' ? 'Bloom 使用说明' : 'Using Bloom', sample: true, papers: [{ id: 'ps', name: w.lang === 'zh' ? 'Bloom 使用说明' : 'Using Bloom' }] }];
      w.details = { sample: { ...d, meeting: { ...d.meeting, id: 'sample', title: w.meetings[0].title, sample: true }, video: null, diff_url: '/snap/sample/' } };
    },
    go: goHistory,
    ready: '[data-testid="history-sample-tag"]',
    transitions: [
      {
        name: 'delete asks (the button turns into the question in place)',
        act: async (c) => {
          await c.page.getByTestId('history-delete-sample').click();
          await c.settle(150);
        },
        expect: ['.mt-hp-acts'],
      },
    ],
  },
  // 第 8 批: a meeting whose transcript and notes went after the kept days (Settings → Storage)
  {
    name: 'history-records-removed',
    what: 'History: a meeting past the retention period (no transcript or notes, says so)',
    page: 'main',
    world: (w) => {
      const id = w.meetings[0].id;
      const d = w.details[id];
      w.details[id] = { ...d, meeting: { ...d.meeting, records_removed: '2026-11-18T09:00:00' }, transcript: [], summary: null, talk: [], bookmarks: [] };
      w.meetings[0] = { ...w.meetings[0], has_summary: false, n_lines: 0 };
    },
    go: goHistory,
    ready: '[data-testid="history-preview"] .mt-hp-none',
  },
  {
    name: 'history-processing',
    what: 'History: the newest meeting still being processed (steps)',
    page: 'main',
    world: processing,
    go: goHistory,
    ready: '[data-testid="history-post"]',
  },
  // 第 5 批: 回顾 → 保存: 纪要 (Word, PDF, 复制（带格式）), 逐字稿 (Markdown, TXT), 导出会议包 with its transcript tick, 存录屏
  {
    name: 'history-share-menu',
    what: 'History: the 保存 menu open (notes, transcript, meeting pack, recording)',
    page: 'main',
    go: openShareMenu,
    reapply: async (c) => {
      if (!(await c.page.getByTestId('history-share-menu').isVisible().catch(() => false))) await c.page.getByTestId('history-share').click();
    },
    ready: '[data-testid="history-share-menu"]',
    transitions: [
      {
        name: 'tick the pack\u2019s transcript (nothing moves)',
        act: async (c) => {
          await c.page.getByTestId('history-share-pack-tr').check();
          await c.settle(150);
        },
        expect: ['[data-testid="history-share-pack-tr"]'],
      },
      {
        name: 'copy the notes with formatting (the menu closes, a toast says so)',
        act: async (c) => {
          await c.page.getByTestId('history-share-copy').click();
          await c.page.getByTestId('toast').waitFor();
        },
        expect: ['[data-testid="history-share-menu"]', '[data-testid="history-share-menu"] *', '[data-testid="toast"]'],
        note: 'the menu is a pop-over: it goes away; the toast floats at the foot of the window',
      },
    ],
  },
  {
    name: 'history-share-menu-stress',
    what: 'The 保存 menu next to a 60-char meeting title',
    page: 'main',
    stress: true,
    go: openShareMenu,
    reapply: async (c) => {
      if (!(await c.page.getByTestId('history-share-menu').isVisible().catch(() => false))) await c.page.getByTestId('history-share').click();
    },
    ready: '[data-testid="history-share-menu"]',
  },
  {
    name: 'history-filter',
    what: 'History: the paper filter open (each paper, how many meetings)',
    page: 'main',
    go: async (c) => {
      await goHistory(c);
      await c.page.getByTestId('history-paper').click();
    },
    ready: '[data-testid="history-paper-opt"]',
    transitions: [
      {
        name: 'pick a paper (the list keeps only its meetings)',
        act: async (c) => {
          await c.page.getByTestId('history-paper-opt').nth(1).click();
          await c.settle(300);
        },
        expect: ['.mt-hlist', '.mt-hlist *', '[data-testid="history-preview"]', '[data-testid="history-preview"] *', '[data-testid="history-paper"] *', '.mt-meta *'],
      },
    ],
  },
  {
    name: 'history-ask',
    what: 'History → Ask: a question over the past meetings, the answer with its sources',
    page: 'main',
    go: askPast,
    ready: '[data-testid="history-ask-answer"]',
    transitions: [
      {
        name: 'click a source (that meeting opens at that moment)',
        act: async (c) => {
          await c.page.getByTestId('history-ask-source').first().click();
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="meeting"]').first().waitFor();
          await c.settle(400);
        },
        byDesign: 'opening a meeting replaces the list (as 打开 does); the recording plays from the line the source points at',
      },
    ],
  },
  {
    name: 'history-ask-stress',
    what: 'History → Ask with a 300-character question, a long answer and long sources (40 meetings)',
    page: 'main',
    stress: true,
    go: askPast,
    ready: '[data-testid="history-ask-answer"]',
  },
  {
    name: 'history-ask-failed',
    what: 'History → Ask when the model is not set up: the reason where the answer goes',
    page: 'main',
    world: (w) => {
      w.askError = w.lang === 'zh' ? '还没设置模型来源（设置 → 通用 → 模型来源）' : 'no model source set (Settings → General → Model source)';
    },
    go: askPast,
    ready: '[data-testid="history-ask-error"]',
  },
  {
    name: 'review-recording',
    what: 'An opened past meeting on its recording tab',
    page: 'main',
    go: openMeeting,
    ready: '[data-testid="meeting-tab-video"]',
  },
  {
    name: 'review-notes',
    what: 'An opened past meeting on its notes tab: times after each line, one card per person, talk time, 重新生成 in the bar',
    page: 'main',
    go: openNotes,
    ready: '[data-testid="summary-regen"]',
    transitions: [
      {
        name: 'regenerate in another format (the button says it is writing)',
        act: async (c) => {
          await c.page.getByTestId('summary-regen').click();
          await c.page.getByTestId('summary-template-progress').click();
          await c.page.locator('[data-testid="summary-regen"][aria-busy="true"]').waitFor();
        },
        expect: ['[data-testid="summary-regen"] *', '.mw-menu'],
        note: '重新生成 → 生成中… in the same box (the words are stacked; the button must not change size)',
      },
      {
        name: 'regenerate fails (a toast)',
        act: async (c) => {
          const d = c.world.details['m-001'];
          d.summary_job = { state: 'failed', template: 'progress', started: NOW_S, error: c.T('还没设置模型来源', 'no model source set') };
          await c.page.waitForResponse((r) => /\/api\/meetings\/m-001$/.test(new URL(r.url()).pathname) && r.request().method() === 'GET', { timeout: 10_000 });
          await c.page.getByTestId('toast').waitFor();
        },
        expect: ['[data-testid="summary-regen"] *', '[data-testid="toast"]'],
        note: 'the failure is said in the toast at the foot of the window; the page itself does not move',
      },
      {
        name: 'click a time in the notes (the recording plays from there)',
        act: async (c) => {
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="summary-at"]').first().click();
          await c.page.locator('[data-testid="meeting-tab-video"][aria-selected="true"]').waitFor();
        },
        expect: ['[data-testid="transcript"] *', '.mt-layer', '.mt-layer *', '.mt-bar *'],
        note: 'the recording tab comes up at that moment and the transcript scrolls to the line (lines inside the list move; the list does not)',
      },
    ],
  },
  {
    name: 'review-notes-stress',
    what: 'Notes with the longest names: 14 people in the per-person cards and the talk time list',
    page: 'main',
    stress: true,
    go: openNotes,
    ready: '[data-testid="summary-talk"]',
  },
  {
    name: 'review-notes-usage',
    what: 'Notes, scrolled to the end: the meeting’s model usage (tokens and estimated cost per model, a total line)',
    page: 'main',
    go: async (c) => {
      await openNotes(c);
      await scrollTo('.mt-hlayer:not(.is-off) [data-testid="summary-usage"]')(c);
    },
    reapply: scrollTo('.mt-hlayer:not(.is-off) [data-testid="summary-usage"]'),
    ready: '[data-testid="summary-usage"]',
  },
  {
    name: 'review-notes-usage-stress',
    what: 'Model usage with a long model name, one model without a price (—) and big numbers',
    page: 'main',
    stress: true,
    go: async (c) => {
      await openNotes(c);
      await scrollTo('.mt-hlayer:not(.is-off) [data-testid="summary-usage"]')(c);
    },
    reapply: scrollTo('.mt-hlayer:not(.is-off) [data-testid="summary-usage"]'),
    ready: '[data-testid="summary-usage"]',
  },
  {
    name: 'review-notes-old',
    what: 'Notes saved by an older version: plain lines, no times, no per-person part (shown as before)',
    page: 'main',
    world: (w) => {
      w.details['m-001'].summary = oldSummary(w);
      w.details['m-001'].talk = null;
    },
    go: openNotes,
    ready: '[data-testid="summary-regen"]',
  },
  {
    name: 'review-notes-none',
    what: 'A past meeting without notes: the board shows, and 生成纪要 in the bar',
    page: 'main',
    world: (w) => {
      w.details['m-001'].summary = null;
    },
    go: async (c) => {
      await openMeeting(c);
      await c.page.getByTestId('meeting-tab-notes').click();
    },
    ready: '[data-testid="summary-regen"]',
  },
  {
    name: 'review-todos',
    what: 'An opened past meeting on its to-do tab (会上已改 also lists the change someone typed straight into the paper)',
    page: 'main',
    stress: true,
    go: async (c) => {
      await openMeeting(c);
      await c.page.getByTestId('meeting-tab-todos').click();
    },
    ready: '[data-testid="todo-hand"]',
  },
  {
    name: 'review-chat',
    what: 'An opened past meeting held without a paper: 纪要 and 录屏 only (no 修订对比), no AI among the people',
    page: 'main',
    world: chatPast,
    go: openMeeting,
    ready: '.mt-hlayer:not(.is-off) [data-testid="meeting-tab-notes"]',
    transitions: [
      {
        name: 'switch to the notes',
        act: async (c) => {
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="meeting-tab-notes"]').click();
          await c.page.locator('.mt-hlayer:not(.is-off) .mt-sum-title').first().waitFor();
        },
        expect: ['.mt-layer', '.mt-layer *', '.mt-bar *', '[data-testid="transcript"] *'],
      },
    ],
  },
  {
    name: 'review-chat-preview',
    what: '回顾 list with a meeting held without a paper selected (its notes and transcript in the preview)',
    page: 'main',
    world: chatPast,
    go: goHistory,
    ready: '[data-testid="history-preview"] .mt-hp-body',
  },
  // ---------------------------------------------------------------- 回顾 → 编辑 (第 7 批): the transcript by hand
  {
    name: 'review-transcript-edit',
    what: 'An opened past meeting with 编辑 on: lines take a click to edit, guests’ names to rename; 撤销 / 替换 in the head',
    page: 'main',
    go: async (c) => {
      await openMeeting(c);
      await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-toggle"]').click();
    },
    ready: '.mt-hlayer:not(.is-off) [data-testid="tredit-toggle"][aria-pressed="true"]',
    transitions: [
      {
        name: 'click a line (it opens in place, the same size)',
        act: async (c) => {
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="transcript-line"]').first().click();
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-line"]').waitFor();
        },
        expect: ['[data-testid="tredit-line"]'],
      },
      {
        name: 'Esc puts it back',
        act: async (c) => {
          await c.page.keyboard.press('Escape');
          await c.settle(200);
        },
        expect: ['[data-testid="transcript-line"]'],
      },
      {
        name: 'click a guest’s name (the field over it)',
        act: async (c) => {
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-who"]').first().click();
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-name"]').waitFor();
        },
        expect: ['[data-testid="tredit-name"]', '[data-testid="tredit-who"]'],
      },
    ],
  },
  {
    name: 'review-transcript-edit-stress',
    what: '编辑 on with the longest names and lines (40 meetings), one line open to edit',
    page: 'main',
    stress: true,
    go: async (c) => {
      await openMeeting(c);
      await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-toggle"]').click();
      await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="transcript-line"]').first().click();
    },
    ready: '.mt-hlayer:not(.is-off) [data-testid="tredit-line"]',
  },
  {
    name: 'review-transcript-busy',
    what: 'A past meeting still being processed: 撤销 / 编辑 shown greyed (nothing moves when they turn on)',
    page: 'main',
    world: (w) => {
      w.details['m-001'].processing = true;
    },
    go: openMeeting,
    ready: '.mt-hlayer:not(.is-off) [data-testid="tredit-toggle"]:disabled',
  },
  // ---------------------------------------------------------------- Settings
  {
    name: 'settings-profile',
    what: 'Settings → General: profile (photo, your name, the AI name, colours)',
    page: 'main',
    go: goSettings,
    ready: '[data-testid="profile-name"]',
    transitions: [
      {
        name: 'name rejected (error under the field)',
        act: async (c) => {
          c.world.profileError = 'taken';
          await c.page.getByTestId('profile-name').fill(c.T('林', 'Lin'));
          await c.page.getByTestId('profile-name').press('Enter');
          await c.settle(400);
        },
        expect: ['[data-testid="profile-name"]', '[data-setting="general.name"]'],
        note: 'the error line appears under the name field; rows below move down if it takes room',
      },
    ],
  },
  {
    name: 'settings-profile-stress',
    what: 'Settings → profile with a 24-char name and AI name',
    page: 'main',
    stress: true,
    go: goSettings,
    ready: '[data-testid="profile-name"]',
  },
  {
    name: 'settings-model-source',
    what: 'Settings → General → Model source (DeepSeek, key saved)',
    page: 'main',
    go: async (c) => {
      await goSettings(c);
      await scrollTo('[data-setting="general.model_source"]')(c);
    },
    reapply: scrollTo('[data-setting="general.model_source"]'),
    ready: '[data-testid="provider-test"]',
  },
  {
    name: 'settings-model-source-local',
    what: 'Model source: Ollama on this computer (no key row), a menu beside each model field, 查文献 and this month’s usage',
    page: 'main',
    world: (w) => {
      Object.assign(w.provider, { kind: 'ollama', base_url: 'http://127.0.0.1:11434/v1', fast: 'qwen3:8b', main: 'qwen3:14b', key_set: false, key_tail: '', label: 'Ollama' });
    },
    go: async (c) => {
      await goSettings(c);
      await scrollTo('[data-setting="general.model_source"]')(c);
    },
    reapply: scrollTo('[data-setting="general.model_source"]'),
    ready: '[data-testid="provider-fast-pick"]',
    transitions: [
      {
        name: 'open the model menu (the endpoint’s models)',
        act: async (c) => {
          await c.page.getByTestId('provider-fast-pick').click();
          await c.page.locator('.mw-menu').waitFor();
        },
        expect: ['.mw-menu', '.mw-menu *', '[data-testid="provider-fast-pick"] *'],
        note: 'the menu hangs under the field; the field and the rows below stay where they are',
      },
      {
        name: 'the model list fails (said in the connection row)',
        act: async (c) => {
          await c.page.keyboard.press('Escape');
          c.world.providerModels = null;
          await c.page.getByTestId('provider-url').fill('http://127.0.0.1:11435/v1');
          await c.page.getByTestId('provider-main-pick').click();
          await c.page.locator('[data-setting="general.provider_check"] .st-detail.warn').waitFor();
        },
        expect: ['[data-setting="general.provider_check"] *', '[data-testid="provider-url"]', '[data-testid="provider-main-pick"] *'],
        note: 'the connection row keeps room for its message: the 查文献 and usage rows below do not move',
      },
      {
        name: 'turn 查文献 off',
        act: async (c) => {
          await c.page.locator('[data-setting="general.lit"] [role="switch"]').click();
          await c.settle(300);
        },
        expect: ['[data-setting="general.lit"] *'],
        note: 'only the switch changes',
      },
    ],
  },
  {
    name: 'settings-model-source-local-stress',
    what: 'Model source: an OpenAI-compatible endpoint with long model names, 60 models in the menu, big usage numbers',
    page: 'main',
    stress: true,
    world: (w) => {
      Object.assign(w.provider, { kind: 'openai', label: 'llm-gateway.internal.example-university.edu.cn' });
    },
    go: async (c) => {
      await goSettings(c);
      await scrollTo('[data-setting="general.model_source"]')(c);
      await c.page.getByTestId('provider-main-pick').click();
      await c.page.locator('.mw-menu').waitFor();
    },
    reapply: scrollTo('[data-setting="general.model_source"]'),
    ready: '.mw-menu',
  },
  {
    name: 'settings-model-source-stress',
    what: 'Model source: custom endpoint, long URL and model names',
    page: 'main',
    stress: true,
    go: async (c) => {
      await goSettings(c);
      await scrollTo('[data-setting="general.model_source"]')(c);
    },
    reapply: scrollTo('[data-setting="general.model_source"]'),
    ready: '[data-testid="provider-url"]',
  },
  {
    name: 'settings-fresh-install',
    // with no paper the lobby lists no "set up a model" need (that meeting keeps only the transcript): the way in is the
    // 准备情况 row of the model, 未设置 (a grey dot without a paper), and its 更换
    what: 'Fresh install: the 准备情况 model row (未设置) → 更换 → Settings at Model source (none picked)',
    page: 'main',
    world: fresh,
    go: async (c) => {
      await c.page.locator('[data-testid="lobby-status"] .lb-st', { hasText: c.T('对话模型', 'Chat model') }).locator('button.link').click();
      await c.page.getByTestId('settings').waitFor();
    },
    ready: '[data-testid="provider-key"]',
  },
  // 准备情况 → 改稿模型 → 更换 with Claude Code's login: Settings opens at Model source, where the models are now
  // listed under the source (the conversation model as text: one choice; the editing model and effort as menus)
  {
    name: 'settings-model-source-claude',
    what: 'Model source: Claude Code account, reached from the lobby’s 改稿模型 → 更换 (its row flashed): 对话模型 as text, 改稿模型 and 思考强度 menus',
    page: 'main',
    world: withClaude,
    go: async (c) => {
      await c.page.getByTestId('lobby-change-main').click();
      await c.page.locator('[data-setting="general.main_model"] .st-popup-btn').waitFor();
      await c.settle(1800);                 // the flash on the row fades
    },
    reapply: scrollTo('[data-setting="general.model_source"]'),
    ready: '[data-setting="general.effort"] .st-popup-btn',
    transitions: [
      {
        name: 'pick 思考强度 Max (only the menu’s text changes)',
        act: async (c) => {
          await c.page.locator('[data-setting="general.effort"] .st-popup-btn').click();
          await c.page.locator('.mw-menu [role="menuitemcheckbox"]', { hasText: 'Max' }).click();
          await c.settle(300);
        },
        expect: ['[data-setting="general.effort"] .st-popup-btn *', '.mw-menu'],
      },
      {
        name: 'switching the editing model fails (said in the connection row; nothing moves)',
        act: async (c) => {
          c.world.models = null;
          await c.page.locator('[data-setting="general.main_model"] .st-popup-btn').click();
          await c.page.locator('.mw-menu [role="menuitemcheckbox"]', { hasText: 'Claude Opus 4.6' }).click();
          await c.page.locator('[data-setting="general.provider_check"] .st-detail.warn').waitFor();
        },
        expect: ['[data-setting="general.provider_check"] *', '.mw-menu'],
        note: 'the connection row keeps room for its message',
      },
    ],
  },
  {
    name: 'settings-model-source-claude-menu',
    what: 'Model source: Claude Code account, the 改稿模型 menu open',
    page: 'main',
    world: withClaude,
    go: async (c) => {
      await goSettings(c);
      await scrollTo('[data-setting="general.model_source"]')(c);
      await c.page.locator('[data-setting="general.main_model"] .st-popup-btn').click();
      await c.page.locator('.mw-menu').waitFor();
    },
    ready: '.mw-menu',
  },
  // 准备情况 → 麦克风 → 更换: Settings → Speech at 麦克风 (new): 系统默认 or one of this PC's inputs
  {
    name: 'settings-from-lobby-mic',
    what: 'Lobby 麦克风 → 更换 → Settings → Speech at 麦克风 (flashed): 系统默认, the default device named under the row',
    page: 'main',
    world: idle,
    go: async (c) => {
      await c.page.getByTestId('lobby-change-mic').click();
      await c.page.locator('[data-testid="mic-pick"] .st-popup-btn:not([disabled])').waitFor();
      await c.settle(1800);
    },
    ready: '[data-setting="speech.mic"] .st-detail',
    transitions: [
      {
        name: 'pick the headset (the line under the row goes; nothing moves)',
        act: async (c) => {
          await c.page.locator('[data-testid="mic-pick"] .st-popup-btn').click();
          await c.page.locator('.mw-menu [role="menuitemcheckbox"]', { hasText: c.T('耳机', 'Headset') }).click();
          await c.page.locator('[data-setting="speech.mic"] .st-detail').waitFor({ state: 'detached' });
        },
        expect: ['[data-setting="speech.mic"] *', '.mw-menu'],
        note: 'the row keeps the height of its second line',
      },
      {
        name: 'saving fails: a meeting started meanwhile (said under the row; nothing moves)',
        act: async (c) => {
          c.world.current.meeting = { ...c.world.current.meeting!, state: 'live' };
          await c.page.locator('[data-testid="mic-pick"] .st-popup-btn').click();
          await c.page.locator('.mw-menu [role="menuitemcheckbox"]').first().click();
          await c.page.locator('[data-setting="speech.mic"] .st-detail.warn').waitFor();
        },
        expect: ['[data-setting="speech.mic"] *', '.mw-menu'],
      },
    ],
  },
  {
    name: 'settings-speech-mic-menu',
    what: 'Settings → Speech → 麦克风: the menu open (系统默认 with the default device under it, then this PC’s inputs)',
    page: 'main',
    world: idle,
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-tab-speech').click();
      await c.page.locator('[data-testid="mic-pick"] .st-popup-btn:not([disabled])').click();
      await c.page.locator('.mw-menu').waitFor();
    },
    ready: '.mw-menu',
  },
  {
    name: 'settings-speech-mic-missing',
    what: 'Settings → Speech → 麦克风: the picked USB microphone isn’t plugged in (未连接 under the row); long device names',
    page: 'main',
    stress: true,
    world: (w) => withMicMissing(idle(w)),
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-tab-speech').click();
    },
    ready: '[data-setting="speech.mic"] .st-detail.warn',
  },
  {
    name: 'settings-summary',
    what: 'Settings → General → Notes: the format; 自定义 opens the box for the group\u2019s own requirements',
    page: 'main',
    go: goSummarySettings,
    reapply: scrollTo('[data-setting="general.summary"]'),
    ready: '[data-setting="general.summary_template"] .st-popup-btn',
    transitions: [
      {
        name: 'pick 自定义 (the requirements box opens under it)',
        act: async (c) => {
          await c.page.locator('[data-setting="general.summary_template"] .st-popup-btn').click();
          await c.page.locator('.mw-menu [role="menuitemcheckbox"]', { hasText: c.T('自定义', 'Custom') }).click();
          await c.page.getByTestId('summary-custom').waitFor();
        },
        expect: ['[data-setting="general.summary_template"] *', '.mw-menu'],
        byDesign: 'a choice that needs more opens its field right under it, as the model source rows do (owner rule: only things that come and go by themselves must not push)',
      },
      {
        name: 'type the requirements and leave the box (the count line stays)',
        act: async (c) => {
          await c.page.getByTestId('summary-custom').fill(c.T('每人写进展、问题、下周计划；老师的意见单独列出。', 'Each person: progress, problems, next week. The advisor comments on their own.'));
          await c.page.getByTestId('summary-custom').blur();
          await c.settle(300);
        },
        expect: ['[data-setting="general.summary_custom"] .st-detail', '[data-testid="summary-custom"]'],
      },
    ],
  },
  {
    name: 'settings-summary-custom-full',
    what: 'Notes format 自定义 with 2000 characters of requirements',
    page: 'main',
    stress: true,
    world: (w) => {
      w.summaryPrefs = { ...w.summaryPrefs, template: 'custom', custom: (w.lang === 'zh' ? '每人写进展、问题和下周计划，老师的意见单独列一节。' : 'Each person: progress, problems, plan. ').repeat(80).slice(0, 2000) };
    },
    go: goSummarySettings,
    reapply: scrollTo('[data-setting="general.summary"]'),
    ready: '[data-testid="summary-custom"]',
  },
  // 第 6 批: Settings → General → 论文 (how a Word paper's edits go back: tracked changes by default, or straight in)
  {
    name: 'settings-paper',
    what: 'Settings → General → 论文: Word 写回 (修订模式 / 直接改)',
    page: 'main',
    go: async (c) => {
      await goSettings(c);
      await scrollTo('[data-setting="general.paper"]')(c);
    },
    reapply: scrollTo('[data-setting="general.paper"]'),
    ready: '[data-setting="general.paper_docx"] .st-popup-btn',
    transitions: [
      {
        name: 'pick 直接改 (the button keeps its width)',
        act: async (c) => {
          await c.page.locator('[data-setting="general.paper_docx"] .st-popup-btn').click();
          await c.page.locator('.mw-menu [role="menuitemcheckbox"]', { hasText: c.T('直接修改', 'Edit directly') }).click();
          await c.settle(300);
        },
        expect: ['[data-setting="general.paper_docx"] .st-popup-btn *', '.mw-menu'],
      },
    ],
  },
  // 第 5 批: Settings → General → 散会后自动保存 (off by default; the folder's line holds the path or the refusal)
  {
    name: 'settings-autosave',
    what: 'Settings → General → 散会后自动保存, off: the switch, the folder (not set), what to save',
    page: 'main',
    go: goAutosaveSettings,
    reapply: scrollTo('[data-setting="general.autosave"]'),
    ready: '[data-testid="autosave-items"]',
    transitions: [
      {
        name: 'untick 修订对比 (nothing moves)',
        act: async (c) => {
          await c.page.getByTestId('autosave-item-diff').uncheck();
          await c.settle(300);
        },
        expect: ['[data-testid="autosave-item-diff"]'],
      },
    ],
  },
  {
    name: 'settings-autosave-on',
    what: '散会后自动保存 on, saving into a 180-character folder (cut with …), everything ticked',
    page: 'main',
    stress: true,
    world: (w) => {
      w.autosave = {
        ...w.autosave,
        on: true,
        dir: ('D:\\' + (w.lang === 'zh' ? '研究生\\组会记录\\2026 秋季学期\\王老师课题组\\每周组会纪要和修改清单' : 'Research\\Group meetings\\2026 Fall\\Advisor group\\Weekly notes and to-dos') + '\\').repeat(4).slice(0, 180),
        items: ['minutes_docx', 'minutes_md', 'todos', 'diff', 'transcript'],
      };
      w.userStrings.push(w.autosave.dir);     // the folder the user picked: theirs, cut with … on purpose
    },
    go: goAutosaveSettings,
    reapply: scrollTo('[data-setting="general.autosave"]'),
    ready: '[data-testid="autosave-items"]',
  },
  {
    name: 'settings-speech',
    what: 'Settings → speech models',
    page: 'main',
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-tab-speech').click();
    },
    ready: '[data-testid="settings-pane-speech"]',
  },
  // 第 7 批: Settings → Speech → 读法 (written = said, 200 at most)
  {
    name: 'settings-speech-pron',
    what: 'Settings → Speech → 读法: the list, its count, 从文件导入',
    page: 'main',
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-tab-speech').click();
      await scrollTo('[data-setting="speech.pron"]')(c);
    },
    reapply: scrollTo('[data-setting="speech.pron"]'),
    ready: '[data-testid="pron"]',
    transitions: [
      {
        name: 'a line without “=” (the count line says so, nothing moves)',
        act: async (c) => {
          await c.page.getByTestId('pron').fill('LaTeX = 雷泰克\nBERT');
          await c.page.getByTestId('pron').blur();
          await c.settle(200);
        },
        expect: ['[data-testid="pron"]', '[data-setting="speech.pron"] .st-detail', '[data-setting="speech.pron"] .st-detail *'],
      },
    ],
  },
  {
    name: 'settings-speech-pron-stress',
    what: 'Settings → Speech → 读法 full: 200 lines, 40 characters each side',
    page: 'main',
    stress: true,
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-tab-speech').click();
      await scrollTo('[data-setting="speech.pron"]')(c);
    },
    reapply: scrollTo('[data-setting="speech.pron"]'),
    ready: '[data-testid="pron"]',
  },
  {
    name: 'settings-storage',
    what: 'Settings → storage',
    page: 'main',
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-tab-storage').click();
    },
    ready: '[data-testid="settings-pane-storage"]',
  },
  // 第 8 批: turning on "delete transcripts and notes automatically" lists the meetings it takes now and asks
  ...([false, true] as const).map((stress): Screen => ({
    name: stress ? 'settings-storage-retention-ask-stress' : 'settings-storage-retention-ask',
    what: stress ? 'Retention confirmation with 40 meetings of 60-character names (the list scrolls, the dialog keeps its size)'
      : 'Settings → storage → 自动删除逐字稿和纪要: the confirmation listing the meetings that lose them now',
    page: 'main',
    stress,
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-tab-storage').click();
      await c.page.locator('[data-setting="storage.records_auto"] .st-popup-btn').click();
      await c.page.locator('.mw-menu [role="menuitemcheckbox"]', { hasText: c.T('30 天后', 'After 30 days') }).click();
    },
    ready: '[data-testid="retention-ask"]',
    scope: '[data-testid="retention-ask"]',
  })),
  {
    name: 'settings-search',
    what: 'Settings search with results',
    page: 'main',
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-search').fill(c.T('模型', 'model'));
    },
    ready: '[data-testid="settings-results"]',
  },
  {
    name: 'settings-search-none',
    what: 'Settings search with no match',
    page: 'main',
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-search').fill('zzzzqqq');
    },
    ready: '[data-testid="settings-results"]',
  },
  // ---------------------------------------------------------------- the guest page (example.com/room/<id>)
  {
    name: 'guest-join',
    what: 'Guest page before joining a live meeting (name field, who is in)',
    page: 'guest',
    ready: '[data-testid="guest-name"]',
  },
  {
    name: 'guest-join-stress',
    what: 'Guest join card: 60-char title, 14 people with long names',
    page: 'guest',
    stress: true,
    ready: '[data-testid="guest-name"]',
  },
  {
    name: 'guest-join-not-recording',
    what: 'Guest join card while the host is not recording (the notice line keeps its place, hidden); recording starts',
    page: 'guest',
    world: (w) => {
      w.relay.state!.rec = null;
    },
    ready: '[data-testid="guest-name"]',
    transitions: [
      {
        name: 'recording starts',
        act: async (c) => {
          c.world.relay.state!.rec = 'screen';
          await c.bumpRelay();
        },
        expect: ['[data-testid="guest-rec-note"]'],
        note: 'only the notice line itself becomes visible; the card keeps its height',
      },
    ],
  },
  {
    name: 'guest-waiting',
    what: 'Guest page: the meeting has not started',
    page: 'guest',
    world: (w) => {
      Object.assign(w.relay.state!, { live: false, upcoming: true });
    },
    ready: '[data-testid="guest-join"]',
  },
  {
    name: 'guest-invalid-link',
    what: 'Guest page: a link to no meeting',
    page: 'guest',
    world: (w) => {
      w.relay.state = null;
    },
    ready: '[data-testid="guest-join"]',
  },
  {
    name: 'guest-in-meeting',
    what: 'Guest inside the meeting (the host’s meeting page through the relay)',
    page: 'guest',
    go: joinGuest,
    ready: '[data-testid="claude-now"]',
    transitions: [
      {
        name: 'voice reconnecting bar',
        act: async (c) => {
          await c.page.evaluate(() => (window as unknown as { __uiAudit: { lk?: { emit: (e: string) => void } } }).__uiAudit.lk?.emit('Reconnecting'));
        },
        expect: ['.mt-notice', '.g-conn'],
        note: 'a connection line appears over the columns',
      },
      {
        name: 'others speaking',
        act: async (c) => {
          const names = (c.world.relay.state?.participants ?? []).filter((p) => p.role === 'guest').map((p) => p.name);
          await c.page.evaluate((n) => (window as unknown as { __uiAudit: { lk?: { speak: (n: string[]) => void } } }).__uiAudit.lk?.speak(n), names);
        },
      },
      {
        name: 'new line from the relay',
        act: async (c) => {
          c.world.relay.transcript.push({ t: '20:31', sec: 1231, who: 'teacher', text: c.T('好，我们继续看第四节。', "OK, let's move on to section 4.") });
          await c.bumpRelay(true);
        },
        expect: ['.mt-right-n', '[data-testid="transcript"] *'],
      },
      {
        name: 'meeting ends while inside',
        act: async (c) => {
          Object.assign(c.world.relay.state!, { live: false, ended: isoAgo(0) });
          await c.bumpRelay();
        },
        expect: ['.mt-head-acts', '[data-testid="claude-now"] *', '.mt-meta', '.mt-state', '.mt-notice', '.g-toasts', '[data-testid="transcript"] *'],
        note: 'the page freezes at the end, a notice explains it (lines inside the transcript may move; the list itself must not)',
      },
    ],
  },
  {
    name: 'guest-tools',
    what: 'A participant inside the meeting: 标记 only (no box to type to the AI: that is the host’s and the co-hosts’)',
    page: 'guest',
    go: joinGuest,
    ready: '[data-testid="composer"]',
    transitions: [
      {
        name: 'mark this moment (the note card; the mark joins the transcript)',
        act: async (c) => {
          await c.page.getByTestId('bookmark').click();
          await c.page.getByTestId('bookmark-note').waitFor();
          await c.settle(1500);
        },
        expect: ['[data-testid="bookmark-note"]', '[data-testid="bookmark-note"] *', '[data-testid="transcript"] *', '.mt-right-n'],
      },
      {
        name: 'the meeting ends while inside (the strip and the box stay, switched off)',
        act: async (c) => {
          await c.page.keyboard.press('Escape');
          Object.assign(c.world.relay.state!, { live: false, ended: isoAgo(0) });
          await c.bumpRelay();
        },
        expect: ['.mt-head-acts', '[data-testid="claude-now"] *', '.mt-meta', '.mt-state', '.mt-notice', '.g-toasts', '[data-testid="transcript"] *', '.mt-compose *', '[data-testid="bookmark-note"]'],
      },
    ],
  },
  {
    name: 'guest-tools-cohost',
    what: 'A co-host on the guest page: the box to type to the AI',
    page: 'guest',
    world: asCohost,
    go: joinGuest,
    ready: '[data-testid="ask-input"]',
    transitions: [
      {
        name: 'type a 500-character question',
        act: async (c) => {
          const q = (c.lang === 'zh' ? '表 2 的基线够了吗？' : 'Are the table 2 baselines enough? ').repeat(60).slice(0, 500);
          c.world.userStrings.push(q.trim());
          await c.page.getByTestId('ask-input').fill(q);
        },
        expect: ['[data-testid="ask-input"]', '[data-testid="ask-send"] *'],
      },
      {
        name: 'send it (through the relay to the host, the box clears, the question joins the transcript marked typed)',
        act: async (c) => {
          const typed = await c.page.locator('[data-testid="transcript"] .mt-typed').count();
          await c.page.getByTestId('ask-send').click();
          await c.page.waitForFunction(() => (document.querySelector('[data-testid="ask-input"]') as HTMLTextAreaElement | null)?.value === '');
          await c.page.waitForFunction((n) => document.querySelectorAll('[data-testid="transcript"] .mt-typed').length > n, typed, { timeout: 10_000 });
          await c.settle(400);
        },
        expect: ['[data-testid="ask-input"]', '[data-testid="ask-send"] *', '[data-testid="transcript"] *', '.mt-right-n'],
      },
      {
        name: 'the meeting ends while inside (编辑 goes; the views and the bar stay put)',
        act: async (c) => {
          Object.assign(c.world.relay.state!, { live: false, ended: isoAgo(0) });
          await c.bumpRelay();
        },
        expect: ['.mt-head-acts', '[data-testid="claude-now"] *', '.mt-meta', '.mt-state', '.mt-notice', '.g-toasts', '[data-testid="transcript"] *', '.mt-compose *', '[data-testid="paper-edit"]'],
      },
    ],
  },
  {
    // 2026-11-19 on a phone: only the host, the AI and you — the people are chips (avatars only on a phone), not the stack
    name: 'guest-three-people',
    what: 'A co-host with only the host and the AI in the meeting; the meeting ends',
    page: 'guest',
    world: (w) => {
      asCohost(w);
      w.relay.state!.participants = (w.relay.state!.participants ?? []).filter((p) => p.rank === 'host' || p.rank === 'cohost' || p.id === 'claude');
    },
    go: joinGuest,
    ready: '[data-testid="ask-input"]',
    transitions: [
      {
        name: 'the meeting ends while inside',
        act: async (c) => {
          Object.assign(c.world.relay.state!, { live: false, ended: isoAgo(0) });
          await c.bumpRelay();
        },
        expect: ['.mt-head-acts', '[data-testid="claude-now"] *', '.mt-meta', '.mt-state', '.mt-notice', '.g-toasts', '[data-testid="transcript"] *', '.mt-compose *', '[data-testid="paper-edit"]'],
      },
    ],
  },
  {
    name: 'guest-tools-stress',
    what: 'A participant inside the meeting: a long bookmark note, longest names',
    page: 'guest',
    stress: true,
    go: joinGuest,
    ready: '[data-testid="composer"]',
  },
  {
    name: 'guest-tools-cohost-stress',
    what: 'A co-host on the guest page with the longest names and a 500-character question typed',
    page: 'guest',
    stress: true,
    world: asCohost,
    go: async (c) => {
      await joinGuest(c);
      const q = c.T('老师问样本量为什么这么小，我怎么答？', 'Why is the sample so small? How do I answer? ').repeat(40).slice(0, 500);
      c.world.userStrings.push(q.trim());
      await c.page.getByTestId('ask-input').fill(q);
    },
    ready: '[data-testid="ask-input"]',
  },
  {
    name: 'guest-in-meeting-stress',
    what: 'Guest inside the meeting with the longest names, 14 people, a very long line',
    page: 'guest',
    stress: true,
    go: joinGuest,
    ready: '[data-testid="claude-now"]',
  },
  // a meeting without a paper: no paper page, no 论文 / 逐字稿 tabs on a phone, no AI, 标记 only
  {
    name: 'guest-chat',
    what: 'Guest inside a meeting without a paper: the transcript alone (on a phone too), 标记 only',
    page: 'guest',
    world: chatLive,
    go: joinGuest,
    ready: '.mt-cols.is-chat [data-testid="bookmark"]',
    transitions: [
      {
        name: 'new line from the relay',
        act: async (c) => {
          c.world.relay.transcript.push({ t: '20:31', sec: 1231, who: 'teacher', text: c.T('好，下一个话题。', "OK, next topic.") });
          await c.bumpRelay(true);
        },
        expect: ['.mt-right-n', '[data-testid="transcript"] *'],
      },
      {
        name: 'meeting ends while inside',
        act: async (c) => {
          Object.assign(c.world.relay.state!, { live: false, ended: isoAgo(0) });
          await c.bumpRelay();
        },
        expect: ['.mt-head-acts', '.mt-meta', '.mt-state', '.mt-notice', '.g-toasts', '[data-testid="transcript"] *', '.mt-compose *'],
      },
    ],
  },
  {
    name: 'guest-chat-cohost',
    what: 'A co-host inside a meeting without a paper: 标记 only (nobody types to the AI there)',
    page: 'guest',
    world: (w) => {
      chatLive(w);
      asCohost(w);
    },
    go: joinGuest,
    ready: '.mt-cols.is-chat [data-testid="bookmark"]',
  },
  {
    name: 'guest-chat-stress',
    what: 'Guest inside a meeting without a paper, longest names, 14 people, a very long line',
    page: 'guest',
    stress: true,
    world: chatLive,
    go: joinGuest,
    ready: '.mt-cols.is-chat [data-testid="bookmark"]',
  },
];
