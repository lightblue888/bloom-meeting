// The first-run guide: the whole window, five steps — 你 (photo, name, colour), 模型 (model source and key, the AI's
// name and colour), 转写 (the speech model on this PC, words it should know, the microphone), 声音 (the AI's voice, a
// sample), 完成 (what's ready, what still keeps 开始会议 off). Every step is the same group Settings shows, so what is
// set here is what Settings shows. 跳过 is always there; done or skipped, the backend remembers it (/api/setup) and the
// guide doesn't come back by itself (Settings → General → 首次引导 runs it again).
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Icon } from '@blurt/icons';
import { t } from '../i18n';
import { avatarUrl } from '../meetingApi';
import { setupApi } from '../setupApi';
import { Flower, HOST_COLOR, nameColor } from './Flower';
import { BloomMark } from './MeetingLayout';
import { SpeechModelsGroup } from './ModelsSettings';
import { useReadiness, type Readiness } from './readiness';
import { ProfileGroup, ProviderGroup } from './SettingsView';
import { HotwordsGroup, VoiceGroup } from './VoiceSettings';
import './setup.css';

type Step = 'me' | 'model' | 'listen' | 'voice' | 'done';
const STEPS: { id: Step }[] = [{ id: 'me' }, { id: 'model' }, { id: 'listen' }, { id: 'voice' }, { id: 'done' }];

export function Onboarding({ onClose }: { onClose: () => void }) {
  const [at, setAt] = useState(0);
  const [leaving, setLeaving] = useState(false);
  const r = useReadiness(true);
  const body = useRef<HTMLDivElement>(null);
  const step = STEPS[at].id;
  useEffect(() => {
    body.current?.scrollTo({ top: 0 });
  }, [at]);
  const finish = async (how: 'done' | 'skipped') => {
    setLeaving(true);
    try {
      await setupApi.mark(how === 'done' ? { done: true } : { skipped: true });
    } catch {
      /* the backend isn't up: the guide simply closes; it comes back next time */
    }
    onClose();
  };
  const state = (s: Step): 'ok' | 'warn' | null =>
    s === 'model' ? (r.provider ? (r.provider.kind === 'unset' ? 'warn' : 'ok') : null) : s === 'listen' ? (r.asr === 'ok' ? 'ok' : r.asr === 'unknown' ? null : 'warn') : null;
  return (
    <div className="ob" role="dialog" aria-modal="true" aria-label={t('ob.title')} data-testid="onboarding">
      <aside className="ob-side">
        <div className="ob-brand">
          <BloomMark size={34} />
          <div className="ob-brand-t">{t('ob.title')}</div>
        </div>
        <ol className="ob-steps">
          {STEPS.map((s, i) => {
            const st = state(s.id);
            return (
              <li key={s.id}>
                <button className={`ob-step ${i === at ? 'is-on' : ''} ${i < at ? 'is-past' : ''}`} onClick={() => setAt(i)} aria-current={i === at ? 'step' : undefined} data-testid={`ob-step-${s.id}`}>
                  <span className="ob-step-n">{i < at && st !== 'warn' ? <Icon name="check" size={12} /> : i + 1}</span>
                  <span className="ob-step-t">{t(`ob.step.${s.id}`)}</span>
                  {st === 'warn' && <span className="ob-step-warn" aria-label={t('ob.needs_setup')} />}
                </button>
              </li>
            );
          })}
        </ol>
        <button className="link ob-skip" onClick={() => void finish('skipped')} disabled={leaving} data-testid="ob-skip">
          {t('ob.skip')}
        </button>
      </aside>

      <section className="ob-main">
        <div className="ob-body" ref={body}>
          <div className="ob-col">
            <h1 className="ob-h">{t(`ob.h.${step}`)}</h1>
            {(step === 'me' || step === 'model') && <Preview r={r} />}
            {step === 'me' && <ProfileGroup part="me" title="" />}
            {step === 'model' && (
              <>
                <ProviderGroup title="" usage={false} />
                <ProfileGroup part="ai" title={t('ob.ai_name')} />
              </>
            )}
            {step === 'listen' && (
              <>
                <SpeechModelsGroup />
                <MicLine r={r} />
                <HotwordsGroup />
              </>
            )}
            {step === 'voice' && <VoiceGroup title="" />}
            {step === 'done' && (
              <>
                <Summary r={r} go={(s) => setAt(STEPS.findIndex((x) => x.id === s))} />
                {!!r.blockers.length && (
                  <p className="ob-sum-note is-block">
                    {t('ob.blocked', { a: r.blockers.map((b) => t(`ob.sum.${b === 'provider' ? 'model' : 'asr'}`)).join(t('ob.and')) })}
                  </p>
                )}
              </>
            )}
          </div>
        </div>
        <footer className="ob-foot">
          <span className="ob-dots" aria-hidden="true">
            {STEPS.map((s, i) => (
              <span key={s.id} className={i === at ? 'is-on' : ''} />
            ))}
          </span>
          <span className="grow" />
          {at > 0 && (
            <button onClick={() => setAt(at - 1)} data-testid="ob-back">
              {t('ob.back')}
            </button>
          )}
          {step !== 'done' ? (
            <button className="primary" onClick={() => setAt(at + 1)} data-testid="ob-next">
              {t('ob.next')}
            </button>
          ) : (
            <button className="primary" onClick={() => void finish('done')} disabled={leaving} data-testid="ob-finish">
              {t('ob.finish')}
            </button>
          )}
        </footer>
      </section>
    </div>
  );
}

/** how you and the AI show in a meeting's header: what the first two steps change, as they change it */
function Preview({ r }: { r: Readiness }) {
  const p = r.profile;
  if (!p) return null;
  return (
    <div className="ob-preview" aria-label={t('ob.preview')}>
      <span className="ob-preview-l">{t('ob.preview')}</span>
      <span className="ob-chip">
        <span className="ob-chip-av">{p.avatar ? <img src={avatarUrl(p.avatar)} alt="" /> : <Flower color={p.color || HOST_COLOR} />}</span>
        <span className="ob-chip-n" style={{ color: nameColor(p.color) }}>{p.name}</span>
        <span className="ob-chip-r">{t('ob.host')}</span>
      </span>
      <span className="ob-chip">
        <span className="ob-chip-av"><Flower color={p.ai_color} /></span>
        <span className="ob-chip-n" style={{ color: nameColor(p.ai_color) }}>{p.assistant}</span>
      </span>
    </div>
  );
}

function MicLine({ r }: { r: Readiness }) {
  const name = r.mic?.mic;
  return (
    <section className="st-group">
      <h2 className="st-head"><span>{t('ob.mic')}</span></h2>
      <div className="st-rows">
        <div className="st-row">
          <div className="st-text">
            <div className="st-row-title"><span className="ob-trunc">{name || (r.mic?.probing || !r.mic ? t('ob.checking') : t('ob.mic_none'))}</span></div>
          </div>
        </div>
      </div>
    </section>
  );
}

/** the last step: each piece and where it stands; the two a meeting needs say so when they're missing */
function Summary({ r, go }: { r: Readiness; go: (s: Step) => void }) {
  const p = r.profile;
  const row = (title: string, value: ReactNode, ok: boolean | null, fix?: Step, need?: boolean) => (
    <div className={`ob-sum-row ${ok === false ? (need ? 'is-block' : 'is-warn') : ''}`}>
      <span className="ob-sum-t">{title}</span>
      <span className="ob-sum-v">{value}</span>
      <span className="ob-sum-s">
        {ok === true ? <Icon name="check" size={14} /> : ok === false && fix ? <button className="link" onClick={() => go(fix)}>{t('ob.fix')}</button> : null}
      </span>
    </div>
  );
  const voice = r.voiceEngine === undefined ? t('ob.checking') : r.voiceEngine === null ? t('ob.voice_off') : t(`ob.voice.${r.voiceEngine}`);
  const model = !r.provider ? '…' : r.provider.kind === 'unset' ? t('ob.unset') : r.model ?? (r.provider.label || '…');
  const asr = r.asr === 'ok' ? r.asrName : r.asr === 'downloading' ? t('ob.downloading') : r.asr === 'missing' ? t('ob.not_downloaded') : '…';
  return (
    <div className="ob-sum" data-testid="ob-summary">
      {row(t('ob.sum.name'), <span className="ob-trunc" style={{ color: nameColor(p?.color) }}>{p?.name ?? '…'}</span>, p ? true : null, 'me')}
      {row(t('ob.sum.model'), <span className="ob-trunc" title={[r.modelFast, r.model].filter(Boolean).join(' / ')}>{model}</span>,
        r.provider ? r.provider.kind !== 'unset' : null, 'model', true)}
      {row(t('ob.sum.asr'), <span className="ob-trunc">{asr}</span>, r.asr === 'ok' ? true : r.asr === 'unknown' ? null : false, 'listen', true)}
      {row(t('ob.mic'), <span className="ob-trunc">{r.mic?.mic || (r.mic?.probing || !r.mic ? '…' : t('ob.mic_none'))}</span>,
        r.mic?.mic ? true : r.mic && !r.mic.probing ? false : null, 'listen')}
      {row(t('ob.sum.voice'), <span className="ob-trunc">{voice}</span>, r.voiceEngine === undefined ? null : true, 'voice')}
    </div>
  );
}
