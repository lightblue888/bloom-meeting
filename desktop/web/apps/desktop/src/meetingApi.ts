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
  /** 第 8 批: when Settings → Storage deleted this meeting's transcripts and notes (they are past the kept days) */
  records_removed?: string | null;
  /** 第 8 批: the sample meeting that comes with Bloom (zuhui/sample.py) */
  sample?: boolean;
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
  /** typed into the meeting page's box instead of said (the AI took it the same way) */
  typed?: boolean;
  /** 回顾: the line's index in the transcript as said (a hand edit names the line by it) */
  i?: number;
  /** 回顾: its words were changed by hand */
  edited?: boolean;
}

// ---------------------------------------------------------------- 第 7 批: the transcript by hand
/** 回顾 → 编辑 (zuhui/transcript_edit.py): what a hand edit can hold */
export const EDIT_LINE_MAX = 2000;
export const EDIT_WORD_MAX = 40;
export const GUEST_NAME_MAX = 32;
/** the last hand edit (撤销 takes it back) */
export type EditOp =
  | { op: 'line'; i: number; text: string }
  | { op: 'replace'; find: string; repl: string }
  | { op: 'speaker'; from: string; to: string };
export interface Edits {
  n: number;
  last: EditOp | null;
}
/** the answer to a hand edit: how many there are now, the last one; a replace says how many it changed and whether
 *  the new word went into the custom words (added / exists / full) */
export interface EditResult extends Edits {
  count?: number;
  hot?: 'added' | 'exists' | 'full' | 'failed' | 'bad';
}

// ---------------------------------------------------------------- in the meeting (第 3 批, zuhui/inmeeting.py)
/** the longest question the meeting's box takes (the backend refuses longer ones) */
export const ASK_TEXT_MAX = 500;
/** a bookmark's note */
export const BOOKMARK_NOTE_MAX = 100;
/** this long without anyone speaking: the host is reminded (the meeting never ends by itself) */
export const QUIET_S = 600;

/** 标记: a moment of the meeting someone marked (sec / t: when), with a short note */
export interface Bookmark {
  id: string;
  sec: number;
  t: string;
  note: string;
  /** who marked it (the host's own name when the host did) */
  by: string | null;
  host: boolean;
  at?: number;
}
/** 第 6 批: a comment typed on a paragraph of the paper (zuhui/comments.py); the paper's page draws them */
export interface PaperComment {
  id: string;
  /** which paper (meetings.paper_id) */
  paper?: string | null;
  /** the paragraph: its first letters, the start of its text, its section and place (when its letters no longer match) */
  key?: string;
  quote?: string;
  section?: string | null;
  ri?: number | null;
  by: string;
  at?: string;
  /** "mm:ss" into the meeting */
  t?: string | null;
  text: string;
  resolved?: boolean;
  resolved_by?: string | null;
  replies?: { id: string; by: string; at?: string; t?: string | null; text: string }[];
}
/** the comments the paper's page shows, and what the viewer may do with them */
export interface PaperComments {
  items: PaperComment[];
  canAdd: boolean;
  canDelete: boolean;
}
/** 第 6 批: one saved version of a paper (zuhui/paper_versions.py): before / after a meeting, before a restore, a restore */
export interface PaperVersion {
  v: string;
  kind: 'before' | 'after' | 'backup' | 'restore' | string;
  at: string;
  mid?: string | null;
  title?: string | null;
  chars?: number;
  bytes?: number;
  /** a restore: which version it went back to */
  of?: string;
  /** the same as the paper is now */
  same?: boolean;
}
export interface PaperVersions {
  paper: { id: string; name?: string | null };
  /** newest first */
  items: PaperVersion[];
  current: { sha: string; chars: number } | null;
  /** a meeting is on: no restoring */
  live?: boolean;
}
export type DocxMode = 'track' | 'direct';
export const DOCX_MODES: DocxMode[] = ['track', 'direct'];

/** 先选中再说: the passage someone selected in the paper; their next words to the AI are about it */
export interface AnchorSel {
  text: string;
  section?: string | null;
  /** null: the host; else the co-host's name */
  by?: string | null;
  at: number;
  left_s?: number;
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
  /** for a note: what kind of work it waits for (zuhui/board.py NOTE_TAGS), drawn as chips: rerun, data, figure, lit, discuss */
  tags?: string[] | null;
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

/** one line of the 纪要. Older summaries hold plain strings; newer ones say where in the meeting it was said
 *  (`t` "mm:ss" and `sec`, a transcript line's), who gave it (`by`, feedback and asks) or who does it (`who`, to-dos) */
export type SumItem = string | { text: string; t?: string | null; sec?: number | null; by?: string | null; who?: string | null };
/** the text of a 纪要 line, either shape */
export const itemText = (x: SumItem | null | undefined): string => (typeof x === 'string' ? x : x?.text ?? '');
/** where in the meeting a 纪要 line was said (seconds), when the summary says */
export const itemSec = (x: SumItem | null | undefined): number | null => (x && typeof x === 'object' && typeof x.sec === 'number' ? x.sec : null);
/** one person in the 纪要: what they said, the feedback they got, what they were asked to do */
export interface SumPerson {
  name: string;
  said?: SumItem[];
  feedback?: SumItem[];
  asks?: SumItem[];
}
/** the 纪要's format (Settings → General → Notes; 回顾 can write it again in another) */
export type SummaryTemplate = 'default' | 'progress' | 'paper' | 'proposal' | 'custom';
export const SUMMARY_TEMPLATES: SummaryTemplate[] = ['default', 'progress', 'paper', 'proposal', 'custom'];
export interface Summary {
  title: string;
  speakers: string[];
  abstract: string;
  conclusions: SumItem[];
  sections: { title: string; t?: string | null; sec?: number | null; cards: { title: string; t?: string | null; sec?: number | null; bullets: SumItem[] }[] }[];
  todos: SumItem[];
  /** newer summaries: one entry per person (older ones have none) */
  people?: SumPerson[];
  /** newer summaries: the format it was written in */
  template?: SummaryTemplate;
  generated?: string;
  markdown?: string;
  /** the meeting started with things left from earlier ones: each says whether it got done this time */
  carried?: CarriedRow[];
}
/** what an earlier meeting left: a to-do from its 纪要, a note pinned on its board, an edit nobody accepted in 修订对比 */
export type CarryKind = 'todo' | 'note' | 'edit';
/** one thing left from a past meeting on this paper (the lobby lists them; a tick is saved with that meeting) */
export interface CarryItem {
  /** "<meeting id>/<key>" */
  id: string;
  mid: string;
  title: string;
  started: string | null;
  kind: CarryKind;
  text: string;
  who?: string | null;
  t?: string | null;
  sec?: number | null;
  section?: string | null;
}
/** a 纪要's "left from last time": the item, whether this meeting got it done, and how (or what's missing) */
export interface CarriedRow {
  id: string;
  ref?: string;
  kind: CarryKind;
  item: string;
  from?: string | null;
  started?: string | null;
  done: boolean;
  text?: string;
  t?: string | null;
  sec?: number | null;
}
/** 回顾 → 提问: the answer ([n] marks its sources) and the lines it came from */
export interface PastSource {
  n: number;
  mid: string;
  title: string;
  started: string | null;
  text: string;
  t?: string | null;
  sec?: number | null;
}
export interface PastAnswer {
  answer: string;
  sources: PastSource[];
  /** false: nothing in the past meetings matched (the model was not asked) */
  found: boolean;
}
/** the longest question 提问 takes (the backend refuses longer ones) */
export const ASK_MAX = 300;
/** 阶段汇总: how many meetings one can cover */
export const DIGEST_MIN = 2;
export const DIGEST_MAX = 12;
export interface DigestMeeting {
  id: string;
  title: string;
  started: string | null;
}
export interface DigestBrief {
  id: string;
  created: string;
  state: 'running' | 'done' | 'failed';
  error?: string | null;
  mids: string[];
  meetings: DigestMeeting[];
  title: string;
}
export interface DigestResult {
  title: string;
  overview: string;
  /** `meetings`: 1-based positions in the digest's meetings */
  issues: { text: string; meetings: number[] }[];
  feedback: { text: string; by?: string; meetings: number[] }[];
  open: { text: string; who?: string; meeting: number | null }[];
}
export interface Digest extends DigestBrief {
  result: DigestResult | null;
  markdown: string;
}
/** the answer split at its source marks: text and [n] in order (only marks with a source become links) */
export function splitCites(answer: string, known: number[]): (string | number)[] {
  const out: (string | number)[] = [];
  let last = 0;
  for (const m of answer.matchAll(/\[(\d{1,2})\]/g)) {
    const n = Number(m[1]);
    if (!known.includes(n)) continue;
    if (m.index! > last) out.push(answer.slice(last, m.index));
    out.push(n);
    last = m.index! + m[0].length;
  }
  if (last < answer.length) out.push(answer.slice(last));
  return out;
}
/** how long each person spoke (worked out from the transcript, no model): the host, the AI, each guest by name */
export interface TalkTime {
  who: Line['who'];
  name?: string | null;
  sec: number;
  n: number;
  share: number;
}
/** 回顾 → 纪要 → 重新生成, while it runs or after it failed */
export interface SummaryJob {
  state: 'running' | 'failed';
  template: SummaryTemplate;
  started?: number | null;
  error?: string | null;
}
/** Settings → General → Notes */
export interface SummaryPrefs {
  template: SummaryTemplate;
  custom: string;
  custom_max: number;
}

/** 第 5 批 导出和分享 (zuhui/export.py, zuhui/autosave.py): what 回顾 → 保存 writes with the save dialog */
export type ExportKind = 'docx' | 'pdf' | 'pack';
/** 散会后自动保存: what it can save (纪要 Word, 纪要 Markdown, 修改清单, 修订对比, 逐字稿), in this order */
export const AUTOSAVE_ITEMS = ['minutes_docx', 'minutes_md', 'todos', 'diff', 'transcript'] as const;
export type AutosaveItem = (typeof AUTOSAVE_ITEMS)[number];
export interface AutosaveResult {
  meeting: string;
  title: string;
  /** epoch seconds */
  at: number;
  ok: boolean;
  /** a code (not_dir, no_summary, no_pandoc, write…) and the reason as the system said it */
  error?: string | null;
  detail?: string | null;
  dir: string;
  files: string[];
  failed: string[];
}
export interface AutosavePrefs {
  on: boolean;
  dir: string;
  items: AutosaveItem[];
  dir_max: number;
  last?: AutosaveResult | null;
}
/** the export errors the app has words for (meeting.export.err.*); anything else shows the backend's own words */
const EXPORT_ERRORS = ['not_found', 'no_summary', 'no_pandoc', 'no_browser', 'convert', 'bad_path', 'write', 'not_dir', 'kind'];
/** an export failure's reason, for "保存失败：原因" */
export function exportReason(e: unknown): string {
  const err = e as { code?: string; detail?: string; message?: string } | null;
  const code = err?.code ?? String(err?.message ?? e ?? '');
  if (EXPORT_ERRORS.includes(code)) return t(`meeting.export.err.${code}`);
  return err?.detail || code;
}
class ExportFail extends Error {
  code: string;
  detail?: string;
  constructor(code: string, detail?: string) {
    super(code);
    this.code = code;
    this.detail = detail;
  }
}
/** "2026-11-18 组会 纪要.docx": the save dialog's file name (nothing Windows refuses in a name) */
export function exportName(title: string, started: string | null | undefined, word: string, ext: string): string {
  const d = started ? new Date(started) : null;
  const p = (n: number) => String(n).padStart(2, '0');
  const day = d && !Number.isNaN(d.getTime()) ? `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` : '';
  // eslint-disable-next-line no-control-regex
  const base = cleanTitle(title).replace(/[\\/:*?"<>|\u0000-\u001f]+/g, ' ').replace(/\s+/g, ' ').trim().replace(/[. ]+$/, '');
  return `${day}${base} ${word}.${ext}`.replace(/\s+/g, ' ').trim();
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
  /** the papers open in it (回顾 filters by paper); older meetings have none */
  papers?: { id: string; name: string }[];
  /** 第 8 批: the sample meeting that comes with Bloom: listed only while there is no meeting of the user's own */
  sample?: boolean;
  /** 第 8 批: its transcripts and notes were deleted (Settings → Storage) */
  records_removed?: boolean;
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
  /** Settings → Speech: the AI stays silent (text only) */
  tts_off?: boolean;
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
  /** your name's colour and the AI's (#rrggbb), and the colours Settings offers */
  color: string;
  ai_color: string;
  palette: string[];
}

const toProfile = (j: Partial<Profile>): Profile => ({
  name: String(j.name ?? ''),
  avatar: j.avatar ?? null,
  custom: !!j.custom,
  assistant: String(j.assistant ?? ''),
  assistant_default: String(j.assistant_default ?? ''),
  assistant_custom: !!j.assistant_custom,
  color: String(j.color ?? '#60a5fa'),
  ai_color: String(j.ai_color ?? '#e3906f'),
  palette: Array.isArray(j.palette) ? j.palette.map(String) : [],
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
export type ProviderKind = 'claude' | 'deepseek' | 'kimi' | 'glm' | 'qwen' | 'anthropic' | 'custom' | 'openai' | 'ollama' | 'lmstudio';
export interface ProviderPreset {
  label: string;
  base_url: string;
  fast: string;
  main: string;
  /** the endpoint's format: Anthropic Messages, or OpenAI Chat Completions (OpenAI-compatible, Ollama, LM Studio) */
  format?: 'anthropic' | 'openai';
  /** 'none': no key at all (the local ones); 'optional': may be left empty */
  key?: 'required' | 'optional' | 'none';
  /** runs on this computer (Ollama, LM Studio) */
  local?: boolean;
}
/** one model's tokens in a meeting / this month (an API source only); cost only when every call could be priced */
export interface UsageModel {
  model: string;
  /** the model's readable name (config.pretty_model) */
  pretty?: string;
  /** the provider's name (DeepSeek, Ollama…) */
  label: string;
  calls: number;
  in: number;
  out: number;
  cache_read: number;
  cache_write: number;
  tokens: number;
  currency: string | null;
  cost: number | null;
}
export interface Usage {
  models: UsageModel[];
  tokens: number;
  /** the estimated money by currency ('CNY', 'USD'), from the priced models only */
  costs: Record<string, number>;
}
export interface MonthUsage extends Usage {
  month: string;
  /** the model source now: 'claude' (a subscription) shows none */
  mode?: 'api' | 'claude' | 'none';
}
export interface Provider {
  /** 'unset': a fresh install that hasn't picked one yet */
  kind: ProviderKind | 'unset';
  base_url: string;
  fast: string;
  main: string;
  key_set: boolean;
  key_tail: string;
  active: boolean;
  label: string;
  /** 查文献 (an API source's literature lookup), on by default */
  lit?: boolean;
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
  /** the meeting (on, or the last one): its bookmarks */
  bookmarks?: Bookmark[];
  /** 第 6 批: the comments typed on the paper in the meeting (on, or the last one) */
  comments?: PaperComment[];
  /** live: the passage selected in the paper */
  anchor?: AnchorSel | null;
  /** live, the host's only: how long nobody has spoken (seconds) */
  quiet_s?: number;
  /** 散会后自动保存 the last time (within a few hours; null when it is off or never ran): a failure is said once */
  autosave?: AutosaveResult | null;
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
  /** each person's talk time, longest first */
  talk?: TalkTime[] | null;
  /** the meeting's API tokens and estimated cost (null: no API calls, e.g. a Claude Code account) */
  usage?: Usage | null;
  /** 纪要 being written again in another format (or that just failed) */
  summary_job?: SummaryJob | null;
  /** the moments marked in the meeting */
  bookmarks?: Bookmark[];
  /** 第 6 批: the comments typed on the paper in the meeting (read only here) */
  comments?: PaperComment[];
  /** the left-over keys ticked in this meeting's carry.json ("note:T3"…): 修改清单 and the lobby share them */
  carry_done?: string[];
  /** the hand edits of its transcript (the transcript above has them applied) */
  edits?: Edits | null;
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
  /** in the meeting: bookmarks and, for a co-host, typed questions and a selected passage go through the relay to the
   *  host's Bloom */
  act?: (path: string, body: unknown) => Promise<Record<string, unknown>>;
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
  /** the shared public relay (this PC has no keys of its own) */
  public?: boolean;
  /** connected to the relay now: own relays always; the public one once invites are on */
  armed?: boolean;
  guests?: number;
  error?: string | null;
}

/** a meeting's title without the date older meetings carried in it ("FooBench 论文合稿 · 11月11日 05:03") */
export function cleanTitle(title: string | null | undefined): string {
  return (title ?? '').replace(/\s*·\s*\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*$/, '').trim();
}

/** one thing done in the meeting (zuhui/live_api.py): on the guest page it goes through the relay to the host's Bloom.
 *  Answers the backend's JSON; a refusal throws its code (empty, too_long, not_live, host_only, cohost_only…) */
async function liveAct<T extends Record<string, unknown>>(path: string, body: unknown, timeoutMs = 15000): Promise<T> {
  if (guestT?.act) {
    const d = await guestT.act(path, body);
    if (!d || d.ok === false) throw new Error(String(d?.error ?? 'failed'));
    return d as T;
  }
  const r = await fetch(`${ZUHUI_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    body: JSON.stringify(body ?? {}),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const j = (await r.json().catch(() => ({}))) as T & { ok?: boolean; error?: string };
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

/** the codes the meeting's own actions answer with, worded for the page */
const LIVE_ERRORS = ['empty', 'too_long', 'not_live', 'host_only', 'cohost_only', 'not_ready', 'not_found', 'not_yours', 'too_many', 'failed'];
export const liveError = (e: unknown): string => {
  const c = String((e as Error)?.message ?? e);
  // "too_many" would read as a plural form of the catalog: its words are under over_cap
  return LIVE_ERRORS.includes(c) ? t(`meeting.live.err.${c === 'too_many' ? 'over_cap' : c}`) : c;
};

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
  // 第 8 批: `sample` lists the sample meeting while there is no meeting of the user's own (回顾 asks for it), in the
  // app's language (zuhui/sample.py)
  meetings: (opts?: { sample?: boolean }) => get<MeetingBrief[]>(`/api/meetings?ui=${uiLang()}${opts?.sample ? '&sample=1' : ''}`),
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
  /** your name's colour ('color') or the AI's ('ai_color'), #rrggbb */
  setProfileColor: (which: 'color' | 'ai_color', value: string) => postProfile('/api/profile', { [which]: value }),
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
  /** an OpenAI-format endpoint's models (GET <url>/models; free): from the fields as typed, the saved key when none is typed */
  providerModels: async (p: { kind: ProviderKind; base_url: string; key?: string }): Promise<{ models?: string[]; error?: string }> => {
    const r = await fetch(`${ZUHUI_BASE}/api/provider/models`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(p),
      signal: AbortSignal.timeout(20000),
    });
    return (await r.json().catch(() => ({ error: `HTTP ${r.status}` }))) as { models?: string[]; error?: string };
  },
  /** 查文献 on / off; answers the source again (its lit) */
  setLit: async (on: boolean): Promise<Provider> => {
    const r = await fetch(`${ZUHUI_BASE}/api/provider/lit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on }),
      signal: AbortSignal.timeout(8000),
    });
    const j = (await r.json().catch(() => ({}))) as Partial<Provider> & { error?: string };
    if (!r.ok || !j.kind) throw new Error(j.error || 'failed');
    return j as Provider;
  },
  /** this month's API usage (Settings → Model source) */
  usageMonth: async (): Promise<MonthUsage> => {
    const r = await fetch(`${ZUHUI_BASE}/api/usage`, { cache: 'no-store', signal: AbortSignal.timeout(8000) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()) as MonthUsage;
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
  /** Settings → General → Notes: the format and the group's own requirements */
  summaryPrefs: () => get<SummaryPrefs>('/api/summary/prefs'),
  /** saves the format and / or the requirements; an error's message is its code (template, too_long) */
  setSummaryPrefs: async (p: { template?: SummaryTemplate; custom?: string }): Promise<SummaryPrefs> => {
    const r = await fetch(`${ZUHUI_BASE}/api/summary/prefs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(p),
      signal: AbortSignal.timeout(8000),
    });
    const j = (await r.json().catch(() => ({}))) as SummaryPrefs & { error?: string };
    if (!r.ok || j.error) throw new Error(j.error || 'failed');
    return j;
  },
  /** 回顾 → 保存 → 复制（带格式）: the meeting's 纪要 as HTML (headings, lists) and as plain text, for the clipboard */
  minutesClip: async (id: string): Promise<{ html: string; text: string }> => {
    const r = await fetch(`${ZUHUI_BASE}/api/meetings/${encodeURIComponent(id)}/minutes?ui=${uiLang()}`, { signal: AbortSignal.timeout(15000), cache: 'no-store' });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; html?: string; text?: string; error?: string; detail?: string };
    if (!r.ok || !j.ok || j.html == null) throw new ExportFail(j.error || `HTTP ${r.status}`, j.detail);
    return { html: j.html, text: j.text ?? '' };
  },
  /** 回顾 → 保存 → 存成 Word / 存成 PDF / 导出会议包: the system save dialog, then the backend writes the file there
   *  (a PDF takes a few seconds). The saved path back, null when cancelled; a failure throws (exportReason) */
  saveExport: async (id: string, kind: ExportKind, name: string, transcript = false): Promise<string | null> => {
    const ext = kind === 'pack' ? 'html' : kind;
    const to = await tauriInvoke<string | null>('zuhui_pick_save', { name, ext, title: t(`meeting.history.share.dialog.${kind}`) });
    if (!to) return null;
    const r = await fetch(`${ZUHUI_BASE}/api/meetings/${encodeURIComponent(id)}/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ kind, to, transcript, ui: uiLang() }),
      signal: AbortSignal.timeout(180000),
    });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; path?: string; error?: string; detail?: string };
    if (!r.ok || !j.ok) throw new ExportFail(j.error || `HTTP ${r.status}`, j.detail);
    return j.path || to;
  },
  /** Settings → General → 散会后自动保存 */
  autosave: () => get<AutosavePrefs>('/api/autosave'),
  /** saves any of on / dir / items; an error's message is its code (empty, not_abs, too_long, not_dir, no_write, items) */
  setAutosave: async (p: { on?: boolean; dir?: string; items?: AutosaveItem[] }): Promise<AutosavePrefs> => {
    const r = await fetch(`${ZUHUI_BASE}/api/autosave`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(p),
      signal: AbortSignal.timeout(8000),
    });
    const j = (await r.json().catch(() => ({}))) as AutosavePrefs & { error?: string };
    if (!r.ok || j.error) throw new Error(j.error || 'failed');
    return j;
  },
  /** the system folder picker (散会后自动保存's folder); null when cancelled or outside the app */
  pickFolder: async (): Promise<string | null> => {
    try {
      return (await tauriInvoke<string | null>('zuhui_pick_folder', { title: t('meeting.autosave.pick_title') })) ?? null;
    } catch {
      return null;
    }
  },
  /** 回顾 → 纪要: write the meeting's summary again in this format (the model runs; the old one stays until it is done).
   *  An error's message is its code (busy, custom_empty, template, not_found) */
  regenSummary: async (id: string, template: SummaryTemplate): Promise<SummaryJob> => {
    const r = await fetch(`${ZUHUI_BASE}/api/meetings/${encodeURIComponent(id)}/summary`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ template }),
      signal: AbortSignal.timeout(15000),
    });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; error?: string; job?: SummaryJob };
    if (!r.ok || !j.ok || !j.job) throw new Error(j.error || 'failed');
    return j.job;
  },
  /** the lobby: what the last meetings on the paper on the desk left undone (ticked ones are not listed) */
  carry: () => get<{ paper: string | null; items: CarryItem[] }>('/api/carry'),
  /** ticks (or unticks) one of them; saved with the meeting it came from. An error's message is its code */
  tickCarry: async (id: string, done: boolean): Promise<void> => {
    const r = await fetch(`${ZUHUI_BASE}/api/carry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ id, done }),
      signal: AbortSignal.timeout(8000),
    });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; error?: string };
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
  },
  /** 回顾 → 提问: asks the past meetings (`mids`: only these; none = all). The model runs. An error's message is its
   *  code (empty, too_long) or the reason */
  askPast: async (question: string, mids?: string[] | null): Promise<PastAnswer> => {
    const r = await fetch(`${ZUHUI_BASE}/api/past/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(mids ? { question, mids } : { question }),
      signal: AbortSignal.timeout(200_000),
    });
    const j = (await r.json().catch(() => ({}))) as Partial<PastAnswer> & { ok?: boolean; error?: string };
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return { answer: j.answer ?? '', sources: j.sources ?? [], found: !!j.found };
  },
  /** 回顾 → 阶段汇总: the saved ones, newest first */
  digests: () => get<DigestBrief[]>('/api/digests'),
  digest: (id: string) => get<Digest>(`/api/digests/${encodeURIComponent(id)}`),
  /** writes one for these meetings in the background (the model runs); comes back at once, still running. An
   *  error's message is its code (too_few, too_many, not_found, busy) */
  startDigest: async (mids: string[]): Promise<Digest> => {
    const r = await fetch(`${ZUHUI_BASE}/api/digests`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ mids }),
      signal: AbortSignal.timeout(15000),
    });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; error?: string; digest?: Digest };
    if (!r.ok || !j.ok || !j.digest) throw new Error(j.error || 'failed');
    return j.digest;
  },
  /** 第 6 批 · 设置 → 论文: how a Word paper's edits go back (Word's tracked changes, or straight in) */
  paperPrefs: () => get<{ docx_mode: DocxMode }>('/api/paper/prefs'),
  setPaperPrefs: async (p: { docx_mode: DocxMode }): Promise<{ docx_mode: DocxMode }> => {
    const r = await fetch(`${ZUHUI_BASE}/api/paper/prefs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(p),
      signal: AbortSignal.timeout(8000),
    });
    const j = (await r.json().catch(() => ({}))) as { ok?: boolean; error?: string; docx_mode?: DocxMode };
    if (!r.ok || !j.ok || !j.docx_mode) throw new Error(j.error || `HTTP ${r.status}`);
    return { docx_mode: j.docx_mode };
  },
  /** 第 6 批: a paper's saved versions (none given: the one on screen) */
  paperVersions: (paper?: string | null) =>
    get<PaperVersions>(`/api/paper/versions${paper ? `?paper=${encodeURIComponent(paper)}` : ''}`),
  /** the diff page between two versions ('cur' = the paper as it is now), for an iframe */
  versionDiffUrl: (paper: string, a: string, b: string) =>
    `${ZUHUI_BASE}/api/paper/versions/diff?paper=${encodeURIComponent(paper)}&a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}&embed=1&host=bloom&ui=${uiLang()}`,
  /** put version `v` back (the paper as it is now is saved first); the list as it is now. Throws the code: live, busy, not_found, gone, failed */
  restoreVersion: async (paper: string, v: string): Promise<PaperVersions> => {
    const r = await fetch(`${ZUHUI_BASE}/api/paper/versions/restore`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ paper, v }),
      signal: AbortSignal.timeout(20000),
    });
    const j = (await r.json().catch(() => ({}))) as PaperVersions & { ok?: boolean; error?: string };
    if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
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
  /** the meeting's box (the host, a co-host): ask the AI in writing, the same as saying it (in the transcript, said aloud) */
  ask: (text: string) => liveAct('/api/ask', { text }),
  /** 标记: this moment (`note` optional); with `id`, that bookmark's note changes */
  bookmark: (note: string, id?: string) => liveAct<{ bookmark: Bookmark }>('/api/bookmark', id ? { id, note } : { note }),
  /** 先选中再说: the passage selected in the paper (null: clear it) */
  anchor: (sel: { text: string; section?: string | null } | null) =>
    liveAct<{ anchor: AnchorSel | null }>('/api/anchor', sel ? { text: sel.text, section: sel.section ?? null } : { clear: true }),
  /** 回顾 → 编辑: one hand edit of a past meeting's transcript. A refusal throws its code (no_match, too_long, taken,
   *  same, busy, live, not_found, nothing…) */
  editTranscript: async (
    id: string,
    body: { op: 'line'; i: number; text: string } | { op: 'replace'; find: string; repl: string; hot?: boolean } | { op: 'speaker'; from: string; to: string } | { op: 'undo' },
  ): Promise<EditResult> => {
    const r = await fetch(`${ZUHUI_BASE}/api/meetings/${encodeURIComponent(id)}/transcript`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(15000),
    });
    const j = (await r.json().catch(() => ({}))) as Partial<EditResult> & { ok?: boolean; error?: string };
    if (!r.ok || !j.ok) throw new Error(j.error || 'failed');
    return { n: j.n ?? 0, last: j.last ?? null, count: j.count, hot: j.hot };
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

/** "11月11日" from an ISO time: which meeting, when the hour doesn't matter */
export function fmtDay(iso: string | null | undefined, lang: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  try {
    return new Intl.DateTimeFormat(lang === 'zh-Hans' ? 'zh-CN' : lang, { month: 'short', day: 'numeric' }).format(d);
  } catch {
    return d.toLocaleDateString();
  }
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
