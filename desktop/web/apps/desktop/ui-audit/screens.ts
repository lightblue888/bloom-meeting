// THE LIST: every screen / state the audit visits. To audit a new screen (the onboarding wizard, the meeting lobby…)
// add one entry: a name, how to get there from a fresh window, the selector that says it's there, and (optionally)
// a fixture tweak and the state changes to watch for layout shifts. See README.md.
//
// Each entry runs in both languages; each run is checked at every size × theme (lib/audit.ts SIZES, THEMES), and its
// transitions run at SHIFT_SIZES. `world` starts as fixtures/world.ts baseWorld(lang, stress): a live meeting with
// three people, a transcript, a task board, six past meetings, a DeepSeek model source.
import type { Page } from '@playwright/test';
import { ended, firstRun, fresh, idle, isoAgo, NOW_S, processing, STRESS, type Lang, type World } from './fixtures/world';

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
const brainWith = (c: Ctx, patch: Record<string, unknown>) => ({ ...c.world.current.brain, now: NOW_S, ...patch });

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
  {
    name: 'history-processing',
    what: 'History: the newest meeting still being processed (steps)',
    page: 'main',
    world: processing,
    go: goHistory,
    ready: '[data-testid="history-post"]',
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
    what: 'An opened past meeting on its notes (summary) tab',
    page: 'main',
    go: async (c) => {
      await openMeeting(c);
      await c.page.getByTestId('meeting-tab-notes').click();
    },
    ready: '.mt-sum-title',
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
    name: 'settings-speech',
    what: 'Settings → speech models',
    page: 'main',
    go: async (c) => {
      await goSettings(c);
      await c.page.getByTestId('settings-tab-speech').click();
    },
    ready: '[data-testid="settings-pane-speech"]',
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
    name: 'guest-in-meeting-stress',
    what: 'Guest inside the meeting with the longest names, 14 people, a very long line',
    page: 'guest',
    stress: true,
    go: joinGuest,
    ready: '[data-testid="claude-now"]',
  },
];
