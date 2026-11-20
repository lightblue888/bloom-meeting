// Settings → Speech: the microphone a meeting records (MicSettings.tsx), the transcription models this PC can run (the meeting's live transcript is one of them, e.g.
// Fun-ASR Nano), each with its size — installed, coming down (its progress and Stop where its button was), or Download;
// then the AI's voice and the words transcription should know (VoiceSettings.tsx). One download at a time; a failed one
// says so on its own row, with Try Again. The first-run guide shows the same groups.
import { api } from '../api';
import { Icon } from '@blurt/icons';
import { Group, Line, Page, S } from '../settings/kit';
import { mb, ModelProgress, useDictationModels } from './ModelDownload';
import { HotwordsGroup, VoiceGroup } from './VoiceSettings';
import { PronounceGroup } from './PronSettings';
import { MicGroup } from './MicSettings';

function StopDownload() {
  // the same size as the Download it replaced
  return <button onClick={() => api.modelDownloadCancel().catch(() => {})}>{S('speech.download.stop')}</button>;
}

export function ModelsPane() {
  return (
    <Page id="speech">
      <MicGroup />
      <SpeechModelsGroup />
      <VoiceGroup />
      <HotwordsGroup />
      <PronounceGroup />
    </Page>
  );
}

/** the transcription models, each installed / coming down / to download */
export function SpeechModelsGroup() {
  const { models, err, dl, failed, failedId, download } = useDictationModels();
  return (
    <Group id="speech.models" footer={err || (failed && !failedId ? S('win.models.failed') : undefined)}>
      {!models && !err && <Line icon="waveform" title={S('speech.loading')} />}
      {/* a failed download says so on its own row (Try Again); one coming down shows its progress where its button
          was, the row keeping its two lines and its height */}
      {models?.models.map((m) => {
        const broke = failed && failedId === m.id;
        return (
          <Line key={m.id} icon="waveform" title={m.name} badge={m.recommended && !m.installed ? S('models.recommended') : undefined} warn={broke}
            detail={broke ? `${mb(m.size_mb)} · ${S('win.models.failed_row')}` : dl?.id === m.id ? S('speech.rec.downloading', '').trim() : mb(m.size_mb)}>
            {m.installed ? (
              <span className="st-have"><Icon name="check" size={13} />{S('win.models.installed')}</span>
            ) : dl?.id === m.id ? (
              <><ModelProgress dl={dl} /><StopDownload /></>
            ) : (
              <button disabled={!!dl} title={dl ? S('win.models.one_at_a_time') : undefined} onClick={() => download(m.id)}>{S(broke ? 'speech.rec.retry' : 'speech.download.button')}</button>
            )}
          </Line>
        );
      })}
    </Group>
  );
}
