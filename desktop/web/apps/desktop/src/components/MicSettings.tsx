// Settings → Speech → Microphone: the one a meeting records and transcribes. 系统默认 first (the system's default input,
// named under the row and in the menu), then this PC's inputs by the names the recorder finds them by. Saved in
// ~/.blurt/config.json record.mic (zuhui/mic_prefs.py), not during a meeting. A picked one that isn't plugged in now
// stays in the menu and is said under the row. The row keeps room for that line (settings.css), so nothing moves.
import { useEffect, useState } from 'react';
import { Group, Popup, Row, S } from '../settings/kit';
import { meetingApi, type MicChoice } from '../meetingApi';

const MIC_ERRORS = ['live', 'bad', 'too_long', 'unreadable'];

export function MicGroup() {
  const [c, setC] = useState<MicChoice | null>(null);
  const [down, setDown] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = () =>
      meetingApi.micDevice().then(
        (x) => {
          if (!alive) return;
          setC(x);
          setDown(false);
        },
        () => {
          if (!alive) return;
          setDown(true);
          timer = window.setTimeout(load, 4000);
        },
      );
    void load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);
  const pick = async (v: string) => {
    setBusy(true);
    setMsg(null);
    try {
      setC(await meetingApi.setMicDevice(v));
    } catch (e) {
      const code = String((e as Error)?.message ?? e);
      setMsg(S('settings.mic.err', S(`settings.mic.err.${MIC_ERRORS.includes(code) ? code : 'failed'}`)));
    } finally {
      setBusy(false);
    }
  };
  const cur = c?.mic ?? '';
  const choices: [string, string][] = [['', S('settings.mic.default')], ...(c?.devices ?? []).map((d) => [d, d] as [string, string])];
  // picked but not here now (unplugged), or turned off by hand: still listed, so the menu shows what is set
  if (cur && !choices.some(([v]) => v === cur)) choices.push([cur, cur === 'off' ? S('settings.mic.off') : cur]);
  const hints = choices.map(([v]) => (v === '' ? c?.default ?? undefined : v === cur && c?.missing ? S('settings.mic.missing') : undefined));
  const missing = !!c?.missing && cur !== '' && cur !== 'off';
  // the system's default by name, on one line (cut with …, whole on hover)
  const now = cur === '' && c?.default ? <span className="st-path" title={c.default}>{S('voice.now', c.default)}</span> : null;
  const detail = msg ?? (!c ? null : c.live ? S('settings.mic.live') : missing ? S('settings.mic.missing') : now);
  return (
    <Group id="speech.mic" footer={down ? S('settings.profile.offline') : undefined}>
      <Row id="speech.mic" detail={detail} warn={!!msg || (missing && !c?.live)}>
        <span data-testid="mic-pick">
          <Popup wide label={S('settings.speech.mic')} value={cur} choices={choices} dividers={[1]} hints={hints}
            disabled={!c || busy || c.live} onChange={(v) => void pick(v)} />
        </span>
      </Row>
    </Group>
  );
}
