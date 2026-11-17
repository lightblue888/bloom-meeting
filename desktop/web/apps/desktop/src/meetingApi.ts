// The 组会 (group meeting) backend: `python -m zuhui serve` on 127.0.0.1:47330 (docs/meeting-ui-spec.md). The page only
// reads it over local HTTP, polling; the Tauri command here starts it (src-tauri/src/zuhui.rs).
// Everything is wrapped so that a backend that isn't there, or a page outside Tauri, never throws into React.

import { invoke as tauriInvoke } from '@tauri-apps/api/core';
import { inTauri } from './api';
import { i18n, t } from './i18n';

/** the revision page's own words follow the app's language (the backend reads ui=zh / ui=en) */
const uiLang = () => (i18n().lang === 'zh-Hans' ? 'zh' : 'en');

/** the backend's origin; `?zuhui=http://127.0.0.1:47331` in the browser preview points the page at a stand-in */
export const ZUHUI_BASE = (typeof location !== 'undefined' && !('__TAURI_INTERNALS__' in window) && new URLSearchParams(location.search).get('zuhui')) || 'http://127.0.0.1:47330';
export const POLL_MS = 1500;

export type MeetingState = 'idle' | 'live' | 'ended';

export interface Meeting {
  id: string;
  title: string;
  started: string;
  ended: string | null;
  state: MeetingState;
  duration_s: number;
  session?: string;
  /** the meeting's own language ("en" | "zh"); the meeting area's words follow it. Missing (older meetings): the app's */
  lang?: string;
  /** false once the host renamed the meeting by hand (the name no longer follows the paper); missing (older meetings): true */
  title_auto?: boolean;
  /** the papers opened in this meeting, first = the one it started with (missing in older meetings: one paper) */
  papers?: MeetingPaper[];
  /** which of them is on screen */
  active_paper?: string | null;
}

/** one of the papers opened in a meeting (zuhui/meetings.py paper_entry) */
export interface MeetingPaper {
  id: string;
  name: string;
}

export interface Line {
  /** "mm:ss" since the meeting started */
  t: string;
  sec: number;
  who: 'teacher' | 'student' | 'system' | 'guest';
  text: string;
  /** a guest's line: who said it (the meeting's audio bridge tells guests apart) */
  name?: string;
  /** Claude's line: how it came about — "#chat" (DeepSeek, a quick reply) | "#ask" (DeepSeek, answers / analysis from the
   *  paper) | "#edit" (a revision task) | "#deep" (DeepSeek's first words, then handed to the background Claude) |
   *  "#deep-answer" (the background Claude's answer) | "#report" (a revision finished) | "#status" | "#app" | "#look" */
  tag?: string;
  /** Claude's line: seconds from the end of what it answered to its first sound */
  lat?: number;
  /** #edit / #app: the task's title; #report: the task's title */
  title?: string;
  /** #deep-answer: seconds the background Claude took */
  took?: number;
  /** #report: which task ("T2") and whether it went through */
  task?: string;
  ok?: boolean;
  /** a person's line the AI didn't answer: why ("这句在跟周老师说"). Kept in the data for the logs; never shown — the
   *  transcript is what people said, not the AI's judgments about it */
  skip?: string;
  /** Claude's line: cut off while speaking (the rest wasn't heard) */
  cut?: boolean;
  /** which model said it, full name and effort: "DeepSeek V4 Flash", "Claude Haiku 5.5 Low", "Claude Opus 5.5 Extra" */
  model?: string;
  /** #deep-answer from the quick lookup (Haiku searching the paper for a count, a place, a sentence): shown as 思考 */
  quick?: boolean;
  /** a looked-up answer put on screen the moment it was found, not said yet (it waits for a pause) */
  unspoken?: boolean;
  /** found, but not said: it waited too long or the talk had moved on */
  stale?: boolean;
  /** what Claude did around this line (searched, read, looked up online, handed off, revised): one grey line each */
  steps?: { t?: string; k: string; v?: string }[];
  /** 回顾: where this line starts in the meeting's screen recording, seconds (view.py lines it up with the recording) */
  vt?: number | null;
}

/** one edit the post-meeting cleanup made to the transcript (helper E's meetings.get → "cleanup") */
export interface CleanupItem {
  id: string;
  /** the line's index in transcript_raw */
  line: number;
  kind: 'retract' | 'fact' | 'sensitive' | 'profanity' | string;
  before: string;
  after: string;
  reason?: string;
  /** put back: the clean transcript shows the original words again */
  restored?: boolean;
}

/** the meeting's screen recording (GET /api/meetings/<id>/video, Range) */
export interface VideoInfo {
  url: string;
  type?: string;
  duration?: number | null;
  bytes?: number;
  /** meeting start minus recording start, seconds */
  offset?: number;
}

/** GET /api/models: what Claude's four parts use now, and what the first three can be switched to */
export interface ModelChoice {
  current: string;
  options: { id: string; label: string }[];
}
export interface Models {
  dialog?: ModelChoice;
  edit?: ModelChoice;
  effort?: ModelChoice;
  tts?: string;
  asr?: string;
}
export type ModelPart = 'dialog' | 'edit' | 'effort';

/** this machine's microphone and speaker (only the host's own card shows them) */
export interface Devices {
  mic?: string | null;
  speaker?: string | null;
  probing?: boolean;
}

export type TaskStatus = 'queued' | 'working' | 'done' | 'failed';

export interface Task {
  id: string;
  idx: number;
  title: string;
  remark: string;
  status: TaskStatus;
  result?: string | null;
  error?: string | null;
  section?: string | null;
  /** a note: the text was not changed (it needs new data or results, or the speaker said to just note it); it goes on the to-do list */
  note?: boolean;
  /** for a note: how to change the paper after the meeting, and what has to be done first (data, regressions) */
  plan?: string | null;
  need?: string | null;
  /** 'annotate': a note whose plan is still being written */
  mode?: string | null;
  resolved?: boolean;
  rejected?: boolean;
  focus?: { section?: string | null; text?: string | null } | null;
  created: string;
  started?: string | null;
  finished?: string | null;
  /** the editing worker's live progress while it works (and the final count after) */
  progress?: { step?: string | null; section?: string | null; edits?: number; started?: number; updated?: number; log?: string[] } | null;
  /** feedback about the meeting app itself, not the paper: kept on the to-do list under its own group, never drawn on the paper */
  app?: boolean;
  kind?: string | null;
  /** which of the meeting's papers it is on (missing on tasks from before papers could be switched) */
  paper?: string | null;
  paper_name?: string | null;
}

/** the board spans more than one paper: cards and the to-do list then say which paper each is on */
export const multiPaper = (tasks: Task[] | null | undefined): boolean => new Set((tasks ?? []).map((x) => x.paper).filter(Boolean)).size > 1;

export interface Board {
  counts: Partial<Record<TaskStatus | 'notes', number>>;
  tasks: Task[];
}

export interface Summary {
  title: string;
  speakers: string[];
  abstract: string;
  conclusions: string[];
  sections: { title: string; cards: { title: string; bullets: string[] }[] }[];
  todos: string[];
  generated?: string;
  markdown?: string;
}

export interface MeetingBrief {
  id: string;
  title: string;
  started: string;
  ended: string | null;
  state?: MeetingState;
  duration_s: number;
  n_tasks: number;
  n_done: number;
  n_lines: number;
  has_summary?: boolean;
  has_diff?: boolean;
  /** the post-meeting work (recording, transcript, cleanup, summary) is still running: its details will change */
  processing?: boolean;
}

export interface PaperInfo {
  path: string;
  name: string;
  exists: boolean;
  bytes?: number;
  modified?: string;
  kind?: string;
  words?: number;
  outline?: string[];
  /** during a meeting: the paper's id in the meeting, and whether it was just added (false: it was open already) */
  id?: string;
  added?: boolean;
}

/** what `/api/current.ready` says is up (view.py readiness) */
export interface Ready {
  paper: boolean;
  fast: boolean;
  tts: boolean;
  blurt: boolean;
  recording: boolean;
  hands?: string;
  /** what each part is called: conversation model, editing model, speech synthesis, speech recognition */
  models?: { fast?: string; hands?: string; tts?: string; asr?: string };
  /** did the conversation model answer its last call (null: not tried yet) */
  fast_online?: boolean | null;
  /** the last reply's time to first token, ms */
  fast_ms?: number | null;
  hands_ready?: boolean;
  working?: number;
  queued?: number;
  /** this machine's microphone / speaker as last asked (GET /api/devices asks again) */
  devices?: Devices | null;
}

/** someone in the meeting: the user, Claude, or a guest who joined by link (`avatar` is server-relative) */
export interface Participant {
  id: string;
  name: string;
  role: 'teacher' | 'student' | 'guest' | string;
  /** 主持人 / Claude / 参会者 / 老师 (a participant the host marked: what they say is acted on like the host's) */
  rank?: 'host' | 'assistant' | 'participant' | 'cohost' | 'teacher';
  you?: boolean;
  /** talking right now (the guest page knows it from the meeting's voice; the host's app doesn't send it) */
  speaking?: boolean;
  avatar?: string | null;
  /** their colour (hex): the flower drawn when they have no photo of their own, and their name */
  color?: string | null;
}

/** the line being spoken right now, before the pause that ends it */
export interface PartialLine {
  text: string;
  t: string;
  n?: number;
}

/** what the diff page says is on screen: the section, the first visible paragraph */
export interface Focus {
  section: string;
  text?: string | null;
  no?: string | null;
}

/** what Claude is doing right now (live only): the fast model in front, the background Claude behind */
export interface Brain {
  front: 'listening' | 'thinking' | 'speaking' | 'muted';
  front_model?: string;
  back_model?: string;
  /** the background Claude, full name and effort: when looking something up / when revising */
  deep_model?: string;
  edit_model?: string;
  /** the newest lookup (older clients); `deeps` has every one running */
  deep?: Lookup | null;
  /** every lookup the background is doing (several can run at once), oldest first */
  deeps?: Lookup[];
  /** the revision being made right now: its progress and steps, pushed with the rest */
  edits?: { id: string; idx: number; title: string; started?: number | null; edits?: number; log?: string[] }[];
  queued?: number;
  /** answers found and on screen, not said yet */
  unspoken?: number;
  /** the backend's clock when this was made (seconds); the page sets `skew` (backend − page) when it arrives */
  now?: number;
  skew?: number;
}

/** one thing the background is looking up: its short name (Haiku names it once), when it started (t0) and took its
 *  last step (last), where in the meeting it was handed over (sec0), every step so far */
export interface Lookup {
  id?: number;
  q: string;
  topic?: string | null;
  secs: number;
  t0?: number;
  last?: number;
  sec0?: number | null;
  state: string;
  step?: string | null;
  steps?: { t?: string; k: string; v?: string }[];
  idle?: number;
  quick?: boolean;
  model?: string;
}

/** "Claude 在干什么" pushed by the backend whenever it changes (Server-Sent Events): no polling for it, the page only
 *  keeps time. Calls `on` with each new state; returns a function that stops listening. */
export function watchBrain(on: (b: Brain | null) => void, onDown?: () => void): () => void {
  let es: EventSource | null = null;
  try {
    es = new EventSource(`${ZUHUI_BASE}/api/brain/stream`);
  } catch {
    onDown?.();
    return () => undefined;
  }
  es.onmessage = (e) => {
    try {
      const b = JSON.parse(e.data) as Brain | null;
      if (b && typeof b.now === 'number') b.skew = b.now - Date.now() / 1000;
      on(b);
    } catch {
      /* a broken message: wait for the next one */
    }
  };
  es.onerror = () => onDown?.();
  return () => es?.close();
}

/** what /api/mic/stream pushes: whether the host's microphone is muted, and who is talking right now ("me" = the
 *  host, "claude" = Claude, anyone else by name; their avatars get the ring). On the guest page: the guest's own mute. */
export interface MicState {
  muted?: boolean;
  talk?: string[];
}

/** one message of /api/mic/stream → MicState (`{"muted": false, "talk": ["me"]}`); anything else is null (the caller
 *  waits for the next one). */
export function parseMic(data: string): MicState | null {
  let v: unknown;
  try {
    v = JSON.parse(data);
  } catch {
    return null;
  }
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
  const o = v as { muted?: unknown; talk?: unknown };
  return {
    ...(typeof o.muted === 'boolean' ? { muted: o.muted } : {}),
    talk: Array.isArray(o.talk) ? o.talk.filter((x): x is string => typeof x === 'string') : [],
  };
}

/** The host's mute and who is talking, pushed by the backend when they change. Returns a function that stops listening. */
export function watchMic(on: (s: MicState) => void): () => void {
  if (guestT) return guestT.watchMic(on);
  let es: EventSource | null = null;
  try {
    es = new EventSource(`${ZUHUI_BASE}/api/mic/stream`);
  } catch {
    return () => undefined;
  }
  es.onmessage = (e) => {
    const s = parseMic(String(e.data));
    if (s) on(s);
  };
  // the stream dropped: nobody is shown talking; the mute state stays as last known
  es.onerror = () => on({ talk: [] });
  return () => es?.close();
}

/** Settings → General → Profile: your name, your own photo (null: the flower; `custom`: picked in Settings, so it can
 *  be removed) and the AI's name in meetings (`assistant_custom` false: none given, it goes by the model, which would
 *  call it `assistant_default`). Kept by the Bloom backend (~/.zuhui/profile.json, avatars/me.png). */
export interface Profile {
  name: string;
  avatar: string | null;
  custom: boolean;
  assistant: string;
  assistant_default: string;
  assistant_custom: boolean;
}

const toProfile = (j: Partial<Profile>): Profile => ({
  name: String(j.name ?? ''),
  avatar: j.avatar ?? null,
  custom: !!j.custom,
  assistant: String(j.assistant ?? ''),
  assistant_default: String(j.assistant_default ?? ''),
  assistant_custom: !!j.assistant_custom,
});

/** fired on window after the name or the photo changed, so the rail's avatar loads again */
export const PROFILE_EVENT = 'bloom-profile';

async function postProfile(path: string, body: unknown): Promise<Profile> {
  const r = await fetch(`${ZUHUI_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(20000),
  });
  const j = (await r.json().catch(() => ({}))) as Partial<Profile> & { error?: string };
  // the error is a code the page words (empty, too_long, taken, too_big, bad_image, failed)
  if (!r.ok || typeof j.name !== 'string') throw new Error(j.error || 'failed');
  window.dispatchEvent(new Event(PROFILE_EVENT));
  return toProfile(j);
}

/** Settings → General → Model source: Claude Code's own login ('claude'), or an API key — for an Anthropic-compatible
 *  endpoint (DeepSeek, Kimi, GLM, Qwen, any other) or for Anthropic's API. The key never comes back, only its last four. */
export type ProviderKind = 'claude' | 'deepseek' | 'kimi' | 'glm' | 'qwen' | 'anthropic' | 'custom';
export interface ProviderPreset {
  label: string;
  base_url: string;
  fast: string;
  main: string;
}
export interface Provider {
  kind: ProviderKind;
  base_url: string;
  fast: string;
  main: string;
  key_set: boolean;
  key_tail: string;
  active: boolean;
  label: string;
  presets: Record<string, ProviderPreset>;
}

export interface Current {
  backend: boolean;
  /** live only: what Claude is doing (null when no meeting is on) */
  brain?: Brain | null;
  paper?: PaperInfo | null;
  ready?: Ready | null;
  meeting: Meeting | null;
  transcript: Line[];
  board: Board;
  summary: Summary | null;
  last_report?: string | null;
  diff_url?: string | null;
  participants?: Participant[] | null;
  partial?: PartialLine | null;
  focus?: Focus | null;
  /** participants the student thinks are teachers and the host hasn't marked yet (shown once each) */
  role_suggest?: { name: string; why?: string }[];
  /** live: the papers opened in this meeting (empty when no meeting is on) and the one on screen */
  papers?: MeetingPaper[];
  active_paper?: string | null;
  /** after the meeting: the transcript as said, and the cleanup's edits (null until the cleanup has run) */
  transcript_raw?: Line[] | null;
  cleanup?: CleanupItem[] | null;
  processing?: boolean;
}

export interface MeetingDetail {
  meeting: Meeting;
  /** the clean version (restored edits applied) once the cleanup has run; else the transcript as said */
  transcript: Line[];
  transcript_raw?: Line[] | null;
  cleanup?: CleanupItem[] | null;
  board: Board;
  summary: Summary | null;
  diff_url: string | null;
  participants?: Participant[] | null;
  video?: VideoInfo | null;
  processing?: boolean;
  /** while the post-meeting work runs: its steps (保存录屏 / 转写 / 整理 / 纪要) and where each is */
  post?: { steps: { key: string; label: string; state: 'pending' | 'running' | 'done' | 'failed' | string }[]; progress?: number | null; error?: string | null } | null;
}

/** Dev: one simulated meeting (scratch_ab/sim) as the list shows it — a 回顾 row plus its scenario, round, score */
export interface DevRun extends MeetingBrief {
  scenario: string;
  round?: number | null;
  paper: string;
  note?: string;
  effort?: string | null;
  /** the director's expected tag vs Claude's, after the meeting */
  match?: number | null;
  total?: number | null;
  n_human?: number;
  n_claude?: number;
  phase?: string | null;
  /** the run's process went away before the meeting ended */
  stopped?: boolean;
}

/** Dev: one simulated meeting, shaped like a 回顾 meeting; the review in `summary`, the status strip while it runs */
export interface DevDetail extends MeetingDetail {
  brain?: Brain | null;
  partial?: PartialLine | null;
  dev: DevRun;
}

/** an avatar's full URL (the backend serves /avatars/…); '' when there is none */
export const avatarUrl = (rel: string | null | undefined): string => (rel ? (rel.startsWith('http') || guestT ? rel : ZUHUI_BASE + rel) : '');

// 8 s: the backend can stop answering for seconds (2026-11-12: 5-9 s while a guest joined; cause still being traced
// via ~/.zuhui/stall.log); a slow answer is not a dead backend
/** The guest page (example.com/room/<id>) shows this same meeting page, fed by the relay instead of a backend on this
 *  machine: the few calls the page makes by itself go to the guest page's own handlers. */
export interface GuestTransport {
  /** Claude's card: what it uses now (no choices: only the host switches models) */
  models: () => Promise<Models | null>;
  /** the guest's own card: their microphone (the browser's) */
  devices: () => Promise<Devices>;
  setMicMuted: (muted: boolean) => Promise<boolean>;
  /** the level and mute state beside the guest's own avatar (their browser's microphone) */
  watchMic: (on: (s: MicState) => void) => () => void;
}
let guestT: GuestTransport | null = null;
export const setGuestTransport = (t: GuestTransport | null) => {
  guestT = t;
};

async function get<T>(path: string, timeoutMs = 8000): Promise<T> {
  const r = await fetch(ZUHUI_BASE + path, { signal: AbortSignal.timeout(timeoutMs), cache: 'no-store' });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

/** the invite link: the relay's join page with this user's fixed room (zuhui/share.py) */
export interface Invite {
  ok: boolean;
  url?: string;
  room?: string;
  /** can the backend reach the relay (LiveKit keys present) */
  relay?: boolean;
  guests?: number;
  error?: string | null;
}

/** a meeting's title without the date older meetings carried in it ("FooBench 论文合稿 · 11月11日 05:03") */
export function cleanTitle(title: string | null | undefined): string {
  return (title ?? '').replace(/\s*·\s*\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*$/, '').trim();
}

/** 只给干净版：原始版和删改清单不进界面（用户定的；原始版只留在磁盘上给转写训练用） */
function cleanOnly<T extends { transcript_raw?: unknown; cleanup?: unknown }>(d: T): T {
  if (d && typeof d === 'object') {
    delete d.transcript_raw;
    delete d.cleanup;
  }
  return d;
}

export const meetingApi = {
  current: () => get<Current>('/api/current').then(cleanOnly),
  invite: () => get<Invite>('/api/invite', 4000),
  meetings: () => get<MeetingBrief[]>('/api/meetings'),
  devRuns: () => get<DevRun[]>('/api/dev/runs'),
  devRun: (id: string) => get<DevDetail>(`/api/dev/runs/${encodeURIComponent(id)}`),
  meeting: (id: string) => get<MeetingDetail>(`/api/meetings/${encodeURIComponent(id)}`).then(cleanOnly),
  /** a past meeting goes to the backend's trash folder (~/.zuhui/meetings-trash, recoverable); not the live one */
  /** deletes a meeting; its screen recording goes to the Recycle Bin with it (`recording` says what happened) */
  deleteMeeting: async (id: string): Promise<{ recording?: { mb: number; deleted: boolean; pending?: boolean; error?: string | null } | null }> => {
    const r = await fetch(`${ZUHUI_BASE}/api/meetings/${encodeURIComponent(id)}/delete`, { method: 'POST', signal: AbortSignal.timeout(30000) });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; error?: string; recording?: { mb: number; deleted: boolean; pending?: boolean; error?: string | null } | null };
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return { recording: j.recording ?? null };
  },
  /** 设为老师 / 改回参会者 */
  setRole: async (name: string, role: 'cohost' | 'participant'): Promise<void> => {
    const r = await fetch(`${ZUHUI_BASE}/api/roles`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, role }), signal: AbortSignal.timeout(8000) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  },
  /** "看起来是老师" → 不是 */
  dismissRole: async (name: string): Promise<void> => {
    await fetch(`${ZUHUI_BASE}/api/roles/dismiss`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }), signal: AbortSignal.timeout(8000) });
  },
  /** 回顾 → 分享: a meeting's transcript as Markdown or TXT, the clean or the raw version, saved where the user picks (host only) */
  exportTranscript: async (id: string, fmt: 'md' | 'txt', version: 'clean' | 'raw' = 'clean'): Promise<string | null> => {
    const r = await fetch(`${ZUHUI_BASE}/api/meetings/${encodeURIComponent(id)}/export?fmt=${fmt}&version=${version}`, { signal: AbortSignal.timeout(15000) });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; name?: string; text?: string; error?: string };
    if (!r.ok || !j.ok || j.text == null) throw new Error(j.error || `HTTP ${r.status}`);
    return (await tauriInvoke<string | null>('zuhui_save_text', { name: j.name ?? `${t('meeting.export.filename')}.${fmt}`, ext: fmt, text: j.text })) ?? null;
  },
  /** 回顾 → 保存 → 存录屏: the system save dialog, then the backend copies the recording to the chosen file (it never
   *  passes through the page). The saved path back, null when cancelled. */
  saveRecording: async (id: string, name: string, ext: string): Promise<string | null> => {
    const to = await tauriInvoke<string | null>('zuhui_pick_recording', { name, ext });
    if (!to) return null;
    const r = await fetch(`${ZUHUI_BASE}/api/recording/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meeting: id, to }),
      signal: AbortSignal.timeout(600000),
    });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; path?: string; error?: string };
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j.path || to;
  },
  /** Claude's card: the models now and the choices; null when the backend can't switch models (503) */
  models: async (): Promise<Models | null> => {
    if (guestT) return guestT.models();
    const r = await fetch(`${ZUHUI_BASE}/api/models`, { signal: AbortSignal.timeout(4000), cache: 'no-store' });
    if (!r.ok) return null;
    return (await r.json()) as Models;
  },
  /** Claude's card: switch one part; answers the models as they are now */
  setModel: async (part: ModelPart, value: string): Promise<Models> => {
    if (guestT) throw new Error('only the host switches models');
    const r = await fetch(`${ZUHUI_BASE}/api/models`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ part, value }),
      signal: AbortSignal.timeout(20000),
    });
    const j = (await r.json().catch(() => ({}))) as Models & { ok?: boolean; error?: string };
    if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  },
  /** the host's own card: microphone and speaker (asks the system again; `probing` = the answer is on its way) */
  devices: () => (guestT ? guestT.devices() : get<Devices>('/api/devices', 4000)),
  /** the host mutes / unmutes their microphone (a click on the level bars beside their avatar); the state as saved */
  setMicMuted: async (muted: boolean): Promise<boolean> => {
    if (guestT) return guestT.setMicMuted(muted);
    const r = await fetch(`${ZUHUI_BASE}/api/mic/mute`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ muted }),
      signal: AbortSignal.timeout(8000),
    });
    const j = (await r.json().catch(() => ({}))) as { muted?: unknown; error?: string };
    if (!r.ok || typeof j.muted !== 'boolean') throw new Error(j.error || `HTTP ${r.status}`);
    return j.muted;
  },
  profile: async (): Promise<Profile> => {
    const r = await fetch(`${ZUHUI_BASE}/api/profile`, { cache: 'no-store', signal: AbortSignal.timeout(8000) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return toProfile((await r.json()) as Partial<Profile>);
  },
  setProfileName: (name: string) => postProfile('/api/profile', { name }),
  /** the AI's name in meetings; '' goes back to the model's */
  setAssistantName: (assistant: string) => postProfile('/api/profile', { assistant }),
  /** a picture as a data: URL; the backend squares it to 256 px */
  setAvatar: (data: string) => postProfile('/api/profile/avatar', { data }),
  removeAvatar: () => postProfile('/api/profile/avatar', { remove: true }),
  provider: async (): Promise<Provider> => {
    const r = await fetch(`${ZUHUI_BASE}/api/provider`, { cache: 'no-store', signal: AbortSignal.timeout(8000) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()) as Provider;
  },
  /** an empty key keeps the one saved; the error is a code the page words (kind, key, base_url, model, failed) */
  setProvider: async (p: { kind: ProviderKind; base_url?: string; key?: string; fast?: string; main?: string }): Promise<Provider> => {
    const r = await fetch(`${ZUHUI_BASE}/api/provider`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(p),
      signal: AbortSignal.timeout(10000),
    });
    const j = (await r.json().catch(() => ({}))) as Partial<Provider> & { error?: string };
    if (!r.ok || !j.kind) throw new Error(j.error || 'failed');
    window.dispatchEvent(new Event(PROFILE_EVENT)); // the AI's default name follows the source
    return j as Provider;
  },
  /** asks the saved source one short question (costs the provider a few tokens) */
  testProvider: async (): Promise<{ ok: boolean; ms?: number; error?: string }> => {
    const r = await fetch(`${ZUHUI_BASE}/api/provider/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
      signal: AbortSignal.timeout(100000),
    });
    return (await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }))) as { ok: boolean; ms?: number; error?: string };
  },
  /** the live 修订对比 page, for the iframe (host=bloom: its own toolbar hides, the tools sit in the shell's row) */
  liveDiffUrl: () => `${ZUHUI_BASE}/?embed=1&host=bloom&ui=${uiLang()}`,
  /** a past meeting's snapshot (`diff_url` is server-relative) */
  diffUrl: (rel: string | null | undefined) => (rel ? ((rel.startsWith('http') ? rel : ZUHUI_BASE + rel) + (rel.includes('?') ? '&' : '?') + `embed=1&host=bloom&ui=${uiLang()}`) : ''),
  /** a past meeting's screen recording (`url` is server-relative) */
  videoUrl: (rel: string | null | undefined) => (rel ? (rel.startsWith('http') ? rel : ZUHUI_BASE + rel) : ''),
  /** a paper dropped in: the backend imports it as the mirror and answers its name / kind / size */
  paper: async (path: string): Promise<PaperInfo> => {
    const r = await fetch(`${ZUHUI_BASE}/api/paper`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ path }),
      // a PDF goes through MinerU (formulas, tables, figures): 30–60 s, longer on a busy GPU
      signal: AbortSignal.timeout(600_000),
    });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; error?: string; paper?: PaperInfo };
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j.paper as PaperInfo;
  },
  /** 改会议名 (a live or a past meeting); '' goes back to the name it got from its paper. The meeting as saved */
  renameMeeting: async (id: string, title: string): Promise<Meeting> => {
    const r = await fetch(`${ZUHUI_BASE}/api/meetings/${encodeURIComponent(id)}/title`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ title }),
      signal: AbortSignal.timeout(8000),
    });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; error?: string; meeting?: Meeting };
    if (!r.ok || !j.ok || !j.meeting) throw new Error(j.error || `HTTP ${r.status}`);
    return j.meeting;
  },
  /** during a meeting: put another of its papers on screen (adding one goes through `paper`) */
  switchPaper: async (id: string): Promise<PaperInfo> => {
    const r = await fetch(`${ZUHUI_BASE}/api/paper/switch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ id }),
      signal: AbortSignal.timeout(30000),
    });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; error?: string; paper?: PaperInfo };
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j.paper as PaperInfo;
  },
  /** where the paper being imported is (MinerU's progress): shown under 正在导入 */
  paperStatus: async (): Promise<{ busy?: boolean; text?: string; done?: number; total?: number; elapsed?: number }> => {
    const r = await fetch(`${ZUHUI_BASE}/api/paper/status`, { signal: AbortSignal.timeout(3000) });
    return r.ok ? r.json() : {};
  },
  /** Rust: spawn `python -X utf8 -m zuhui serve` in the project folder (zuhui.rs); false outside Tauri */
  backendStart: async (): Promise<boolean> => {
    if (!inTauri) return false;
    try {
      await tauriInvoke<void>('zuhui_backend_start');
      return true;
    } catch (e) {
      throw new Error(String(e));
    }
  },
  /** Rust: the system file picker for a paper; null when cancelled or outside Tauri */
  pickPaper: async (): Promise<string | null> => {
    if (!inTauri) return null;
    return (await tauriInvoke<string | null>('zuhui_pick_paper')) ?? null;
  },
  /** 开会: Bloom records only its own window (it follows the window, covered or not, no frame drawn); the region is
   * the fallback where the window handle can't be had */
  recordStart: (region: [number, number, number, number] | null) => tauriInvoke<string>('capture_start', { opts: { pick: false, region, window: true } }),
  /** 散会: the backend ends the meeting at once (the page shows it ended), then the recording stops and finishes in the background */
  recordStop: async () => {
    try {
      await fetch(`${ZUHUI_BASE}/api/meeting/end`, { method: 'POST', signal: AbortSignal.timeout(8000) });
    } catch {
      /* an older backend: the meeting still ends when the recording does */
    }
    await tauriInvoke<void>('capture_control', { cmd: 'stop' });
  },
};

/** "mm:ss" (or "h:mm:ss") from seconds */
export function fmtDuration(s: number): string {
  const n = Math.max(0, Math.floor(s));
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const sec = n % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** "11月11日 01:30" from an ISO time (the backend's local time) */
export function fmtWhen(iso: string | null | undefined, lang: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  try {
    return new Intl.DateTimeFormat(lang === 'zh-Hans' ? 'zh-CN' : lang, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(d);
  } catch {
    return d.toLocaleString();
  }
}
