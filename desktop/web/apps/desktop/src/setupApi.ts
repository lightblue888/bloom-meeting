// The first-run guide and Settings → Speech talk to the backend here: whether the guide was done (/api/setup), the
// AI's voice and the words transcription should know (/api/voice), a spoken sample (/api/voice/test).
import { ZUHUI_BASE } from './meetingApi';

export type VoiceKind = 'auto' | 'clone' | 'edge' | 'sapi' | 'off';

export interface Voice {
  voice: VoiceKind;
  edge_voice: string;
  /** '' = picked by the sentence's language */
  sapi_voice: string;
  hotwords: string[];
  hot_max: number;
  hot_len: number;
  /** 读法: how the AI says a word (written → said), before any voice speaks; older backends have none */
  pron?: Pron[];
  pron_max?: number;
  pron_len?: number;
  edge_voices: { id: string; lang: 'zh' | 'en'; gender: 'm' | 'f' }[];
  /** null while Windows is still being asked */
  sapi_voices: { name: string; lang: string; gender: string }[] | null;
  has_clone: boolean;
  has_edge: boolean;
}

/** one line of 读法: the word as written, and how it is to be said */
export interface Pron {
  w: string;
  r: string;
}

export interface Component {
  ok: boolean;
  path?: string | null;
  detail?: string;
}

export interface SetupState {
  done: boolean;
  skipped: boolean;
  /** what the backend can find (speech model, pandoc, ffmpeg…); null on a backend without the check */
  components?: Record<string, Component> | null;
  /** what will actually speak in meetings: clone / edge / sapi; null = silent */
  voice_engine?: 'clone' | 'edge' | 'sapi' | null;
}

export const SETUP_EVENT = 'bloom-setup';

async function getJson<T>(path: string, ms = 8000): Promise<T> {
  const r = await fetch(ZUHUI_BASE + path, { cache: 'no-store', signal: AbortSignal.timeout(ms) });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()) as T;
}
async function postJson<T>(path: string, body: unknown, ms = 10000): Promise<T> {
  const r = await fetch(ZUHUI_BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(ms),
  });
  const j = (await r.json().catch(() => ({}))) as T & { error?: string };
  if (!r.ok) throw new Error(j.error || 'failed');
  return j;
}

export const setupApi = {
  state: () => getJson<SetupState>('/api/setup'),
  /** the guide finished ({done}), was skipped ({skipped}), or is to run again ({reset}) */
  mark: async (p: { done?: boolean; skipped?: boolean; reset?: boolean }) => {
    const s = await postJson<SetupState>('/api/setup', p);
    window.dispatchEvent(new Event(SETUP_EVENT));
    return s;
  },
  voice: () => getJson<Voice>('/api/voice'),
  /** errors come back as a code the page words (voice, edge_voice, sapi_voice, hot_too_long, hot_too_many, pron_bad, pron_too_long, pron_too_many) */
  setVoice: async (p: Partial<Pick<Voice, 'voice' | 'edge_voice' | 'sapi_voice'>> & { hotwords?: string[]; pron?: Pron[] }) => {
    const v = await postJson<Voice>('/api/voice', p);
    window.dispatchEvent(new Event(SETUP_EVENT));
    return v;
  },
  /** says a sentence in the chosen voice on this PC's speakers; {ok, engine} or {ok: false, error: off|busy|live|failed|not_ready} */
  testVoice: (text = '') => postJson<{ ok: boolean; engine?: string | null; error?: string }>('/api/voice/test', { text }, 90000),
};

/** Settings → General → Invites: the relay pasted as a connection code (relay/setup.sh prints it) */
export type RelayMode = 'public' | 'own' | 'off';
export interface Relay {
  mode: RelayMode;
  configured: boolean;
  /** the public relay: guests may join (copying the invite link turns it on); own: always */
  invite_on: boolean;
  /** the relay's host name, e.g. 1-2-3-4.sslip.io */
  host: string;
  key_tail: string;
  /** set here (it can be removed here); false = set by hand in config.json */
  managed: boolean;
}

/** what a new relay server runs (the repository's relay/setup.sh) */
export const RELAY_SETUP_CMD = 'curl -fsSL https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/relay/setup.sh | sudo bash';

export const relayApi = {
  get: () => getJson<Relay>('/api/relay'),
  /** errors: code (not a connection code), failed */
  save: (code: string) => postJson<Relay>('/api/relay', { code }),
  clear: () => postJson<Relay>('/api/relay', { clear: true }),
  setMode: (mode: 'public' | 'off') => postJson<Relay>('/api/relay', { mode }),
  setInvite: (on: boolean) => postJson<Relay>('/api/relay', { invite_on: on }),
  /** {ok, ms} or {ok: false, error: unset|unreachable|auth|livekit} */
  test: () => postJson<{ ok: boolean; ms?: number; error?: string }>('/api/relay/test', {}, 30000),
};
