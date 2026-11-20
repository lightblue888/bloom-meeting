// The mocked backend's whole state ("the world") for one screen: what /api/current, /api/meetings, /api/profile … and
// the guest relay answer. Each test builds a fresh one (baseWorld), the screen's entry tweaks it, and the routes in
// lib/mock-backend.ts read it on every request — so a transition can change it and the app's next poll sees that.
// Shapes follow src/meetingApi.ts (types imported from there, so a renamed field shows up as a type error here).
import type {
  Bookmark,
  Board,
  Brain,
  Current,
  Devices,
  Invite,
  Line,
  MeetingBrief,
  MeetingDetail,
  Models,
  Participant,
  Profile,
  Provider,
  SumItem,
  Summary,
  SummaryPrefs,
  AutosavePrefs,
  TalkTime,
  Task,
  CarryItem,
  Digest,
  PastAnswer,
  MonthUsage,
  Usage,
  PaperComment,
  PaperVersions,
} from '../../src/meetingApi';

/** a 纪要 line's text (src/meetingApi.ts itemText; only types come from there: the page's modules don't load in Node) */
const itemText = (x: SumItem) => (typeof x === 'string' ? x : x.text);

export type Lang = 'zh' | 'en';

/** the audit's frozen "now" (page.clock.setFixedTime): clocks and "N seconds ago" stay the same in every screenshot */
export const NOW_MS = Date.parse('2026-11-18T10:30:00+08:00');
export const NOW_S = NOW_MS / 1000;
export const TZ = 'Asia/Shanghai';
/** the backend's local ISO time (no zone), `ago` seconds before now */
export const isoAgo = (ago: number) => new Date(NOW_MS - ago * 1000 + 8 * 3600 * 1000).toISOString().slice(0, 19);
const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

// ------------------------------------------------------------------------------------------------ stress strings
/** the longest things people can type: names (24), the AI's name (24), a guest's name (32), a meeting's title (60) */
export const STRESS = {
  hostZh: '欧阳慕容司马上官端木皇甫令狐长孙宇文轩辕东方南宫诸葛', // 24 CJK
  hostEn: 'Maximilian Montgomery-WW', // 24, wide Latin
  ai: 'Professor Wolfgang Mmmmmm', // 25 → cut to 24 below
  guestsZh: ['王一凡同学来自计算机科学与技术学院人工智能系的研究生一年级硕士生', '李思远', '赵雨桐', '陈子墨', '刘梓萱', '杨浩然', '黄诗涵', '周俊熙', '吴雨泽', '徐一诺', '孙铭轩', '马若曦'],
  guestsEn: ['Alexandra Konstantinopoulou-Wang', 'Li Siyuan', 'Zhao Yutong', 'Chen Zimo', 'Liu Zixuan', 'Yang Haoran', 'Huang Shihan', 'Zhou Junxi', 'Wu Yuze', 'Xu Yinuo', 'Sun Mingxuan', 'Ma Ruoxi'],
  titleZh: '基于大语言模型的组会论文实时修订助手面向研究生学术写作场景的多智能体协同系统设计与实现及其在真实组会中的评估研究报告全文', // 60
  titleEn: 'Real-time Paper Revision from Spoken Decisions in Group Meetin', // 60
  modelFast: 'qwen3.7-max-preview-2026-11-01-instruct-long-context',
  modelMain: 'my-company/deepseek-v4-flash-distilled-fp8-experimental',
  mic: 'Microphone Array (Intel® Smart Sound Technology for Digital Microphones)',
  speaker: 'Speakers (Realtek(R) Audio) - Dell U2723QE Monitor HDMI Output',
  url: 'https://example.com/a/very/long/link/that/someone/pasted/into/the/meeting/chat/without/any/spaces/at/all/2026-11-18/FooBench-final-final-v7-really-final.pdf?token=0123456789abcdef0123456789abcdef',
};
STRESS.ai = STRESS.ai.slice(0, 24);
STRESS.hostZh = [...STRESS.hostZh].slice(0, 24).join('');
STRESS.guestsZh[0] = [...STRESS.guestsZh[0]].slice(0, 32).join('');
STRESS.titleZh = STRESS.titleZh.slice(0, 60);
STRESS.titleEn = STRESS.titleEn.slice(0, 60);

// ------------------------------------------------------------------------------------------------ the world
export interface RelayState {
  title?: string;
  host?: string;
  live?: boolean;
  upcoming?: boolean;
  started?: string;
  ended?: string;
  v?: number;
  meeting?: Record<string, unknown>;
  papers?: { id: string; name: string }[];
  active_paper?: string | null;
  partial?: { text: string; t: string } | null;
  brain?: Brain | null;
  participants?: Participant[];
  assistant?: { name?: string; dialog?: string; edit?: string; effort?: string; tts?: string; asr?: string };
  focus?: unknown;
  bookmarks?: Bookmark[];
  anchor?: unknown;
  avatar_default?: string;
  lv?: string;
  /** 第 8 批: the host is recording (the join card's line, the bar's marker) */
  rec?: 'screen' | 'audio' | null;
  /** a meeting without a paper: mode "chat", paper false (no paper page, the AI stays out) */
  mode?: 'chat' | 'paper';
  paper?: boolean;
}

export interface World {
  lang: Lang;
  stress: boolean;
  /** false: every /api call is refused (the backend isn't up yet) */
  backendUp: boolean;
  current: Current;
  meetings: MeetingBrief[];
  details: Record<string, MeetingDetail>;
  /** null: /api/models answers 503 (can't switch) */
  models: Models | null;
  devices: Devices;
  mic: { muted: boolean; talk: string[] };
  profile: Profile;
  provider: Provider;
  invite: Invite;
  /** error code the next POST /api/profile answers with (empty, too_long, taken …), then cleared */
  profileError: string | null;
  /** has the host a photo of their own (GET /avatars/me) */
  avatarMe: boolean;
  /** what the stub paper page in the iframe reports (the paper tools in the bar) */
  paper: { cur: number; total: number; view: string; views: string[]; canEdit: boolean; editing: boolean; zoom: number; zoomSaved: boolean };
  /** the guest relay */
  relay: { sv: number; pv: number; state: RelayState | null; transcript: Line[]; board: Board | null; lastAct?: Record<string, unknown> };
  /** the first-run guide (/api/setup): done / skipped; the guide shows while neither */
  setup: { done: boolean; skipped: boolean; components?: Record<string, { ok: boolean; path?: string | null; detail?: string }> | null; voice_engine?: 'clone' | 'edge' | 'sapi' | null };
  /** the AI's voice and the words transcription should know (/api/voice) */
  voice: Record<string, unknown>;
  /** Settings → Storage (/api/storage, src/storageApi.ts StorageInfo) */
  storage: Record<string, unknown>;
  /** what the Tauri side says (fixtures/tauri-mock.ts): the speech models on this PC, Storage's paths */
  tauri: { speechInstalled: string[]; home: string; login: boolean; autoDays: number | null };
  /** Settings → General → Notes (/api/summary/prefs) */
  summaryPrefs: SummaryPrefs;
  /** Settings → Model source → this month (/api/usage) */
  usageMonth: MonthUsage;
  /** what an OpenAI-format endpoint lists (/api/provider/models); null: listing fails */
  providerModels: string[] | null;
  /** Settings → General → 散会后自动保存 (/api/autosave); off by default */
  autosave: AutosavePrefs;
  /** 第 6 批: Settings → General → 论文 (/api/paper/prefs) and the paper menu's 历史版本 (/api/paper/versions) */
  paperPrefs?: { docx_mode: 'track' | 'direct' };
  paperVersions?: PaperVersions;
  /** the lobby's left-over items (/api/carry); empty unless a screen turns them on (withCarry) */
  carry: CarryItem[];
  /** what withCarry puts there: the last meetings' to-dos, notes, edits not accepted */
  carryDemo: CarryItem[];
  /** 回顾 → 提问's answer (/api/past/ask) */
  askAnswer: PastAnswer;
  /** set: /api/past/ask fails with this (502) */
  askError: string | null;
  /** 回顾 → 阶段汇总: the saved ones, newest first (/api/digests) */
  digests: Digest[];
  /** strings typed by people (names, titles, model names): "…" on these is the app's intended behaviour */
  userStrings: string[];
}

const L = (lang: Lang) => (zh: string, en: string) => (lang === 'zh' ? zh : en);

export function baseWorld(lang: Lang, stress = false): World {
  const T = L(lang);
  const host = stress ? (lang === 'zh' ? STRESS.hostZh : STRESS.hostEn) : T('林晓', 'Lin Xiao');
  const ai = stress ? STRESS.ai : 'DeepSeek';
  const guests = stress ? (lang === 'zh' ? STRESS.guestsZh : STRESS.guestsEn) : [T('王一凡', 'Emma Wang')];
  const title = stress ? (lang === 'zh' ? STRESS.titleZh : STRESS.titleEn) : T('FooBench 论文合稿', 'FooBench paper draft');
  const fast = stress ? STRESS.modelFast : 'DeepSeek V4 Flash';
  const main = stress ? STRESS.modelMain : 'DeepSeek V4 Pro';
  const effort = stress ? 'Extra High Reasoning' : T('高', 'High');
  const palette = ['#60a5fa', '#e3906f', '#34d399', '#f472b6', '#a78bfa', '#fbbf24', '#22d3ee', '#f87171'];

  const people: Participant[] = [
    { id: 'me', name: host, role: 'teacher', rank: 'host', you: true, color: palette[0] },
    { id: 'claude', name: ai, role: 'student', rank: 'assistant', color: palette[1] },
    ...guests.map((g, i): Participant => ({
      id: `g${i + 1}`,
      name: g,
      role: 'guest',
      rank: i === 1 ? 'cohost' : 'participant',
      avatar: i % 3 === 0 ? `/avatars/g${i + 1}.png` : null,
      color: palette[(i + 2) % palette.length],
    })),
  ];
  const g1 = guests[0];
  const started = 1234; // seconds into the meeting at "now"

  const lines: Line[] = [
    { t: '00:12', sec: 12, who: 'teacher', text: T('我们开始吧，今天主要看第三节的实验设置。', "Let's start. Today we mainly look at the experimental setup in section 3.") },
    { t: '00:20', sec: 20, who: 'student', tag: '#chat', lat: 0.8, model: fast, text: T('好的，我看到第三节了。', 'Sure, I have section 3 open.') },
    { t: '01:05', sec: 65, who: 'guest', name: g1, text: T('表 2 的基线是不是少了一个？', 'Is a baseline missing from table 2?') },
    {
      t: '01:12', sec: 72, who: 'student', tag: '#deep', lat: 1.1, model: fast,
      text: T('稍等，我在论文里查一下。我让后台去核对表 2 的基线。', "One moment, I'll look in the paper. I'm asking the background to check the baselines in table 2."),
    },
    {
      t: '01:40', sec: 100, who: 'student', tag: '#deep-answer', took: 24, model: `${main} ${effort}`,
      text: T('查好了：表 2 列了 5 个基线，少了 TimeGAN，正文第 4 段提到了它。', 'Found it: table 2 lists 5 baselines and misses TimeGAN, which paragraph 4 mentions.'),
      steps: [
        { t: '01:20', k: T('搜论文', 'Searched the paper'), v: T('表 2 基线', 'table 2 baselines') },
        { t: '01:31', k: T('读', 'Read'), v: T('4.1 实验设置', '4.1 Experimental setup') },
      ],
    },
    { t: '02:10', sec: 130, who: 'teacher', text: T('那把 TimeGAN 补进表 2，顺便把 3.2 节第二段改清楚一点。', 'Then add TimeGAN to table 2, and make the second paragraph of 3.2 clearer.') },
    { t: '02:16', sec: 136, who: 'student', tag: '#edit', lat: 0.9, title: T('表 2 补上 TimeGAN 基线', 'Add the TimeGAN baseline to table 2'), model: fast, text: T('记下了，我来改表 2。', "Noted, I'll edit table 2.") },
    {
      t: '03:30', sec: 210, who: 'student', tag: '#report', task: 'T1', ok: true, title: T('表 2 补上 TimeGAN 基线', 'Add the TimeGAN baseline to table 2'), model: `${main} ${effort}`,
      text: T('表 2 改好了，加了一行 TimeGAN。', 'Table 2 is done: one row added for TimeGAN.'),
    },
    { t: '04:02', sec: 242, who: 'system', text: T(`${g1} 加入了会议`, `${g1} joined the meeting`) },
    { t: '19:58', sec: 1198, who: 'guest', name: g1, text: T('3.2 节的数据划分是按时间切的吗？', 'Is the data split in 3.2 by time?') },
    {
      t: '20:05', sec: 1205, who: 'student', tag: '#edit', lat: 1.2, title: T('改写 3.2 节第二段', 'Rewrite the second paragraph of 3.2'), model: fast,
      text: T('是按时间切的，我把这一点写进 3.2 节。', "Yes, by time. I'll make that explicit in 3.2."),
    },
    { t: '20:20', sec: 1220, who: 'teacher', text: T('好，第三节的实验设置我们再看一下，尤其是数据划分。', "Good. Let's look at the setup in section 3 again, the data split in particular.") },
  ];
  if (stress) {
    lines.push({
      t: '20:26', sec: 1226, who: 'guest', name: g1,
      text: T(
        '我补充一下，这个链接里是上次组会的录音整理稿，大家可以看看：' + STRESS.url + ' 另外我觉得 3.2 节的数据划分还需要再说清楚一点，比如训练集、验证集和测试集各自覆盖的时间范围，以及有没有做滚动窗口的交叉验证，如果做了的话窗口长度和步长分别是多少，这些都应该在正文里交代清楚，否则审稿人一定会问。'.repeat(3),
        'One more thing, the notes from last time are here: ' + STRESS.url + ' Also I think the data split in 3.2 needs to be clearer: which time range each of the train, validation and test sets covers, and whether there was rolling-window cross-validation, and if so the window length and the step. All of that should be in the text, or the reviewers will ask. '.repeat(3),
      ),
    });
  }

  const tasks: Task[] = [
    {
      id: 'T1', idx: 1, title: T('表 2 补上 TimeGAN 基线', 'Add the TimeGAN baseline to table 2'), remark: T('林晓：把 TimeGAN 补进表 2', 'Lin Xiao: add TimeGAN to table 2'),
      status: 'done', section: '4.1', created: isoAgo(1100), finished: isoAgo(1020), result: T('表 2 加了一行 TimeGAN。', 'Added a TimeGAN row to table 2.'), paper: 'p1', paper_name: 'FooBench.md',
      progress: { edits: 1 },
    },
    {
      id: 'T2', idx: 2, title: T('改写 3.2 节第二段', 'Rewrite the second paragraph of 3.2'), remark: '', status: 'working', section: '3.2', created: isoAgo(40), started: isoAgo(36), paper: 'p1', paper_name: 'FooBench.md',
      progress: { step: T('改写', 'Rewriting'), section: '3.2', edits: 1, started: NOW_S - 36, updated: NOW_S - 2, log: [T('定位 3.2 节', 'Found 3.2'), T('改写第二段', 'Rewriting paragraph 2')] },
    },
    { id: 'T3', idx: 3, title: T('统一图 3 的坐标轴单位', 'Use the same axis units in figure 3'), remark: '', status: 'queued', section: '4.3', created: isoAgo(20), paper: 'p1', paper_name: 'FooBench.md' },
    {
      id: 'N1', idx: 4, title: T('补充消融实验', 'Add an ablation study'), remark: T('林晓：会后补消融', 'Lin Xiao: add the ablation after the meeting'), status: 'done', note: true,
      plan: T('在 4.4 节加一张表，对比去掉记忆模块前后的结果', 'Add a table in 4.4 comparing with and without the memory module'), need: T('需要新跑实验', 'Needs new runs'), tags: ['rerun', 'figure'], created: isoAgo(500), paper: 'p1', paper_name: 'FooBench.md',
    },
    { id: 'A1', idx: 5, title: T('字幕延迟有点大', 'The captions lag a bit'), remark: '', status: 'done', note: true, app: true, created: isoAgo(300) },
  ];
  const board: Board = { counts: { queued: 1, working: 1, done: 3, failed: 0, notes: 2 }, tasks };

  const brain: Brain = {
    front: 'listening',
    front_model: fast,
    back_model: main,
    deep_model: `${main} ${effort}`,
    edit_model: `${main} ${effort}`,
    deeps: [],
    edits: [{ id: 'T2', idx: 2, title: tasks[1].title, started: NOW_S - 36, edits: 1, log: tasks[1].progress!.log }],
    queued: 1,
    unspoken: 0,
    now: NOW_S,
  };

  const papers = [
    { id: 'p1', name: stress ? (lang === 'zh' ? '组会论文实时修订助手-大创申报书-最终版-第七稿-真的最终版.docx' : 'FooBench-camera-ready-final-final-v7-really-final.md') : 'FooBench.md' },
    { id: 'p2', name: T('申报书.docx', 'proposal.docx') },
  ];

  // 第 3 批: two bookmarks
  const bookmarks: Bookmark[] = [
    { id: 'b1', sec: 130, t: '02:10', note: T('表 2 要补基线', 'Table 2 needs a baseline'), by: host, host: true },
    {
      id: 'b2', sec: 1199, t: '19:59', host: false, by: g1,
      note: stress ? T('数据划分'.repeat(25), 'data split '.repeat(9) + 'x').slice(0, 100) : '',
    },
  ];

  const current: Current = {
    backend: true,
    mode: 'paper',
    meeting: {
      id: 'm-live', title, started: isoAgo(started), ended: null, state: 'live', duration_s: started - 3, lang, title_auto: true, papers, active_paper: 'p1',
    },
    paper: {
      path: `C:\\Users\\maya\\Documents\\${papers[0].name}`, name: papers[0].name, exists: true, kind: 'md', words: 8231, id: 'p1', modified: '2026-11-18 09:40:12', bytes: 61234,
      outline: stress
        ? [T('第一章 绪论：基于大语言模型的组会论文实时修订助手的研究背景、意义与国内外研究现状综述', '1 Introduction: background, motivation and a survey of related work on real-time paper revision from spoken decisions'), T('第二章 相关工作', '2 Related work'), T('第三章 方法', '3 Method'), T('第四章 实验', '4 Experiments'), T('第五章 讨论', '5 Discussion'), T('第六章 结论与展望', '6 Conclusion and future work')]
        : ['1 Introduction', '2 Related work', '3 Method', '4 Experiments'],
    },
    ready: {
      paper: true, fast: true, tts: true, blurt: true, recording: true, hands: `${main} · ${effort}`,
      models: { fast, hands: `${main} · ${effort}`, tts: 'CosyVoice 3', asr: 'Fun-ASR Nano' },
      fast_online: true, fast_ms: 820, hands_ready: true, working: 1, queued: 1,
      devices: { mic: stress ? STRESS.mic : T('麦克风阵列 (Realtek(R) Audio)', 'Microphone Array (Realtek(R) Audio)'), speaker: stress ? STRESS.speaker : T('扬声器 (Realtek(R) Audio)', 'Speakers (Realtek(R) Audio)') },
    },
    transcript: lines,
    board,
    summary: null,
    participants: people,
    partial: { text: T('那第三节的实验设置我们', 'So the setup in section 3, we'), t: mmss(started - 2) },
    brain,
    role_suggest: [],
    papers,
    active_paper: 'p1',
    processing: false,
    bookmarks,
    anchor: null,
    quiet_s: 30,
  };

  // the 纪要 as the backend writes it now: each line says when it was said (a transcript line's t / sec), one entry per
  // person; one conclusion and one to-do are plain strings, as older summaries hold them
  const at = (sec: number) => ({ t: mmss(sec), sec });
  const summary: Summary = {
    title: T('FooBench 论文合稿 · 组会纪要', 'FooBench paper draft · meeting notes'),
    speakers: [host, ai, ...guests.slice(0, 3)],
    template: 'default',
    abstract: T(
      `${host} 和 ${g1} 过了一遍第三、四节：表 2 补上了 TimeGAN 基线，3.2 节的数据划分改写得更清楚；消融实验留到会后。`,
      `${host} and ${g1} went through sections 3 and 4: TimeGAN was added to table 2, the data split in 3.2 was made clearer; the ablation is left for after the meeting.`,
    ),
    conclusions: [
      { text: T('表 2 补上 TimeGAN', 'Add TimeGAN to table 2'), ...at(130) },
      { text: T('3.2 节说明按时间划分', 'State in 3.2 that the split is by time'), ...at(1205) },
      T('消融实验会后补', 'Ablation after the meeting'),
    ],
    sections: [
      {
        title: T('实验设置', 'Experimental setup'),
        ...at(65),
        cards: [
          { title: T('基线', 'Baselines'), ...at(65), bullets: [{ text: T('表 2 少了 TimeGAN，已补', 'Table 2 missed TimeGAN: added'), ...at(100) }, { text: T('正文第 4 段与表 2 对齐', 'Paragraph 4 now matches table 2'), ...at(130) }] },
          { title: T('数据划分', 'Data split'), ...at(1198), bullets: [{ text: T('按时间切分', 'Split by time'), ...at(1205) }, T('写明各集合的时间范围', 'State the time range of each set')] },
        ],
      },
    ],
    people: [
      { name: host, said: [{ text: T('今天主要看第三节的实验设置', 'Today is about the setup in section 3'), ...at(12) }, { text: T('把 TimeGAN 补进表 2，3.2 节第二段改清楚', 'Add TimeGAN to table 2; make 3.2 paragraph 2 clearer'), ...at(130) }], feedback: [], asks: [] },
      {
        name: g1,
        said: [{ text: T('表 2 的基线少了一个', 'A baseline is missing from table 2'), ...at(65) }, { text: T('问 3.2 节是不是按时间划分', 'Asked whether 3.2 splits by time'), ...at(1198) }],
        feedback: [{ text: T('数据划分要写清楚', 'The data split must be spelled out'), by: host, ...at(1220) }],
        asks: [{ text: T('核对表 2 的基线', 'Check the baselines in table 2'), by: host, ...at(130) }],
      },
      ...guests.slice(1).map((g, i) => ({ name: g, said: [{ text: T('汇报了这周的进展', 'Reported this week’s progress'), ...at(1198 + i) }], feedback: [], asks: [] })),
    ],
    todos: [{ text: T('补消融实验（需要新跑）', 'Run the ablation (needs new runs)'), who: host, ...at(130) }, T('图 3 坐标轴单位统一', 'Same axis units in figure 3')],
    // what the meeting before left: one got done this time, one didn't come up
    carried: [
      {
        id: 'c1', kind: 'todo', item: T('表 2 补上 TimeGAN 基线', 'Add the TimeGAN baseline to table 2'), from: 'm-002', started: isoAgo(86400 * 2), done: true,
        text: stress
          ? T(`${g1} 跑完了 TimeGAN，表 2 已经补上一行，正文第 4 段也跟着改了，方差和置信区间下周再补`, `${g1} ran TimeGAN; table 2 has the row now and paragraph 4 follows it; the variance and confidence intervals come next week`)
          : T(`${g1} 跑完了，表 2 已补`, `${g1} ran it; table 2 has it`),
        ...at(130),
      },
      { id: 'c2', kind: 'note', item: T('图 3 坐标轴单位统一', 'Same axis units in figure 3'), from: 'm-002', started: isoAgo(86400 * 2), done: false, text: T('会上没提到', 'Not mentioned') },
    ],
    generated: isoAgo(100),
  };
  // each person's talk time (the backend works it out from the transcript), longest first
  const talk: TalkTime[] = [
    { who: 'teacher', name: null, sec: 412.5, n: 9, share: 0.41 },
    { who: 'student', name: null, sec: 301.2, n: 14, share: 0.3 },
    { who: 'guest', name: g1, sec: 220.4, n: 5, share: 0.22 },
    ...guests.slice(1).map((g, i): TalkTime => ({ who: 'guest', name: g, sec: 20 - i, n: 1, share: 0.006 })),
  ];

  // 回顾: past meetings, newest first
  const nMeetings = stress ? 40 : 6;
  const meetings: MeetingBrief[] = [];
  const details: Record<string, MeetingDetail> = {};
  for (let i = 0; i < nMeetings; i++) {
    const id = `m-${String(i + 1).padStart(3, '0')}`;
    const ago = 86400 * (i + 1) + 3600 * (i % 5);
    const dur = 900 + ((i * 731) % 5400);
    const mt = stress
      ? i % 2 === 0
        ? STRESS.titleZh
        : STRESS.titleEn
      : [T('FooBench 论文合稿', 'FooBench paper draft'), T('大创申报书修改', 'Grant proposal revision'), T('第三章实验讨论', 'Chapter 3 experiments'), 'Weekly sync', T('开题报告', 'Thesis proposal'), T('投稿前最后一遍', 'Last pass before submission')][i % 6];
    meetings.push({
      id, title: mt, started: isoAgo(ago), ended: isoAgo(ago - dur), state: 'ended', duration_s: dur, n_tasks: 4, n_done: 3, n_lines: lines.length, has_summary: true, has_diff: true, processing: false,
      // the papers it had open (回顾 filters by them): most meetings the main paper, some the proposal, one both
      papers: i % 3 === 1 ? [papers[1]] : i === 2 ? [papers[0], papers[1]] : [papers[0]],
    });
    details[id] = {
      meeting: { id, title: mt, started: isoAgo(ago), ended: isoAgo(ago - dur), state: 'ended', duration_s: dur, lang, title_auto: true, papers, active_paper: 'p1' },
      // `i`: the line's place in the transcript as said (回顾 → 编辑 names a line by it)
      transcript: lines.map((l, k) => ({ ...l, vt: k * 2, i: k })),
      board,
      summary: { ...summary, title: mt },
      diff_url: `/snap/${id}/`,
      participants: people,
      video: i === 0 ? { url: `/api/meetings/${id}/video`, type: 'video/webm', duration: 3, bytes: 4000, offset: 0 } : null,
      processing: false,
      post: null,
      talk,
      summary_job: null,
      bookmarks,
      carry_done: ['note:N1'],
      edits: { n: 0, last: null },
    };
  }

  // the lobby's left-over items from the last meetings on this paper (withCarry turns them on)
  const carryTexts = stress
    ? [
        T('补消融实验：去掉记忆模块和去掉注意力模块各跑一组，三个随机种子，结果放进 4.4 节新的表里，正文同步改写', 'Run the ablation: without the memory module and without attention, three seeds each, results in a new table in 4.4 and the text updated to match'),
        T('图 3 坐标轴单位统一成毫秒，图例字号和正文一致', 'Same axis units (ms) in figure 3, legend font size as in the text'),
        T('3.2 节第二段写清楚按时间划分训练集、验证集和测试集各自覆盖的时间范围', 'Make 3.2 paragraph 2 say which time range each of the train, validation and test sets covers'),
      ]
    : [
        T('补消融实验（需要新跑）', 'Run the ablation (needs new runs)'),
        T('图 3 坐标轴单位统一', 'Same axis units in figure 3'),
        T('3.2 节第二段改清楚', 'Make 3.2 paragraph 2 clearer'),
        T('表 2 补上方差', 'Add the variance to table 2'),
        T('相关工作补两篇 2025 年的', 'Two 2025 papers in related work'),
      ];
  const carryKinds: CarryItem['kind'][] = ['todo', 'note', 'edit'];
  const carryDemo: CarryItem[] = Array.from({ length: stress ? 23 : 5 }, (_, i) => {
    const m = meetings[(i % 3) * 3 % meetings.length];
    return {
      id: `${m.id}/${carryKinds[i % 3]}:${i}`, mid: m.id, title: m.title, started: m.started, kind: carryKinds[i % 3],
      text: carryTexts[i % carryTexts.length] + (i >= carryTexts.length ? ` (${i + 1})` : ''),
      who: i % 3 === 0 ? (i % 2 ? g1 : host) : null,
      ...(i % 3 === 0 ? at(130) : {}),
    };
  });
  // 回顾 → 提问: an answer with two sources
  const askAnswer: PastAnswer = {
    found: true,
    answer: stress
      ? T(`11月17日那次 ${g1} 问到第三章的消融实验，${host} 说不够，要补一组去掉注意力模块的对照 [1]。11月16日那次又提过一次，要求三个随机种子 [2]。`.repeat(2),
          `On Nov 17 ${g1} asked about the ablation in chapter 3; ${host} said it wasn't enough and asked for a run without attention [1]. On Nov 16 it came up again, with three seeds [2]. `.repeat(2))
      : T(`11月17日那次 ${host} 说第三章的消融实验不够，要补一组去掉注意力模块的对照 [1]，11月16日那次也提过 [2]。`,
          `On Nov 17 ${host} said the ablation in chapter 3 wasn't enough and asked for a run without attention [1]; it came up on Nov 16 too [2].`),
    sources: [
      { n: 1, mid: meetings[0].id, title: meetings[0].title, started: meetings[0].started, t: '02:10', sec: 130, text: T(`${host}：那把 TimeGAN 补进表 2，消融实验也要补一组`, `${host}: add TimeGAN to table 2, and one more ablation run`) + (stress ? ' ' + STRESS.url : '') },
      { n: 2, mid: meetings[1].id, title: meetings[1].title, started: meetings[1].started, t: '19:58', sec: 1198, text: T(`${g1}：3.2 节的数据划分是按时间切的吗？`, `${g1}: Is the data split in 3.2 by time?`) },
    ],
  };
  // 回顾 → 阶段汇总: one saved
  const dgMeetings = meetings.slice(0, 3).reverse().map((m) => ({ id: m.id, title: m.title, started: m.started }));
  const digests: Digest[] = [
    {
      id: 'd-001', created: isoAgo(3600), state: 'done', error: null, mids: dgMeetings.map((m) => m.id), meetings: dgMeetings,
      title: stress ? T('十一月第三周到第四周组会阶段汇总：第三章实验与数据划分', 'Weeks 3–4 of November: chapter 3 experiments and the data split') : T('十一月阶段汇总', 'November digest'),
      result: {
        title: stress ? T('十一月第三周到第四周组会阶段汇总：第三章实验与数据划分', 'Weeks 3–4 of November: chapter 3 experiments and the data split') : T('十一月阶段汇总', 'November digest'),
        overview: T('这三次会都在改第三章：补基线、补消融、把数据划分写清楚。', 'All three meetings worked on chapter 3: baselines, the ablation, and the data split.'),
        issues: [
          { text: T('消融实验够不够', 'Is the ablation enough'), meetings: [1, 2, 3] },
          { text: T('数据划分是不是按时间切', 'Is the split by time'), meetings: [2, 3] },
        ],
        feedback: [{ text: T('数据划分要写清楚', 'Spell out the data split'), by: host, meetings: [2, 3] }],
        open: [{ text: carryTexts[0], who: g1, meeting: 3 }, { text: carryTexts[1], who: '', meeting: 2 }],
      },
      markdown: '# digest',
    },
    {
      id: 'd-000', created: isoAgo(86400), state: 'failed', error: T('还没设置模型来源', 'no model source set'), mids: dgMeetings.slice(0, 2).map((m) => m.id),
      meetings: dgMeetings.slice(0, 2), title: '', result: null, markdown: '',
    },
  ];

  const profile: Profile = {
    name: host, avatar: null, custom: false, assistant: stress ? ai : '', assistant_default: 'DeepSeek', assistant_custom: stress,
    color: palette[0], ai_color: palette[1], palette,
  };
  const presets = {
    deepseek: { label: 'DeepSeek', base_url: 'https://api.deepseek.com/anthropic', fast: 'deepseek-v4-flash', main: 'deepseek-v4-pro' },
    kimi: { label: 'Kimi', base_url: 'https://api.moonshot.cn/anthropic', fast: 'kimi-k3-turbo', main: 'kimi-k3' },
    glm: { label: T('智谱 GLM', 'Zhipu GLM'), base_url: 'https://open.bigmodel.cn/api/anthropic', fast: 'glm-5-air', main: 'glm-5' },
    qwen: { label: T('通义千问', 'Qwen'), base_url: 'https://dashscope.aliyuncs.com/apps/anthropic', fast: 'qwen3.7-flash', main: 'qwen3.7-max' },
    anthropic: { label: 'Anthropic', base_url: 'https://api.anthropic.com', fast: 'claude-haiku-5-5', main: 'claude-opus-5-5' },
    custom: { label: '', base_url: '', fast: '', main: '' },
    openai: { label: '', base_url: '', fast: '', main: '', format: 'openai' as const, key: 'optional' as const },
    ollama: { label: 'Ollama', base_url: 'http://127.0.0.1:11434/v1', fast: '', main: '', format: 'openai' as const, key: 'none' as const, local: true },
    lmstudio: { label: 'LM Studio', base_url: 'http://127.0.0.1:1234/v1', fast: '', main: '', format: 'openai' as const, key: 'none' as const, local: true },
  };
  const provider: Provider = stress
    ? { kind: 'custom', base_url: 'https://llm-gateway.internal.example-university.edu.cn/v1/anthropic-compatible/endpoint', fast: STRESS.modelFast, main: STRESS.modelMain, key_set: true, key_tail: '9f3a', active: true, label: 'Custom', lit: true, presets }
    : { kind: 'deepseek', base_url: presets.deepseek.base_url, fast: 'deepseek-v4-flash', main: 'deepseek-v4-pro', key_set: true, key_tail: 'a1b2', active: true, label: 'DeepSeek', lit: true, presets };
  // this month's API usage (Settings) and each past meeting's (回顾 → 纪要, last)
  const usageMonth: MonthUsage = stress
    ? {
        month: '2026-11', mode: 'api', tokens: 98_765_432, costs: { CNY: 1234.56, USD: 78.9 },
        models: [
          { model: STRESS.modelMain, pretty: STRESS.modelMain, label: 'Custom', calls: 4321, in: 60_000_000, out: 8_765_432, cache_read: 30_000_000, cache_write: 0, tokens: 98_765_432, currency: null, cost: null },
        ],
      }
    : {
        month: '2026-11', mode: 'api', tokens: 3_456_000, costs: { CNY: 6.42 },
        models: [
          { model: 'deepseek-v4-pro', pretty: 'DeepSeek V4 Pro', label: 'DeepSeek', calls: 120, in: 1_800_000, out: 260_000, cache_read: 900_000, cache_write: 0, tokens: 2_960_000, currency: 'CNY', cost: 5.31 },
          { model: 'deepseek-v4-flash', pretty: 'DeepSeek V4 Flash', label: 'DeepSeek', calls: 410, in: 300_000, out: 96_000, cache_read: 100_000, cache_write: 0, tokens: 496_000, currency: 'CNY', cost: 1.11 },
        ],
      };
  const meetingUsage: Usage = stress
    ? {
        tokens: 12_345_678, costs: { CNY: 123.45 },
        models: [
          { model: STRESS.modelMain, pretty: STRESS.modelMain, label: 'Custom', calls: 321, in: 9_000_000, out: 1_345_678, cache_read: 2_000_000, cache_write: 0, tokens: 12_345_678, currency: null, cost: null },
          { model: 'deepseek-v4-pro', pretty: 'DeepSeek V4 Pro', label: 'DeepSeek', calls: 99, in: 400_000, out: 60_000, cache_read: 0, cache_write: 0, tokens: 460_000, currency: 'CNY', cost: 123.45 },
        ],
      }
    : {
        tokens: 186_400, costs: { CNY: 0.42 },
        models: [
          { model: 'deepseek-v4-pro', pretty: 'DeepSeek V4 Pro', label: 'DeepSeek', calls: 6, in: 120_000, out: 18_400, cache_read: 30_000, cache_write: 0, tokens: 168_400, currency: 'CNY', cost: 0.39 },
          { model: 'deepseek-v4-flash', pretty: 'DeepSeek V4 Flash', label: 'DeepSeek', calls: 31, in: 14_000, out: 4_000, cache_read: 0, cache_write: 0, tokens: 18_000, currency: 'CNY', cost: 0.03 },
        ],
      };
  for (const id of Object.keys(details)) details[id].usage = meetingUsage;

  const models: Models = {
    dialog: { current: 'fast', options: [{ id: 'fast', label: fast }, { id: 'main', label: main }] },
    edit: { current: 'main', options: [{ id: 'main', label: main }, { id: 'fast', label: fast }] },
    effort: {
      current: 'high',
      options: [
        { id: 'low', label: T('低', 'Low') },
        { id: 'medium', label: T('中', 'Medium') },
        { id: 'high', label: effort },
        { id: 'max', label: T('最高', 'Max') },
      ],
    },
    tts: 'CosyVoice 3',
    asr: 'Fun-ASR Nano',
  };

  const relayState: RelayState = {
    title, host, live: true, started: isoAgo(started), v: 2,
    meeting: { id: 'm-live', title, started: isoAgo(started), duration_s: started, lang },
    papers, active_paper: 'p1', partial: current.partial, brain,
    participants: people.map((p) => ({ ...p, you: false })),
    assistant: { name: ai, dialog: fast, edit: main, effort, tts: 'CosyVoice 3', asr: 'Fun-ASR Nano' },
    focus: null, lv: '1',
    bookmarks, anchor: null,
    rec: 'screen',
  };

  return {
    lang, stress, backendUp: true, current, meetings, details, models,
    devices: { mic: current.ready!.devices!.mic, speaker: current.ready!.devices!.speaker },
    mic: { muted: false, talk: [] },
    profile, provider, usageMonth,
    providerModels: stress ? Array.from({ length: 60 }, (_, i) => `${STRESS.modelMain}-${i}`) : ['gemma3:4b', 'llama3.1:8b', 'qwen3:8b', 'qwen3:14b'],
    invite: { ok: true, url: 'https://relay.example.org/room/k3J9xQ2mZp', room: 'k3J9xQ2mZp', relay: true, public: true, armed: false, guests: guests.length },
    profileError: null, avatarMe: false,
    paper: { cur: 1, total: 6, view: 'mark', views: ['mark', 'new', 'old'], canEdit: true, editing: false, zoom: 1, zoomSaved: true },
    relay: { sv: 1, pv: 1, state: relayState, transcript: lines, board },
    setup: {
      done: true,
      skipped: false,
      components: { asr: { ok: true, path: 'C:\\Users\\maya\\.blurt\\models\\fun-asr-nano-int8' }, pandoc: { ok: true }, ffmpeg: { ok: true }, edge_tts: { ok: true } },
      voice_engine: 'edge',
    },
    voice: {
      voice: 'auto',
      edge_voice: 'zh-CN-XiaoxiaoNeural',
      sapi_voice: '',
      hotwords: stress ? Array.from({ length: 40 }, (_, i) => `${['FooBench', 'TimeGAN', 'TSGBench', 'Diffusion-TS', 'CSDI', '消融实验', '组会', 'FinTS'][i % 8]}${i >= 8 ? i : ''}`) : ['FooBench', 'TimeGAN', 'TSGBench'],
      hot_max: 200,
      hot_len: 40,
      // 第 7 批: 读法 (200 at most, 40 characters each side; stress: all 200, the longest)
      pron: stress
        ? Array.from({ length: 200 }, (_, i) => ({ w: `${'W'.repeat(36)}${String(i).padStart(3, '0')}`, r: '读'.repeat(40) }))
        : [{ w: 'LaTeX', r: lang === 'zh' ? '雷泰克' : 'lay-tek' }, { w: 'BERT', r: lang === 'zh' ? '伯特' : 'bert' }],
      pron_max: 200,
      pron_len: 40,
      edge_voices: [
        { id: 'zh-CN-XiaoxiaoNeural', lang: 'zh', gender: 'f' },
        { id: 'zh-CN-YunxiNeural', lang: 'zh', gender: 'm' },
        { id: 'en-US-AriaNeural', lang: 'en', gender: 'f' },
        { id: 'en-US-GuyNeural', lang: 'en', gender: 'm' },
      ],
      sapi_voices: [
        { name: 'Microsoft Huihui Desktop', lang: 'zh-CN', gender: 'Female' },
        { name: 'Microsoft Zira Desktop', lang: 'en-US', gender: 'Female' },
      ],
      has_clone: false,
      has_edge: true,
    },
    storage: (() => {
      const home = stress ? 'C:\\Users\\Maximilian.Montgomery-WW' : 'C:\\Users\\maya';
      const GB = 1e9;
      const t = (count: number, bytes: number) => ({ count, bytes });
      return {
        total: 14.2 * GB,
        free: 214 * GB,
        split: { recordings: 9.8 * GB, models: 3.6 * GB, records: 0.4 * GB, other: 0.4 * GB },
        recordings: {
          bytes: 9.8 * GB, meetings: 14, folder: `${home}\\.zuhui\\recordings`,
          older: { '30': t(6, 4.1 * GB), '90': t(3, 2.2 * GB), '180': t(1, 0.7 * GB), all: t(14, 9.8 * GB) },
          others: { ...t(2, 0.9 * GB), ready: t(2, 0.9 * GB) },
          auto_days: 0,
          auto_preview: { '30': t(6, 4.1 * GB), '90': t(3, 2.2 * GB), '180': t(1, 0.7 * GB) },
        },
        models: {
          bytes: 3.6 * GB, folder: `${home}\\.zuhui\\models`,
          used: [
            { role: 'live', name: 'Fun-ASR Nano', paths: [`${home}\\.blurt\\models\\fun-asr-nano-int8`], bytes: 0.99 * GB, langs: [] },
            { role: 'after', name: stress ? 'SenseVoice-Small-Long-Form-Transcription-With-Punctuation-v2' : 'SenseVoice Small', paths: [`${home}\\.blurt\\models\\sensevoice-small`], bytes: 0.23 * GB, langs: [] },
            { role: 'addr', name: 'Bloom turn model', paths: [`${home}\\.zuhui\\models\\addr-zh`], bytes: 1.1 * GB, langs: ['zh', 'en'] },
            { role: 'voice', name: 'CosyVoice 3', paths: [`${home}\\.zuhui\\models\\cosyvoice3`], bytes: 1.2 * GB, langs: [] },
          ],
          unused: [{ path: `${home}\\.zuhui\\models\\old-whisper-large-v3`, name: 'whisper-large-v3', bytes: 3.1 * GB }],
        },
        records: {
          bytes: 0.4 * GB, meetings: { ...t(14, 0.3 * GB), path: `${home}\\.zuhui\\meetings` }, papers: { ...t(9, 0.1 * GB), path: `${home}\\.zuhui\\papers` },
          // 第 8 批: what turning on "delete transcripts and notes" would take now (the confirmation lists them)
          retention: (() => {
            const old = Array.from({ length: stress ? 40 : 3 }, (_, i) => ({
              id: `20260${(i % 9) + 1}0${(i % 3) + 1}-1000${String(i).padStart(2, '0')}`,
              title: stress ? (lang === 'zh' ? STRESS.titleZh : STRESS.titleEn) : T(['周三组会', '开题预答辩', '论文第三章'][i % 3], ['Wednesday group meeting', 'Proposal rehearsal', 'Chapter 3'][i % 3]),
              started: `2026-0${(i % 9) + 1}-0${(i % 3) + 1}T10:00:00`,
            }));
            const of = (n: number) => ({ count: n, bytes: n * 180_000, meetings: old.slice(0, n) });
            return { days: 0, preview: { '30': of(old.length), '90': of(Math.ceil(old.length / 2)), '180': of(1), '365': of(0) } };
          })(),
        },
        caches: { bytes: 0.3 * GB, clearable: 0.25 * GB, path: `${home}\\.zuhui\\cache` },
        logs: { bytes: 0.05 * GB, path: `${home}\\.zuhui\\logs` },
      };
    })(),
    tauri: { speechInstalled: ['fun-asr-nano-onnx', 'sensevoice-small'], home: stress ? 'C:\\Users\\Maximilian.Montgomery-WW' : 'C:\\Users\\maya', login: false, autoDays: null },
    summaryPrefs: { template: 'default', custom: '', custom_max: 2000 },
    autosave: { on: false, dir: '', items: ['minutes_docx', 'todos', 'diff'], dir_max: 180, last: null },
    carry: [],
    carryDemo,
    askAnswer,
    askError: null,
    digests,
    userStrings: [...tasks.map((x) => x.title), ...tasks.map((x) => x.plan ?? '').filter(Boolean), ...tasks.map((x) => x.result ?? '').filter(Boolean), ...tasks.map((x) => x.remark).filter(Boolean), ...summary.conclusions.map(itemText), host, ai, ...guests, title, fast, main, effort, ...papers.map((p) => p.name), STRESS.mic, STRESS.speaker, current.ready!.devices!.mic!, current.ready!.devices!.speaker!, 'DeepSeek V4 Pro', 'DeepSeek V4 Flash', 'CosyVoice 3', 'Fun-ASR Nano', ...meetings.map((m) => m.title), STRESS.url, provider.base_url,
      ...carryDemo.map((x) => x.text), ...bookmarks.map((b) => b.note).filter(Boolean), ...askAnswer.sources.map((x) => x.text), ...askAnswer.sources.map((x) => x.title), ...digests.map((d) => d.title), ...digests.map((d) => d.error ?? '').filter(Boolean)],
  };
}

// ------------------------------------------------------------------------------------------------ variants
/** before the meeting: a paper on the desk, the last meeting's name, nothing going on */
export function idle(w: World): World {
  const c = w.current;
  c.meeting = { ...c.meeting!, state: 'ended', ended: isoAgo(600), duration_s: 1834 };
  c.brain = null;
  c.partial = null;
  c.transcript = [];
  c.participants = c.participants!.filter((p) => p.id === 'me' || p.id === 'claude');
  c.board = { counts: {}, tasks: [] };
  c.anchor = null;
  return w;
}
/** the lobby with what the last meetings on this paper left undone */
export function withCarry(w: World): World {
  idle(w);
  w.carry = clone(w.carryDemo);
  return w;
}
/** 第 6 批: the paper's saved versions (the paper menu's 历史版本): before / after three meetings, a restore. Stress: long
 *  meeting names, 120 versions */
export function withVersions(w: World): World {
  const T = (zh: string, en: string) => (w.lang === 'zh' ? zh : en);
  const names = w.stress
    ? [T('FooBench 论文合稿第三轮修改讨论（老师提了很多关于实验部分和图表排版的意见，需要逐条处理）', 'FooBench joint draft, third round of edits (the advisor had many comments on the experiments and the figure layout)')]
    : [T('第三次组会', 'Third meeting'), T('第二次组会', 'Second meeting'), T('第一次组会', 'First meeting')];
  const n = w.stress ? 120 : 6;
  const items = Array.from({ length: n }, (_, k) => {
    const m = Math.floor(k / 2);
    const kind = k === 0 ? 'restore' : k % 2 ? 'after' : 'before';
    return {
      v: String(n - k), kind, at: isoAgo(3600 * 24 * (m + 1) + k * 60), mid: `m${m}`,
      title: kind === 'restore' ? null : names[m % names.length], chars: 18000 + k * 37, same: k === 0,
      ...(kind === 'restore' ? { of: String(n - 3) } : {}),
    };
  });
  const paper = { id: w.current.active_paper ?? 'p1', name: w.current.paper?.name ?? 'paper' };
  w.paperVersions = { paper, items, current: { sha: 'x', chars: 18000 }, live: w.current.meeting?.state === 'live' };
  w.userStrings.push(...names, paper.name);     // meeting titles and the paper's name: what people typed
  return w;
}
/** 第 6 批: comments typed on the paper (the paper's page draws them; the shell only passes them on) */
export function withComments(w: World): World {
  const T = (zh: string, en: string) => (w.lang === 'zh' ? zh : en);
  const long = w.stress ? T('这一段的论证还不够，'.repeat(45), 'This paragraph does not argue it well enough yet. '.repeat(10)).slice(0, 500) : '';
  const c: PaperComment[] = [
    { id: 'c1', paper: w.current.active_paper ?? null, key: 'x', quote: T('第一段', 'First paragraph'), ri: 1, by: w.stress ? 'Alexandra Konstantinopoulou-Wang' : T('赵同学', 'Sam Zhao'),
      at: isoAgo(600), t: '12:03', text: long || T('这句能不能举个具体例子？', 'Can this sentence get a concrete example?'), resolved: false,
      replies: [{ id: 'c1-1', by: w.current.participants?.[0]?.name ?? 'host', at: isoAgo(500), t: '13:10', text: T('下周补上', 'Next week') }] },
    { id: 'c2', paper: w.current.active_paper ?? null, key: 'y', quote: T('实验', 'Experiments'), ri: 4, by: T('李老师', 'Prof. Li'), at: isoAgo(300), t: '30:00',
      text: T('要写清楚是哪个数据集。', 'Say which dataset this is.'), resolved: true, resolved_by: T('赵同学', 'Sam Zhao'), replies: [] },
  ];
  w.current.comments = c;
  if (w.relay.state) (w.relay.state as { comments?: PaperComment[] }).comments = clone(c);
  return w;
}
// ------------------------------------------------------------------------------------------------ no paper (mode "chat")
// A meeting without a paper keeps only the transcript: no paper column, the AI not among the people, no AI lines, no
// board; its name is the backend's "组会 · <date>" (it is the meeting's name: listed as user content)
const humanOnly = (ps: Participant[] | null | undefined) => (ps ?? []).filter((p) => p.id !== 'claude');
const humanLines = (ls: Line[]) => ls.filter((l) => l.who !== 'student');
/** the lobby with no paper picked (the next meeting keeps only the transcript); everything else ready */
export function noPaper(w: World): World {
  idle(w);
  const c = w.current;
  c.paper = null;
  c.papers = [];
  c.active_paper = null;
  c.mode = 'chat';
  return w;
}
/** the lobby whose chosen paper's file is gone (moved or renamed): its name, 文件找不到了, 开始会议 off until 更换 / 移除 */
export function paperMissing(w: World): World {
  idle(w);
  const c = w.current;
  const name = c.paper?.name ?? 'FooBench.md';
  c.paper = { path: `C:\\Users\\maya\\Documents\\${name}`, name, exists: false };
  c.mode = 'chat';
  return w;
}
/** in a meeting without a paper: the host and the guests talk, the transcript takes the page */
export function chatLive(w: World): World {
  const T = L(w.lang);
  const c = w.current;
  const title = T('组会 · 11月18日', 'Meeting · Nov 18');
  c.mode = 'chat';
  c.meeting = { ...c.meeting!, title, mode: 'chat', papers: [], active_paper: null };
  c.paper = null;
  c.papers = [];
  c.active_paper = null;
  c.participants = humanOnly(c.participants);
  c.transcript = humanLines(c.transcript);
  c.brain = null;
  c.board = { counts: {}, tasks: [] };
  c.anchor = null;
  c.comments = [];
  c.ready = { ...c.ready!, working: 0, queued: 0 };
  const r = w.relay;
  if (r.state) {
    Object.assign(r.state, {
      title, mode: 'chat', paper: false, papers: [], active_paper: null, brain: null, anchor: null,
      meeting: { ...r.state.meeting, title },
      participants: humanOnly(r.state.participants),
    });
  }
  r.transcript = humanLines(r.transcript);
  r.board = null;
  w.userStrings.push(title);
  return w;
}
/** 回顾: the newest past meeting was held without a paper (纪要, transcript, recording; no 修订对比, no board) */
export function chatPast(w: World): World {
  const T = L(w.lang);
  const title = T('组会 · 11月17日', 'Meeting · Nov 17');
  const m = w.meetings[0];
  Object.assign(m, { title, mode: 'chat', papers: [], has_diff: false, n_tasks: 0, n_done: 0 });
  const d = w.details[m.id];
  d.meeting = { ...d.meeting, title, mode: 'chat', papers: [], active_paper: null };
  d.diff_url = null;
  d.board = { counts: {}, tasks: [] };
  d.participants = humanOnly(d.participants);
  d.transcript = humanLines(d.transcript);
  d.talk = (d.talk ?? []).filter((x) => x.who !== 'student');
  d.comments = [];
  d.carry_done = [];
  if (d.summary) d.summary = { ...d.summary, title, speakers: d.summary.speakers.filter((s) => s !== w.profile.assistant_default && s !== w.profile.assistant) };
  w.userStrings.push(title);
  return w;
}
/** a fresh install: no paper, no meeting, no model source picked */
export function fresh(w: World): World {
  idle(w);
  const c = w.current;
  c.meeting = null;
  c.paper = null;
  c.papers = [];
  c.active_paper = null;
  c.mode = 'chat';
  w.provider = { ...w.provider, kind: 'unset', base_url: '', fast: '', main: '', key_set: false, key_tail: '', active: false };
  w.usageMonth = { month: w.usageMonth.month, mode: 'none', models: [], tokens: 0, costs: {} };
  w.meetings = [];
  w.details = {};
  // (the first-run guide was skipped: see firstRun for the guide itself); no speech model downloaded yet
  w.setup = { ...w.setup, done: false, skipped: true };
  w.tauri = { ...w.tauri, speechInstalled: [] };
  return w;
}
/** a fresh install whose first-run guide hasn't been done or skipped: the guide covers the window */
export function firstRun(w: World): World {
  fresh(w);
  w.setup = { ...w.setup, done: false, skipped: false };
  w.profile = { ...w.profile, name: '', assistant: '', assistant_custom: false };
  return w;
}
/** the meeting just ended (the 会议 page keeps showing it) */
export function ended(w: World): World {
  const c = w.current;
  c.meeting = { ...c.meeting!, state: 'ended', ended: isoAgo(60), duration_s: 1180 };
  c.brain = null;
  c.partial = null;
  c.ready = { ...c.ready!, recording: false, working: 0, queued: 0 };
  return w;
}
/** 回顾 with one meeting still being processed (recording → transcript → cleanup → notes) */
export function processing(w: World): World {
  const T = L(w.lang);
  const m = w.meetings[0];
  m.processing = true;
  m.has_summary = false;
  const d = w.details[m.id];
  d.processing = true;
  d.summary = null;
  d.post = {
    steps: [
      { key: 'video', label: T('保存录屏', 'Saving the recording'), state: 'done' },
      { key: 'asr', label: T('转写', 'Transcribing'), state: 'running' },
      { key: 'clean', label: T('整理', 'Cleaning up'), state: 'pending' },
      { key: 'notes', label: T('纪要', 'Notes'), state: 'pending' },
    ],
    progress: 0.4,
    error: null,
  };
  return w;
}

/** a deep clone (the routes hand out copies: the page must never share objects with the world) */
export const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;
