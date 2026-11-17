// Typed wrappers for the Rust commands (apps/desktop/src-tauri/src/lib.rs). Outside Tauri (vite dev in a browser,
// unit tests) calls go to ./mock so the pages can be worked on without the app; the meeting pages talk to the meeting
// backend over HTTP themselves (meetingApi.ts).

import { invoke as tauriInvoke } from '@tauri-apps/api/core';
import { listen as tauriListen } from '@tauri-apps/api/event';
import { mockInvoke, mockListen } from './mock';

export const inTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

export function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  return inTauri ? tauriInvoke<T>(cmd, args) : mockInvoke<T>(cmd, args);
}

export function listen<T>(event: string, cb: (payload: T) => void): () => void {
  if (!inTauri) return mockListen(event, cb as (p: unknown) => void);
  let off: (() => void) | null = null;
  let dead = false;
  tauriListen<T>(event, (e) => cb(e.payload)).then((u) => (dead ? u() : (off = u)));
  return () => {
    dead = true;
    off?.();
  };
}

export interface AppStatus {
  version: string;
  os: string;
  platform: 'win' | 'gnome' | 'mac';
  lang: string;
  blurt_home: string;
  scripts: string | null;
  uv: string | null;
  capture: { recording: boolean; session: string | null };
}

export interface Settings {
  config: Record<string, any>;
  config_path: string;
}

export interface DictationModel {
  id: string;
  name: string;
  size_mb: number;
  installed: boolean;
  recommended: boolean;
}
/** The speech models (`transcribe.py dictation-models`) and the download under way */
export interface DictationModels {
  recommended: string;
  selected: string;
  ready: boolean;
  models: DictationModel[];
  downloading?: string | null;
}
export interface ModelDownload {
  id: string;
  state: 'running' | 'done' | 'failed';
  done_mb: number;
  total_mb: number;
}

export interface LoginItem {
  supported: boolean;
  enabled: boolean;
}

/** `transcribe.py models`' rows (Storage names the model folders with them) */
export interface AsrModel {
  id?: string;
  name?: string;
  backend?: string;
  installed?: boolean;
  size_mb?: number;
  languages?: string[] | string;
  [k: string]: unknown;
}

export interface StorageScan {
  folders: { path: string; bytes: number; sessions: number }[];
  videos: { session: string; bytes: number; age_days: number }[];
  models: { path: string; folder: string; bytes: number }[];
  files: { key: string; path: string; bytes: number; removable: boolean }[];
  auto_days: number | null;
  /** free space on Bloom's disk (bytes) */
  available?: number | null;
}

export const api = {
  status: () => invoke<AppStatus>('app_status'),
  settings: () => invoke<Settings>('settings_get'),
  setSettings: (patch: Record<string, unknown>) => invoke<Record<string, any>>('settings_set', { patch }),
  asrModels: () => invoke<{ models: AsrModel[] }>('asr_models'),
  dictationModels: () => invoke<DictationModels>('dictation_models'),
  modelDownload: (id: string) => invoke<void>('model_download', { id }),
  modelDownloadCancel: () => invoke<boolean>('model_download_cancel'),
  loginItem: () => invoke<LoginItem>('login_item_get'),
  setLoginItem: (on: boolean) => invoke<LoginItem>('login_item_set', { on }),
  reveal: (path: string) => invoke<void>('reveal', { path }),
  storageScan: () => invoke<StorageScan>('storage_scan'),
  storageTrash: (paths: string[]) => invoke<string[]>('storage_trash', { files: paths }),
  videosTrash: (days: number | null) => invoke<{ count: number; bytes: number }>('videos_trash', { days }),
  videosAutoSet: (days: number | null) => invoke<{ auto_days: number | null }>('videos_auto_set', { days }),
};
