// In-browser stand-in for the Rust commands (vite dev, unit tests): Settings' small fixed world — the config, two
// speech models (one coming down on Download), Storage's folders. Writes are kept in memory. ?lang=en|zh-Hans and
// ?platform=win pick the language and the look.

type Listener = (p: unknown) => void;
const listeners = new Map<string, Set<Listener>>();

export function mockListen(event: string, cb: Listener): () => void {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event)!.add(cb);
  return () => listeners.get(event)?.delete(cb);
}
const emit = (event: string, p: unknown) => listeners.get(event)?.forEach((cb) => cb(p));

const param = (k: string) => (typeof location === 'undefined' ? null : new URLSearchParams(location.search).get(k));
const H = 'C:\\Users\\maya';
const S1 = `${H}\\Blurt\\inbox\\sessions\\20261102-101500`;

let mockCfg: Record<string, any> = { lang: param('lang') ?? undefined, record: {} };
let login = false;
const modelsIn = new Set<string>(['sensevoice-small']);
let mdl: { id: string; done: number; total: number; timer: ReturnType<typeof setInterval> } | null = null;

/** config.json's two-level merge (settings.rs `merged`): null removes a key */
function mockMerge(patch: Record<string, any>) {
  const out: Record<string, any> = { ...mockCfg };
  for (const [k, v] of Object.entries(patch)) {
    if (v === null) delete out[k];
    else if (v && typeof v === 'object' && !Array.isArray(v) && out[k] && typeof out[k] === 'object') out[k] = { ...out[k], ...v };
    else out[k] = v;
  }
  mockCfg = out;
}

function mockDownload(id: string) {
  const total = id === 'fun-asr-nano-onnx' ? 950 : 228;
  mdl = { id, done: 0, total, timer: setInterval(() => {
    if (!mdl) return;
    mdl.done = Math.min(mdl.total, mdl.done + mdl.total / 12);
    if (mdl.done >= mdl.total) {
      clearInterval(mdl.timer);
      modelsIn.add(mdl.id);
      emit('model-download', { id: mdl.id, state: 'done', done_mb: 0, total_mb: 0 });
      mdl = null;
    } else emit('model-download', { id: mdl.id, state: 'running', done_mb: mdl.done, total_mb: mdl.total });
  }, 400) };
}

export async function mockInvoke<T>(cmd: string, args: Record<string, any> = {}): Promise<T> {
  const r = (v: unknown) => v as T;
  switch (cmd) {
    case 'app_status':
      return r({ version: '1.0.16', os: 'windows', platform: param('platform') === 'gnome' ? 'gnome' : 'win', lang: param('lang') ?? navigator.language,
        blurt_home: `${H}\\.blurt`, scripts: `${H}\\AppData\\Local\\Blurt\\scripts`, uv: `${H}\\.blurt\\bin\\uv.exe`, capture: { recording: false, session: null } });
    case 'settings_get':
      return r({ config: JSON.parse(JSON.stringify(mockCfg)), config_path: `${H}\\.blurt\\config.json` });
    case 'settings_set':
      mockMerge(args.patch ?? {});
      return r(mockCfg);
    case 'asr_models':
      return r({ models: [{ id: 'fun-asr-nano-onnx', name: 'Fun-ASR Nano', backend: 'sherpa-onnx', installed: modelsIn.has('fun-asr-nano-onnx'), size_mb: 950, purposes: ['live'] },
        { id: 'sensevoice-small', name: 'SenseVoice Small', backend: 'sherpa-onnx', installed: true, size_mb: 228, purposes: [] }] });
    case 'dictation_models':
      return r({ recommended: 'fun-asr-nano-onnx', selected: 'fun-asr-nano-onnx', ready: modelsIn.has('fun-asr-nano-onnx'), downloading: mdl?.id ?? null,
        models: [{ id: 'fun-asr-nano-onnx', name: 'Fun-ASR-Nano (CPU)', size_mb: 950, installed: modelsIn.has('fun-asr-nano-onnx'), recommended: true },
          { id: 'sensevoice-small', name: 'SenseVoice Small', size_mb: 228, installed: modelsIn.has('sensevoice-small'), recommended: false }] });
    case 'model_download':
      if (mdl) throw new Error('a download is already running');
      mockDownload(args.id);
      return r(undefined);
    case 'model_download_cancel':
      if (mdl) {
        clearInterval(mdl.timer);
        emit('model-download', { id: mdl.id, state: 'stopped', done_mb: 0, total_mb: 0 });
        mdl = null;
      }
      return r(true);
    case 'login_item_get':
      return r({ supported: true, enabled: login });
    case 'login_item_set':
      login = args.on;
      return r({ supported: true, enabled: login });
    case 'storage_scan':
      return r({ folders: [{ path: `${H}\\Blurt\\inbox\\sessions`, bytes: 2_310_000_000, sessions: 14 }],
        videos: [{ session: S1, bytes: 980_000_000, age_days: 41 }],
        models: [{ path: `${H}\\.blurt\\models\\fun-asr-nano-int8`, folder: 'fun-asr-nano-int8', bytes: 996_000_000 }, { path: `${H}\\.blurt\\models\\silero_vad.onnx`, folder: 'silero_vad.onnx', bytes: 2_300_000 }],
        files: [{ key: 'jobs', path: `${H}\\.blurt\\jobs`, bytes: 1_900_000, removable: false }, { key: 'logs', path: `${H}\\.blurt\\logs`, bytes: 3_100_000, removable: false }],
        auto_days: (mockCfg.record?.delete_videos_after_days as number) ?? null, available: 214_000_000_000 });
    case 'videos_auto_set':
      mockMerge({ record: { delete_videos_after_days: args.days } });
      return r({ auto_days: args.days });
    case 'videos_trash':
      return r({ count: 1, bytes: 980_000_000 });
    case 'storage_trash':
      return r(args.files ?? []);
    default:
      return r(undefined);
  }
}
