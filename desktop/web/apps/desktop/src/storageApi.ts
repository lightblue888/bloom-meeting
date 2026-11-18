// Settings → Storage talks to the meeting backend (zuhui/storage.py, GET /api/storage and POST /api/storage/…): how much
// Bloom keeps and what it is, and the clean-ups (recordings by age, automatic deletion, unused models, temporary files).
// Showing a folder goes through the app (Tauri's reveal / open_path); outside Tauri those do nothing.
import { invoke } from './api';
import { ZUHUI_BASE } from './meetingApi';

export interface Tally {
  count: number;
  bytes: number;
}

export type ModelRole = 'live' | 'after' | 'addr' | 'voice' | 'turn' | 'asr';

export interface UsedModel {
  role: ModelRole;
  name: string;
  paths: string[];
  bytes: number;
  /** addr: the meeting languages it serves ("zh", "en") */
  langs: string[];
}

export interface StorageInfo {
  total: number;
  free: number | null;
  split: { recordings: number; models: number; records: number; other: number };
  recordings: {
    /** meeting recordings (video and audio) */
    bytes: number;
    /** meetings that still have one */
    meetings: number;
    folder: string | null;
    /** what "move to the Recycle Bin" would take now, by age ("30", "90", "180", "all") */
    older: Record<string, Tally>;
    /** recordings no meeting points at */
    others: Tally & { ready: Tally };
    auto_days: number;
    /** what turning automatic deletion on would take right away, by age */
    auto_preview: Record<string, Tally>;
  };
  models: { bytes: number; folder: string; used: UsedModel[]; unused: { path: string; name: string; bytes: number }[] };
  records: { bytes: number; meetings: Tally & { path: string }; papers: Tally & { path: string }; retention?: Retention };
  caches: { bytes: number; clearable: number; path: string };
  logs: { bytes: number; path: string };
}

/** 第 8 批: deleting old meetings' transcripts and notes automatically (0: never); `preview` = what turning it on
 *  for that many days would take right away ("30", "90", "180", "365"), the meetings listed for the confirmation */
export interface Retention {
  days: number;
  preview: Record<string, Tally & { meetings: { id: string; title: string; started: string | null }[] }>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${ZUHUI_BASE}/api/storage/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    signal: AbortSignal.timeout(120000),
  });
  const j = (await r.json().catch(() => ({}))) as T & { error?: string };
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

export const storageApi = {
  get: async (): Promise<StorageInfo> => {
    const r = await fetch(`${ZUHUI_BASE}/api/storage`, { cache: 'no-store', signal: AbortSignal.timeout(30000) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()) as StorageInfo;
  },
  /** recordings older than `days` (null: all) to the Recycle Bin; transcripts, screenshots and summaries stay */
  trashRecordings: (days: number | null, which: 'meetings' | 'others') => post<Tally>('recordings', { days, which }),
  /** automatic deletion after `days` (0: never); what is already older goes at once */
  setAuto: (days: number) => post<Tally & { auto_days: number }>('auto', { days }),
  /** old meetings' transcripts and notes after `days` (0: never); what is already older goes at once */
  setRetention: (days: number) => post<Tally & { days: number }>('retention', { days }),
  trashModels: (paths: string[]) => post<Tally>('models', { paths }),
  clearCaches: () => post<{ bytes: number }>('caches', {}),
  /** a folder opens in File Explorer; a file or model folder is selected in its parent */
  open: (path: string) => invoke<void>('open_path', { path }),
  reveal: (path: string) => invoke<void>('reveal', { path }),
};
