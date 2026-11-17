// The speech models and their download (Settings → Models, Storage's names): the list (`dictation_models`), one
// download at a time (`model_download`) and its progress from `model-download` events. A download already under way —
// setup.rs starts the recommended model by itself at the first launch — shows as one, never as a second Download.
import { useCallback, useEffect, useState } from 'react';
import { api, listen, type DictationModels, type ModelDownload } from '../api';
import { i18n, t } from '../i18n';

/** "950 MB", "1.6 GB" */
export const mb = (n: number) =>
  n >= 1000
    ? i18n().formatNumber(n / 1024, { style: 'unit', unit: 'gigabyte', maximumFractionDigits: 1 })
    : i18n().formatNumber(n, { style: 'unit', unit: 'megabyte', maximumFractionDigits: 0 });

export interface DictationModelsState {
  models: DictationModels | null;
  /** the list couldn't be read (the speech tools aren't there yet) */
  err: string;
  /** the download under way (from the list's `downloading` or the events) */
  dl: ModelDownload | null;
  /** the last download stopped before it finished */
  failed: boolean;
  /** …and which model's it was (its row says so, with Try Again) */
  failedId: string | null;
  load: () => Promise<DictationModels | null>;
  download: (id: string) => Promise<void>;
}

export function useDictationModels(): DictationModelsState {
  const [models, setModels] = useState<DictationModels | null>(null);
  const [err, setErr] = useState('');
  const [dl, setDl] = useState<ModelDownload | null>(null);
  const [failed, setFailed] = useState(false);
  const [failedId, setFailedId] = useState<string | null>(null);
  const load = useCallback(
    () =>
      api.dictationModels().then(
        (m) => {
          setModels(m);
          setErr('');
          // only events end a download here: a list read just before it started mustn't hide it
          if (m.downloading) setDl((d) => (d?.id === m.downloading ? d : { id: m.downloading!, state: 'running', done_mb: 0, total_mb: 0 }));
          return m;
        },
        (e) => {
          setErr(String(e));
          return null;
        },
      ),
    [],
  );
  useEffect(() => {
    load();
    return listen<ModelDownload>('model-download', (p) => {
      if (p.state === 'running') {
        setDl(p);
        setFailed(false);
        setFailedId(null);
      } else {
        setDl(null);
        setFailed(p.state === 'failed');
        setFailedId(p.state === 'failed' ? p.id : null);
        if (p.state === 'done') load();
      }
    });
  }, [load]);
  const download = useCallback(
    async (id: string) => {
      setFailed(false);
      setDl({ id, state: 'running', done_mb: 0, total_mb: 0 });
      try {
        await api.modelDownload(id);
      } catch (e) {
        // one is already running (the first-launch download): show that one
        if (!String(e).includes('already running')) {
          setDl(null);
          setErr(String(e));
        }
        load();
      }
    },
    [load],
  );
  return { models, err, dl, failed, failedId, load, download };
}

/** The one bar while a model comes down: "Connecting…" until the first megabytes, then "412 MB of 950 MB". */
export function ModelProgress({ dl }: { dl: ModelDownload }) {
  const started = dl.total_mb > 0;
  return (
    <span className="mprog" data-testid="model-progress">
      <progress max={started ? dl.total_mb : undefined} value={started ? Math.min(dl.done_mb, dl.total_mb) : undefined} />
      <span className="dim small mprog-txt">{started ? t('dictation.model.progress', { done: mb(dl.done_mb), total: mb(dl.total_mb) }) : t('dictation.model.starting')}</span>
    </span>
  );
}
