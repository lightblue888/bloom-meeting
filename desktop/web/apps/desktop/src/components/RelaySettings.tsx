// Settings → General → Invites: the meeting relay a guest's browser joins through. A server gets it with one command
// (relay/setup.sh), which ends by printing a connection code; pasting that here is all the setup this PC needs.
import { useEffect, useState } from 'react';
import { Icon } from '@blurt/icons';
import { Group, Popup, Row, S } from '../settings/kit';
import { RELAY_SETUP_CMD, relayApi, type Relay, type RelayMode } from '../setupApi';
import { isImeKey } from '../ime';

const ERRORS = ['code', 'unset', 'unreachable', 'auth', 'livekit', 'register'];

export function RelayGroup() {
  const [r, setR] = useState<Relay | null>(null);
  const [code, setCode] = useState('');
  const [editing, setEditing] = useState(false);
  const [msg, setMsg] = useState<{ text: string; warn: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = () =>
      relayApi.get().then(
        (x) => alive && setR(x),
        () => {
          if (alive) timer = window.setTimeout(load, 4000);
        },
      );
    void load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, []);
  const err = (c: string) => S(`relay.err.${ERRORS.includes(c) ? c : 'failed'}`);
  const save = async () => {
    if (!code.trim()) return;
    setBusy(true);
    setMsg(null);
    try {
      setR(await relayApi.save(code));
      setCode('');
      setEditing(false);
      setOwn(false);
      setMsg({ text: S('relay.saved'), warn: false });
    } catch (e) {
      setMsg({ text: err(String((e as Error)?.message ?? e)), warn: true });
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    setBusy(true);
    setMsg(null);
    try {
      setR(await relayApi.clear());
      setOwn(false);
    } catch {
      setMsg({ text: err('failed'), warn: true });
    } finally {
      setBusy(false);
    }
  };
  const test = async () => {
    setBusy(true);
    setMsg({ text: S('relay.testing'), warn: false });
    try {
      const x = await relayApi.test();
      setMsg(x.ok ? { text: S('relay.ok', ((x.ms ?? 0) / 1000).toFixed(1)), warn: false } : { text: err(x.error ?? 'failed'), warn: true });
    } catch {
      setMsg({ text: err('unreachable'), warn: true });
    } finally {
      setBusy(false);
    }
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(RELAY_SETUP_CMD);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* the field is selectable: copy by hand */
    }
  };
  const [own, setOwn] = useState(false);
  const mode: RelayMode = own ? 'own' : r?.mode ?? 'public';
  const choose = async (m: RelayMode) => {
    setMsg(null);
    if (m === 'own') return setOwn(true);           // 粘了连接码才算换过去
    setOwn(false);
    setBusy(true);
    try {
      setR(await relayApi.setMode(m));
    } catch {
      setMsg({ text: err('failed'), warn: true });
    } finally {
      setBusy(false);
    }
  };
  const showInput = !r?.configured || editing || (own && r?.mode !== 'own');
  return (
    <Group id="general.invite">
      <Row id="general.relay_mode" detail={mode === 'public' ? r?.host ?? null : null}>
        <Popup label={S('settings.general.relay_mode')} value={mode} disabled={!r || busy}
          choices={(['public', 'own', 'off'] as RelayMode[]).map((m) => [m, S(`relay.mode.${m}`)] as [RelayMode, string])}
          onChange={(m) => void choose(m)} />
      </Row>
      {mode === 'own' && (
        <>
      <Row id="general.relay_cmd">
        <input className="st-name st-wide st-mono" readOnly value={RELAY_SETUP_CMD} title={RELAY_SETUP_CMD} aria-label={S('settings.general.relay_cmd')}
          onFocus={(e) => e.currentTarget.select()} data-testid="relay-cmd" />
        <button onClick={() => void copy()} aria-label={S('relay.copy')} data-testid="relay-copy">
          <Icon name={copied ? 'check' : 'copy'} size={14} />
        </button>
      </Row>
      <Row id="general.relay" detail={r ? (r.mode === 'own' && r.configured ? r.host : S('relay.unset')) : null}>
        {showInput ? (
          <>
            <input
              className="st-name st-wide"
              type="password"
              value={code}
              placeholder={S('relay.placeholder')}
              autoComplete="off"
              spellCheck={false}
              maxLength={600}
              disabled={!r || busy}
              aria-label={S('settings.general.relay')}
              data-testid="relay-code"
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => {
                if (isImeKey(e)) return;
                if (e.key === 'Enter') void save();
                if (e.key === 'Escape' && editing) {
                  setCode('');
                  setEditing(false);
                }
              }}
            />
            <button className="primary" disabled={!r || busy || !code.trim()} onClick={() => void save()} data-testid="relay-save">
              {S('relay.save')}
            </button>
          </>
        ) : (
          <>
            <button disabled={busy} onClick={() => setEditing(true)} data-testid="relay-change">{S('relay.change')}</button>
            {r?.managed && <button disabled={busy} onClick={() => void remove()} data-testid="relay-remove">{S('relay.remove')}</button>}
          </>
        )}
      </Row>
        </>
      )}
      {mode !== 'off' && (
        <Row id="general.relay_check" detail={msg?.text ?? undefined} warn={!!msg?.warn}>
          <button disabled={!r?.configured || busy || (own && r?.mode !== 'own')} onClick={() => void test()} data-testid="relay-test">{S('relay.test')}</button>
        </Row>
      )}
    </Group>
  );
}
