// The audit's stand-in for src/mock.ts (the Tauri commands outside the app): vite.audit.config.ts resolves src/api.ts's
// `./mock` to this file, so the audit decides what the Rust side says (which speech model is on this PC, Settings'
// storage, the language) from the test's world — window.__uiAuditTauri, set by an init script before the app loads.
// Same commands and shapes as src/mock.ts; one difference: a finished download clears its state *before* the "done"
// event (as models.rs does), so the list read right after it doesn't call it running again.

type Listener = (p: unknown) => void;
const listeners = new Map<string, Set<Listener>>();
export function mockListen(event: string, cb: Listener): () => void {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event)!.add(cb);
  return () => listeners.get(event)?.delete(cb);
}
const emit = (event: string, p: unknown) => listeners.get(event)?.forEach((cb) => cb(p));

interface TauriWorld {
  speechInstalled: string[];
  /** a 24-char Windows user name etc.: the paths Settings → Storage shows */
  home: string;
  login: boolean;
  autoDays: number | null;
}
const W: TauriWorld = {
  speechInstalled: ['fun-asr-nano-onnx', 'sensevoice-small'],
  home: 'C:\\Users\\maya',
  login: false,
  autoDays: null,
  ...((globalThis as unknown as { __uiAuditTauri?: Partial<TauriWorld> }).__uiAuditTauri ?? {}),
};
const param = (k: string) => (typeof location === 'undefined' ? null : new URLSearchParams(location.search).get(k));
const H = W.home;
let cfg: Record<string, any> = { lang: param('lang') ?? undefined, record: {} };
const installed = new Set(W.speechInstalled);
let mdl: { id: string; done: number; total: number; timer: ReturnType<typeof setInterval> } | null = null;

function merge(patch: Record<string, any>) {
  const out: Record<string, any> = { ...cfg };
  for (const [k, v] of Object.entries(patch)) {
    if (v === null) delete out[k];
    else if (v && typeof v === 'object' && !Array.isArray(v) && out[k] && typeof out[k] === 'object') out[k] = { ...out[k], ...v };
    else out[k] = v;
  }
  cfg = out;
}

function download(id: string) {
  const total = id === 'fun-asr-nano-onnx' ? 950 : 228;
  mdl = {
    id,
    done: 0,
    total,
    timer: setInterval(() => {
      if (!mdl) return;
      mdl.done = Math.min(mdl.total, mdl.done + mdl.total / 8);
      if (mdl.done >= mdl.total) {
        clearInterval(mdl.timer);
        const done = mdl.id;
        installed.add(done);
        mdl = null;
        emit('model-download', { id: done, state: 'done', done_mb: 0, total_mb: 0 });
      } else emit('model-download', { id: mdl.id, state: 'running', done_mb: mdl.done, total_mb: mdl.total });
    }, 300),
  };
}

export async function mockInvoke<T>(cmd: string, args: Record<string, any> = {}): Promise<T> {
  const r = (v: unknown) => v as T;
  switch (cmd) {
    case 'app_status':
      return r({ version: '1.0.16', os: 'windows', platform: 'win', lang: param('lang') ?? navigator.language, blurt_home: `${H}\\.blurt`, scripts: `${H}\\AppData\\Local\\Blurt\\scripts`, uv: `${H}\\.blurt\\bin\\uv.exe`, capture: { recording: false, session: null } });
    case 'settings_get':
      return r({ config: JSON.parse(JSON.stringify(cfg)), config_path: `${H}\\.blurt\\config.json` });
    case 'settings_set':
      merge(args.patch ?? {});
      return r(cfg);
    case 'asr_models':
      return r({
        models: [
          { id: 'fun-asr-nano-onnx', name: 'Fun-ASR Nano', backend: 'sherpa-onnx', installed: installed.has('fun-asr-nano-onnx'), size_mb: 950, purposes: ['live'] },
          { id: 'sensevoice-small', name: 'SenseVoice Small', backend: 'sherpa-onnx', installed: installed.has('sensevoice-small'), size_mb: 228, purposes: [] },
        ],
      });
    case 'dictation_models':
      return r({
        recommended: 'fun-asr-nano-onnx',
        selected: 'fun-asr-nano-onnx',
        ready: installed.has('fun-asr-nano-onnx'),
        downloading: mdl?.id ?? null,
        models: [
          { id: 'fun-asr-nano-onnx', name: 'Fun-ASR-Nano (CPU)', size_mb: 950, installed: installed.has('fun-asr-nano-onnx'), recommended: true },
          { id: 'sensevoice-small', name: 'SenseVoice Small', size_mb: 228, installed: installed.has('sensevoice-small'), recommended: false },
        ],
      });
    case 'model_download':
      if (mdl) throw new Error('a download is already running');
      download(args.id);
      return r(undefined);
    case 'model_download_cancel':
      if (mdl) {
        clearInterval(mdl.timer);
        const id = mdl.id;
        mdl = null;
        emit('model-download', { id, state: 'stopped', done_mb: 0, total_mb: 0 });
      }
      return r(true);
    case 'login_item_get':
      return r({ supported: true, enabled: W.login });
    case 'login_item_set':
      W.login = args.on;
      return r({ supported: true, enabled: W.login });
    case 'storage_scan':
      return r({
        folders: [{ path: `${H}\\Blurt\\inbox\\sessions`, bytes: 2_310_000_000, sessions: 14 }],
        videos: [{ session: `${H}\\Blurt\\inbox\\sessions\\20261102-101500`, bytes: 980_000_000, age_days: 41 }],
        models: [
          { path: `${H}\\.blurt\\models\\fun-asr-nano-int8`, folder: 'fun-asr-nano-int8', bytes: 996_000_000 },
          { path: `${H}\\.blurt\\models\\silero_vad.onnx`, folder: 'silero_vad.onnx', bytes: 2_300_000 },
        ],
        files: [
          { key: 'jobs', path: `${H}\\.blurt\\jobs`, bytes: 1_900_000, removable: false },
          { key: 'logs', path: `${H}\\.blurt\\logs`, bytes: 3_100_000, removable: false },
        ],
        auto_days: (cfg.record?.delete_videos_after_days as number) ?? W.autoDays,
        available: 214_000_000_000,
      });
    case 'videos_auto_set':
      merge({ record: { delete_videos_after_days: args.days } });
      return r({ auto_days: args.days });
    case 'videos_trash':
      return r({ count: 1, bytes: 980_000_000 });
    case 'storage_trash':
      return r(args.files ?? []);
    default:
      return r(undefined);
  }
}
