// Settings → Speech (and the first-run guide): the AI's voice — which kind, which speaker, a spoken sample — and the
// words transcription should know (names, terms). Both live in the Bloom backend (~/.zuhui/speech.json), so they wait
// for it like the profile does.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Group, Popup, Row, S, lang } from '../settings/kit';
import { setupApi, SETUP_EVENT, type Voice, type VoiceKind } from '../setupApi';
import { isImeKey } from '../ime';

const KINDS: VoiceKind[] = ['auto', 'clone', 'edge', 'sapi', 'off'];

/** what will actually speak (the backend's onboard.voice_engine): clone / edge / sapi, null = silent */
export function engineOf(v: Pick<Voice, 'voice' | 'has_clone' | 'has_edge'>): 'clone' | 'edge' | 'sapi' | null {
  if (v.voice === 'off') return null;
  if ((v.voice === 'auto' || v.voice === 'clone') && v.has_clone) return 'clone';
  if (v.voice !== 'sapi' && v.has_edge) return 'edge';
  return 'sapi';
}

/** "Yunxi · 中文男声" from zh-CN-YunxiNeural */
export function edgeLabel(e: Voice['edge_voices'][number]): string {
  const name = e.id.replace(/^[a-z]{2}-[A-Z]{2}-/, '').replace(/Neural$/, '');
  const zh = lang() === 'zh-Hans';
  return `${name} · ${S(`voice.${e.lang}`)}${zh ? '' : ' '}${S(`voice.${e.gender}`)}`;
}

/** the voice from the backend, read again when the guide or another page changed it; null while it's not up */
export function useVoice(): [Voice | null, (v: Voice) => void] {
  const [v, setV] = useState<Voice | null>(null);
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = () =>
      setupApi.voice().then(
        (x) => {
          if (!alive) return;
          setV(x);
          // Windows' own voices are still being listed: ask again shortly
          if (x.sapi_voices === null) timer = window.setTimeout(load, 1500);
        },
        () => {
          if (alive) timer = window.setTimeout(load, 4000);
        },
      );
    void load();
    const again = () => void load();
    window.addEventListener(SETUP_EVENT, again);
    return () => {
      alive = false;
      clearTimeout(timer);
      window.removeEventListener(SETUP_EVENT, again);
    };
  }, []);
  return [v, setV];
}

export function VoiceGroup({ footer, title }: { footer?: string; title?: string }) {
  const [v, setV] = useVoice();
  const [msg, setMsg] = useState<{ text: string; warn: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async (p: Parameters<typeof setupApi.setVoice>[0]) => {
    setMsg(null);
    try {
      setV(await setupApi.setVoice(p));
    } catch {
      setMsg({ text: S('voice.err.save'), warn: true });
    }
  };
  const test = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await setupApi.testVoice();
      setMsg(r.ok ? { text: S('voice.playing', S(`voice.${r.engine === 'gptsovits' || r.engine === 'indextts' ? 'clone' : r.engine ?? 'sapi'}`)), warn: false } : { text: S(`voice.err.${r.error ?? 'failed'}`), warn: true });
    } catch {
      setMsg({ text: S('voice.err.failed'), warn: true });
    } finally {
      setBusy(false);
    }
  };
  const eng = v ? engineOf(v) : null;
  const choices = KINDS.filter((k) => k !== 'clone' || v?.has_clone || v?.voice === 'clone').filter((k) => k !== 'edge' || v?.has_edge || v?.voice === 'edge');
  const hints = choices.map((k) => (k === 'edge' ? S('voice.edge_net') : undefined));
  const now = !v ? undefined : eng ? S('voice.now', S(`voice.${eng}`)) : S('voice.now_off');
  return (
    <Group id="speech.voice" title={title} footer={footer}>
      <Row id="speech.voice" detail={now}>
        <Popup wide label={S('settings.speech.voice_engine')} value={v?.voice ?? 'auto'} disabled={!v} dividers={[choices.length - 1]}
          choices={choices.map((k) => [k, S(`voice.${k}`)] as [VoiceKind, string])} hints={hints} onChange={(k) => void save({ voice: k })} />
      </Row>
      {v && eng === 'edge' && (
        <Row id="speech.voice_name">
          <Popup wide label={S('settings.speech.voice_name')} value={v.edge_voice} dividers={[v.edge_voices.findIndex((e) => e.lang === 'en')]}
            choices={v.edge_voices.map((e) => [e.id, edgeLabel(e)] as [string, string])} onChange={(id) => void save({ edge_voice: id })} />
        </Row>
      )}
      {v && eng === 'sapi' && (
        <Row id="speech.voice_name">
          {v.sapi_voices === null ? (
            <span className="st-dim">{S('voice.loading')}</span>
          ) : (
            <Popup wide label={S('settings.speech.voice_name')} value={v.sapi_voice} dividers={[1]}
              choices={[['', S('voice.default')] as [string, string], ...v.sapi_voices.map((x) => [x.name, x.name.replace(/^Microsoft\s+/, '').replace(/\s+Desktop$/, '')] as [string, string])]}
              onChange={(name) => void save({ sapi_voice: name })} />
          )}
        </Row>
      )}
      <Row id="speech.voice_test" detail={msg?.text ?? undefined} warn={!!msg?.warn}>
        <button disabled={!v || !eng || busy} onClick={() => void test()} data-testid="voice-test">{S('voice.play')}</button>
      </Row>
    </Group>
  );
}

export function HotwordsGroup() {
  const [v, setV] = useVoice();
  const [text, setText] = useState('');
  const [msg, setMsg] = useState<{ text: string; warn: boolean } | null>(null);
  const editing = useRef(false);
  const cancelled = useRef(false);
  useEffect(() => {
    if (v && !editing.current) setText(v.hotwords.join('\n'));
  }, [v]);
  const save = useCallback(async () => {
    editing.current = false;
    if (cancelled.current) {
      cancelled.current = false;
      return;
    }
    if (!v) return;
    const words = text.split('\n').map((x) => x.trim()).filter(Boolean);
    if (words.join('\n') === v.hotwords.join('\n')) return setMsg(null);
    try {
      const x = await setupApi.setVoice({ hotwords: words });
      setV(x);
      setText(x.hotwords.join('\n'));
      setMsg({ text: S('voice.hot.saved'), warn: false });
    } catch (e) {
      const c = String((e as Error)?.message ?? e);
      setMsg({ text: c === 'hot_too_long' ? S('voice.hot.too_long', v.hot_len) : c === 'hot_too_many' ? S('voice.hot.count_max', v.hot_max) : S('voice.err.save'), warn: true });
    }
  }, [text, v, setV]);
  const max = v?.hot_max ?? 12;
  return (
    <Group id="speech.hotwords">
      <Row id="speech.hotwords" detail={msg?.text ?? S('settings.speech.hotwords.detail', max)} warn={!!msg?.warn} />
      <div className="st-hot">
        <textarea
          value={text}
          rows={4}
          disabled={!v}
          spellCheck={false}
          maxLength={max * ((v?.hot_len ?? 20) + 2)}
          placeholder={S('voice.hot.placeholder')}
          aria-label={S('settings.speech.hotwords_row')}
          data-testid="hotwords"
          onFocus={() => (editing.current = true)}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => void save()}
          onKeyDown={(e) => {
            if (isImeKey(e)) return;
            if (e.key === 'Escape') {
              cancelled.current = true;
              setText(v?.hotwords.join('\n') ?? '');
              setMsg(null);
              (e.target as HTMLTextAreaElement).blur();
            }
          }}
        />
      </div>
    </Group>
  );
}
