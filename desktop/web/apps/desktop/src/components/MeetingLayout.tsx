// One meeting on screen, live or past (docs/meeting-ui-spec.md §页面). Left to right, after the app's own sidebar:
//   the body  — 修订对比 (the backend's diff page in an iframe, embed mode: full-width text, its own chapter list, and
//               the annotations: a small pin beside each changed paragraph that opens on click and goes away when
//               ticked), 纪要 (the Summary after the meeting) and, in 回顾, 录屏 (the meeting's screen recording);
//   逐字稿    — who said what, with avatars; the line still being spoken shows as it arrives.
// The row above the body: while a meeting is on there is no tab switch, only the paper's tools (上一处 / 下一处 /
// 编辑 / 修订标记 · 修订后 · 原文, drawn here and sent to the iframe by postMessage; the iframe's own toolbar hides);
// after the meeting and in 回顾 the switch comes back with the tools on the same row.
// The top bar: title, state, clock, who is in the meeting, the action buttons. No prompt chips, no input field.
// A meeting without a paper (mode "chat", `chat`): the transcript alone, centred on the page; the AI is not in it.
import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react';
import { relayApi } from '../setupApi';
import { useDismiss } from './useDismiss';
import { backLine, backTip, elapsed, lookupsOf, notesOf } from './claudeNow';
import { handOff } from './paperSwap';
import { tidy, useMenu } from './Menu';
import { isImeKey } from '../ime';
import { Icon } from '@blurt/icons';
import { mi18n as i18n, mt as t, setMeetingLang, i18n as appI18n, t as ut } from '../i18n';
import { foldSteps, reportSteps, stepK, stepV, tidyStep } from './stepText';
import {
  avatarUrl,
  cleanTitle,
  DOC_EDIT_TAG,
  liveError,
  meetingApi,
  type Bookmark,
  type Devices,
  type Invite,
  fmtDuration,
  fmtWhen,
  type Board,
  type Brain,
  type Line,
  type Lookup,
  type Meeting,
  type MeetingPaper,
  type MeetingState,
  type PaperComments,
  type ModelChoice,
  type ModelPart,
  type Models,
  type PartialLine,
  type Participant,
  type Ready,
  type Summary,
  type SummaryJob,
  type TalkTime,
  type Usage,
  type VideoInfo,
  watchMic,
} from '../meetingApi';
import { SummaryTools, SummaryView, TaskBoard, TodoList } from './MeetingSummary';
import { Composer, MarkButton, QuietNote, ToolsFloat, type LiveTools } from './MeetingTools';
import { Highlight } from './Highlight';
import { CLAUDE_COLOR, colorsById, colorsByName, Flower, HOST_COLOR, nameColor, ownPhoto } from './Flower';
import { EditTools, LineEditor, NameEditor, type TranscriptEditing } from './TranscriptEdit';

export type Tab = 'diff' | 'notes' | 'todos' | 'video';
// the transcript is where you see what Claude does (each reply's kind, the background work): wide by default
const TR_DEFAULT = 600;
const TR_MIN = 320;
const TR_MAX = 1000;
const TR_KEY = 'bloom.transcript.w2';
/** the paper's zoom as the iframe last said (the iframe keeps its own copy; this one puts it back if that is lost) */
const ZOOM_KEY = 'bloom.paper.zoom';
const VIEWS = ['mark', 'new', 'old'] as const;
/** one empty list, so a memoized column doesn't see "new people" on every poll */
const NOBODY: Participant[] = [];

/** what the diff page in the iframe says about itself (view.py: zhState) */
interface PaperState {
  cur: number;
  total: number;
  view: string | null;
  views?: string[];
  canEdit: boolean;
  editing: boolean;
  zoom: number;
  zoomSaved?: boolean;
  live?: boolean;
}

export function MeetingLayout({
  meeting,
  titlePrefix,
  titleOverride,
  invite,
  onRename,
  papers,
  activePaper,
  onSwitchPaper,
  onAddPaper,
  transcript,
  partial,
  people,
  suggest,
  onRole,
  board,
  brain,
  summary,
  talk,
  usage,
  summaryJob,
  postBusy,
  onSummaryStarted,
  onToast,
  diffSrc,
  video,
  live,
  status,
  actions,
  notice,
  left,
  review,
  active = true,
  paperVersion,
  onFrame,
  jump,
  tools: toolsIn,
  bookmarks,
  carryDone,
  transcriptEdit,
  comments,
  onHistory,
  chat = false,
}: {
  /** a meeting without a paper (mode "chat"): only the transcript is kept and the AI stays out — no paper column (the
   *  transcript takes the page, centred; in 回顾 the switch has no 修订对比), no AI among the people, no "what the AI is
   *  doing" strip, nobody types to it (the box is 标记 alone: `tools.noAi`) */
  chat?: boolean;
  /** 第 6 批: the comments typed on the paper; the paper's page draws them (a bubble beside the paragraph, a card over
   *  the text) and, when `canAdd`, lets the viewer write, reply and resolve; `canDelete`: the host and co-hosts */
  comments?: PaperComments | null;
  /** 第 6 批: the paper menu's 历史版本 (the host's page): the paper on screen's saved versions */
  onHistory?: (() => void) | null;
  /** 回顾: the host fixes the transcript by hand (编辑 / 替换 / 撤销 in its head) */
  transcriptEdit?: TranscriptEditing | null;
  /** the meeting page and the guest page (live or just ended): the box to type to the AI, 标记, the answer only the
   *  one who asked sees, a selected passage. 回顾 and Dev have none */
  tools?: LiveTools | null;
  /** the moments marked in the meeting: drawn in the transcript at their time; a click goes there */
  bookmarks?: Bookmark[] | null;
  /** 回顾 → 修改清单: what is ticked (the meeting's carry.json, the lobby's left-overs share it) */
  carryDone?: string[] | null;
  /** 回顾: go to this moment (seconds into the meeting) as a click on a 纪要 time does; a new `k` goes again */
  jump?: { sec: number; k: number } | null;
  /** the guest page (example.com): the paper's version on the relay. A newer one loads behind and swaps in, like the
   *  host's own page does when it says it has a new version (the guest's copy can't ask a backend) */
  paperVersion?: number;
  /** the paper's frame on screen (after every swap): the guest page follows the host's scrolling inside it */
  onFrame?: (el: HTMLIFrameElement | null) => void;
  /** 回顾 (an opened past meeting): the 修订对比 / 纪要 / 录屏 switch. The 会议 page never has it: the paper and its tools only */
  review?: boolean;
  /** on screen now: the keys (zoom, Ctrl+F) are this page's; hidden (kept mounted for a fast switch back): popovers close, the recording pauses */
  active?: boolean;
  meeting: Meeting | null;
  /** the paper on the desk: the title when no meeting has one (the live view) */
  titlePrefix?: string | null;
  /** the name shown instead of the meeting's own (the live page before a meeting: the name the next one will get) */
  titleOverride?: string | null;
  /** show the 邀请 button (the live page) */
  invite?: boolean;
  /** 改会议名: the title edits in place. Only before a meeting starts (the live page); once it starts, and in 回顾,
   *  the name is locked (null). '' goes back to the paper's name */
  onRename?: ((title: string) => Promise<void>) | null;
  /** the papers on the desk (a meeting can have several); the title row's 新增 menu lists them, the one on screen ticked */
  papers?: MeetingPaper[] | null;
  /** which of them is on screen */
  activePaper?: string | null;
  /** put another of the papers on screen */
  onSwitchPaper?: ((id: string) => void) | null;
  /** 新增: the file picker; the paper joins the others (before or during the meeting) and comes up */
  onAddPaper?: (() => void) | null;
  /** the transcript: after the cleanup, the clean version */
  transcript: Line[];
  /** the line being spoken right now, not yet finished (live only) */
  partial?: PartialLine | null;
  /** who is in the meeting: the user, Claude, anyone who joined by link */
  people?: Participant[] | null;
  /** participants who were co-hosts last time and haven't been set in this meeting yet */
  suggest?: { name: string; why?: string }[] | null;
  /** 设为共同主持人 / 改回参会者 / dismiss a reminder (the live page only) */
  onRole?: RoleAction;
  board: Board | null;
  /** live only: what Claude is doing right now (the strip on top of the transcript) */
  brain?: Brain | null;
  summary: Summary | null;
  /** 回顾: each person's talk time (worked out from the transcript), shown under the 纪要 */
  talk?: TalkTime[] | null;
  /** 回顾: the meeting's API tokens and estimated cost, shown last under the 纪要 */
  usage?: Usage | null;
  /** 回顾: the 纪要 being written again in another format (or that just failed) */
  summaryJob?: SummaryJob | null;
  /** 回顾: the post-meeting work still runs (the 纪要 can't be written again yet) */
  postBusy?: boolean;
  /** 回顾: a past meeting of the library (not Dev's runs): the 纪要's format and 重新生成 in the bar; called once a
   *  new one has been asked for (fetch the meeting again) */
  onSummaryStarted?: (() => void) | null;
  /** 回顾: a short message at the foot of the window (重新生成 failed) */
  onToast?: (msg: string) => void;
  /** the iframe's page: the live diff, or a past meeting's snapshot; '' = none yet */
  diffSrc: string;
  /** 回顾: the meeting's screen recording; a click on a line plays it from there */
  video?: VideoInfo | null;
  /** the live view: the clock runs, the transcript follows, no tab switch until the meeting ends */
  live: boolean;
  /** what is up behind Claude (models, speech) and on this machine (mic, speaker): the cards on the top bar */
  status?: Ready | null;
  /** the top bar's trailing buttons (开始会议 / 散会) */
  actions?: ReactNode;
  /** a line over the columns (an error, the backend's last word) */
  notice?: ReactNode;
  /** replaces the body column entirely (the drop zone before a paper is in) */
  left?: ReactNode;
}) {
  // the meeting area speaks the meeting's language (an English meeting under a Chinese UI: English cards, steps, board).
  // Set during render so this render's children already use it; only the view on screen sets it (the live page stays
  // mounted, hidden, while 回顾 is open)
  if (active) setMeetingLang(meeting?.lang ?? null);
  const state: MeetingState = meeting?.state ?? 'idle';
  // the 会议 page: the paper and its tools only, before, during and after the meeting (纪要 and the rest are in 回顾)
  const showTabs = !!review;
  const videoSrc = meetingApi.videoUrl(video?.url);
  // 修改清单 (To-dos) shows once the meeting left something on the board: notes to do after it, edits made in it
  const hasTodos = !!board?.tasks?.some((x) => x.note || (x.status === 'done' && !x.rejected))
    || transcript.some((l) => l.tag === DOC_EDIT_TAG);    // or someone typed a change straight into the paper
  // a meeting without a paper has no 修订对比: its first tab is the 纪要
  const first: Tab = chat ? 'notes' : 'diff';
  const tabs: Tab[] = [...(chat ? [] : (['diff'] as Tab[])), 'notes', ...(hasTodos ? (['todos'] as Tab[]) : []), ...(videoSrc ? (['video'] as Tab[]) : [])];
  // 回顾: opening a meeting that has a recording shows the recording
  const [tabSel, setTab] = useState<Tab>(() => (videoSrc ? 'video' : first));
  const tab: Tab = showTabs && tabs.includes(tabSel) ? tabSel : first;
  // the 会议 page of a meeting without a paper: no left column at all, the transcript alone in the middle
  const bare = chat && !showTabs;
  // and nobody types to the AI there (whatever the page around says)
  const tools = useMemo(() => (toolsIn && chat && (!toolsIn.noAi || toolsIn.anchor) ? { ...toolsIn, noAi: true, anchor: null } : toolsIn), [toolsIn, chat]);
  // the host's page of such a meeting (the only one with 邀请): 标记, its one tool, sits in the top bar beside 邀请 and
  // the bar at the foot goes; what comes and goes (the quiet reminder, the co-host reminder, an error) has a slot of its
  // own at the page's foot, so it covers neither the search box nor the transcript (2026-11-20 review). The guest page
  // keeps its box (a phone's top bar has no room)
  const soloHost = bare && !!invite;
  const frame = useRef<HTMLIFrameElement | null>(null);
  // the paper's two iframe slots: a new version of the live page loads in the hidden one and swaps in when it is ready
  // (no blank page, no jump); the slots never move in the DOM (moving an iframe reloads it)
  const slotA = useRef<HTMLIFrameElement | null>(null);
  const slotB = useRef<HTMLIFrameElement | null>(null);
  const [slots, setSlots] = useState<{ a: string; b: string; vis: 'a' | 'b'; loading: boolean }>({ a: diffSrc, b: '', vis: 'a', loading: false });
  const slotsRef = useRef(slots);
  slotsRef.current = slots;
  // the hidden slot has loaded and is being handed the reading place (paperSwap.ts): which one, and how to call it off
  const staged = useRef<{ which: 'a' | 'b'; cancel: (() => void) | null } | null>(null);
  const safety = useRef(0);
  useEffect(() => {
    staged.current?.cancel?.();
    staged.current = null;
    window.clearTimeout(safety.current);
    setSlots({ a: diffSrc, b: '', vis: 'a', loading: false });
  }, [diffSrc]);
  useEffect(
    () => () => {
      staged.current?.cancel?.();
      window.clearTimeout(safety.current);
    },
    [],
  );
  // the paper hasn't come up yet (a long paper typesets its formulas for seconds): a "loading" layer over it, not a black box
  const [diffReady, setDiffReady] = useState(false);
  useEffect(() => setDiffReady(false), [diffSrc]);
  // the visible slot is "the" frame everything else talks to (set after each commit, when the refs are attached)
  const lastFrame = useRef<HTMLIFrameElement | null>(null);
  useEffect(() => {
    frame.current = slots.vis === 'a' ? slotA.current : slotB.current;
    if (onFrame && frame.current !== lastFrame.current) {
      lastFrame.current = frame.current;
      onFrame(frame.current);
    }
  });
  // the guest page: a newer paper on the relay loads in the hidden slot and swaps in when ready (no blank page)
  const pvSeen = useRef<number | undefined>(paperVersion);
  useEffect(() => {
    if (paperVersion === undefined || paperVersion === pvSeen.current) return;
    pvSeen.current = paperVersion;
    setSlots((p) => {
      if (p.loading) return p;
      const next = `${diffSrc}${diffSrc.includes('?') ? '&' : '?'}v=${paperVersion}`;
      return p.vis === 'a' ? { ...p, b: next, loading: true } : { ...p, a: next, loading: true };
    });
  }, [paperVersion, diffSrc]);
  const post = useCallback((m: object) => frame.current?.contentWindow?.postMessage(m, '*'), []);

  // ---------------------------------------------------------------- 第 6 批: the comments typed on the paper
  // the paper's page doesn't fetch them: they go in by postMessage when they change and when a page (a new version in
  // the other slot, a reload) says it is ready ({zhCmReady}); the page sends new ones itself (/api/comment)
  const cmMsg = useMemo(() => {
    if (!comments) return null;
    const colors: Record<string, string> = {};
    const byName = colorsByName(people ?? NOBODY);
    for (const k of Object.keys(byName)) colors[k] = byName[k];
    return { zhComments: { items: comments.items, can: { add: comments.canAdd, del: comments.canDelete }, colors, paper: activePaper ?? null } };
  }, [comments, people, activePaper]);
  const cmRef = useRef(cmMsg);
  cmRef.current = cmMsg;
  const cmSent = useRef('');
  useEffect(() => {
    const k = cmMsg ? JSON.stringify(cmMsg) : '';
    if (!cmMsg || k === cmSent.current) return;
    cmSent.current = k;
    post(cmMsg);
  }, [cmMsg, post]);
  useEffect(() => {
    const on = (e: MessageEvent) => {
      if (!(e.data as { zhCmReady?: number } | null)?.zhCmReady || !cmRef.current) return;
      if (e.source !== slotA.current?.contentWindow && e.source !== slotB.current?.contentWindow) return;
      (e.source as Window).postMessage(cmRef.current, '*');
    };
    window.addEventListener('message', on);
    return () => window.removeEventListener('message', on);
  }, []);

  // ---------------------------------------------------------------- 先选中再说: a passage selected in the paper
  // (the host's and a co-host's, while the meeting is on) goes to the backend; their next words to the AI are about it
  const toolsRef = useRef(tools);
  toolsRef.current = tools;
  useEffect(() => {
    const on = (e: MessageEvent) => {
      const sel = (e.data as { zhSel?: { text?: string; section?: string | null } } | null)?.zhSel;
      const tl = toolsRef.current;
      if (!sel?.text || !tl || !tl.live || tl.role === 'guest') return;
      if (!frame.current || e.source !== frame.current.contentWindow) return;
      meetingApi.anchor({ text: sel.text, section: sel.section ?? null }).then(
        () => tl.onChanged?.(),
        (err) => tl.onError(t('meeting.live.anchor_failed', { a: liveError(err) })),
      );
    };
    window.addEventListener('message', on);
    return () => window.removeEventListener('message', on);
  }, []);
  // the page shows 「引用」 after a selection only to who can point the AI at a passage (2026-11-21: a selection no longer
  // quotes by itself): it asks when it loads ({zhQuoteAsk}), and hears again when that changes (the meeting ends…)
  const canQuote = !!tools && tools.live && tools.role !== 'guest' && !tools.noAi;
  const quoteRef = useRef(canQuote);
  quoteRef.current = canQuote;
  useEffect(() => {
    for (const s of [slotA.current, slotB.current]) s?.contentWindow?.postMessage({ zhQuote: { can: canQuote } }, '*');
  }, [canQuote]);
  useEffect(() => {
    const on = (e: MessageEvent) => {
      if (!(e.data as { zhQuoteAsk?: number } | null)?.zhQuoteAsk) return;
      if (e.source !== slotA.current?.contentWindow && e.source !== slotB.current?.contentWindow) return;
      (e.source as Window).postMessage({ zhQuote: { can: quoteRef.current } }, '*');
    };
    window.addEventListener('message', on);
    return () => window.removeEventListener('message', on);
  }, []);

  // ---------------------------------------------------------------- the paper's tools (the iframe's state)
  const [paper, setPaper] = useState<PaperState | null>(null);
  useEffect(() => setPaper(null), [diffSrc]);
  useEffect(() => {
    const on = (e: MessageEvent) => {
      if (!frame.current || e.source !== frame.current.contentWindow) return;
      if ((e.data as { zhReload?: number } | null)?.zhReload !== undefined) {
        // the page has a new version: load it behind, swap when ready
        frame.current.contentWindow?.postMessage({ zhReloadAck: 1 }, '*');
        setSlots((p) => {
          if (p.loading) return p;
          const sep = diffSrc.includes('?') ? '&' : '?';
          const next = `${diffSrc}${sep}v=${Date.now()}`;
          return p.vis === 'a' ? { ...p, b: next, loading: true } : { ...p, a: next, loading: true };
        });
        // safety: the hidden slot never got ready (backend restarting…): reload the visible one instead of staying stale
        // (not while a loaded one waits for you to stop scrolling / selecting / writing: paperSwap.ts; an earlier
        // version's timer goes when this one is set or a swap is done, so it can't reload a later one mid-read)
        window.clearTimeout(safety.current);
        safety.current = window.setTimeout(
          () =>
            setSlots((q) => {
              if (!q.loading || staged.current) return q;
              const again = `${diffSrc}${diffSrc.includes('?') ? '&' : '?'}v=${Date.now()}`;
              return q.vis === 'a' ? { a: again, b: '', vis: 'a', loading: false } : { a: '', b: again, vis: 'b', loading: false };
            }),
          10_000,
        );
        return;
      }
      const s = (e.data as { zhState?: PaperState } | null)?.zhState;
      if (!s) return;
      setPaper(s);
      try {
        if (s.zoomSaved) localStorage.setItem(ZOOM_KEY, String(s.zoom));
        else {
          // the iframe lost its own copy (storage cleared): give it ours
          const z = Number(localStorage.getItem(ZOOM_KEY));
          if (z && z !== s.zoom) post({ zhZoom: z });
        }
      } catch {
        /* private mode: nothing remembered */
      }
    };
    window.addEventListener('message', on);
    return () => window.removeEventListener('message', on);
  }, [post, diffSrc]);
  // the hidden slot finished loading: give the page a moment to typeset, then ask the page on screen where you are
  // reading (and wait while you scroll, select or write in it), put the new page on that paragraph, swap (2026-11-21:
  // a landed edit used to put the paper back where it was a second or two earlier, under your scrolling)
  const slotLoaded = useCallback((which: 'a' | 'b') => {
    const p = slotsRef.current;
    if (!p.loading || p.vis === which || !(which === 'a' ? p.a : p.b)) return;
    if (staged.current?.which === which) return; // already being handed over (the page reloaded itself for a newer version)
    const st: { which: 'a' | 'b'; cancel: (() => void) | null } = { which, cancel: null };
    staged.current = st;
    const win = (k: 'a' | 'b') => (k === 'a' ? slotA : slotB).current?.contentWindow;
    window.setTimeout(() => {
      if (staged.current !== st) return;
      st.cancel = handOff(win(which === 'a' ? 'b' : 'a'), win(which), () => {
        if (staged.current === st) staged.current = null;
        window.clearTimeout(safety.current);
        setSlots((q) => {
          if (!q.loading || q.vis === which) return q;
          return q.vis === 'a' ? { ...q, a: '', vis: which, loading: false } : { ...q, b: '', vis: which, loading: false };
        });
        window.setTimeout(() => win(which)?.postMessage({ zhHello: 1 }, '*'), 50);
      });
    }, 700);
  }, []);
  // Ctrl + 加号 / 减号 / 0 while the focus is in the shell: the paper zooms (inside the iframe the page catches them)
  const noPaper = !!left || chat;
  useEffect(() => {
    if (!active || !diffSrc || tab !== 'diff' || noPaper) return;
    const k = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.defaultPrevented) return;
      const z = e.key === '=' || e.key === '+' || e.code === 'Equal' || e.code === 'NumpadAdd' ? 'in'
        : e.key === '-' || e.key === '_' || e.code === 'Minus' || e.code === 'NumpadSubtract' ? 'out'
          : e.key === '0' || e.code === 'Digit0' || e.code === 'Numpad0' ? 'reset' : null;
      if (!z) return;
      e.preventDefault();
      post({ zhZoom: z });
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [active, diffSrc, tab, noPaper, post]);

  // Ctrl+F while the focus is in the shell: the paper's own find bar opens (the diff page draws it; inside the frame
  // the page catches the key itself)
  // (no paper: the transcript's own search box takes it)
  useEffect(() => {
    if (!active) return;
    const onFind = () => {
      if (chat) {
        (rootRef.current?.querySelector('[data-testid="transcript-search"]') as HTMLInputElement | null)?.focus();
        return;
      }
      const go = () => {
        frame.current?.focus();
        post({ zhFind: 1 });
      };
      if (tab === 'diff') go();
      else {
        setTab('diff');
        setTimeout(go, 600);
      }
    };
    window.addEventListener('bloom-find', onFind);
    return () => window.removeEventListener('bloom-find', onFind);
  }, [active, chat, tab, post]);

  // switched away (the page stays mounted, hidden): the recording stops, nothing inside keeps the focus
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (active) return;
    vid.current?.pause();
    const f = document.activeElement as HTMLElement | null;
    if (f && rootRef.current?.contains(f)) f.blur();
  }, [active]);

  // ---------------------------------------------------------------- the transcript column's width
  // 600 px by default, dragged between 320 and 1000, remembered; a double-click puts it back
  // a narrow window (a 1024-px laptop, the guest page in a browser): the transcript never takes more than 42 % of it,
  // the paper keeps the room (a width dragged on a wide window comes back when the window is wide again)
  const [vw, setVw] = useState(() => (typeof window === 'undefined' ? 1600 : window.innerWidth));
  useEffect(() => {
    const on = () => setVw(window.innerWidth);
    window.addEventListener('resize', on);
    return () => window.removeEventListener('resize', on);
  }, []);
  const [trW, setTrW] = useState(() => {
    const v = Number(localStorage.getItem(TR_KEY));
    return v >= TR_MIN && v <= TR_MAX ? v : TR_DEFAULT;
  });
  const trRef = useRef(trW);
  trRef.current = trW;
  const setTr = useCallback((w: number) => {
    const v = Math.round(Math.min(TR_MAX, Math.max(TR_MIN, w)));
    setTrW(v);
    try {
      localStorage.setItem(TR_KEY, String(v));
    } catch {
      /* private mode: the width just isn't remembered */
    }
  }, []);
  const dragTr = useCallback((e: ReactMouseEvent) => {
    e.preventDefault();
    const x0 = e.clientX;
    const w0 = trRef.current;
    document.body.classList.add('mw-resizing');
    const move = (ev: MouseEvent) => setTr(w0 + (x0 - ev.clientX));
    const up = () => {
      document.body.classList.remove('mw-resizing');
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  }, [setTr]);
  const resetTr = useCallback(() => setTr(TR_DEFAULT), [setTr]);

  // ---------------------------------------------------------------- the recording
  const vid = useRef<HTMLVideoElement>(null);
  const [vtime, setVtime] = useState<number | null>(null);
  const onVideoTime = () => {
    const v = vid.current;
    if (v) setVtime(v.currentTime);
  };
  const playAt = useCallback((l: Line) => {
    const v = vid.current;
    if (!v || l.vt == null) return;
    setTab('video');
    try {
      v.currentTime = Math.max(0, l.vt);
    } catch {
      /* not loaded yet: the browser seeks once it can */
    }
    setVtime(Math.max(0, l.vt));
    void v.play().catch(() => {});
  }, []);
  // a time in the 纪要 clicked: the transcript line said then (the same second, else the nearest) comes into view and
  // lights up; with a recording, it plays from that line (as a click on the line does)
  const [focusLine, setFocusLine] = useState<{ sec: number; k: number } | null>(null);
  const jumpTo = useCallback(
    (sec: number) => {
      let best: Line | null = null;
      let seen: Line | null = null;
      for (const l of transcript) {
        if (l.who === 'system' || !l.text.trim()) continue;
        if (!best || Math.abs(l.sec - sec) < Math.abs(best.sec - sec)) best = l;
        if (l.vt != null && (!seen || Math.abs(l.sec - sec) < Math.abs(seen.sec - sec))) seen = l;
      }
      if (!best) return;
      if (videoSrc && seen && Math.abs(seen.sec - sec) < 120) playAt(seen);
      const at = best.sec;
      setFocusLine((f) => ({ sec: at, k: (f?.k ?? 0) + 1 }));
    },
    [transcript, videoSrc, playAt],
  );
  // asked from outside (a source of an answer in 回顾 → 提问, a left-over item in the lobby): the same jump
  useEffect(() => {
    if (jump && transcript.length) jumpTo(jump.sec);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump?.k, transcript.length > 0]);
  // the line being played: the latest-starting line that has started (lines are not strictly in vt order)
  const playing = useMemo(() => {
    if (vtime == null || !videoSrc) return null;
    let best: Line | null = null;
    for (const l of transcript) if (l.vt != null && l.vt <= vtime + 0.25 && (!best || l.vt >= (best.vt ?? 0))) best = l;
    return best;
  }, [transcript, vtime, videoSrc]);
  // 纪要 is built when the page is idle (or when it is opened, whichever comes first), then stays (hidden) like the
  // paper and the recording: the first switch to it is a repaint too
  const [notesSeen, setNotesSeen] = useState(false);
  useEffect(() => {
    if (tab === 'notes') {
      setNotesSeen(true);
      return;
    }
    if (!showTabs || notesSeen) return;
    const w = window as unknown as { requestIdleCallback?: (f: () => void, o?: { timeout: number }) => number; cancelIdleCallback?: (h: number) => void };
    if (w.requestIdleCallback) {
      const h = w.requestIdleCallback(() => setNotesSeen(true), { timeout: 2500 });
      return () => w.cancelIdleCallback?.(h);
    }
    const h = setTimeout(() => setNotesSeen(true), 1200);
    return () => clearTimeout(h);
  }, [tab, showTabs, notesSeen]);

  const lang = appI18n().lang;   // the date in the header and other chrome follow the app's language, not the meeting's
  // without a paper the AI is not in the meeting: not among the people (the backend leaves it out too)
  const who = useMemo(() => {
    const all = people ?? NOBODY;
    return chat && all.some((p) => p.id === 'claude') ? all.filter((p) => p.id !== 'claude') : all;
  }, [people, chat]);

  // the running time: the backend's duration_s, and between polls the clock here
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!(live && state === 'live')) return;
    const h = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(h);
  }, [live, state]);
  const duration = (() => {
    if (!meeting) return 0;
    if (state === 'live' && meeting.started) {
      const t0 = new Date(meeting.started).getTime();
      if (!Number.isNaN(t0)) return Math.max(meeting.duration_s, (now - t0) / 1000);
    }
    return meeting.duration_s;
  })();

  // one name: the meeting's own (its paper), else the paper on the desk; the date and length sit on the line below
  const title = titleOverride || cleanTitle(meeting?.title) || titlePrefix || t('meeting.title.none');
  // ended, its post-meeting work still running (回顾: 编辑 is greyed until it's done): 整理中
  const tidying = !!postBusy && state === 'ended';
  const stateKey = state === 'live' ? 'meeting.state.live' : tidying ? 'meeting.state.post' : state === 'ended' ? 'meeting.state.ended' : 'meeting.state.idle';
  const showBoard = live && state !== 'ended' ? true : !summary;
  const ptools = !chat && tab === 'diff' && !!diffSrc && !!paper;
  // the layers of the body: all stay mounted (the paper's page, its MathJax, the decoded recording), the ones not shown
  // are hidden in place, at their size — switching is a repaint, no reload, no reflow of the paper
  const layer = (k: Tab) => `mt-layer ${tab === k ? '' : 'is-off'}`;
  // what comes and goes floats and takes no room: over the paper's column, or (no paper column) over the page's middle
  const suggestNote = onRole && suggest && suggest.length > 0 ? <RoleSuggest list={suggest} onRole={onRole} /> : null;
  const notes = (
    <>
      {tools && <QuietNote tools={tools} />}
      {notice && <div className="mt-notice">{notice}</div>}
    </>
  );
  const floats = (
    <>
      {suggestNote}
      {notes}
    </>
  );

  return (
    <div className={`mt-root ${chat ? 'is-chat' : ''} ${bare ? 'is-one-col' : ''}`} data-testid="meeting" ref={rootRef}>
      <header className="mt-head">
        <div className="mt-head-main">
          {/* one line, fixed height: the title fills the row and its field opens inside that same box, so renaming or
              a longer name never moves anything or changes the paper's size */}
          <div className="mt-title-row">
            <MeetingTitle key={meeting?.id ?? ''} title={title} onRename={onRename ?? null} />
          </div>
          <div className="mt-meta">
            <span className={`mt-state is-${tidying ? 'post' : state}`}>
              <span className="mt-state-dot" />
              {/* the words share one box as wide as the widest: 录制中 → 整理中 → 已结束 never moves the date, nor 新增 after
                  the name block (2026-11-18) */}
              <span className="mt-state-t">
                {(['meeting.state.live', 'meeting.state.post', 'meeting.state.ended', 'meeting.state.idle'] as const).map((k) => (
                  <span key={k} className={k === stateKey ? undefined : 'is-ghost'} aria-hidden={k === stateKey ? undefined : true}>
                    {t(k)}
                  </span>
                ))}
              </span>
            </span>
            {meeting?.started && <span className="mt-meta-item">{fmtWhen(meeting.started, lang)}</span>}
            {meeting && state !== 'idle' && <span className="mt-meta-item mt-clock">{fmtDuration(duration)}</span>}
          </div>
        </div>
        {/* 新增 right after the name, on the bar's centre line like everything else; the people, 邀请 and 开会/散会 sit at
            the right end (2026-11-15: 新增 at the title row's end hung mid-bar, 12 px above the people, and crowded them) */}
        {!chat && (onAddPaper || (papers?.length ?? 0) > 1) && (
          <PaperMenu papers={papers ?? []} active={activePaper ?? null} onSwitch={onSwitchPaper ?? null} onAdd={onAddPaper ?? null} onHistory={onHistory ?? null} />
        )}
        <People people={who} status={status ?? null} onRole={onRole} active={active} uiLang={appI18n().lang} />
        {soloHost && tools && <MarkButton tools={tools} inHead />}
        {invite && <InviteButton topic={title} active={active} uiLang={appI18n().lang} />}
        {actions && <div className="mt-head-acts">{actions}</div>}
      </header>
      <div
        className={`mt-cols ${bare ? 'is-chat' : ''} ${soloHost ? 'is-solo' : ''}`}
        style={{ gridTemplateColumns: bare ? 'minmax(0, 1fr)' : `minmax(0, 1fr) ${Math.max(TR_MIN, Math.min(trW, Math.round(vw * 0.42)))}px` }}
      >
        {!bare && (
        <section className="mt-left" aria-label={t('meeting.left.label')}>
          {left ?? (
            <>
              {(showTabs || ptools) && (
                <div className={`mt-bar ${showTabs ? '' : 'is-tools-only'}`} data-testid="meeting-bar">
                  {showTabs && (
                    <nav className="mt-tabs" role="tablist">
                      {tabs.map((k) => (
                        <button key={k} role="tab" aria-selected={tab === k} className={`mt-tab ${tab === k ? 'is-sel' : ''}`} onClick={() => setTab(k)} data-testid={`meeting-tab-${k}`}>
                          {ut(`meeting.tab.${k}`)}
                        </button>
                      ))}
                    </nav>
                  )}
                  {ptools && paper && <PaperTools s={paper} send={post} />}
                  {showTabs && tab === 'notes' && meeting && onSummaryStarted && (
                    <SummaryTools
                      meetingId={meeting.id}
                      summary={summary}
                      job={summaryJob ?? null}
                      busy={!!postBusy || state === 'live'}
                      onStarted={onSummaryStarted}
                      onToast={onToast}
                    />
                  )}
                </div>
              )}
              <div className="mt-pane" role="tabpanel">
                {/* 论文页一直挂着，切到别的页时只是原地藏起来（大小不变）：切回来不重新加载、不重排，读到哪还在哪 */}
                {chat ? null : diffSrc ? (
                  <div className={layer('diff')}>
                    <iframe
                      ref={slotA}
                      className={`mt-diff ${slots.vis === 'a' ? '' : 'is-staging'}`}
                      src={slots.a || 'about:blank'}
                      title={t('meeting.tab.diff')}
                      onLoad={() => (slots.vis === 'a' ? (setDiffReady(true), post({ zhHello: 1 })) : slotLoaded('a'))}
                    />
                    <iframe
                      ref={slotB}
                      className={`mt-diff ${slots.vis === 'b' ? '' : 'is-staging'}`}
                      src={slots.b || 'about:blank'}
                      title={t('meeting.tab.diff')}
                      aria-hidden={slots.vis !== 'b'}
                      onLoad={() => (slots.vis === 'b' ? (setDiffReady(true), post({ zhHello: 1 })) : slotLoaded('b'))}
                    />
                    {!diffReady && (
                      <div className="mt-diff-wait" role="status" data-testid="diff-loading">
                        <span className="mw-spinner" aria-hidden="true" />
                        <span>{t('meeting.diff.loading')}</span>
                      </div>
                    )}
                  </div>
                ) : (
                  tab === 'diff' && (
                    <div className="mt-empty">
                      <p>{t('meeting.diff.none')}</p>
                    </div>
                  )
                )}
                {showTabs && (tab === 'notes' || notesSeen) && (
                  <div className={`${layer('notes')} mt-scroll`}>
                    {showBoard ? <TaskBoard board={board} /> : summary ? <SummaryView summary={summary} people={who} talk={talk} usage={usage} onJump={jumpTo} /> : null}
                  </div>
                )}
                {showTabs && tab === 'todos' && (
                  <div className={`${layer('todos')} mt-scroll`}>
                    <TodoList board={board} meetingId={meeting?.id ?? null} done={carryDone ?? null} hand={transcript} />
                  </div>
                )}
                {/* 录屏也一直挂着：切去看论文时接着放，逐字稿照样跟着亮 */}
                {videoSrc && (
                  <div className={`${layer('video')} mt-video-box`}>
                    <video
                      ref={vid}
                      className="mt-video"
                      src={videoSrc}
                      controls
                      // WebView2's 下载 in the ⋮ menu does nothing; 保存 → 存录屏 in 回顾 saves it instead
                      controlsList="nodownload"
                      preload="metadata"
                      onTimeUpdate={onVideoTime}
                      onSeeked={onVideoTime}
                      onPlay={onVideoTime}
                      data-testid="meeting-video"
                    />
                  </div>
                )}
              </div>
            </>
          )}
          {/* what comes and goes floats over the paper's column and takes no room: the reminder under the paper's
              bar, the notice at the column's foot (2026-11-18: over the transcript they hid its header and newest line) */}
          {floats}
        </section>
        )}
        {/* the right column: the transcript, and the box at its foot (one height, live or ended); what comes and goes
            (the answer only the one who asked sees, a selected passage) floats over the transcript. Without a paper
            column it is the page's one column, centred, at a width a line still reads well at */}
        <div className="mt-rcol">
          {!bare && <div className="mt-divider" onMouseDown={dragTr} onDoubleClick={resetTr} aria-hidden="true" />}
          <Transcript
            uiLang={appI18n().lang}
            lines={transcript}
            partial={live ? partial : null}
            people={who}
            follow={live}
            active={playing}
            tick={vtime == null ? 0 : Math.floor(vtime)}
            onPick={videoSrc ? playAt : undefined}
            focus={focusLine}
            brain={live && !chat ? brain ?? null : null}
            board={live && !chat ? board : null}
            offline={!live}
            noAi={chat}
            marks={bookmarks ?? undefined}
            onMark={jumpTo}
            editing={transcriptEdit ?? null}
          />
          {tools && <ToolsFloat tools={tools} />}
          {/* the host's page without a paper: the co-host reminder in the transcript's title row (between 逐字稿 and
              the search box, where there is room); the quiet reminder and an error in a slot of their own at the foot,
              one row high, there whether or not anything is in it (nothing moves when one comes or goes) */}
          {soloHost && suggestNote}
          {soloHost && (
            <div className="mt-solo-notes" data-testid="solo-notes">
              {notes}
            </div>
          )}
          {tools && !soloHost && <Composer tools={tools} />}
          {/* no paper column (the guest page): they float over this one (the notice in the empty left of the 标记 row) */}
          {bare && !soloHost && floats}
        </div>
      </div>
    </div>
  );
}

/** The meeting's name, in a box that fills the title row. With `onRename` (before the meeting starts) a click, or
 *  Enter / F2 on it, opens a field laid over that same box: nothing beside it moves or changes size. Enter or leaving
 *  the field saves, Esc puts it back; emptied, the name goes back to the paper's. Enter while the IME is still choosing
 *  a candidate does nothing. Without `onRename` (the meeting is on, or over) it is plain text: no pencil, no hover
 *  hint, nothing to click. */
function MeetingTitle({ title, onRename }: { title: string; onRename?: ((title: string) => Promise<void>) | null }) {
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const field = useRef<HTMLInputElement>(null);
  const editing = draft !== null && !!onRename;
  useEffect(() => {
    if (editing) field.current?.select();
  }, [editing]);
  // the meeting started while the field was open: the name is locked now, the field goes away
  useEffect(() => {
    if (!onRename) setDraft(null);
  }, [onRename]);
  if (!onRename)
    return (
      <h1 className="mt-title" data-testid="meeting-title-box">
        <span className="mt-title-text" data-testid="meeting-title">{title}</span>
      </h1>
    );
  const save = async () => {
    if (draft === null || busy) return;
    const v = draft.replace(/\s+/g, ' ').trim();
    if (v === title) return setDraft(null);
    setBusy(true);
    try {
      await onRename(v);
      setDraft(null);
    } catch {
      // the caller says what went wrong; the field stays open with what was typed
      field.current?.focus();
    } finally {
      setBusy(false);
    }
  };
  return (
    <h1 className={`mt-title is-editable ${editing ? 'is-editing' : ''}`} data-testid="meeting-title-box">
      <button
        type="button"
        className="mt-title-btn"
        onClick={() => setDraft(title)}
        onKeyDown={(e) => {
          if (e.key === 'F2') {
            e.preventDefault();
            setDraft(title);
          }
        }}
        aria-label={`${title} · ${t('meeting.title.rename')}`}
        tabIndex={editing ? -1 : 0}
        aria-hidden={editing || undefined}
        data-testid="meeting-title"
      >
        <span className="mt-title-text">{title}</span>
        <Icon name="pencil" size={13} className="mt-title-pen" />
      </button>
      {editing && (
        <input
          ref={field}
          className="mt-title-input"
          value={draft ?? ''}
          maxLength={60}
          disabled={busy}
          placeholder={t('meeting.title.placeholder')}
          aria-label={t('meeting.title.rename')}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void save()}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !isImeKey(e)) {
              e.preventDefault();
              void save();
            } else if (e.key === 'Escape') {
              e.preventDefault();
              setDraft(null);
            }
          }}
          data-testid="meeting-title-input"
        />
      )}
    </h1>
  );
}

/** 新增: one button at the end of the title row, before and during the meeting, its label never changing. A click opens
 *  the papers on the desk (the one on screen ticked; a click on another puts it on screen) and, last, 新增 (the file
 *  picker: the paper joins the others). With no paper listed yet it goes straight to the picker. */
function PaperMenu({ papers, active, onSwitch, onAdd, onHistory }: {
  papers: MeetingPaper[];
  active: string | null;
  onSwitch: ((id: string) => void) | null;
  onAdd: (() => void) | null;
  /** 第 6 批: 历史版本 of the paper on screen */
  onHistory?: (() => void) | null;
}) {
  const menu = useMenu();
  const cur = papers.find((p) => p.id === active) ?? papers[0];
  const open = (e: ReactMouseEvent<HTMLButtonElement>) => {
    if (!papers.length) return onAdd?.();
    menu.open(
      tidy([
        ...papers.map((p) => ({
          label: p.name,
          checked: p.id === cur?.id,
          disabled: p.id !== cur?.id && !onSwitch,
          run: () => (p.id !== cur?.id ? onSwitch?.(p.id) : undefined),
          testId: `paper-item-${p.id}`,
        })),
        !!(onAdd || onHistory) && { kind: 'separator' as const },
        !!onHistory && { label: t('meeting.paper.history'), icon: <Icon name="history" size={14} />, run: onHistory, testId: 'paper-history' },
        !!onAdd && { label: t('meeting.paper.new'), icon: <Icon name="plus" size={14} />, run: onAdd, testId: 'paper-add' },
      ]),
      { x: 0, y: 0, anchor: e.currentTarget, align: 'end', minWidth: 220 },
      t('meeting.paper.menu'),
    );
  };
  return (
    <>
      <button
        className="mt-btn mt-paper-btn"
        onClick={open}
        aria-haspopup={papers.length ? 'menu' : undefined}
        aria-expanded={papers.length ? menu.isOpen : undefined}
        data-testid="paper-menu"
      >
        <Icon name="plus" size={13} />
        {t('meeting.paper.new')}
        {papers.length > 0 && <Icon name="chevron-down" size={12} />}
      </button>
      {menu.node}
    </>
  );
}

/** The paper's tools in the shell's row: 上一处 · n / N · 下一处 … 编辑 · 修订标记 / 修订后 / 原文; the zoom when it isn't 100 %. */
function PaperTools({ s, send }: { s: PaperState; send: (m: object) => void }) {
  const views = (s.views?.length ? s.views : [...VIEWS]).filter((v) => (VIEWS as readonly string[]).includes(v));
  return (
    <div className="mt-ptools" data-testid="paper-tools">
      <div className="mt-pnav">
        <button className="mt-pbtn" onClick={() => send({ zhCmd: 'prev' })} disabled={!s.total} data-testid="paper-prev">
          <Icon name="chevron-left" size={13} />
          {t('meeting.paper.prev')}
        </button>
        <span className="mt-ppos" aria-live="polite" data-testid="paper-pos">
          <b>{s.cur > 0 ? s.cur : '–'}</b> / {s.total}
        </span>
        <button className="mt-pbtn" onClick={() => send({ zhCmd: 'next' })} disabled={!s.total} data-testid="paper-next">
          {t('meeting.paper.next')}
          <Icon name="chevron-right" size={13} />
        </button>
      </div>
      <span className="mt-pgap" />
      {Math.abs(s.zoom - 1) > 0.001 && (
        <button className="mt-pzoom" onClick={() => send({ zhZoom: 'reset' })} aria-label={t('meeting.paper.zoom_reset')} data-testid="paper-zoom">
          {Math.round(s.zoom * 100)}%
        </button>
      )}
      {s.canEdit && (
        <button className={`mt-pbtn mt-pedit ${s.editing ? 'is-on' : ''}`} aria-pressed={s.editing} onClick={() => send({ zhCmd: 'edit' })} data-testid="paper-edit">
          {t(s.editing ? 'meeting.paper.edit_done' : 'meeting.paper.edit')}
        </button>
      )}
      {views.length > 0 && (
        <div className="mt-pviews" role="group" aria-label={t('meeting.paper.views')}>
          {views.map((v) => (
            <button key={v} className={s.view === v ? 'is-sel' : ''} aria-pressed={s.view === v} onClick={() => send({ zhCmd: 'view', view: v })} data-testid={`paper-view-${v}`}>
              {t(`meeting.paper.view.${v}`)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** One popover of the top bar at a time: opening a person's card closes 邀请, opening 邀请 closes the cards (they were
 *  laid over each other when the pointer crossed a person while 邀请 was open). */
const POP_EVENT = 'mt-popover';
function useOnePopover(id: string, open: boolean, close: () => void) {
  useEffect(() => {
    if (open) window.dispatchEvent(new CustomEvent(POP_EVENT, { detail: id }));
  }, [open, id]);
  useEffect(() => {
    const on = (e: Event) => {
      if ((e as CustomEvent<string>).detail !== id) close();
    };
    window.addEventListener(POP_EVENT, on);
    return () => window.removeEventListener(POP_EVENT, on);
  }, [id, close]);
}

/** 邀请: a fixed link to this user's room on the relay; the popover shows it with one button that copies the link itself. */
// uiLang: only so a change of the app's language re-renders these cached parts
// eslint-disable-next-line @typescript-eslint/no-unused-vars
const InviteButton = memo(function InviteButton({ topic, active, uiLang: _ui }: { topic: string; active: boolean; uiLang: string }) {
  const [open, setOpen] = useState(false);
  const [inv, setInv] = useState<Invite | null>(null);
  const [copied, setCopied] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  // a press anywhere else (the paper included), Esc, or switching pages closes it
  useDismiss(open, () => setOpen(false), box, active);
  const closeInvite = useCallback(() => setOpen(false), []);
  useOnePopover('invite', open, closeInvite);
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setCopied(false);
    meetingApi.invite().then(
      (v) => alive && (setInv(v), setErr(v.ok ? null : v.error ?? null)),
      (e) => alive && setErr(String((e as Error)?.message ?? e)),
    );
    return () => {
      alive = false;
    };
  }, [open]);
  const put = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    setCopied(true);
  };
  const guests = inv?.guests ?? 0;
  return (
    <div className="mt-invite" ref={box}>
      <button className={`mt-btn mt-invite-btn ${open ? 'is-open' : ''}`} onClick={() => setOpen((v) => !v)} aria-expanded={open} data-testid="meeting-invite">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
          <path d="M6.6 9.4a3 3 0 0 0 4.2 0l2.1-2.1a3 3 0 0 0-4.2-4.2l-.9.9" />
          <path d="M9.4 6.6a3 3 0 0 0-4.2 0L3.1 8.7a3 3 0 0 0 4.2 4.2l.9-.9" />
        </svg>
        {t('meeting.invite')}
      </button>
      {open && (
        <div className="mt-invite-pop" role="dialog" aria-label={t('meeting.invite')}>
          <div className="mt-invite-top">
            <span className="mt-invite-head">{t('meeting.invite.title')}</span>
            {guests > 0 && <span className="mt-invite-count">{t('meeting.invite.guests', { n: guests })}</span>}
          </div>
          {inv?.url ? (
            <>
              <div className="mt-invite-topic">
                <span className="mt-invite-k">{t('meeting.invite.topic')}</span>
                <span className="mt-invite-v">{topic}</span>
              </div>
              <div className="mt-invite-field">
                <span className="mt-invite-url" data-testid="meeting-invite-url">{inv.url}</span>
              </div>
              {/* 只复制链接本身（不是整段邀请文字） */}
              <button className="mt-invite-all" onClick={() => {
                void put(inv.url!);
                if (inv.public && !inv.armed) void relayApi.setInvite(true).then(() => setInv({ ...inv, armed: true }), () => {});
              }} data-testid="meeting-invite-copy">
                <Icon name={copied ? 'check' : 'copy'} size={14} />
                {t(copied ? 'meeting.invite.copied' : 'meeting.invite.copy_link')}
              </button>
              {inv.relay === false ? (
                <p className="mt-invite-warn">{t('meeting.invite.no_relay')}</p>
              ) : inv.error ? (
                <p className="mt-invite-warn">{t('meeting.invite.relay_down')}</p>
              ) : null}
            </>
          ) : err ? (
            <p className="mt-invite-warn">{err}</p>
          ) : (
            <span className="mw-spinner sm" aria-hidden="true" />
          )}
        </div>
      )}
    </div>
  );
});

/** Bloom's mark: five petals around a centre (brand/make_logo.py). It is the start-meeting button. */
export function BloomMark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 240 240" aria-hidden="true">
      <g transform="translate(120,120)">
        {[0, 1, 2, 3, 4].map((k) => (
          <ellipse key={k} cx="0" cy="-46" rx="22" ry="40" fill={k % 2 === 0 ? '#3B82F6' : '#60A5FA'} transform={`rotate(${k * 72})`} />
        ))}
        <circle r="26" fill="#1D4ED8" />
        <circle r="9" fill="#EFF6FF" />
      </g>
    </svg>
  );
}

// the host's lines are the host's (rank 'host'); on the guest page 'you' is the guest, not the host
const meOf = (people: Participant[]) => people.find((p) => p.rank === 'host') ?? people.find((p) => p.you) ?? people.find((p) => p.role === 'teacher');
const claudeOf = (people: Participant[]) => people.find((p) => p.id === 'claude') ?? people.find((p) => p.role === 'student');

/** a person's photo; without one of their own (none, the guests' stand-in, or it doesn't load) Bloom's flower in their
 *  colour, on a disc tinted with that colour (meeting.css .mt-avatar.is-flower: light in the light theme) */
function Avatar({ person, color }: { person?: Participant; color: string }) {
  const src = ownPhoto(person?.avatar) ? avatarUrl(person?.avatar) : '';
  const [broken, setBroken] = useState('');
  if (src && broken !== src) return <img className="mt-avatar is-img" src={src} alt="" draggable={false} onError={() => setBroken(src)} />;
  return (
    <span className="mt-avatar is-flower" style={{ '--av-c': color } as CSSProperties} aria-hidden="true">
      <Flower color={color} />
    </span>
  );
}

type Level = 'ok' | 'busy' | 'wait' | 'bad';

/** the four parts' state, from /api/current's ready */
function partLevels(r: Ready): Record<'fast' | 'hands' | 'tts' | 'asr', { level: Level; text: string }> {
  const fastBad = !r.fast || r.fast_online === false;
  const handsBad = r.hands_ready === false;
  const editing = (r.working ?? 0) > 0;
  return {
    fast: { level: fastBad ? 'bad' : 'ok', text: t(fastBad ? 'meeting.status.offline' : 'meeting.status.online') },
    hands: { level: handsBad ? 'bad' : editing ? 'busy' : 'ok', text: t(handsBad ? 'meeting.status.unavailable' : editing ? 'meeting.status.editing' : 'meeting.status.ready') },
    // silent (Settings → Speech): nothing to load; the Microsoft / Windows voices are ready at once (r.tts true)
    tts: r.tts_off ? { level: 'ok', text: t('meeting.status.silent') } : { level: r.tts ? 'ok' : 'wait', text: t(r.tts ? 'meeting.status.ready' : 'meeting.status.loading') },
    asr: { level: !r.blurt ? 'bad' : r.recording ? 'busy' : 'ok', text: t(!r.blurt ? 'meeting.status.disconnected' : r.recording ? 'meeting.status.listening' : 'meeting.status.ready') },
  };
}

const labelOf = (c: ModelChoice | undefined) => (c ? c.options.find((o) => o.id === c.current)?.label ?? c.current : '');

/** 顶栏：谁在会上。三个人以内每人一枚"头像 + 名字"；超过三个人时把人叠成一摞头像（放不下的记成 +N，
    悬浮看名单），Claude 不叠，始终单独一枚。 */
type RoleAction = (name: string, role: 'cohost' | 'participant' | 'dismiss') => void;

/** Who is in the meeting. Every person has a card on hover: avatar, name, what they are here (主持人 / Claude /
 * 参会者 / 老师). Your own card adds your microphone, speaker and transcription (each person sees only their own);
 * Claude's adds its four parts in full — 对话模型, 改稿模型 with 思考强度, 语音合成, 语音识别 — and a click on the first
 * two switches them; a participant's card has 设为老师 / 改回参会者. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars
const People = memo(function People({ people, status, onRole, active, uiLang: _ui }: { people: Participant[]; status: Ready | null; onRole?: RoleAction; active: boolean; uiLang: string }) {
  const [models, setModels] = useState<Models | null>(null);
  const [devices, setDevices] = useState<Devices | null>(null);
  /** the effort slider is open (over the card's last two rows) */
  const [pick, setPick] = useState<'effort' | null>(null);
  /** the stop the slider is on while it moves (shown in the effort chip; committed on release) */
  const [drag, setDrag] = useState<number | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const rangeRef = useRef<HTMLInputElement>(null);
  const paneRef = useRef<HTMLSpanElement>(null);
  const chipRef = useRef<HTMLButtonElement>(null);
  const commitRef = useRef<(i: number) => void>(() => {});
  // the slider folds away on a press anywhere but the slider itself — the card's other rows included
  useEffect(() => {
    if (pick !== 'effort') return;
    const down = (e: PointerEvent) => {
      const n = e.target as Node;
      if (paneRef.current?.contains(n) || chipRef.current?.contains(n)) return;
      setPick(null);
      setDrag(null);
    };
    document.addEventListener('pointerdown', down, true);
    return () => document.removeEventListener('pointerdown', down, true);
  }, [pick]);
  // the native `change` of a range input fires once on release and once per arrow key (React's onChange is `input`)
  useEffect(() => {
    const el = rangeRef.current;
    if (!el || pick !== 'effort') return;
    const f = () => commitRef.current(Number(el.value));
    el.addEventListener('change', f);
    return () => el.removeEventListener('change', f);
  }, [pick]);
  // which card is open ('me', 'claude', a participant's id, or 'stack'): opened on hover or focus, closed when the
  // pointer leaves (unless the card has the focus — a click on it — or the slider is held), on a press outside, on a
  // click inside the paper (its clicks and its mouse moves never reach this page: it posts them over), on Esc, and
  // when the page is switched away. Not CSS :hover — the pointer going into the paper's iframe leaves :hover stuck.
  const box = useRef<HTMLDivElement>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const shut = useCallback(() => {
    setOpenId(null);
    setPick(null);
    setDrag(null);
    const f = document.activeElement as HTMLElement | null;
    if (f && box.current?.contains(f)) f.blur();
  }, []);
  useDismiss(openId != null, shut, box, active);
  useOnePopover('people', openId != null, shut);
  const dragging = useRef(false);
  dragging.current = drag != null;
  useEffect(() => {
    if (!openId) return;
    // the pointer is over the paper now: a card opened by hovering goes away (one holding the focus stays)
    const on = (e: MessageEvent) => {
      const d = e.data as { zhHover?: unknown } | null;
      if (d?.zhHover && !dragging.current && !box.current?.contains(document.activeElement)) shut();
    };
    window.addEventListener('message', on);
    return () => window.removeEventListener('message', on);
  }, [openId, shut]);
  // the live page only (a past meeting has no status); a boolean, so the poll's fresh `status` object every 1.5 s
  // doesn't refetch anything
  const hasStatus = !!status;
  const mic = useMic(active && hasStatus);
  const loadModels = useCallback(() => {
    if (!hasStatus) return;
    meetingApi.models().then((m) => setModels(m), () => setModels(null));
  }, [hasStatus]);
  const loadDevices = useCallback(() => {
    if (!hasStatus) return;
    meetingApi.devices().then(
      (d) => {
        setDevices(d);
        // the system is being asked: its answer is there in a moment
        if (d.probing) setTimeout(() => meetingApi.devices().then(setDevices, () => {}), 1500);
      },
      () => {},
    );
  }, [hasStatus]);
  // voice can switch the models too ("换成 Flash"): the card asks again whenever it opens, and once at the start
  useEffect(() => loadModels(), [loadModels]);
  if (!people.length) return null;
  const lv = status ? partLevels(status) : null;
  const rows = lv
    ? (Object.values(lv) as { level: Level }[])
    : [];
  const overall: Level = rows.some((x) => x.level === 'bad') ? 'bad' : rows.some((x) => x.level === 'wait') ? 'wait' : rows.some((x) => x.level === 'busy') ? 'busy' : 'ok';
  // talking: the guest page knows it from the voice room (p.speaking); the host's page from /api/mic/stream (mic.talk)
  const talking = (p: Participant) => !!p.speaking || mic.talk.has(p.id) || (!p.you && p.id !== 'claude' && mic.talk.has(p.name));
  const kind = (p: Participant) => `${p.you ? 'is-me' : p.id === 'claude' ? 'is-claude' : rank(p) === 'cohost' ? 'is-cohost' : ''}${talking(p) ? ' is-speaking' : ''}`;
  const colorOf = colorsById(people);
  // 'teacher' is the older name of a co-host (meetings before 2026-11-17)
  const rank = (p: Participant) => { const r = p.rank ?? (p.you ? 'host' : p.id === 'claude' ? 'assistant' : 'participant'); return r === 'teacher' ? 'cohost' : r; };
  const choose = async (part: ModelPart, value: string) => {
    setBusy(`${part}:${value}`);
    setErr(null);
    try {
      setModels(await meetingApi.setModel(part, value));
    } catch (e) {
      setErr(String((e as Error)?.message ?? e));
    } finally {
      setBusy(null);
    }
  };

  /** a click on a model's name switches to the next one (two choices each: a toggle); the card doesn't change size */
  const cycle = (part: 'dialog' | 'edit') => {
    const c = models?.[part];
    if (!c?.options?.length || busy) return;
    const i = c.options.findIndex((o) => o.id === c.current);
    const next = c.options[(i + 1) % c.options.length];
    if (next && next.id !== c.current) void choose(part, next.id);
  };
  const effortOpts = models?.effort?.options ?? [];
  const effortIdx = Math.max(0, effortOpts.findIndex((o) => o.id === models?.effort?.current));
  /** the slider let go (or a key pressed on it): the stop it is on becomes the effort */
  const commitEffort = (i: number) => {
    const o = effortOpts[i];
    if (o && o.id !== models?.effort?.current) void choose('effort', o.id).finally(() => setDrag(null));
    else setDrag(null);
  };
  commitRef.current = commitEffort;

  const claudeRows = () => {
    if (!status || !lv) return null;
    const m = status.models ?? {};
    // the names: /api/models when the backend can switch, else what /api/current says
    const handsName = m.hands ?? '';
    const dialog = labelOf(models?.dialog) || m.fast || '';
    const edit = labelOf(models?.edit) || handsName.split(' · ')[0] || '';
    // while the slider moves, the chip shows the stop it is on
    const effort = (drag != null ? effortOpts[drag]?.label : null) || labelOf(models?.effort) || handsName.split(' · ')[1] || '';
    const tts = models?.tts || m.tts || '';
    const asr = models?.asr || m.asr || '';
    const canDialog = (models?.dialog?.options?.length ?? 0) > 1;
    const canEdit = (models?.edit?.options?.length ?? 0) > 1;
    const canEffort = effortOpts.length > 1;
    const row = (key: string, label: string, name: ReactNode, st: { level: Level; text: string }) => (
      <span key={key} className="mt-status-row" data-testid={`model-row-${key}`}>
        <span className="mt-status-hit">
          <span className="mt-status-label" title={label}>{label}</span>
          <span className="mt-status-model">{typeof name === 'string' ? <span className="mt-mname-t" title={name}>{name}</span> : name}</span>
        </span>
        <span className={`mt-status-state is-${st.level}`}>{st.text}</span>
      </span>
    );
    const nameBtn = (part: 'dialog' | 'edit', text: string, can: boolean) =>
      can ? (
        <button
          className={`mt-mname ${busy?.startsWith(part) ? 'is-busy' : ''}`}
          onClick={() => cycle(part)}
          disabled={!!busy}
          aria-label={t('meeting.models.switch', { a: t(`meeting.models.${part}`), b: text })}
          title={text}
          data-testid={`model-${part}`}
        >
          <span className="mt-mname-t">{text}</span>
          <Icon name="sync" size={11} />
        </button>
      ) : (
        <span className="mt-mname-t" title={text}>{text}</span>
      );
    return (
      <>
        {row('dialog', t('meeting.status.fast'), nameBtn('dialog', dialog, canDialog), lv.fast)}
        {row(
          'edit',
          t('meeting.status.hands'),
          <>
            {nameBtn('edit', edit, canEdit)}
            {effort &&
              (canEffort ? (
                <button
                  ref={chipRef}
                  className={`mt-effort is-btn ${pick === 'effort' ? 'is-open' : ''} ${busy?.startsWith('effort') ? 'is-busy' : ''}`}
                  onClick={() => setPick((x) => (x === 'effort' ? null : 'effort'))}
                  aria-expanded={pick === 'effort'}
                  aria-label={t('meeting.models.effort_open', { a: effort })}
                  title={effort}
                  data-testid="model-effort-chip"
                >
                  <span className="mt-effort-t">{t('meeting.models.effort_short', { a: effort })}</span>
                </button>
              ) : (
                <span className="mt-effort" title={effort}>
                  <span className="mt-effort-t">{t('meeting.models.effort_short', { a: effort })}</span>
                </span>
              ))}
          </>,
          lv.hands,
        )}
        {/* the last two rows keep their place; the slider (or an error) is laid over them, inside the card: opening it
            never makes the card bigger */}
        <span className="mt-rows-b">
          {row('tts', t('meeting.status.tts'), tts, lv.tts)}
          {row('asr', t('meeting.status.asr'), asr, lv.asr)}
          {pick === 'effort' && canEffort && (
            <span className="mt-effort-pane" ref={paneRef} data-testid="effort-pane">
              <span
                className="mt-effort-track"
                style={{ '--f': String(effortOpts.length > 1 ? (drag ?? effortIdx) / (effortOpts.length - 1) : 0) } as unknown as import('react').CSSProperties}
              >
                <span className="mt-effort-rail" aria-hidden="true" />
                <span className="mt-effort-fill" aria-hidden="true" />
                {effortOpts.map((o, i) => (
                  <span
                    key={o.id}
                    aria-hidden="true"
                    className={`mt-effort-stop ${i <= (drag ?? effortIdx) ? 'is-past' : ''}`}
                    style={{ left: `calc(9px + (100% - 18px) * ${effortOpts.length > 1 ? i / (effortOpts.length - 1) : 0})` }}
                  />
                ))}
                <input
                  ref={rangeRef}
                  type="range"
                  className="mt-effort-range"
                  min={0}
                  max={effortOpts.length - 1}
                  step={1}
                  value={drag ?? effortIdx}
                  disabled={!!busy}
                  aria-label={t('meeting.models.effort')}
                  aria-valuetext={effortOpts[drag ?? effortIdx]?.label}
                  onChange={(e) => setDrag(Number(e.currentTarget.value))}
                  data-testid="effort-slider"
                />
              </span>
            </span>
          )}
          {err && (
            <span className="mt-effort-pane is-err" role="alert" onClick={() => setErr(null)}>
              {err}
            </span>
          )}
        </span>
      </>
    );
  };

  /** your own card: your microphone, speaker and transcription — nobody else's */
  const meRows = () => {
    if (!status || !lv) return null;
    const d = devices ?? status.devices ?? null;
    const dash = <span className="mt-status-none">{t('meeting.devices.unknown')}</span>;
    return (
      <>
        <span className="mt-status-row">
          <span className="mt-status-hit">
            <span className="mt-status-label">{t('meeting.devices.mic')}</span>
            <span className="mt-status-model">{d?.mic || dash}</span>
          </span>
        </span>
        <span className="mt-status-row">
          <span className="mt-status-hit">
            <span className="mt-status-label">{t('meeting.devices.speaker')}</span>
            <span className="mt-status-model">{d?.speaker || dash}</span>
          </span>
        </span>
        <span className="mt-status-row">
          <span className="mt-status-hit">
            <span className="mt-status-label">{t('meeting.devices.asr')}</span>
            <span className="mt-status-model">{status.models?.asr || dash}</span>
          </span>
          <span className={`mt-status-state is-${lv.asr.level}`}>{lv.asr.text}</span>
        </span>
      </>
    );
  };

  const card = (p: Participant) => (
    <>
      <span className="mt-card-head">
        <Avatar person={p} color={colorOf[p.id]} />
        <span className="mt-card-name" style={{ color: nameColor(colorOf[p.id]) }} title={p.name}>{p.name}</span>
        <span className={`mt-rank is-${rank(p)}`}>{t(`meeting.rank.${rank(p)}`)}</span>
      </span>
      {p.id === 'claude' && claudeRows()}
      {p.you && meRows()}
      {p.you && hasStatus && (
        <button
          className={`mt-card-act ${mic.muted ? 'is-muted' : ''}`}
          onClick={(e) => {
            e.stopPropagation();
            void mic.toggle();
          }}
          aria-pressed={mic.muted}
          data-testid="mic-mute"
        >
          {t(mic.muted ? 'meeting.mic.unmute' : 'meeting.mic.mute')}
        </button>
      )}
      {!p.you && p.id !== 'claude' && onRole && (
        <button className="mt-card-act" onClick={() => onRole(p.name, rank(p) === 'cohost' ? 'participant' : 'cohost')} data-testid={`role-${p.id}`}>
          {t(rank(p) === 'cohost' ? 'meeting.rank.unset' : 'meeting.rank.set')}
        </button>
      )}
    </>
  );
  const opened = (p: Participant) => () => {
    if (p.id === 'claude') loadModels();
    if (p.you) loadDevices();
  };
  const enter = (id: string, p?: Participant) => {
    setOpenId(id);
    if (p) opened(p)();
    else loadDevices();
  };
  const leave = (e: { currentTarget: HTMLElement }) => {
    if (dragging.current || busy || e.currentTarget.contains(document.activeElement)) return;
    setOpenId(null);
    setPick(null);
  };
  const blurOut = (e: { currentTarget: HTMLElement; relatedTarget: EventTarget | null }) => {
    if (dragging.current || e.currentTarget.contains(e.relatedTarget as Node)) return;
    if (!e.currentTarget.matches(':hover')) {
      setOpenId(null);
      setPick(null);
    }
  };
  const chip = (p: Participant) => (
    <span
      key={p.id}
      className={`mt-person has-status ${kind(p)} ${openId === p.id ? 'is-open' : ''}`}
      tabIndex={0}
      onMouseEnter={() => enter(p.id, p)}
      onFocus={() => openId !== p.id && enter(p.id, p)}
      onMouseLeave={leave}
      onBlur={blurOut}
      data-testid={p.id === 'claude' ? 'claude-status' : `person-${p.id}`}
    >
      {/* what comes and goes sits on the avatar, so the name and the rest of the bar never move: the muted mark covers
          your avatar, the AI's state is a dot on its corner */}
      <span className="mt-av-wrap">
        <Avatar person={p} color={colorOf[p.id]} />
        {p.you && hasStatus && mic.muted && <MutedMark onUnmute={mic.toggle} />}
        {p.id === 'claude' && rows.length > 0 && <span className={`mt-status-dot is-${overall}`} aria-hidden="true" />}
      </span>
      <span className="mt-person-name" style={{ color: nameColor(colorOf[p.id]) }} title={p.name}>{p.name}</span>
      <span className={`mt-status-pop mt-card-pop ${p.id === 'claude' ? 'is-claude' : ''}`} role="dialog" aria-label={p.name}>
        {card(p)}
      </span>
    </span>
  );
  // how narrow the group may get on a narrow bar: every chip down to its avatar and three letters (CHIP_MIN, meeting.css
  // .mt-person min-width), the stack never (a flex item's own minimum would be its names' full width, so the bar
  // couldn't shrink them and pushed 散会 off the window: 2026-11-18, 1024 px, English)
  if (people.length <= 3) {
    return (
      <div className="mt-people" ref={box} style={{ minWidth: people.length * CHIP_MIN + (people.length - 1) * 6 }} aria-label={t('meeting.people.label')} data-testid="meeting-people">
        {people.map(chip)}
      </div>
    );
  }
  const humans = people.filter((p) => p.id !== 'claude');
  const claude = people.find((p) => p.id === 'claude');
  const shown = humans.slice(0, 4);
  const more = humans.length - shown.length;
  // the stack: 8 px, then 20 px per avatar (and the +N); its margin, the gap, Claude's chip
  const stackW = 8 + (shown.length + (more > 0 ? 1 : 0)) * 20 + 4;
  return (
    <div className="mt-people" ref={box} style={{ minWidth: stackW + (claude ? 6 + CHIP_MIN : 0) }} aria-label={t('meeting.people.label')} data-testid="meeting-people">
      <span
        className={`mt-stack has-status ${openId === 'stack' ? 'is-open' : ''}`}
        tabIndex={0}
        onMouseEnter={() => enter('stack')}
        onFocus={() => openId !== 'stack' && enter('stack')}
        onMouseLeave={leave}
        onBlur={blurOut}
        data-testid="people-stack"
      >
        {shown.map((p) => (
          <span key={p.id} className={`mt-stack-item ${kind(p)}`}>
            <Avatar person={p} color={colorOf[p.id]} />
            {p.you && hasStatus && mic.muted && <MutedMark onUnmute={mic.toggle} />}
          </span>
        ))}
        {more > 0 && <span className="mt-stack-more">+{more}</span>}
        <span className="mt-status-pop mt-card-pop is-list" role="dialog" aria-label={t('meeting.people.label')}>
          <span className="mt-card-scroll">
            {humans.map((p) => (
              <span key={p.id} className="mt-card-item">
                {card(p)}
              </span>
            ))}
          </span>
        </span>
      </span>
      {claude && chip(claude)}
    </div>
  );
});

const SILENT: ReadonlySet<string> = new Set();
/** a person's chip at its narrowest (meeting.css .mt-people > .mt-person): 3 + 24 avatar + 7 + 32 name + 10 */
const CHIP_MIN = 76;

/** Your microphone's mute switch and who is talking now, from /api/mic/stream (on the guest page: the guest's own mute).
 *  Both are React state, replaced only when they change (the stream itself pushes only changes). */
function useMic(on: boolean) {
  const [muted, setMuted] = useState(false);
  const [talk, setTalk] = useState<ReadonlySet<string>>(SILENT);
  const busy = useRef(false);
  const mutedRef = useRef(false);
  mutedRef.current = muted;
  useEffect(() => {
    if (!on) {
      setTalk(SILENT);
      return;
    }
    return watchMic((s) => {
      if (typeof s.muted === 'boolean' && !busy.current) setMuted(s.muted);
      const now = s.talk;
      if (now) setTalk((prev) => (prev.size === now.length && now.every((x) => prev.has(x)) ? prev : now.length ? new Set(now) : SILENT));
    });
  }, [on]);
  const toggle = useCallback(async () => {
    if (busy.current) return;
    const want = !mutedRef.current;
    busy.current = true;
    setMuted(want);
    try {
      setMuted(await meetingApi.setMicMuted(want));
    } catch {
      setMuted(!want);
    } finally {
      busy.current = false;
    }
  }, []);
  return { muted, talk, toggle };
}

/** a small badge on your avatar's corner while your microphone is muted (nothing there otherwise, so nothing shifts): a crossed-out
 *  microphone; a click unmutes. The switch itself is in your card. */
function MutedMark({ onUnmute }: { onUnmute: () => void }) {
  return (
    <button
      type="button"
      className="mt-mic-off"
      onClick={(e) => {
        e.stopPropagation();
        onUnmute();
      }}
      aria-label={t('meeting.mic.unmute')}
      title={t('meeting.mic.unmute')}
      data-testid="mic-muted"
    >
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
        <rect x="5.5" y="1.8" width="5" height="8.4" rx="2.5" />
        <path d="M3.2 7.8a4.8 4.8 0 0 0 9.6 0M8 12.6v1.8" />
        <path d="M2.4 2.4l11.2 11.2" />
      </svg>
    </button>
  );
}

/** "某某 上次是共同主持人 · 恢复": someone who was a co-host in an earlier meeting joined; co-hosts are set again each meeting. */
function RoleSuggest({ list, onRole }: { list: { name: string }[]; onRole: RoleAction }) {
  const first = list[0];
  if (!first) return null;
  return (
    <div className="mt-suggest" role="status" data-testid="role-suggest">
      <span className="mt-name"><span className="mt-name-t">{first.name}</span></span>
      <span>{t('meeting.rank.suggest')}</span>
      <button className="mt-suggest-yes" onClick={() => onRole(first.name, 'cohost')}>
        {t('meeting.rank.restore')}
      </button>
      <button className="mt-suggest-no" onClick={() => onRole(first.name, 'dismiss')}>
        {t('meeting.rank.no')}
      </button>
    </div>
  );
}

/** 逐字稿栏：谁说了什么。同一个人连着说的话并成一轮，一轮只出一次"头像 + 名字 + 时间"，下面每句一个自然段
    （换人、插了系统行、或隔了 90 秒以上才另起一轮）。新行自动滚到底，上滚就停住；搜索框过滤；正在说的那半句
    接在当前这一轮末尾，颜色淡一点。左缘那条细线可以拖宽窄。回顾里有录屏时：点一句话，录屏从那一刻放；
    放的时候正在说的那句亮起来，轻轻地留在视野里（你自己在滚的时候不抢）。 */
interface Turn {
  who: Line['who'];
  /** a guest's name (guests are told apart by name) */
  name?: string;
  t: string;
  lines: Line[];
  /** not a turn: a moment marked (标记) */
  mark?: Bookmark;
}

/** Where each of Claude's words came from — three kinds, each with its own name and colour:
 *  quick  随口   — the fast model (DeepSeek) filling in: hello, got it, "记下了", "稍等我查一下", "还在查"
 *  think  思考   — the fast model's own thinking: answers, analysis, summaries from the paper it holds
 *  claude Claude — the background Claude: what it found after looking it up, a revision it finished */
export type Cat = 'quick' | 'think' | 'claude';
/** a Claude line split by where its words came from (an old line can hold a filler and the background's answer) */
export type ShownLine = Line & { cat?: Cat; lead?: boolean; shownSteps?: Step[]; stepsFirst?: boolean };
/** one thing Claude did, shown as a grey line by its bubble: "01:05 · 联网搜 TSGBench problem definition" */
export type Step = { t?: string; k: string; v?: string };

// the fillers the app says itself (zuhui/app.py _HOLD_* and the progress nudges): always 随口, whatever line they sit in
const HOLDS = [
  '我去论文里核对一下。',
  '稍等，我在论文里查一下。',
  '稍等，我查一下。',
  '这个我核实一下再说。',
  '我想想，马上回你。',
  '我想一想，马上回你。',
  '刚才那个问题查好了。',
  '还在查，再等我几秒。',
  '快好了，我再核对一下。',
  '还在看原文，马上说。',
  // English meetings: the same fillers word for word (zuhui/app.py _HOLD_*_EN and the progress nudge)
  'Let me check the paper.',
  "One moment, I'll look in the paper.",
  'One moment, let me check.',
  'Let me verify that first.',
  "Let me think, I'll be right back.",
  "Here's what I found:",
  'Still checking, give me a few more seconds.',
  "Almost there, I'm double-checking.",
  'Still reading the source, one moment.',
];

function peelHolds(text: string): [string, string] {
  let head = '';
  let rest = text;
  for (let moved = true; moved; ) {
    moved = false;
    for (const h of HOLDS) {
      if (rest.startsWith(h)) {
        head += h;
        rest = rest.slice(h.length).trimStart();
        moved = true;
      }
    }
  }
  return [head, rest];
}

/** the step the backend names the question it hands over with ("深入思考 要不要在第三章介绍表二的大类"; an English
 *  meeting's words) */
const DEEP_KS = new Set(['深入思考', 'Thinking deeper']);
/** a hand-over to the background (#deep): one step saying what it is to think about. Its own steps name the question:
 *  those; none does: "交给后台" and the question when the tag carries one ("#deep <问题>"). It used to add
 *  "派任务 深入查找" under "深入思考 …": the same thing twice, and not what about (2026-11-21, the user) */
function handOver(l: Line, own: Step[], at: (k: string, v?: string) => Step): Step[] {
  if (own.some((x) => DEEP_KS.has(x.k) && x.v)) return own;
  const q = (l.tag || '').replace(/^\S+\s*/, '').trim();
  return [...own, at(t('meeting.ai.step.to_back'), q || undefined)];
}

/** one of Claude's lines → one or two pieces, each a single kind (cat) with what it was doing (detail) */
export function splitClaude(l: Line): ShownLine[] {
  const tag = (l.tag || '').split(/\s+/)[0];
  const [hold, rest] = peelHolds(l.text);
  const own: Step[] = (l.steps ?? []).map((x) => ({ t: x.t, k: x.k, v: x.v }));
  const at = (k: string, v?: string): Step => ({ t: l.t, k, v });
  const out: ShownLine[] = [];
  // first: the steps came before the words (what the background Claude searched and read before answering)
  const add = (text: string, cat: Cat, steps: Step[] = [], first = false) =>
    text && out.push({ ...l, text, cat, lead: out.length === 0, shownSteps: steps, stepsFirst: first });
  // three kinds, each in its own bubble (2026-11-13, the user): Haiku 随口 (fillers, "好的，我看看", what it says before
  // handing a question over — its bubble then ends with "派任务 …"), Haiku 思考 (its own answers, the quick lookup's
  // result), Opus 思考 (the background's answer, reports, a look at the screen). Both 思考 are blue; the model chip tells them apart.
  if (tag === '#hold') {
    add(l.text, 'quick');
  } else if (tag === '#deep-answer' || tag === '#look') {
    add(hold, 'quick');
    // the quick lookup is Haiku searching the paper (Haiku 思考); the background answer is Opus (Opus 思考)
    add(rest, l.quick ? 'think' : 'claude', tag === '#look' ? [at(t('meeting.ai.step.look')), ...own] : own, true);
  } else if (tag === '#report') {
    add(hold, 'quick');
    const what = l.title || '';
    // each step at its own time, under its own name; a failure says so once ("失败 修改超时 420 秒"), not again under the title
    const did = reportSteps(own);
    const why = l.ok === false && did.some((x) => x.k === '失败');
    // a note / not done went on the to-do list with the text untouched: it says so, not "已修改" (2026-11-21 evening, T3)
    const end = l.noted ? t('meeting.ai.step.noted') : l.ok === false ? t('meeting.ai.step.failed') : t('meeting.ai.step.report');
    add(rest, 'claude', why ? did : [...did, at(end, what)], true);
  } else if (tag === '#deep') {
    add(hold, 'quick');
    // an old line (before 2026-11-12, no lat): the filler and then the background's answer, all in one line
    if (l.lat == null) add(rest, 'claude', own, true);
    else add(rest, 'quick', handOver(l, own, at));
  } else if (tag === '#find') {
    add(l.text, 'quick', [at(t('meeting.ai.step.task'), t('meeting.ai.step.to_find'))]);
  } else if (tag === '#ask') {
    add(hold, 'quick');
    add(rest, 'think', own);
  } else {
    const steps: Step[] =
      tag === '#edit'
        ? [at(t('meeting.ai.step.task'), l.title)]
        : tag === '#app'
          ? [at(t('meeting.ai.step.app'), l.title)]
          : tag === '#status'
            ? [at(t('meeting.ai.step.board'))]
            : [];
    add(l.text, 'quick', steps);
  }
  // cut off while speaking: say so under the last piece only
  const res = out.length ? out : [{ ...l, cat: 'quick' as Cat, lead: true }];
  return res.map((x, i) => (i === res.length - 1 ? x : { ...x, cut: undefined }));
}

// 秒数统一写法：1 秒及以上一律取整（"3 秒""12 秒"），不到 1 秒才留一位小数（"0.8 秒"）。
// 以前 10 秒以下留一位小数，同一屏里"出声 3.0 秒""查了 12 秒"并排，看着像单位不统一
const secs = (s: number) => (s < 1 ? s.toFixed(1) : String(Math.round(s)));

/** the chips on a piece of Claude's: its kind (随口 / 思考), the model's full name, how long it took. No "让 Opus 深想"
 *  button any more (2026-11-13, the user): you hand a question to Opus by saying so ("再想想""让 Opus 看看") */
function ClaudeChips({ l }: { l: ShownLine }) {
  const cat = l.cat ?? 'quick';
  return (
    <span className="mt-chips">
      <span className={`mt-chip is-cat is-${cat}`}>{t(`meeting.ai.cat.${cat}`)}</span>
      {l.model && <span className="mt-chip is-by" data-tip={l.model} data-tip-trunc="">{l.model}</span>}
      {(cat === 'claude' || l.quick) && l.took != null ? (
        <span className="mt-chip is-time">{t('meeting.ai.took', { s: secs(l.took) })}</span>
      ) : l.lead && l.lat != null ? (
        <span className="mt-chip is-time">{t('meeting.ai.lat', { s: secs(l.lat) })}</span>
      ) : null}
      {l.stale ? (
        <span className="mt-chip is-unsaid">{t('meeting.ai.stale_chip')}</span>
      ) : l.unspoken ? (
        <span className="mt-chip is-unsaid">{t('meeting.ai.unspoken_chip')}</span>
      ) : null}
    </span>
  );
}

const STEP_FOLD = 5;

/** what Claude did around a reply, one grey line each: time · what kind of step · the details. More than 5: the newest
 *  5 show, "查看全部 N 条" opens the rest (2026-11-13, the user). A live line (the step being done, "在想… N 秒") blinks.
 *  `head`: a line always shown above them and never folded away ("正在修改 · 已改 41 处 · 7 分 5 秒": past 5 steps it
 *  went into "查看全部" with the oldest, 2026-11-21) */
function Steps({ steps: given, t0, head }: { steps: (Step & { live?: boolean })[]; t0: string; head?: Step & { live?: boolean } }) {
  const [all, setAll] = useState(false);
  // older meetings' steps read like the new ones: a search's words, not its pattern; no app files
  const steps = given.flatMap((x) => tidyStep(x) ?? []);
  const more = steps.length - STEP_FOLD;
  const shown = all || more <= 0 ? steps : steps.slice(-STEP_FOLD);
  const line = (x: Step & { live?: boolean }, key: string) => (
    <p key={key} className={`mt-step ${x.live ? 'is-live' : ''}`}>
      {x.t !== '' ? (
        <>
          <time>{x.t || t0}</time>
          <span aria-hidden="true"> · </span>
        </>
      ) : null}
      <b>{stepK(x.k)}</b>
      {x.v ? <span className="mt-step-v"> {stepV(x.v)}</span> : null}
    </p>
  );
  return (
    <div className="mt-steps">
      {head && line(head, 'head')}
      {more > 0 && (
        <button type="button" className="mt-steps-more" onClick={() => setAll((v) => !v)} aria-expanded={all} data-testid="steps-more">
          {all ? t('meeting.ai.log_less') : t('meeting.ai.log_all', { n: String(steps.length) })}
        </button>
      )}
      {shown.map((x, i) => line(x, `${i}-${x.t ?? ''}-${x.k}`))}
    </div>
  );
}

/** re-render every `ms`: the page keeps time itself (the backend only pushes when something changes) */
function useTick(ms = 1000) {
  const [, set] = useState(0);
  useEffect(() => {
    const h = setInterval(() => set((x) => x + 1), ms);
    return () => clearInterval(h);
  }, [ms]);
}
/** the backend's clock, read off the page's: the push carries the backend's "now", the difference is kept as skew */
const backendNow = (b: Brain | null | undefined) => Date.now() / 1000 + (b?.skew ?? 0);
const since = (b: Brain | null | undefined, t0?: number | null, fallback = 0) =>
  t0 ? Math.max(0, Math.round(backendNow(b) - t0)) : fallback;

/** "Claude 现在…": two rows only — the front (listening / thinking / speaking / muted) and the background (what it is
 *  looking up, which revision it is on). The steps themselves hang under the transcript's bubbles (LiveDeep / LiveEdit).
 *  Several things at once read as one short line: "在查 3 件：问题定义写法、FinTS 次数 等；在改 T3：…；还有 2 条排队".
 *  Each lookup's short name is Haiku's, made once when it starts; the line is put together here, so it never lags.
 *  Pointed at anywhere (the strip itself is the target): all of it, one part a line, with the step each is on
 *  (claudeNow.ts backTip), in the window's own tip (Tooltip.tsx), not the WebView's `title`: that one is a bare system
 *  box, and a `title` whose seconds change every second made it blink the whole time it was pointed at (2026-11-21,
 *  the user). The tip takes its words once, when it opens, and keeps them (and its size) until the pointer leaves */
function ClaudeNow({ brain, board, ai }: { brain: Brain; board: Board | null; ai: string }) {
  useTick(1000);
  const now = backendNow(brain);
  const front = brain.front === 'muted' ? t('meeting.ai.muted', { ai }) : t(`meeting.ai.${brain.front}`);
  const back = backLine(brain, board, now);
  const looks = lookupsOf(brain);
  const dp = looks[looks.length - 1] ?? null;
  // notes don't make the back busy: the text wasn't changed; they are counted after the state
  const notes = notesOf(board);
  const tail = notes ? ' · ' + t('meeting.ai.notes', { n: String(notes) }) : '';
  const backModel = (dp ? dp.model || brain.deep_model : brain.edit_model) || brain.back_model;
  return (
    <div className="mt-now" role="status" aria-live="polite" data-testid="claude-now" data-tip={backTip(brain, board, now)}>
      <div className="mt-now-row">
        <span className={`mt-now-dot is-${brain.front}`} aria-hidden="true" />
        {/* a model name cut with "…" shows itself on hover; whole, the strip's tip takes over (Tooltip.tsx) */}
        {brain.front_model && <span className="mt-now-m" data-tip={brain.front_model} data-tip-trunc="">{brain.front_model}</span>}
        <span className="mt-now-v">{front}</span>
      </div>
      <div className="mt-now-row">
        <span className={`mt-now-dot ${back.length ? 'is-busy' : 'is-idle'}`} aria-hidden="true" />
        <span className="mt-now-m" data-tip={backModel || undefined} data-tip-trunc="">
          {backModel || t('meeting.ai.back')}
        </span>
        {/* one line: what doesn't fit ends in "…" */}
        <span className="mt-now-v" data-testid="claude-now-back">
          {back.length ? back.join('；') : t('meeting.ai.back_idle')}
          {tail && <span className="mt-now-notes">{tail}</span>}
        </span>
      </div>
    </div>
  );
}

/** the background looking something up, live, under the bubble that handed it over ("派任务 交给 Opus 查"): Opus's head
 *  (思考 · Claude Opus 5.5 Extra · 在查 14 秒) and its steps as they come; when the answer is spoken it becomes the answer's bubble */
function LiveDeep({ deep, brain, Head }: { deep: Lookup; brain: Brain | null; Head: () => ReactNode }) {
  useTick(1000);
  const secs = since(brain, deep.t0, deep.secs);
  const idle = since(brain, deep.last, deep.idle ?? 0);
  const steps: (Step & { live?: boolean })[] = [...(deep.steps ?? [])];
  if (deep.state === 'running' && idle >= 3) steps.push({ t: '', k: t('meeting.ai.musing', { s: String(idle) }), live: true });
  return (
    <section className="mt-turn is-student is-cat-claude is-live-deep" data-testid="live-deep">
      <time className="mt-turn-t" />
      <div className="mt-turn-main">
        <header className="mt-turn-head">
          {Head()}
          <span className="mt-chips">
            <span className={`mt-chip is-cat is-${deep.quick ? 'think' : 'claude'}`}>{t(`meeting.ai.cat.${deep.quick ? 'think' : 'claude'}`)}</span>
            {deep.model && <span className="mt-chip is-by" data-tip={deep.model} data-tip-trunc="">{deep.model}</span>}
            <span className="mt-chip is-time">
              {deep.state === 'done' ? t('meeting.ai.deep_wait') : t('meeting.ai.deep_secs', { s: String(secs) })}
            </span>
          </span>
        </header>
        {steps.length ? <Steps steps={steps} t0="" /> : null}
      </div>
    </section>
  );
}

type LiveEditInfo = { idx: number; started?: number | null; edits?: number; log?: string[]; steps?: Step[] };

/** a revision being made, under the bubble that recorded it ("派任务 T3 …"): "T3 正在改 · 已改 1 处 · 36 秒" and the
 *  worker's steps; the seconds are kept by the page */
function LiveEdit({ w, brain }: { w: LiveEditInfo; brain: Brain | null }) {
  useTick(1000);
  const head = [w.edits ? t('meeting.ai.edits', { n: String(w.edits) }) : '', w.started ? elapsed(since(brain, w.started)) : '']
    .filter(Boolean)
    .join(' · ');
  // each step under its own name and time (the backend's meeting clock), no "改稿" in front of every one
  const did = foldSteps(w.steps ?? (w.log ?? []).map((s): Step => ({ t: '', k: s })));
  // no blinking on the last step: the head line already says it's being edited
  const steps: Step[] = did.map((x) => ({ ...x, t: x.t ?? '' }));
  // the "正在修改 · 已改 N 处 · 时间" line stays in view however many steps fold under "查看全部"
  return (
    <div className="mt-live-edit" data-testid="live-edit">
      <Steps steps={steps} t0="" head={{ t: '', k: t('meeting.ai.edit_live'), v: head }} />
    </div>
  );
}

// memoized: the 1.5 s poll and the clock re-render the page; the transcript only when its lines, the half-said line
// or the playing line actually change (the poll keeps unchanged parts identical: useDismiss.keep)
export const Transcript = memo(function Transcript({
  lines,
  partial,
  people,
  follow,
  onDrag,
  onResetWidth,
  active,
  tick,
  onPick,
  focus,
  brain,
  board,
  offline,
  noAi,
  bare,
  marks,
  onMark,
  editing,
}: {
  /** a meeting without the AI (no paper): no "what the AI is doing" strip at all, not even its empty place */
  noAi?: boolean;
  /** 回顾: the host fixes the transcript by hand (编辑 / 替换 / 撤销 in the head) */
  editing?: TranscriptEditing | null;
  /** the moments marked (标记): a small flag row at their time, between the turns; a click goes there */
  marks?: Bookmark[];
  onMark?: (sec: number) => void;
  /** the app's language: the header, search box and status words follow it, and a change re-renders this cached list */
  uiLang?: string;
  /** set inside another pane (Dev's preview): no edge to drag the width by */
  bare?: boolean;
  /** live only: what Claude is doing right now */
  brain?: Brain | null;
  /** live only: the revision tasks (the background Claude's work and its progress) */
  board?: Board | null;
  /** no meeting on: the strip's place still shows, with a line saying what appears there in a meeting */
  offline?: boolean;
  lines: Line[];
  partial?: PartialLine | null;
  people: Participant[];
  follow: boolean;
  onDrag?: (e: ReactMouseEvent) => void;
  onResetWidth?: () => void;
  /** the line the recording is at */
  active?: Line | null;
  /** the recording's whole second: the playing line is checked against the view once a second */
  tick?: number;
  /** a click on a line: play the recording from there */
  onPick?: (l: Line) => void;
  /** a line to bring into view and light up (a time clicked in the 纪要); k changes on every click */
  focus?: { sec: number; k: number } | null;
}) {
  const [q, setQ] = useState('');
  const box = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  /** when the user last moved the column themselves (the playing line doesn't pull it back for a while after) */
  const userAt = useRef(0);
  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    const base = s ? lines.filter((l) => l.text.toLowerCase().includes(s)) : lines;
    // Claude's lines split by where the words came from (随口 / 思考 / Claude): each piece gets its own head
    return base.flatMap((l): ShownLine[] => (l.who === 'student' ? splitClaude(l) : [l]));
  }, [lines, q]);
  // 回顾 → 编辑: on, a click on a line opens it in place (not the recording), a click on a guest's name renames them
  const [editOn, setEditOn] = useState(false);
  const [edLine, setEdLine] = useState<{ i: number; text: string } | null>(null);
  const [edName, setEdName] = useState<string | null>(null);
  const canEdit = !!editing?.can;
  useEffect(() => {
    if (!canEdit) {
      setEditOn(false);
      setEdLine(null);
      setEdName(null);
    }
  }, [canEdit]);
  const editMode = editOn && canEdit;
  const editable = (l: Line) => editMode && l.i != null && l.who !== 'system';
  /** a piece of Claude's line edited: the whole line's words with that piece changed */
  const saveLine = (l: Line, v: string) => {
    const whole = lines.find((x) => x.i === l.i);
    const text = whole && whole.text !== l.text && whole.text.includes(l.text) ? whole.text.replace(l.text, v.trim()) : v;
    return editing!.line(l.i!, text);
  };
  const turns = useMemo(() => {
    const out: Turn[] = [];
    for (const l of shown) {
      const last = out[out.length - 1];
      const prev = last?.lines[last.lines.length - 1];
      // Claude's pieces each keep their own bubble and head (2026-11-13, the user: Haiku's "好的，我看看" in one bubble —
      // 随口 · Claude Haiku 5.5 Low — then "派任务 …", then Opus's answer in a new bubble — 思考 · Claude Opus 5.5 Extra)
      // a typed line and a said one never share a turn: "文字" sits by the name, so it is said of the whole turn
      if (last && prev && l.who !== 'system' && l.who !== 'student' && last.who === l.who && last.name === l.name && l.sec - prev.sec < 90 && !!l.typed === !!prev.typed)
        last.lines.push(l);
      else out.push({ who: l.who, name: l.name, t: l.t || fmtDuration(l.sec), lines: [l] });
    }
    // the marks, each before the first turn that starts after it (none while searching)
    const ms = q.trim() ? [] : [...(marks ?? [])].sort((a, b) => a.sec - b.sec);
    if (!ms.length) return out;
    const merged: Turn[] = [];
    let k = 0;
    for (const u of out) {
      while (k < ms.length && ms[k].sec < u.lines[0].sec) merged.push({ who: 'system', t: ms[k].t, lines: [], mark: ms[k++] });
      merged.push(u);
    }
    while (k < ms.length) merged.push({ who: 'system', t: ms[k].t, lines: [], mark: ms[k++] });
    return merged;
  }, [shown, marks, q]);
  const saying = !q.trim() && partial?.text ? partial : null;
  // ---- following the newest line (live). Stuck to the bottom while the column is at the bottom: new lines, a line that
  // keeps growing as it is said (the half-said line, a guest's "（接上句）", Claude reading on, the steps under a bubble),
  // a resize — anything that makes the content taller pins it to the bottom again, in the same frame (a ResizeObserver
  // runs after layout, before paint: no jump, no lag). Only the user scrolling up lets go; scrolling back to the bottom
  // sticks again. Scrolls nobody asked for (scroll anchoring, a re-sort) never let go.
  const inner = useRef<HTMLDivElement>(null);
  const following = follow && !q.trim();
  const followRef = useRef(following);
  followRef.current = following;
  const lastTop = useRef(0);
  const pin = useCallback(() => {
    const el = box.current;
    if (!el || !followRef.current || !stuck.current) return;
    const max = el.scrollHeight - el.clientHeight;
    if (max - el.scrollTop > 0.5) el.scrollTop = max;
    lastTop.current = el.scrollTop;
  }, []);
  // every commit of this list (a new line, the half-said line growing, a line said on): pinned in the same task as
  // the DOM change, so no frame ever shows it short of the bottom; the observer below covers what grows on its own
  // (the steps under a bubble ticking, a photo loading, the column resized)
  useLayoutEffect(() => pin());
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    pin();
    const ro = new ResizeObserver(() => pin());
    ro.observe(el);
    if (inner.current) ro.observe(inner.current);
    return () => ro.disconnect();
    // the content box comes and goes with the empty state
  }, [pin, following, turns.length === 0 && !saying]);
  /** the user is moving the column (wheel, touch, keys, the scrollbar or a text selection dragged past its edge) */
  const intentAt = useRef(0);
  const dragging = useRef(false);
  const intent = () => {
    intentAt.current = userAt.current = Date.now();
  };
  const onWheel = (e: { deltaY: number }) => {
    intent();
    const el = box.current;
    // scrolling up lets go at once, before the frame that could pin it back (only when there is somewhere to go)
    if (e.deltaY < 0 && el && el.scrollTop > 0 && el.scrollHeight > el.clientHeight + 1) stuck.current = false;
  };
  const onPointerDown = () => {
    dragging.current = true;
    intent();
    const up = () => {
      dragging.current = false;
      window.removeEventListener('pointerup', up, true);
      window.removeEventListener('pointercancel', up, true);
    };
    window.addEventListener('pointerup', up, true);
    window.addEventListener('pointercancel', up, true);
  };
  const onKeyDown = (e: { key: string }) => {
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(e.key)) intent();
  };
  // the line being played stays in view, gently: only when it drifts out of the comfortable middle, and never while
  // you are scrolling the column yourself
  useEffect(() => {
    const el = box.current;
    if (!el || !active) return;
    if (Date.now() - userAt.current < 4000) return;
    const now = el.querySelector('.mt-line-text.is-now') as HTMLElement | null;
    if (!now) return;
    const b = el.getBoundingClientRect();
    const r = now.getBoundingClientRect();
    if (r.top >= b.top + b.height * 0.1 && r.bottom <= b.bottom - b.height * 0.25) return;
    el.scrollTo({ top: el.scrollTop + (r.top - b.top) - b.height * 0.3, behavior: 'smooth' });
  }, [active, tick]);
  // a time clicked in the 纪要: its line into view (a third from the top) and lit up for a moment. A search that hides
  // the line is cleared first; each click (k) is handled once
  const focusDone = useRef(0);
  const [flash, setFlash] = useState<{ sec: number; k: number } | null>(null);
  useEffect(() => {
    const el = box.current;
    if (!el || !focus || focusDone.current === focus.k) return;
    const line = el.querySelector(`[data-sec="${String(focus.sec)}"]`) as HTMLElement | null;
    if (!line) {
      if (q.trim()) setQ('');
      return;
    }
    focusDone.current = focus.k;
    userAt.current = Date.now();
    const b = el.getBoundingClientRect();
    const r = line.getBoundingClientRect();
    el.scrollTo({ top: el.scrollTop + (r.top - b.top) - b.height * 0.3, behavior: 'smooth' });
    setFlash({ sec: focus.sec, k: focus.k });
  }, [focus, shown, q]);
  useEffect(() => {
    if (!flash) return;
    const h = setTimeout(() => setFlash(null), 2000);
    return () => clearTimeout(h);
  }, [flash]);
  // the lit line: two names for the same animation, so a second click on the same line starts it again
  const flashCls = (l: Line) => (flash && l.sec === flash.sec ? (flash.k % 2 ? ' is-flash' : ' is-flash is-flash2') : '');
  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    const top = el.scrollTop;
    const gap = el.scrollHeight - top - el.clientHeight;
    const up = top < lastTop.current - 0.5;
    lastTop.current = top;
    const byUser = dragging.current || Date.now() - intentAt.current < 400;
    if (gap <= 1.5) stuck.current = true;
    else if (byUser) {
      // up: let go; down to within a line of the bottom: stick again
      if (up) stuck.current = false;
      else if (gap < 24) stuck.current = true;
    } else if (stuck.current) pin();
  };
  const me = meOf(people);
  const claude = claudeOf(people);
  const personOf = (w: Line['who'], name?: string) =>
    w === 'teacher'
      ? me
      : w === 'student'
        ? claude
        : w === 'guest'
          ? people.find((p) => p.role === 'guest' && p.name === name) ?? people.find((p) => !p.you && p.id !== 'claude' && p.name === name)
          : undefined;
  const nameOf = (w: Line['who'], name?: string) =>
    w === 'guest'
      ? name || t('meeting.who.guest')
      : personOf(w)?.name ?? t(w === 'teacher' ? 'meeting.who.teacher' : w === 'student' ? 'meeting.who.student' : 'meeting.who.system');
  // each speaker's colour (their flower and their name): the people's own; a guest no longer listed, by name
  const byId = useMemo(() => colorsById(people), [people]);
  const byName = useMemo(() => colorsByName(people, lines.filter((l) => l.who === 'guest').map((l) => l.name || t('meeting.who.guest'))), [people, lines]);
  const colorOf = (w: Line['who'], name?: string) => {
    const p = personOf(w, name);
    return (p && byId[p.id]) || (w === 'teacher' ? HOST_COLOR : w === 'student' ? CLAUDE_COLOR : byName[nameOf(w, name)] ?? HOST_COLOR);
  };
  const lastIsMe = turns.length > 0 && turns[turns.length - 1].who === 'teacher' && !turns[turns.length - 1].mark;
  const head = (w: Line['who'], name?: string, first?: Line) => {
    const c = colorOf(w, name);
    // 回顾 → 编辑: a guest's name renames them in every line of theirs
    const who =
      // (the field opens on the turn clicked only: the same guest heads several turns)
      editMode && w === 'guest' && name && edName === `${name}@${first?.sec}` ? (
        <NameEditor name={name} color={nameColor(c)} onSave={(to) => editing!.rename(name, to)} onDone={() => setEdName(null)} />
      ) : editMode && w === 'guest' && name ? (
        <button type="button" className="mt-line-who mt-who-btn" style={{ color: nameColor(c) }} onClick={() => setEdName(`${name}@${first?.sec}`)}
          aria-label={`${ut('meeting.tredit.rename')} ${name}`} data-testid="tredit-who">
          {/* the name in its own box: a button's own text can't end in "…" */}
          <span className="mt-who-t" data-tip={name} data-tip-trunc="">{name}</span>
        </button>
      ) : (
        <span className="mt-line-who" style={{ color: nameColor(c) }} title={nameOf(w, name)}>{nameOf(w, name)}</span>
      );
    return (
      <header className="mt-turn-head">
        <Avatar person={personOf(w, name)} color={c} />
        {who}
        {first?.typed && <span className="mt-typed" data-testid="transcript-typed">{t('meeting.line.typed')}</span>}
        {w === 'student' && first && <ClaudeChips l={first as ShownLine} />}
      </header>
    );
  };
  // live steps hang under the bubble they belong to (2026-11-13, the user: not in the strip at the top)
  // · each lookup (several can run at once): under the "派任务" bubble that handed it over — the #deep / #find line
  //   said within a few seconds before it started (sec0); none (a lookup nobody announced) → at the end
  const looks = lookupsOf(brain).filter((d) => d.state === 'running');
  const lookAt = new Map<number, Lookup[]>();
  const lookEnd: Lookup[] = [];
  const claimed = new Set<number>();
  for (const d of looks) {
    let at = -1;
    for (let k = turns.length - 1; k >= 0; k--) {
      const f = turns[k].lines[0];
      if (!f) continue;
      const tag = (f.tag || '').split(/\s+/)[0];
      if (turns[k].who !== 'student' || claimed.has(k) || (tag !== '#deep' && tag !== '#find')) continue;
      if (d.sec0 == null) {
        at = k; // an older backend without sec0: the newest hand-over
        break;
      }
      if (f.sec <= d.sec0 + 3 && d.sec0 - f.sec < 40) {
        at = k;
        break;
      }
    }
    if (at >= 0) {
      claimed.add(at);
      lookAt.set(at, [...(lookAt.get(at) ?? []), d]);
    } else lookEnd.push(d);
  }
  const claudeWho = () => (
    <>
      <Avatar person={personOf('student')} color={colorOf('student')} />
      <span className="mt-line-who" style={{ color: nameColor(colorOf('student')) }} title={nameOf('student')}>{nameOf('student')}</span>
    </>
  );
  // · a revision being made: under the bubble that recorded it ("派任务 T3 …"), its steps as the worker goes. Every
  //   revision under way, with the step it is on, is also in the AI strip's tip above the transcript (ClaudeNow): a big
  //   one goes on for minutes, and by then its bubble is far up the column (the card that said so at the transcript's
  //   foot went, 2026-11-21: "本应该出现在上面的状态栏的东西放在了下面")
  const liveEdit = (u: Turn): LiveEditInfo | null => {
    const f = u.lines[0];
    if (u.who !== 'student' || !(f.tag || '').startsWith('#edit') || !f.title) return null;
    const pushed = (brain?.edits ?? []).find((x) => x.title === f.title);
    if (pushed) return pushed;
    const w = (board?.tasks ?? []).find((x) => x.status === 'working' && x.title === f.title);
    return w ? { idx: w.idx, started: w.progress?.started, edits: w.progress?.edits, log: w.progress?.log } : null;
  };
  const pickable = (l: Line) => !!onPick && l.vt != null;
  // the playing line: Claude's lines are split into pieces (new objects), so compare by place, not identity
  const isNow = (l: Line) => !!active && l.sec === active.sec && l.who === active.who && (l.vt ?? null) === (active.vt ?? null);
  return (
    <aside className={`mt-right ${bare ? 'is-bare' : ''}`} aria-label={ut('meeting.transcript')}>
      {!bare && onDrag && <div className="mt-divider" onMouseDown={onDrag} onDoubleClick={onResetWidth} aria-hidden="true" />}
      <div className="mt-right-head">
        <h2>{ut('meeting.transcript')}</h2>
        <span className="mt-right-n">{lines.length ? i18n().formatNumber(lines.length) : ''}</span>
        {editing && (
          <EditTools editing={editing} on={editMode}
            onToggle={() => (setEditOn((v) => !v), setEdLine(null), setEdName(null))} />
        )}
      </div>
      <div className="mt-search">
        <Icon name="search" size={14} />
        <input value={q} placeholder={ut('meeting.transcript.search')} onChange={(e) => setQ(e.target.value)} aria-label={ut('meeting.transcript.search')} data-testid="transcript-search" />
        {q && (
          <button className="icon-btn" onClick={() => setQ('')} aria-label={t('main.empty.clear_search')}>
            <Icon name="close" size={12} />
          </button>
        )}
      </div>
      {noAi ? null : brain ? (
        <ClaudeNow brain={brain} board={board ?? null} ai={claude?.name || t('meeting.who.student')} />
      ) : (
        // not in a meeting (2026-11-13: the user looked at an ended meeting and couldn't find the strip at all); before
        // the first state comes in, the same strip, empty: the lines under it never jump when it appears
        <div className="mt-now is-off" data-testid="claude-now">
          <div className="mt-now-row">
            <span className="mt-now-dot is-idle" aria-hidden="true" />
            <span className="mt-now-v">{offline ? ut('meeting.ai.offline') : ''}</span>
          </div>
        </div>
      )}
      <div
        className="mt-lines"
        ref={box}
        tabIndex={0}
        role="region"
        aria-label={ut('meeting.transcript')}
        onScroll={onScroll}
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onTouchMove={intent}
        onKeyDown={onKeyDown}
        data-testid="transcript"
      >
        {turns.length === 0 && !saying ? (
          <div className="mt-empty">
            {q.trim() ? <p>{t('meeting.transcript.no_match')}</p> : null}
          </div>
        ) : (
          <div className="mt-lines-in" ref={inner}>
            {turns.map((u, i) => (
              <Fragment key={u.mark ? `m${u.mark.id}` : i}>
              {u.mark ? (
                <div className="mt-mark" data-msec={u.mark.sec} data-testid="transcript-mark">
                  {(() => {
                    const b = u.mark!;
                    const by = b.by || nameOf('teacher');
                    const inner = (
                      <>
                        <Icon name="flag" size={12} />
                        <time>{b.t}</time>
                        <span className="mt-mark-who" title={by}>{by}</span>
                        {b.note ? <span className="mt-mark-note" title={b.note}>{b.note}</span> : null}
                      </>
                    );
                    return onMark ? (
                      <button type="button" className="mt-mark-b" onClick={() => onMark(b.sec)} data-tip={t('meeting.mark.jump', { t: b.t })}>
                        {inner}
                      </button>
                    ) : (
                      <span className="mt-mark-b">{inner}</span>
                    );
                  })()}
                </div>
              ) : u.who === 'system' ? (
                <p key={i} className="mt-sysline">
                  {u.lines.map((l) => l.text).join(' ')}
                </p>
              ) : (
                <section
                  key={i}
                  className={`mt-turn is-${u.who} ${u.who === 'student' ? `is-cat-${(u.lines[0] as ShownLine).cat ?? 'quick'}` : ''}`}
                  style={{ '--who': colorOf(u.who, u.name) } as CSSProperties}
                >
                  <time className="mt-turn-t">{u.t}</time>
                  <div className="mt-turn-main">
                  {head(u.who, u.name, u.lines[0])}
                  {(() => {
                    const f = u.lines[0] as ShownLine;
                    return f.stepsFirst && f.shownSteps?.length ? <Steps steps={f.shownSteps} t0={u.t} /> : null;
                  })()}
                  <div className="mt-bubble">
                  {u.lines.map((l, k) => (
                    <Fragment key={k}>
                    {edLine && editable(l) && edLine.i === l.i && edLine.text === l.text ? (
                      <LineEditor text={l.text} onSave={(v) => saveLine(l, v)} onDone={() => setEdLine(null)} pick={pickable(l)} />
                    ) : editable(l) ? (
                      // 编辑 on: a click opens the line in place (the recording stays where it is)
                      <p
                        className={`mt-line-text is-editable ${pickable(l) ? 'is-pick' : ''}${flashCls(l)}`}
                        data-sec={l.sec}
                        role="button"
                        tabIndex={0}
                        onClick={() => setEdLine({ i: l.i!, text: l.text })}
                        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), setEdLine({ i: l.i!, text: l.text }))}
                        // named by its words (what you see); what a click does is the description
                        aria-description={ut('meeting.tredit.line')}
                        data-testid="transcript-line"
                      >
                        <Highlight text={l.text} q={q} />
                        {/* on hover, right of the bubble (transcript-edit.css) */}
                        <Icon name="pencil" size={13} className="mt-line-pen" />
                      </p>
                    ) : pickable(l) ? (
                      <p
                        className={`mt-line-text is-pick ${isNow(l) ? 'is-now' : ''}${flashCls(l)}`}
                        data-sec={l.sec}
                        role="button"
                        tabIndex={0}
                        onClick={() => onPick!(l)}
                        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onPick!(l))}
                        data-testid="transcript-line"
                      >
                        <Highlight text={l.text} q={q} />
                      </p>
                    ) : (
                      <p className={`mt-line-text${flashCls(l)}`} data-sec={l.sec}>
                        <Highlight text={l.text} q={q} />
                      </p>
                    )}
                    </Fragment>
                  ))}
                  {/* only what was said: the AI's own judgments on a line (l.skip, "不是在跟它说", "你们在互相说") stay
                      in the data for the logs and are never drawn here */}
                  {u.lines.map((l, k) =>
                    l.cut ? (
                      <p key={`c${k}`} className="mt-aside" data-testid="transcript-cut">
                        {t('meeting.ai.cut')}
                      </p>
                    ) : null,
                  )}
                  {saying && lastIsMe && i === turns.length - 1 && (
                    <p className="mt-line-text is-partial" data-testid="transcript-partial">
                      {saying.text}
                    </p>
                  )}
                  </div>
                  {(() => {
                    const f = u.lines[0] as ShownLine;
                    return !f.stepsFirst && f.shownSteps?.length ? <Steps steps={f.shownSteps} t0={u.t} /> : null;
                  })()}
                  {(() => {
                    const ed = liveEdit(u);
                    return ed ? <LiveEdit w={ed} brain={brain ?? null} /> : null;
                  })()}
                  </div>
                </section>
              )}
              {(lookAt.get(i) ?? []).map((d) => (
                <LiveDeep key={`d${d.id ?? d.t0}`} deep={d} brain={brain ?? null} Head={claudeWho} />
              ))}
              </Fragment>
            ))}
            {lookEnd.map((d) => (
              <LiveDeep key={`e${d.id ?? d.t0}`} deep={d} brain={brain ?? null} Head={claudeWho} />
            ))}
            {saying && !lastIsMe && (
              <section className="mt-turn is-teacher" style={{ '--who': colorOf('teacher') } as CSSProperties}>
                <time className="mt-turn-t">{saying.t}</time>
                <div className="mt-turn-main">
                  {head('teacher')}
                  <div className="mt-bubble">
                    <p className="mt-line-text is-partial" data-testid="transcript-partial">
                      {saying.text}
                    </p>
                  </div>
                </div>
              </section>
            )}
          </div>
        )}
      </div>
    </aside>
  );
});
