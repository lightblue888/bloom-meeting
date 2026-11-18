// The mocked backend's whole state ("the world") for one screen: what /api/current, /api/meetings, /api/profile … and
// the guest relay answer. Each test builds a fresh one (baseWorld), the screen's entry tweaks it, and the routes in
// lib/mock-backend.ts read it on every request — so a transition can change it and the app's next poll sees that.
// Shapes follow src/meetingApi.ts (types imported from there, so a renamed field shows up as a type error here).
import type {
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
  Summary,
  Task,
} from '../../src/meetingApi';

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
  avatar_default?: string;
  lv?: string;
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
  relay: { sv: number; pv: number; state: RelayState | null; transcript: Line[]; board: Board | null };
  /** the first-run guide (/api/setup): done / skipped; the guide shows while neither */
  setup: { done: boolean; skipped: boolean; components?: Record<string, { ok: boolean; path?: string | null; detail?: string }> | null; voice_engine?: 'clone' | 'edge' | 'sapi' | null };
  /** the AI's voice and the words transcription should know (/api/voice) */
  voice: Record<string, unknown>;
  /** Settings → Storage (/api/storage, src/storageApi.ts StorageInfo) */
  storage: Record<string, unknown>;
  /** what the Tauri side says (fixtures/tauri-mock.ts): the speech models on this PC, Storage's paths */
  tauri: { speechInstalled: string[]; home: string; login: boolean; autoDays: number | null };
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
      plan: T('在 4.4 节加一张表，对比去掉记忆模块前后的结果', 'Add a table in 4.4 comparing with and without the memory module'), need: T('需要新跑实验', 'Needs new runs'), created: isoAgo(500), paper: 'p1', paper_name: 'FooBench.md',
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

  const current: Current = {
    backend: true,
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
  };

  const summary: Summary = {
    title: T('FooBench 论文合稿 · 组会纪要', 'FooBench paper draft · meeting notes'),
    speakers: [host, ai, ...guests.slice(0, 3)],
    abstract: T(
      `${host} 和 ${g1} 过了一遍第三、四节：表 2 补上了 TimeGAN 基线，3.2 节的数据划分改写得更清楚；消融实验留到会后。`,
      `${host} and ${g1} went through sections 3 and 4: TimeGAN was added to table 2, the data split in 3.2 was made clearer; the ablation is left for after the meeting.`,
    ),
    conclusions: [T('表 2 补上 TimeGAN', 'Add TimeGAN to table 2'), T('3.2 节说明按时间划分', 'State in 3.2 that the split is by time'), T('消融实验会后补', 'Ablation after the meeting')],
    sections: [
      {
        title: T('实验设置', 'Experimental setup'),
        cards: [
          { title: T('基线', 'Baselines'), bullets: [T('表 2 少了 TimeGAN，已补', 'Table 2 missed TimeGAN: added'), T('正文第 4 段与表 2 对齐', 'Paragraph 4 now matches table 2')] },
          { title: T('数据划分', 'Data split'), bullets: [T('按时间切分', 'Split by time'), T('写明各集合的时间范围', 'State the time range of each set')] },
        ],
      },
    ],
    todos: [T('补消融实验（需要新跑）', 'Run the ablation (needs new runs)'), T('图 3 坐标轴单位统一', 'Same axis units in figure 3')],
    generated: isoAgo(100),
  };

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
    meetings.push({ id, title: mt, started: isoAgo(ago), ended: isoAgo(ago - dur), state: 'ended', duration_s: dur, n_tasks: 4, n_done: 3, n_lines: lines.length, has_summary: true, has_diff: true, processing: false });
    details[id] = {
      meeting: { id, title: mt, started: isoAgo(ago), ended: isoAgo(ago - dur), state: 'ended', duration_s: dur, lang, title_auto: true, papers, active_paper: 'p1' },
      transcript: lines.map((l, k) => ({ ...l, vt: k * 2 })),
      board,
      summary: { ...summary, title: mt },
      diff_url: `/snap/${id}/`,
      participants: people,
      video: i === 0 ? { url: `/api/meetings/${id}/video`, type: 'video/webm', duration: 3, bytes: 4000, offset: 0 } : null,
      processing: false,
      post: null,
    };
  }

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
  };
  const provider: Provider = stress
    ? { kind: 'custom', base_url: 'https://llm-gateway.internal.example-university.edu.cn/v1/anthropic-compatible/endpoint', fast: STRESS.modelFast, main: STRESS.modelMain, key_set: true, key_tail: '9f3a', active: true, label: 'Custom', presets }
    : { kind: 'deepseek', base_url: presets.deepseek.base_url, fast: 'deepseek-v4-flash', main: 'deepseek-v4-pro', key_set: true, key_tail: 'a1b2', active: true, label: 'DeepSeek', presets };

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
  };

  return {
    lang, stress, backendUp: true, current, meetings, details, models,
    devices: { mic: current.ready!.devices!.mic, speaker: current.ready!.devices!.speaker },
    mic: { muted: false, talk: [] },
    profile, provider,
    invite: { ok: true, url: 'https://example.com/room/k3J9xQ2mZp', room: 'k3J9xQ2mZp', relay: true, guests: guests.length },
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
        records: { bytes: 0.4 * GB, meetings: { ...t(14, 0.3 * GB), path: `${home}\\.zuhui\\meetings` }, papers: { ...t(9, 0.1 * GB), path: `${home}\\.zuhui\\papers` } },
        caches: { bytes: 0.3 * GB, clearable: 0.25 * GB, path: `${home}\\.zuhui\\cache` },
        logs: { bytes: 0.05 * GB, path: `${home}\\.zuhui\\logs` },
      };
    })(),
    tauri: { speechInstalled: ['fun-asr-nano-onnx', 'sensevoice-small'], home: stress ? 'C:\\Users\\Maximilian.Montgomery-WW' : 'C:\\Users\\maya', login: false, autoDays: null },
    userStrings: [...tasks.map((x) => x.title), ...tasks.map((x) => x.plan ?? '').filter(Boolean), ...summary.conclusions, host, ai, ...guests, title, fast, main, effort, ...papers.map((p) => p.name), STRESS.mic, STRESS.speaker, current.ready!.devices!.mic!, current.ready!.devices!.speaker!, 'DeepSeek V4 Pro', 'DeepSeek V4 Flash', 'CosyVoice 3', 'Fun-ASR Nano', ...meetings.map((m) => m.title), STRESS.url, provider.base_url],
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
  w.provider = { ...w.provider, kind: 'unset', base_url: '', fast: '', main: '', key_set: false, key_tail: '', active: false };
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
