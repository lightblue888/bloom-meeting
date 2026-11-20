// The guest page: example.com/room/<id>. Before joining: the meeting's name, who started it, a name field, the microphone
// switch. After joining: the host's own meeting page (MeetingLayout) — the paper with its revision marks, the
// transcript, the edit cards, what Claude is doing — fed by the relay instead of a backend on this machine, minus the
// host's Dev and the 设为共同主持人 button. Anyone can add a paper (新增) or put another one on screen for everybody;
// confirming / undoing an edit and editing a paragraph in place are the host's and the co-hosts' (the host's Bloom
// decides, the relay only queues). The voice is LiveKit (voice.ts); the paper follows the host's scrolling (follow.ts).
import { memo, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { applyPlatform, applyTheme } from '@blurt/ui';
import { setLang } from '../i18n';
import { setGuestTransport, type AnchorSel, type Board, type Bookmark, type PaperComment, type Brain, type Line, type Lookup, type Meeting, type MeetingPaper, type Models, type Participant, type PartialLine, type Ready } from '../meetingApi';
import { BloomMark, MeetingLayout } from '../components/MeetingLayout';
import type { LiveTools } from '../components/MeetingTools';
import { nameColor } from '../components/Flower';
import { GuestVoice, type VoiceSnapshot } from './voice';
import { Follow, type HostFocus } from './follow';

const ZH = /^zh/i.test(navigator.language || '');
setLang(ZH ? 'zh-Hans' : 'en');
const L = (zh: string, en: string) => (ZH ? zh : en);
/** the relay's error codes (relay/token_server.py) in words; anything else as it came */
const RELAY_ERR: Record<string, [string, string]> = {
  busy: ['服务器繁忙', 'server busy'],
  'too large': ['文件过大', 'file too large'],
  'too many': ['操作过于频繁', 'too many requests'],
  'too many assets': ['文件数量已达上限', 'too many files'],
  type: ['不支持的文件类型', 'unsupported file type'],
  cohost_only: ['仅主持人和共同主持人可操作', 'host and co-hosts only'],
  unauthorized: ['链接已失效', 'link expired'],
  'not found': ['会议不存在', 'meeting not found'],
  'no room': ['会议不存在', 'meeting not found'],
  'no such upload': ['文件不存在', 'file not found'],
  name_taken: ['名字已被占用', 'name taken'],
  identity: ['身份无效', 'invalid identity'],
  'bad json': ['请求无效', 'bad request'],
};
const relayErr = (e: unknown) => {
  const s = String(e ?? '');
  const m = RELAY_ERR[s];
  return m ? L(m[0], m[1]) : s;
};

/** what the host's Bloom pushes to the relay while the meeting is on (zuhui/view.py guest_state, v2) */
interface Assistant { name?: string; dialog?: string; edit?: string; effort?: string; tts?: string; asr?: string }
interface RelayState {
  title?: string;
  host?: string;
  live?: boolean;
  upcoming?: boolean;
  started?: string;
  ended?: string;
  v?: number;
  meeting?: Partial<Meeting>;
  papers?: MeetingPaper[];
  active_paper?: string | null;
  transcript?: Line[];
  partial?: PartialLine | null;
  board?: Board;
  brain?: Brain | null;
  participants?: Participant[];
  people?: { name: string; role?: string; rank?: string; avatar?: string }[];
  assistant?: Assistant;
  focus?: HostFocus | null;
  avatar_default?: string;
  /** the version of the transcript + board, pushed apart from this (they are big and change every few seconds) */
  lv?: string;
  /** 第 3 批: the moments marked, a co-host's selected passage */
  bookmarks?: Bookmark[];
  /** 第 6 批: the comments typed on the paper (everyone in the meeting writes, replies, resolves; co-hosts delete) */
  comments?: PaperComment[];
  anchor?: AnchorSel | null;
  /** 第 8 批: the host's Bloom is recording the screen (and the microphone), only the sound, or nothing */
  rec?: 'screen' | 'audio' | null;
  /** a meeting without a paper: mode "chat", paper false — no paper page to load, only the transcript, the AI stays out */
  mode?: 'chat' | 'paper';
  paper?: boolean;
}

/** the recording notice in one sentence (the join card; the bar's marker shows it on hover) */
const recLine = (rec: RelayState['rec']) =>
  rec === 'audio' ? L('会议正在录音并转写', 'This meeting’s audio is being recorded and transcribed')
    : L('会议正在录屏并转写', 'This meeting is being recorded and transcribed');

const ROOM = (() => {
  const m = location.pathname.match(/\/room\/([A-Za-z0-9_-]{6,64})/);
  return m ? m[1] : new URLSearchParams(location.search).get('room') || '';
})();
const enc = encodeURIComponent;
const store = (k: string, v?: string): string | null => {
  try {
    if (v === undefined) return localStorage.getItem(k);
    localStorage.setItem(k, v);
  } catch { /* private mode */ }
  return null;
};

// ---------------------------------------------------------------- the relay's state, polled (1 s; 4 s in the background)
function useRelay() {
  const [s, setS] = useState<{ sv: number; pv: number; state: RelayState | null; loaded: boolean; lost: number }>({ sv: 0, pv: 0, state: null, loaded: !ROOM, lost: 0 });
  useEffect(() => {
    if (!ROOM) return;
    let alive = true;
    let sv = -1;
    let timer = 0;
    let lv = '';
    let heavy: { transcript?: Line[]; board?: Board; comments?: PaperComment[] } = {};
    const tick = async () => {
      try {
        const r = await fetch(`/view/${enc(ROOM)}/state?sv=${sv}`, { cache: 'no-store', signal: AbortSignal.timeout(6000) });
        const d = await r.json();
        if (!alive) return;
        if (d.same) setS((p) => (p.pv === d.pv && !p.lost ? p : { ...p, pv: d.pv, lost: 0 }));
        else {
          sv = d.sv;
          const st: RelayState | null = d.state ?? null;
          // the transcript and the board come apart (lv); fetched only when they changed
          if (st?.lv && st.lv !== lv) {
            try {
              const h = await (await fetch(`/view/${enc(ROOM)}/lines`, { cache: 'no-store', signal: AbortSignal.timeout(10000) })).json();
              // 第 6 批: the comments on the paper come with them (a host older than that sends none: keep the state's)
              heavy = { transcript: h.transcript ?? [], board: h.board ?? undefined, ...(Array.isArray(h.comments) ? { comments: h.comments } : {}) };
              lv = st.lv;
            } catch { /* next tick tries again (lv stays the old one) */ }
          }
          if (!alive) return;
          setS({ sv: d.sv, pv: d.pv, state: st ? (st.lv ? { ...st, ...heavy } : st) : null, loaded: true, lost: 0 });
        }
      } catch {
        // the relay is away: keep what we have, remember since when (the page says so after a few seconds)
        if (alive) setS((p) => (p.lost ? p : { ...p, lost: Date.now() }));
      }
      if (alive) timer = window.setTimeout(tick, document.hidden ? 4000 : 1000);
    };
    void tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);
  return s;
}

type Phase = 'loading' | 'invalid' | 'wait' | 'live' | 'ended';
const phaseOf = (loaded: boolean, st: RelayState | null): Phase =>
  !loaded ? 'loading' : !st ? 'invalid' : st.live ? 'live' : st.upcoming ? 'wait' : 'ended';

// ---------------------------------------------------------------- a tiny store: its readers redraw only when the part they read changed
function makeStore<T>(get: () => T, keyOf: (v: T) => string) {
  const subs = new Set<() => void>();
  let snap = get();
  let key = keyOf(snap);
  return {
    bump() {
      const next = get();
      const k = keyOf(next);
      if (k === key) return;                 // nothing this store's readers care about changed
      key = k;
      snap = next;
      subs.forEach((f) => f());
    },
    subscribe(f: () => void) {
      subs.add(f);
      return () => subs.delete(f);
    },
    get: () => snap,
  };
}

const latest: { state: RelayState | null } = { state: null };

/** a phone (guest.css's 640 px): the notice hangs over the bottom bar, whichever tab is showing */
const phoneMq = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia('(max-width: 640px)') : null;
const onPhoneChange = (f: () => void) => {
  phoneMq?.addEventListener('change', f);
  return () => phoneMq?.removeEventListener('change', f);
};
const isPhone = () => !!phoneMq?.matches;

/** a model's name as guests see it: never an organisation's prefix ("my-company/…") or a context tag */
const bareModel = (m?: string | null) => (m ? m.replace(/\[1m\]$/i, '').split('/').pop()!.trim() || m : m ?? undefined);

// ---------------------------------------------------------------- the voice and the following: one each for the page,
// made once outside React (StrictMode builds hooks twice in development; two voices would split their state)
let phaseNow: Phase = 'loading';
const sink: { toast: (m: string) => void } = { toast: () => {} };
const follow: Follow = new Follow(() => followStore.bump(), (m) => voice.publish(m));
const followStore = makeStore(() => ({ following: follow.following, host: follow.hasHost() }), (f) => `${f.following}|${f.host}`);
const voice: GuestVoice = new GuestVoice(
  ROOM, L, () => phaseNow === 'live',
  () => metaStore.bump(),
  (topic, m) => {
    if (topic === 'bloom' && m?.t === 'focus') {
      delete m.t;
      follow.onFocus(m, 'dc');
    } else if (topic === 'bloom-s') follow.onStream(m);
  },
  (msg) => sink.toast(msg),
);
/** what the page redraws for: joined, the connection, the microphone on / off, who is talking */
const metaStore = makeStore(() => voice.snapshot(), (s) => JSON.stringify([s.joined, s.connected, s.micOn, s.conn, s.connKind, s.audioBlocked, s.speaking]));

export function GuestApp() {
  useEffect(() => {
    // the colours come in sets per platform and theme (tokens.css): both on <html>, like the app
    applyPlatform(/Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent) ? 'mac' : 'win');
    applyTheme('auto');
  }, []);
  const [me, setMe] = useState<string | null>(null);
  const joined = !!me;
  const relay = useRelay();
  const phase = phaseOf(relay.loaded, relay.state);
  phaseNow = phase;
  // the last full state while the meeting was on: after it ends, the guest keeps looking at it (read only) until they
  // leave — the final revisions, the transcript — instead of being thrown back to the join card
  const frozen = useRef<RelayState | null>(null);
  const frozenPv = useRef(0);
  // decided in the same render the end is seen (an effect would let one render through with the ended state: a new
  // paper version, the guest no longer a co-host — and the paper on screen reloads into "not there")
  const endedSeen = useRef(false);
  if (!joined) endedSeen.current = false;
  else if (phase === 'ended' && frozen.current) endedSeen.current = true;
  const ended = joined && endedSeen.current;
  if (phase === 'live' && (relay.state?.v ?? 0) >= 2 && !ended) {
    frozen.current = relay.state;
    frozenPv.current = relay.pv;     // the relay deletes the paper when the meeting ends: keep the version on screen
  }
  const st = ended ? frozen.current : relay.state;
  latest.state = st;
  // a meeting without a paper: the transcript alone takes the page (on a phone too: no 论文 / 逐字稿 tabs), the AI is
  // not in it, nobody types to it
  const chat = st?.mode === 'chat' || st?.paper === false;

  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const toast = useCallback((text: string) => {
    const id = Date.now() + Math.random();
    setToasts((p) => [...p.slice(-2), { id, text }]);
    window.setTimeout(() => setToasts((p) => p.filter((x) => x.id !== id)), 2800);
  }, []);
  sink.toast = toast;

  const vs = useSyncExternalStore(metaStore.subscribe, metaStore.get);
  const fs = useSyncExternalStore(followStore.subscribe, followStore.get);
  const phone = useSyncExternalStore(onPhoneChange, isPhone);


  // the host's place from the relay too (the data channel is faster, this one is the fallback)
  useEffect(() => {
    if (joined) follow.onFocus(st?.focus ?? null, 'relay');
  }, [joined, st?.focus]);

  // the meeting ended while in it: hang up; the page stays as it was at the end (read only) until they leave. No toast:
  // the notice over the paper and the bar's state already say it (a toast too made it twice, 2026-11-19)
  useEffect(() => {
    if (joined && phase === 'ended') {
      voice.leave('ended');
      if (!frozen.current) setMe(null);
    }
  }, [joined, phase]);

  // ---------------------------------------------------------------- doing things: through the relay to the host's Bloom
  const amCo = useMemo(() => {
    const p = (st?.participants ?? []).find((x) => x.name === me && x.rank !== 'host' && x.id !== 'claude');
    return !!p && (p.rank === 'cohost' || p.rank === 'teacher');
  }, [st?.participants, me]);
  const coRef = useRef(amCo);
  coRef.current = amCo;

  const waitResult = useCallback(async (id: string, ms: number) => {
    const until = Date.now() + ms;
    while (Date.now() < until) {
      await new Promise((r) => setTimeout(r, 450));
      try {
        const tok = await voice.getToken();
        const r = await fetch(`/view/${enc(ROOM)}/result/${enc(id)}`, { headers: { Authorization: `Bearer ${tok}` }, cache: 'no-store' });
        const d = await r.json();
        if (d.done) return d.result ?? { ok: true };
      } catch { /* try again */ }
    }
    return { ok: false, error: L('主持人无响应', 'The host did not respond') };
  }, []);

  const act = useCallback(async (path: string, body: unknown): Promise<Record<string, unknown>> => {
    if (path === '/api/focus' || path === '/api/scroll') return { ok: true };       // the guest's own place: not reported
    try {
      const tok = await voice.getToken();
      const r = await fetch(`/view/${enc(ROOM)}/act`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, body }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.id) return { ok: false, error: d.error || `HTTP ${r.status}` };
      return await waitResult(d.id, 30_000);
    } catch (e) {
      return { ok: false, error: String((e as Error)?.message ?? e) };
    }
  }, [waitResult]);

  const actRef = useRef(act);
  actRef.current = act;

  // the paper page inside the frame (same origin) asks these: may this guest confirm / undo, and do it
  useEffect(() => {
    const w = window as unknown as { bloomCo?: () => boolean; bloomAct?: (p: string, b: unknown) => Promise<unknown> };
    w.bloomCo = () => coRef.current;
    w.bloomAct = (p, b) => act(p, b);
  }, [act]);

  const switchPaper = useCallback(async (id: string) => {
    const d = await act('/api/paper/switch', { id });
    if (d.ok === false) toast(L(`切换失败：${relayErr(d.error)}`, `Switch failed: ${relayErr(d.error)}`));
  }, [act, toast]);

  const addPaper = useCallback(() => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.pdf,.docx,.pptx,.md,.markdown,.txt,.tex';
    input.onchange = async () => {
      const f = input.files?.[0];
      if (!f) return;
      if (f.size > 50 * 1024 * 1024) { toast(L('上传失败：文件超过 50 MB', 'Upload failed: file over 50 MB')); return; }
      toast(L(`正在上传《${f.name}》…`, `Uploading “${f.name}”…`));
      try {
        const tok = await voice.getToken();
        const r = await fetch(`/view/${enc(ROOM)}/upload?name=${enc(f.name)}`, { method: 'POST', headers: { Authorization: `Bearer ${tok}` }, body: f });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.id) { toast(L(`上传失败：${d.error ? relayErr(d.error) : r.status}`, `Upload failed: ${d.error ? relayErr(d.error) : r.status}`)); return; }
        toast(L('已上传，正在导入…', 'Uploaded, importing…'));
        const res = await waitResult(d.id, 10 * 60_000);
        toast(res.ok === false ? L(`导入失败：${relayErr(res.error)}`, `Import failed: ${relayErr(res.error)}`) : L('论文已添加', 'Paper added'));
      } catch (e) {
        toast(L(`上传失败：${String((e as Error)?.message ?? e)}`, `Upload failed: ${String((e as Error)?.message ?? e)}`));
      }
    };
    input.click();
  }, [toast, waitResult]);

  // ---------------------------------------------------------------- Claude's card and the guest's own card
  useEffect(() => {
    setGuestTransport({
      models: async (): Promise<Models | null> => {
        const a = latest.state?.assistant;
        if (!a) return null;
        const one = (v?: string) => (v ? { current: v, options: [{ id: v, label: v }] } : undefined);
        return { dialog: one(bareModel(a.dialog)), edit: one(bareModel(a.edit)), effort: one(a.effort), tts: a.tts, asr: a.asr };
      },
      devices: async () => ({ mic: metaStore.get().micOn ? L('浏览器麦克风', 'Browser microphone') : null, speaker: null }),
      setMicMuted: async (muted: boolean) => {
        if (voice.snapshot().micOn === muted) await voice.toggleMic();
        return !voice.snapshot().micOn;
      },
      watchMic: (on) => {
        const send = () => {
          const s = metaStore.get();
          on({ muted: s.connected ? !s.micOn : false });
        };
        send();
        return metaStore.subscribe(send);
      },
      // 标记 and (a co-host's) typed questions and selected passages: queued on the relay, done by the host's Bloom
      act: (path: string, body: unknown) => actRef.current(path, body),
    });
    return () => setGuestTransport(null);
  }, []);

  const status: Ready | null = useMemo(() => {
    const a = st?.assistant;
    if (!a) return null;
    return {
      paper: true, fast: true, tts: true, blurt: true, recording: true, fast_online: true, hands_ready: true,
      working: st?.board?.counts?.working ?? 0, queued: st?.board?.counts?.queued ?? 0,
      models: { fast: bareModel(a.dialog), hands: [bareModel(a.edit), a.effort].filter(Boolean).join(' · '), tts: a.tts, asr: a.asr },
    };
  }, [st?.assistant, st?.board?.counts?.working, st?.board?.counts?.queued]);

  const talking = vs.speaking.join('|');
  const people: Participant[] = useMemo(() => {
    const now = new Set(talking ? talking.split('|') : []);
    const list = (st?.participants ?? []).map((p) => ({ ...p, you: !!me && p.name === me && p.rank !== 'host' && p.id !== 'claude', speaking: now.has(p.name) }));
    if (me && !list.some((p) => p.you)) list.push({ id: 'guest-me', name: me, role: 'guest', rank: 'participant', you: true, speaking: now.has(me), avatar: st?.avatar_default ?? null, color: null });
    return list.map((p) => (p.avatar || p.rank === 'host' || p.id === 'claude' ? p : { ...p, avatar: st?.avatar_default ?? null }));
  }, [st?.participants, st?.avatar_default, me, talking]);

  // "Claude 在干什么": the host's clock vs this page's (the strip counts seconds itself)
  const brainRef = useRef<{ raw: string; b: Brain | null }>({ raw: '', b: null });
  const brainNow = useMemo(() => {
    const raw = JSON.stringify(st?.brain ?? null);
    if (raw !== brainRef.current.raw) {
      const b = st?.brain ?? null;
      const look = (d: Lookup) => ({ ...d, model: bareModel(d.model) });
      brainRef.current = {
        raw,
        b: b
          ? {
              ...b,
              front_model: bareModel(b.front_model), back_model: bareModel(b.back_model),
              deep_model: bareModel(b.deep_model), edit_model: bareModel(b.edit_model),
              deep: b.deep ? look(b.deep) : b.deep, deeps: b.deeps?.map(look),
              skew: b.now != null ? b.now - Date.now() / 1000 : 0,
            }
          : null,
      };
    }
    return brainRef.current.b;
  }, [st?.brain]);

  // the transcript as guests see it: the model by its bare name (快答 / 思考 and which model answered: 2026-11-20 the
  // user, "怎么把思考的模型也删了"), no timings (guest.css hides the timing chips — `lat` stays in the data, it tells an
  // old #deep line from a new one)
  const lines = useMemo(
    () => (st?.transcript ?? NO_LINES).map((l) => (l.model || l.took != null ? { ...l, model: bareModel(l.model), took: undefined } : l)),
    [st?.transcript],
  );

  const meeting: Meeting | null = useMemo(() => {
    if (!st) return null;
    return {
      id: st.meeting?.id ?? ROOM, title: st.meeting?.title ?? st.title ?? '', started: st.meeting?.started ?? st.started ?? '',
      ended: ended ? relay.state?.ended ?? new Date().toISOString() : null, state: ended ? 'ended' : 'live',
      duration_s: ended && st.started ? Math.max(0, (Date.now() - Date.parse(st.started)) / 1000) : st.meeting?.duration_s ?? 0,
      lang: st.meeting?.lang ?? undefined, papers: st.papers, active_paper: st.active_paper ?? null,
    } as Meeting;
  }, [st, ended, relay.state?.ended]);

  const join = useCallback((name: string, mic: boolean) => {
    store('bloom.name', name);
    store('bloom.mic', mic ? '1' : '0');
    setMe(name);
    voice.join(name, mic);
  }, []);
  const leave = useCallback(() => {
    voice.leave('user');
    setMe(null);
  }, []);

  useEffect(() => {
    document.title = st?.title ? `${st.title} · Bloom` : 'Bloom';
  }, [st?.title]);

  useEffect(() => {
    if (!joined) return;
    const k = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'f' || e.key === 'F')) {
        e.preventDefault();
        window.dispatchEvent(new Event('bloom-find'));
      }
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [joined]);

  // 断开超过 4 秒才说（偶尔一次请求慢不算）
  const [, tick] = useState(0);
  useEffect(() => {
    if (!relay.lost) return;
    const h = window.setTimeout(() => tick((n) => n + 1), 4200);
    return () => clearTimeout(h);
  }, [relay.lost]);
  const offline = !!relay.lost && Date.now() - relay.lost > 4000;

  // 第 3 批: the box (标记; a co-host also types to the AI), a co-host's selected passage
  const tools = useMemo<LiveTools>(
    () => ({
      role: amCo ? 'cohost' : 'guest',
      me,
      live: !ended,
      noAi: chat,
      anchor: chat ? null : st?.anchor ?? null,
      onError: toast,
    }),
    [amCo, me, ended, chat, st?.anchor, toast],
  );

  // 第 6 批: comments on the paper — anyone in the meeting writes, replies and resolves; a co-host also deletes (the
  // host's Bloom checks again); after the meeting they stay to read
  const guestComments = useMemo(
    () => ({ items: st?.comments ?? [], canAdd: !ended, canDelete: !ended && amCo }),
    [st?.comments, ended, amCo],
  );
  const v2 = (st?.v ?? 0) >= 2;
  // phones: the paper and the transcript take turns on the whole screen (a tab bar at the bottom, guest.css); a dot on
  // 逐字稿 says something was said while you were on the paper
  // the tab you were on comes back after a reload or a reconnect (bloom.mtab, this browser only)
  const [mtab, setMtabState] = useState<'paper' | 'talk'>(() => (store('bloom.mtab') === 'talk' ? 'talk' : 'paper'));
  const setMtab = (v: 'paper' | 'talk') => {
    setMtabState(v);
    store('bloom.mtab', v);
  };
  const nLines = st?.transcript?.length ?? 0;
  const [seenLines, setSeenLines] = useState(0);
  useEffect(() => {
    if (mtab === 'talk') setSeenLines(nLines);
  }, [mtab, nLines]);
  const unseen = mtab === 'paper' && nLines > seenLines;
  // the connection, the voice, the end: over the paper's foot on a wide screen; on a phone over the bottom bar, seen
  // from either tab (inside the paper's column it went with the column when 逐字稿 was showing)
  const notice = ended ? (
    <div className="g-conn">
      <span>{L('会议已结束', 'The meeting has ended.')}</span>
    </div>
  ) : (
    <GuestNotes s={vs} offline={offline} voice={voice} following={fs.following} hasHost={fs.host && !chat && !phone} />
  );
  return (
    <div className={`app g-app ${joined ? 'is-in' : ''} ${ended ? 'is-ended' : ''}`} data-mtab={chat ? 'talk' : mtab}>
      {joined && (phase === 'live' || ended) && v2 ? (
        <>
        <MeetingLayout
          meeting={meeting}
          chat={chat}
          papers={chat ? null : st?.papers ?? null}
          activePaper={st?.active_paper ?? null}
          onSwitchPaper={!chat && !ended && (st?.papers?.length ?? 0) > 1 ? (id: string) => void switchPaper(id) : null}
          onAddPaper={ended || chat ? null : addPaper}
          transcript={lines}
          partial={st?.partial ?? null}
          people={people}
          board={st?.board ?? null}
          brain={ended ? null : brainNow}
          summary={null}
          diffSrc={chat ? '' : `/view/${enc(ROOM)}/paper?host=bloom&co=${amCo ? 1 : 0}`}
          paperVersion={ended ? frozenPv.current : relay.pv}
          onFrame={(el) => follow.setFrame(el)}
          live={!ended}
          tools={tools}
          bookmarks={st?.bookmarks ?? null}
          comments={chat ? null : guestComments}
          status={ended ? null : status}
          // after the meeting the same row stays (the recording mark's room just goes blank): the people don't slide over
          actions={<GuestActions co={amCo} onLeave={leave} />}
          notice={phone ? null : notice}
        />
        {phone && (
          <div className="mt-notice g-mnotice" aria-live="polite" data-testid="guest-notice">
            {notice}
          </div>
        )}
        {!chat && (
        <nav className="g-mtabs" role="tablist" aria-label={L('切换', 'Switch')}>
          <button role="tab" aria-selected={mtab === 'paper'} className={mtab === 'paper' ? 'is-sel' : ''} onClick={() => setMtab('paper')} data-testid="guest-tab-paper">
            {L('论文', 'Paper')}
          </button>
          <button role="tab" aria-selected={mtab === 'talk'} className={mtab === 'talk' ? 'is-sel' : ''} onClick={() => setMtab('talk')} data-testid="guest-tab-talk">
            <span className="g-mtab-t">
              {L('逐字稿', 'Transcript')}
              {/* floats at the word's corner: showing or not, the tab doesn't change */}
              {unseen && <i className="g-mtab-dot" aria-label={L('有新的发言', 'New lines')} />}
            </span>
          </button>
        </nav>
        )}
        </>
      ) : joined && phase === 'live' ? (
        <div className="g-center">
          <div className="g-card">
            <BloomMark size={30} />
            <p className="g-msg">{L('主持人的 Bloom 版本过旧，请主持人更新后重新加入', 'The host’s Bloom is out of date. Ask the host to update it, then join again.')}</p>
            <button className="g-btn" onClick={leave}>{L('离开', 'Leave')}</button>
          </div>
        </div>
      ) : (
        <JoinScreen phase={phase} st={st} onJoin={join} />
      )}
      <div className="g-toasts" aria-live="polite">
        {toasts.map((x) => (
          <div key={x.id} className="g-toast">{x.text}</div>
        ))}
      </div>
    </div>
  );
}

const NO_LINES: Line[] = [];

// ---------------------------------------------------------------- the bar's buttons: co-host, leave
// (what comes and goes — back to the host, no voice, reconnecting — floats over the paper in GuestNotes, so the bar's
// title, people and buttons never move)
const GuestActions = memo(function GuestActions({ co, onLeave }: { co: boolean; onLeave: () => void }) {
  return (
    <div className="g-acts">
      {/* recording: the bar's own state line already says 录制中 (and the join card says what is recorded), so no
          second mark here */}
      {co && (
        <span className="g-co" title={L('共同主持人：可确认、撤回修订和修改段落', 'Co-host: can confirm or undo revisions and edit paragraphs')}>
          {L('共同主持人', 'Co-host')}
        </span>
      )}
      <button className="mt-btn g-leave" onClick={onLeave} data-testid="guest-leave">
        {L('离开', 'Leave')}
      </button>
    </div>
  );
});

/** floats at the foot of the paper (MeetingLayout's notice): the connection, the voice, back to the host's place */
function GuestNotes({ s, offline, voice, following, hasHost }: {
  s: VoiceSnapshot;
  offline: boolean;
  voice: GuestVoice;
  following: boolean;
  hasHost: boolean;
}) {
  const conn = offline ? (
    <div className="g-conn is-busy"><span>{L('连接已断开，正在重连…', 'Disconnected, reconnecting…')}</span></div>
  ) : s.conn || s.audioBlocked ? <ConnBar s={s} voice={voice} /> : null;
  const noVoice = !offline && !s.connected && s.joined && !s.conn ? (
    <div className="g-conn is-busy"><span>{L('语音未连接', 'Voice not connected')}</span></div>
  ) : null;
  const back = !following && hasHost ? (
    <button className="mt-btn g-back" onClick={() => follow.setFollow(true)} data-testid="guest-back">
      {L('跟随主持人', 'Follow the host')}
    </button>
  ) : null;
  if (!conn && !noVoice && !back) return null;
  return (
    <div className="g-notes">
      {conn}
      {noVoice}
      {back}
    </div>
  );
}

function ConnBar({ s, voice }: { s: VoiceSnapshot; voice: GuestVoice }) {
  if (s.audioBlocked)
    return (
      <div className="g-conn">
        <span>{L('浏览器已阻止播放', 'Playback blocked by the browser')}</span>
        <button className="g-link" onClick={() => voice.startAudio()}>{L('开启声音', 'Turn on sound')}</button>
      </div>
    );
  if (s.conn)
    return (
      <div className={`g-conn is-${s.connKind}`}>
        <span>{s.conn}</span>
        {s.connKind === 'retry' && <button className="g-link" onClick={() => voice.retry()}>{L('重新连接', 'Reconnect')}</button>}
      </div>
    );
  return null;
}

// ---------------------------------------------------------------- before joining
function JoinScreen({ phase, st, onJoin }: { phase: Phase; st: RelayState | null; onJoin: (name: string, mic: boolean) => void }) {
  const [name, setName] = useState(() => (store('bloom.name') || '').slice(0, 32));
  const [mic, setMic] = useState(() => store('bloom.mic') === '1');
  const [err, setErr] = useState('');
  const topic = phase === 'invalid' ? (ROOM ? L('链接无效', 'Invalid link') : L('Bloom 会议', 'Bloom meeting')) : st?.title || L('组会', 'Meeting');
  const host = (st?.host || '').trim();
  const sub = phase === 'live'
    ? [host && L(`${host} 发起`, `started by ${host}`), st?.started && L(`${hm(st.started)} 开始`, `since ${hm(st.started)}`)].filter(Boolean).join(' · ')
    : phase === 'wait' && host ? L(`${host} 的会议`, `${host}’s meeting`) : '';
  const go = () => {
    const n = name.trim();
    if (!n) { setErr(L('请输入名字', 'Enter your name')); return; }
    onJoin(n, mic);
  };
  const inApp = /MicroMessenger|wxwork|\bQQ\/\d|DingTalk|Lark|Feishu|Weibo/i.test(navigator.userAgent || '');
  const pill = phase === 'live' ? L('会议中', 'Live') : phase === 'wait' ? L('未开始', 'Not started') : phase === 'ended' ? L('已结束', 'Ended') : phase === 'loading' ? L('连接中', 'Connecting') : '';
  return (
    <div className="g-center">
      <div className="g-card" data-testid="guest-join">
        <div className="g-brand">
          <BloomMark size={28} />
          <span>Bloom</span>
          {pill && <span className={`g-pill is-${phase}`}>{pill}</span>}
        </div>
        <h1 className="g-topic">{topic}</h1>
        {sub && <p className="g-sub">{sub}</p>}
        {phase === 'live' && <WhoIsIn st={st} />}
        {/* the recording notice: its line is there whenever the meeting is on, so the card never changes height */}
        {phase === 'live' && (
          <p className={`g-rec-note ${st?.rec ? '' : 'is-off'}`} aria-hidden={st?.rec ? undefined : true} data-testid="guest-rec-note">
            <i className="g-rec-dot" aria-hidden="true" />
            <span>{recLine(st?.rec ?? 'screen')}</span>
          </p>
        )}
        {inApp && phase === 'live' && (
          <p className="g-warn">{L('建议用浏览器打开：点右上角「···」', 'Open in a browser: tap ··· at the top right')}</p>
        )}
        {phase === 'live' ? (
          <div className="g-form">
            <input
              className="g-name"
              value={name}
              maxLength={32}
              autoComplete="name"
              placeholder={L('名字', 'Name')}
              onChange={(e) => { setName(e.target.value); setErr(''); }}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) go(); }}
              data-testid="guest-name"
            />
            <label className="g-switch">
              <input type="checkbox" checked={mic} onChange={(e) => setMic(e.target.checked)} />
              <span>{L('入会时开启麦克风', 'Microphone on when joining')}</span>
            </label>
            {/* its row is always there (guest.css): the message coming doesn't push 进入会议 down */}
            <p className="g-err" role="alert">{err}</p>
            <button className="g-btn is-primary" onClick={go} data-testid="guest-go">{L('进入会议', 'Join')}</button>
          </div>
        ) : (
          <p className="g-msg">
            {phase === 'loading' ? L('正在连接…', 'Connecting…')
              : phase === 'invalid' ? L('请通过邀请链接加入', 'Join with an invite link')
              : phase === 'wait' ? L('会议开始后可加入', 'You can join once the meeting starts')
              : L('会议已结束', 'The meeting has ended')}
          </p>
        )}
      </div>
    </div>
  );
}

function WhoIsIn({ st }: { st: RelayState | null }) {
  const all: { id?: string; name: string; role?: string; rank?: string; color?: string | null }[] = st?.participants?.length
    ? st.participants
    : (st?.people ?? []).map((p, i) => ({ id: `p${i}`, name: p.name, role: p.role, rank: p.rank }));
  // a meeting without a paper: the AI is not in it
  const list = st?.mode === 'chat' || st?.paper === false ? all.filter((p) => p.id !== 'claude' && p.role !== 'student') : all;
  if (!list.length) return null;
  const label = (p: { rank?: string; role?: string; id?: string }) =>
    p.rank === 'host' ? L('主持人', 'host') : p.id === 'claude' || p.role === 'student' ? 'AI' : p.rank === 'cohost' || p.rank === 'teacher' ? L('共同主持人', 'co-host') : '';
  return (
    <div className="g-who" data-testid="guest-who">
      <span className="g-who-h">{L(`${list.length} 人在会中`, `${list.length} in the meeting`)}</span>
      <span className="g-who-list">
        {list.slice(0, 8).map((p) => (
          <span key={p.id ?? p.name} className="g-who-p" style={{ color: nameColor(p.color) }}>
            <span className="g-who-n">{p.name}</span>
            {label(p) && <i>{label(p)}</i>}
          </span>
        ))}
        {list.length > 8 && <span className="g-who-p">+{list.length - 8}</span>}
      </span>
    </div>
  );
}

function hm(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
