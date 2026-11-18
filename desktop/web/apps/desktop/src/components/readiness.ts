// What a meeting needs, in one place for the first-run guide's last step and the meeting lobby: a model source, a
// transcription model on this PC (both required: without them 开始会议 stays off), and what's good to know — the
// microphone, the AI's voice. Asked again when Settings or the guide change something, and every 10 s while shown.
import { useEffect, useState } from 'react';
import { inTauri } from '../api';
import { meetingApi, PROFILE_EVENT, type Devices, type Profile, type Provider } from '../meetingApi';
import { SETUP_EVENT } from '../setupApi';
import { useDictationModels, type DictationModelsState } from './ModelDownload';
import { engineOf, useVoice } from './VoiceSettings';

export type Blocker = 'provider' | 'asr';

export interface Readiness {
  /** the backend answered (profile, model source) */
  up: boolean;
  profile: Profile | null;
  provider: Provider | null;
  /** ok: the selected model is on this PC; unknown: outside the app (no way to ask) */
  asr: 'ok' | 'missing' | 'downloading' | 'unknown';
  asrName: string | null;
  speech: DictationModelsState;
  mic: Devices | null;
  voiceEngine: 'clone' | 'edge' | 'sapi' | null | undefined;
  /** what keeps 开始会议 off, in the order to fix them */
  blockers: Blocker[];
}

export function useReadiness(active = true): Readiness {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [provider, setProvider] = useState<Provider | null>(null);
  const [mic, setMic] = useState<Devices | null>(null);
  const [voice] = useVoice();
  const speech = useDictationModels();
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = () => {
      meetingApi.profile().then((x) => alive && setProfile(x), () => {});
      meetingApi.provider().then((x) => alive && setProvider(x), () => {});
      meetingApi.devices().then((x) => alive && setMic(x), () => {});
    };
    load();
    const h = window.setInterval(load, 10_000);
    window.addEventListener(PROFILE_EVENT, load);
    window.addEventListener(SETUP_EVENT, load);
    return () => {
      alive = false;
      clearInterval(h);
      window.removeEventListener(PROFILE_EVENT, load);
      window.removeEventListener(SETUP_EVENT, load);
    };
  }, [active]);
  const m = speech.models;
  const sel = m?.models.find((x) => x.id === m.selected) ?? m?.models.find((x) => x.id === m.recommended) ?? m?.models[0] ?? null;
  // the list not read yet: unknown (no flash of "missing" at start); couldn't be read inside the app: missing
  const asr: Readiness['asr'] = speech.dl ? 'downloading' : m ? (m.ready || sel?.installed ? 'ok' : 'missing') : inTauri && speech.err ? 'missing' : 'unknown';
  const blockers: Blocker[] = [];
  if (provider && provider.kind === 'unset') blockers.push('provider');
  if (asr === 'missing' || asr === 'downloading') blockers.push('asr');
  return {
    up: !!profile || !!provider,
    profile,
    provider,
    asr,
    asrName: sel?.name ?? null,
    speech,
    mic,
    voiceEngine: voice ? engineOf(voice) : undefined,
    blockers,
  };
}
