// The guest page: example.com/room/<id>. Before joining: the meeting's name, who started it, a name field, the microphone
// switch. After joining: the host's own meeting page (MeetingLayout) — the paper with its revision marks, the
// transcript, the edit cards, what Claude is doing — fed by the relay instead of a backend on this machine, minus the
// host's Dev and the 设为共同主持人 button. Anyone can add a paper (新增) or put another one on screen for everybody;
// confirming / undoing an edit and editing a paragraph in place are the host's and the co-hosts' (the host's Bloom
// decides, the relay only queues). The voice is LiveKit (voice.ts); the paper follows the host's scrolling (follow.ts).
import { memo, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { applyPlatform, applyTheme } from '@blurt/ui';
import { setLang } from '../i18n';
import { setGuestTransport, type Board, type Brain, type Line, type Meeting, type MeetingPaper, type Models, type Participant, type PartialLine, type Ready } from '../meetingApi';
import { BloomMark, MeetingLayout } from '../components/MeetingLayout';
import { GuestVoice, type VoiceSnapshot } from './voice';
import { Follow, type HostFocus } from './follow';

const ZH = /^zh/i.test(navigator.language || '');
setLang(ZH ? 'zh-Hans' : 'en');
const L = (zh: string, en: string) => (ZH ? zh : en);

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
}

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
    let heavy: { transcript?: Line[]; board?: Board } = {};
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
              heavy = { transcript: h.transcript ?? [], board: h.board ?? undefined };
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

  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const toast = useCallback((text: string) => {
    const id = Date.now() + Math.random();
    setToasts((p) => [...p.slice(-2), { id, text }]);
    window.setTimeout(() => setToasts((p) => p.filter((x) => x.id !== id)), 2800);
  }, []);
  sink.toast = toast;

  const vs = useSyncExternalStore(metaStore.subscribe, metaStore.get);
  const fs = useSyncExternalStore(followStore.subscribe, followStore.get);


  // the host's place from the relay too (the data channel is faster, this one is the fallback)
  useEffect(() => {
    if (joined) follow.onFocus(st?.focus ?? null, 'relay');
  }, [joined, st?.focus]);

  // the meeting ended while in it: hang up; the page stays as it was at the end (read only) until they leave
  useEffect(() => {
    if (joined && phase === 'ended') {
      voice.leave('ended');
      if (!frozen.current) setMe(null);
      toast(L('会议已结束', 'The meeting has ended'));
    }
  }, [joined, phase, toast]);

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
    return { ok: false, error: L('主持人那边没回应', 'The host did not answer') };
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

  // the paper page inside the frame (same origin) asks these: may this guest confirm / undo, and do it
  useEffect(() => {
    const w = window as unknown as { bloomCo?: () => boolean; bloomAct?: (p: string, b: unknown) => Promise<unknown> };
    w.bloomCo = () => coRef.current;
    w.bloomAct = (p, b) => act(p, b);
  }, [act]);

  const switchPaper = useCallback(async (id: string) => {
    const d = await act('/api/paper/switch', { id });
    if (d.ok === false) toast(L(`没切过去：${d.error ?? ''}`, `Could not switch: ${d.error ?? ''}`));
  }, [act, toast]);

  const addPaper = useCallback(() => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.pdf,.docx,.md,.markdown,.txt,.tex';
    input.onchange = async () => {
      const f = input.files?.[0];
      if (!f) return;
      if (f.size > 50 * 1024 * 1024) { toast(L('文件太大（最多 50 MB）', 'The file is too large (50 MB at most)')); return; }
      toast(L(`正在传《${f.name}》…`, `Uploading “${f.name}”…`));
      try {
        const tok = await voice.getToken();
        const r = await fetch(`/view/${enc(ROOM)}/upload?name=${enc(f.name)}`, { method: 'POST', headers: { Authorization: `Bearer ${tok}` }, body: f });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.id) { toast(L(`没传上：${d.error || r.status}`, `Upload failed: ${d.error || r.status}`)); return; }
        toast(L('传上了，主持人那边正在导入…', 'Uploaded: the host’s Bloom is importing it…'));
        const res = await waitResult(d.id, 10 * 60_000);
        toast(res.ok === false ? L(`没导入：${res.error ?? ''}`, `Import failed: ${res.error ?? ''}`) : L('论文加进来了', 'The paper is in'));
      } catch (e) {
        toast(L(`没传上：${String((e as Error)?.message ?? e)}`, `Upload failed: ${String((e as Error)?.message ?? e)}`));
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
        return { dialog: one(a.dialog), edit: one(a.edit), effort: one(a.effort), tts: a.tts, asr: a.asr };
      },
      devices: async () => ({ mic: metaStore.get().micOn ? L('浏览器的麦克风', 'the browser’s microphone') : null, speaker: null }),
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
    });
    return () => setGuestTransport(null);
  }, []);

  const status: Ready | null = useMemo(() => {
    const a = st?.assistant;
    if (!a) return null;
    return {
      paper: true, fast: true, tts: true, blurt: true, recording: true, fast_online: true, hands_ready: true,
      working: st?.board?.counts?.working ?? 0, queued: st?.board?.counts?.queued ?? 0,
      models: { fast: a.dialog, hands: [a.edit, a.effort].filter(Boolean).join(' · '), tts: a.tts, asr: a.asr },
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
      brainRef.current = { raw, b: b ? { ...b, skew: b.now != null ? b.now - Date.now() / 1000 : 0 } : null };
    }
    return brainRef.current.b;
  }, [st?.brain]);

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

  const v2 = (st?.v ?? 0) >= 2;
  return (
    <div className={`app g-app ${joined ? 'is-in' : ''}`}>
      {joined && (phase === 'live' || ended) && v2 ? (
        <MeetingLayout
          meeting={meeting}
          papers={st?.papers ?? null}
          activePaper={st?.active_paper ?? null}
          onSwitchPaper={!ended && (st?.papers?.length ?? 0) > 1 ? (id: string) => void switchPaper(id) : null}
          onAddPaper={ended ? null : addPaper}
          transcript={st?.transcript ?? NO_LINES}
          partial={st?.partial ?? null}
          people={people}
          board={st?.board ?? null}
          brain={ended ? null : brainNow}
          summary={null}
          diffSrc={`/view/${enc(ROOM)}/paper?host=bloom&co=${amCo ? 1 : 0}`}
          paperVersion={ended ? frozenPv.current : relay.pv}
          onFrame={(el) => follow.setFrame(el)}
          live={!ended}
          status={ended ? null : status}
          actions={ended ? (
            <button className="mt-btn g-leave" onClick={leave} data-testid="guest-leave">{L('离开', 'Leave')}</button>
          ) : <GuestActions s={vs} following={fs.following} hasHost={fs.host} co={amCo} onLeave={leave} />}
          notice={ended ? (
            <div className="g-conn">
              <span>{L('会议已结束。这是散会那一刻的论文、修订和逐字稿，关掉页面就没了；要留一份请找主持人。',
                'The meeting has ended. This is the paper, its revisions and the transcript as of the end; they are gone once you close the page — ask the host for a copy.')}</span>
            </div>
          ) : offline ? (
            <div className="g-conn is-busy">
              <span>{L('和会议的连接断了，正在重连…（论文和逐字稿停在断开的那一刻）', 'Lost the connection to the meeting, reconnecting… (the paper and transcript are as of then)')}</span>
            </div>
          ) : vs.conn || vs.audioBlocked ? <ConnBar s={vs} voice={voice} /> : undefined}
        />
      ) : joined && phase === 'live' ? (
        <div className="g-center">
          <div className="g-card">
            <BloomMark size={30} />
            <p className="g-msg">{L('主持人的 Bloom 版本太旧，还推不来完整的会议页面。请主持人更新后再进。', 'The host’s Bloom is too old to share the full meeting page. Ask the host to update it.')}</p>
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

// ---------------------------------------------------------------- the bar's buttons: microphone, back to the host, leave
const GuestActions = memo(function GuestActions({ s, following, hasHost, co, onLeave }: {
  s: VoiceSnapshot;
  following: boolean;
  hasHost: boolean;
  co: boolean;
  onLeave: () => void;
}) {
  return (
    <div className="g-acts">
      {co && (
        <span className="g-co" title={L('共同主持人：可以确认、撤回修订，直接修改段落', 'Co-host: you can confirm or undo revisions and edit paragraphs')}>
          {L('共同主持人', 'Co-host')}
        </span>
      )}
      {!following && hasHost && (
        <button className="mt-btn g-back" onClick={() => follow.setFollow(true)} data-testid="guest-back">
          {L('回到主持人位置', 'Back to the host')}
        </button>
      )}
      {!s.connected && s.joined && !s.conn && (
        <span className="g-novoice" title={L('语音没连上：能看、能做事，听不到也说不了', 'Voice not connected: you can read and act, not talk or listen')}>
          {L('语音未连上', 'No voice')}
        </span>
      )}
      <button className="mt-btn g-leave" onClick={onLeave} data-testid="guest-leave">
        {L('离开', 'Leave')}
      </button>
    </div>
  );
});

function ConnBar({ s, voice }: { s: VoiceSnapshot; voice: GuestVoice }) {
  if (s.audioBlocked)
    return (
      <div className="g-conn">
        <span>{L('浏览器挡住了声音', 'The browser blocked the sound')}</span>
        <button className="g-link" onClick={() => voice.startAudio()}>{L('点一下开启声音', 'Turn the sound on')}</button>
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
    if (!n) { setErr(L('先填一下你的名字', 'Enter your name first')); return; }
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
        {inApp && phase === 'live' && (
          <p className="g-warn">{L('在 App 里打开容易断线，建议点右上角「···」用浏览器打开。', 'Opened inside an app: voice drops more easily. Open it in a browser if you can.')}</p>
        )}
        {phase === 'live' ? (
          <div className="g-form">
            <input
              className="g-name"
              value={name}
              maxLength={32}
              autoComplete="name"
              placeholder={L('你的名字', 'Your name')}
              onChange={(e) => { setName(e.target.value); setErr(''); }}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) go(); }}
              data-testid="guest-name"
            />
            <label className="g-switch">
              <input type="checkbox" checked={mic} onChange={(e) => setMic(e.target.checked)} />
              <span>{mic ? L('进会时打开麦克风：大家能听到你', 'Microphone on: everyone hears you') : L('进会后先静音，随时可以打开', 'Join muted; unmute any time')}</span>
            </label>
            {err && <p className="g-err">{err}</p>}
            <button className="g-btn is-primary" onClick={go} data-testid="guest-go">{L('进入会议', 'Join')}</button>
          </div>
        ) : (
          <p className="g-msg">
            {phase === 'loading' ? L('正在连接…', 'Connecting…')
              : phase === 'invalid' ? L('用邀请链接进会', 'Join with an invite link')
              : phase === 'wait' ? L('会议还没开始，开始后这里会自动变成可以进入', 'Not started yet: you can join here once it starts')
              : L('会议已结束', 'The meeting has ended')}
          </p>
        )}
      </div>
    </div>
  );
}

function WhoIsIn({ st }: { st: RelayState | null }) {
  const list: { id?: string; name: string; role?: string; rank?: string; color?: string | null }[] = st?.participants?.length
    ? st.participants
    : (st?.people ?? []).map((p, i) => ({ id: `p${i}`, name: p.name, role: p.role, rank: p.rank }));
  if (!list.length) return null;
  const label = (p: { rank?: string; role?: string; id?: string }) =>
    p.rank === 'host' ? L('主持人', 'host') : p.id === 'claude' || p.role === 'student' ? 'AI' : p.rank === 'cohost' || p.rank === 'teacher' ? L('共同主持人', 'co-host') : '';
  return (
    <div className="g-who" data-testid="guest-who">
      <span className="g-who-h">{L(`会上 ${list.length} 人`, `${list.length} in the meeting`)}</span>
      <span className="g-who-list">
        {list.slice(0, 8).map((p) => (
          <span key={p.id ?? p.name} className="g-who-p" style={{ color: p.color ?? undefined }}>
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
