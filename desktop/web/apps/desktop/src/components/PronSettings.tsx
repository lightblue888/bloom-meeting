// Settings → Speech → 读法 (第 7 批): how the AI says a word — "written = said", one per line, imported from a .txt / .csv
// ("written,said" per row) too. Kept by the backend with the voice (~/.zuhui/speech.json → pron); every voice goes through
// it before speaking (zuhui/mouth.py speakable), the transcript keeps the written form. Laid out like 专有名词 above it.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Group, Row, S } from '../settings/kit';
import { setupApi, type Pron } from '../setupApi';
import { isImeKey } from '../ime';
import { useVoice } from './VoiceSettings';

/** the separators a line may use between the written form and the said one (the first one counts) — the backend's
 *  voice_prefs._PRON_SEP */
const SEP = /^(.*?)\s*(?:=|→|->|\t|,|，)\s*(.*)$/;
const squeeze = (s: string) => s.replace(/\s+/g, ' ').trim();

/** "a = b" lines → the pairs (the same written form twice: the later one wins, in the first one's place); `bad`: lines
 *  that aren't "written = said" */
export function parsePron(raw: string): { pairs: Pron[]; bad: number } {
  const out = new Map<string, string>();
  let bad = 0;
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const m = SEP.exec(line.trim());
    const w = m ? squeeze(m[1]) : '';
    const r = m ? squeeze(m[2]) : '';
    if (!w || !r) {
      bad += 1;
      continue;
    }
    out.set(w, r);
  }
  return { pairs: [...out].map(([w, r]) => ({ w, r })), bad };
}
export const pronText = (ps: Pron[]) => ps.map((p) => `${p.w} = ${p.r}`).join('\n');

export function PronounceGroup() {
  const [v, setV] = useVoice();
  const [text, setText] = useState('');
  const [msg, setMsg] = useState<{ text: string; warn: boolean } | null>(null);
  const editing = useRef(false);
  const cancelled = useRef(false);
  const file = useRef<HTMLInputElement>(null);
  const saved = v?.pron ?? [];
  useEffect(() => {
    if (v && !editing.current) setText(pronText(v.pron ?? []));
  }, [v]);
  const max = v?.pron_max ?? 200;
  const len = v?.pron_len ?? 40;
  const words = (code: string) =>
    code === 'pron_too_long' ? S('voice.pron.too_long', len) : code === 'pron_too_many' ? S('voice.pron.count_max', max) : code === 'pron_bad' ? S('voice.pron.bad') : S('voice.err.save');
  const store = useCallback(
    async (pairs: Pron[], done?: string) => {
      if (!v) return;
      try {
        const x = await setupApi.setVoice({ pron: pairs });
        setV(x);
        setText(pronText(x.pron ?? []));
        setMsg(done ? { text: done, warn: false } : null);
      } catch (e) {
        setMsg({ text: words(String((e as Error)?.message ?? e)), warn: true });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [v, setV, max, len],
  );
  const save = useCallback(async () => {
    editing.current = false;
    if (cancelled.current) {
      cancelled.current = false;
      return;
    }
    if (!v) return;
    const { pairs, bad } = parsePron(text);
    // a line without "=": said where the count is, the text stays as typed (nothing is lost)
    if (bad) return setMsg({ text: S('voice.pron.bad'), warn: true });
    if (pairs.some((p) => p.w.length > len || p.r.length > len)) return setMsg({ text: S('voice.pron.too_long', len), warn: true });
    if (pairs.length > max) return setMsg({ text: S('voice.pron.count_max', max), warn: true });
    if (pronText(pairs) === pronText(saved)) {
      setText(pronText(saved));
      return setMsg(null);
    }
    await store(pairs);
  }, [text, v, saved, store, len, max]);
  // a .txt / .csv: "written = said" or "written,said" per line; added after the ones here (a written form already
  // here takes the file's reading); a header row ("写法,读法") is skipped
  const importFile = async (f: File | undefined) => {
    if (!f || !v) return;
    try {
      const { pairs } = parsePron(await f.text());
      const add = pairs.filter((p) => !/^(写法|word|written)$/i.test(p.w));
      const merged = new Map(saved.map((p) => [p.w, p.r]));
      let n = 0;
      for (const p of add) {
        if (p.w.length > len || p.r.length > len) continue;
        if (merged.get(p.w) !== p.r) n += 1;
        merged.set(p.w, p.r);
      }
      const all = [...merged].map(([w, r]) => ({ w, r })).slice(0, max);
      await store(all, S('voice.pron.imported', n));
    } catch {
      setMsg({ text: S('voice.err.save'), warn: true });
    } finally {
      if (file.current) file.current.value = '';
    }
  };
  const count = parsePron(text).pairs.length;
  return (
    <Group id="speech.pron">
      <Row id="speech.pron" detail={msg?.text ?? `${count} / ${max}`} warn={!!msg?.warn}>
        <button disabled={!v} onClick={() => file.current?.click()} data-testid="pron-import">{S('voice.hot.import')}</button>
        <input ref={file} type="file" accept=".txt,.csv,text/plain,text/csv" hidden onChange={(e) => void importFile(e.target.files?.[0])} />
      </Row>
      <div className="st-hot">
        <textarea
          value={text}
          rows={6}
          disabled={!v}
          spellCheck={false}
          maxLength={max * (len * 2 + 5)}
          placeholder={S('voice.pron.placeholder')}
          aria-label={S('settings.speech.pron_row')}
          data-testid="pron"
          onFocus={() => (editing.current = true)}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => void save()}
          onKeyDown={(e) => {
            if (isImeKey(e)) return;
            if (e.key === 'Escape') {
              cancelled.current = true;
              setText(pronText(saved));
              setMsg(null);
              (e.target as HTMLTextAreaElement).blur();
            }
          }}
        />
      </div>
    </Group>
  );
}
