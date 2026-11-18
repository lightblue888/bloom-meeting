// THE LIST: every screen / state the audit visits. To audit a new screen (the onboarding wizard, the meeting lobby…)
// add one entry: a name, how to get there from a fresh window, the selector that says it's there, and (optionally)
// a fixture tweak and the state changes to watch for layout shifts. See README.md.
//
// Each entry runs in both languages; each run is checked at every size × theme (lib/audit.ts SIZES, THEMES), and its
// transitions run at SHIFT_SIZES. `world` starts as fixtures/world.ts baseWorld(lang, stress): a live meeting with
// three people, a transcript, a task board, six past meetings, a DeepSeek model source.
import type { Page } from '@playwright/test';
import { ended, firstRun, fresh, idle, isoAgo, NOW_S, processing, STRESS, withCarry, withComments, withVersions, type Lang, type World } from './fixtures/world';
import type { Summary } from '../src/meetingApi';

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
const goDigest = async (c: Ctx) => {
  await goHistory(c);
  await c.page.getByTestId('history-digest').click();
  await c.page.getByTestId('history-m-001').click();
  await c.page.getByTestId('history-m-002').click();
  await c.page.getByTestId('history-digest-doc').waitFor();
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
          await c.page.getByTestId('model-progress').first().waitFor();
        },
        expect: ['.lb-need', '.lb-needs', '.lb-st', '[data-testid="model-progress"]'],
        note: 'the "missing" line under 开始会议 turns into a progress bar',
      },
      {
        name: 'speech model ready (its need goes away)',
        act: async (c) => {
          await c.page.getByTestId('model-progress').first().waitFor({ state: 'detached', timeout: 15_000 });
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
    ],
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
    name: 'history-digest',
    what: 'History → Digest: two meetings picked, the saved digests, one open',
    page: 'main',
    go: goDigest,
    ready: '[data-testid="history-digest-doc"]',
    transitions: [
      {
        name: 'pick one more meeting (the count changes, nothing moves)',
        act: async (c) => {
          await c.page.getByTestId('history-m-003').click();
          await c.settle(200);
        },
        expect: ['.mt-hlist *', '[data-testid="history-digest-picked"]'],
      },
      {
        name: 'write a digest (the button says so; the new one tops the saved list)',
        act: async (c) => {
          await c.page.getByTestId('history-digest-go').click();
          await c.page.locator('[data-testid="history-digest-go"][aria-busy="true"]').waitFor();
          await c.settle(200);
        },
        expect: ['[data-testid="history-digest-go"] *', '.mt-hlist *', '[data-testid="history-digest-picked"]'],
        byDesign: 'a new digest joins the saved list at the top, so the rows and the open digest below it move down one row',
      },
    ],
  },
  {
    name: 'history-digest-stress',
    what: 'History → Digest with 40 meetings, 60-character titles and long digest lines',
    page: 'main',
    stress: true,
    go: goDigest,
    ready: '[data-testid="history-digest-doc"]',
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
    what: 'An opened past meeting on its to-do tab',
    page: 'main',
    stress: true,
    go: async (c) => {
      await openMeeting(c);
      await c.page.getByTestId('meeting-tab-todos').click();
    },
    ready: '[data-testid="meeting-tab-todos"][aria-selected="true"]',
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
        name: 'open 替换 (the box floats over the lines)',
        act: async (c) => {
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-replace"]').click();
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-pop"]').waitFor();
        },
        expect: ['[data-testid="tredit-pop"]', '[data-testid="tredit-pop"] *', '[data-testid="tredit-replace"]'],
      },
      {
        name: 'type a word to find (the count shows in its room)',
        act: async (c) => {
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-find"]').fill('TimeGAN');
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-with"]').fill('TimeGAN-v2');
          await c.settle(200);
        },
        expect: ['[data-testid="tredit-pop"] input', '[data-testid="tredit-msg"]', '[data-testid="tredit-hot"]'],
      },
      {
        name: 'replace all (a toast; the box closes)',
        act: async (c) => {
          await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-go"]').click();
          await c.page.getByTestId('toast').waitFor();
        },
        expect: ['[data-testid="tredit-pop"]', '[data-testid="toast"]', '[data-testid="transcript"] *', '[data-testid="tredit-undo"]', '.mt-hlist *'],
        byDesign: 'the replaced words change the lines that had them (TimeGAN → TimeGAN-v2 can wrap a line); 撤销 turns on',
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
    name: 'review-transcript-replace',
    what: '替换 open: find / replace with / add to custom words, the count, a failure in the same room',
    page: 'main',
    world: (w) => {
      w.details['m-001'].edits = { n: 2, last: { op: 'replace', find: '欧佩斯', repl: 'Opus' } };
    },
    go: async (c) => {
      await openMeeting(c);
      await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-replace"]').click();
      await c.page.locator('.mt-hlayer:not(.is-off) [data-testid="tredit-find"]').fill(c.T('没有这个词', 'not there'));
    },
    ready: '.mt-hlayer:not(.is-off) [data-testid="tredit-pop"]',
  },
  {
    name: 'review-transcript-busy',
    what: 'A past meeting still being processed: 撤销 / 替换 / 编辑 shown greyed (nothing moves when they turn on)',
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
    what: 'Fresh install: the lobby’s "set up a model" fix → Settings at Model source (none picked)',
    page: 'main',
    world: fresh,
    go: async (c) => {
      await c.page.locator('.lb-need button.link', { hasText: c.T('去设置', 'Set up') }).click();
      await c.page.getByTestId('settings').waitFor();
    },
    ready: '[data-testid="provider-key"]',
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
          await c.page.locator('.mw-menu [role="menuitemcheckbox"]', { hasText: c.T('直接改', 'Edit directly') }).click();
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
        name: 'send it (through the relay to the host, the box clears)',
        act: async (c) => {
          await c.page.getByTestId('ask-send').click();
          await c.page.waitForFunction(() => (document.querySelector('[data-testid="ask-input"]') as HTMLTextAreaElement | null)?.value === '');
          await c.settle(400);
        },
        expect: ['[data-testid="ask-input"]', '[data-testid="ask-send"] *'],
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
];
