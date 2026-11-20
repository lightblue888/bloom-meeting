// Settings → General → Model source: Claude Code's own login, or an API — an Anthropic-format endpoint (DeepSeek, Kimi,
// GLM, Qwen, Anthropic, any compatible one) or an OpenAI-format one (any compatible endpoint, Ollama and LM Studio on
// this computer, which take no key). An OpenAI-format endpoint lists its models (GET <url>/models) in a menu beside
// each model field. With an API source two more rows: 查文献 (the AI may look up papers in OpenAlex / arXiv / Crossref)
// and this month's tokens and estimated cost. The key goes to the backend once and is never shown again, only its last
// four. Messages (saved, a test, a failure) all land in the connection row, which keeps room for them.
import { useEffect, useRef, useState } from 'react';
import { Icon } from '@blurt/icons';
import { Group, Popup, Row, S, Switch } from '../settings/kit';
import { meetingApi, PROFILE_EVENT, type ModelChoice, type ModelPart, type Models, type MonthUsage, type Provider, type ProviderKind } from '../meetingApi';
import { Menu, type MenuAt } from './Menu';
import { usageBreakdown, usageLine } from '../usageText';

// hosted APIs first (the default for a new install), then the compatible endpoints, the local ones, Claude Code's own login last
const SOURCES: ProviderKind[] = ['deepseek', 'kimi', 'glm', 'qwen', 'anthropic', 'openai', 'custom', 'ollama', 'lmstudio', 'claude'];
/** the two local apps' names when the backend's preset list doesn't carry them (an older backend still running) */
const LOCAL_NAMES: Partial<Record<ProviderKind, string>> = { ollama: 'Ollama', lmstudio: 'LM Studio' };
const PROVIDER_ERRORS = ['key', 'base_url', 'model'];
const MODEL_MAX = 120;

/** a model field with its menu: type a name, or pick one the endpoint lists (fetched when the menu opens) */
function ModelCombo({ value, set, disabled, testid, label, list }: {
  value: string;
  set: (v: string) => void;
  disabled: boolean;
  testid: string;
  label: string;
  /** the endpoint's models; null when they couldn't be listed (the reason is shown in the connection row) */
  list: () => Promise<string[] | null>;
}) {
  const box = useRef<HTMLSpanElement>(null);
  const [at, setAt] = useState<MenuAt | null>(null);
  const [items, setItems] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const open = async () => {
    if (at) return setAt(null);
    setLoading(true);
    const got = await list();
    setLoading(false);
    if (!got?.length || !box.current) return;
    setItems(got);
    setAt({ x: 0, y: 0, anchor: box.current, align: 'end', minWidth: box.current.offsetWidth });
  };
  return (
    <span className="st-combo" ref={box}>
      <input
        className="st-name"
        value={value}
        maxLength={MODEL_MAX}
        spellCheck={false}
        disabled={disabled}
        aria-label={label}
        title={value || undefined}
        data-testid={testid}
        onChange={(e) => set(e.target.value.trim())}
        onKeyDown={(e) => {
          if (e.altKey && e.key === 'ArrowDown') {
            e.preventDefault();
            void open();
          }
        }}
      />
      {/* the chevron and the spinner share one fixed box: fetching the list never moves the field */}
      <button type="button" className="st-combo-btn" aria-haspopup="menu" aria-expanded={!!at} aria-label={S('settings.provider.pick_model')}
        aria-busy={loading || undefined} disabled={disabled || loading} onClick={() => void open()} data-testid={`${testid}-pick`}>
        {loading ? <span className="mw-spinner sm" aria-hidden="true" /> : <Icon name="chevron-down" size={12} />}
      </button>
      {at && (
        <Menu
          entries={items.map((m) => ({ label: m, checked: m === value, run: () => set(m), testId: `${testid}-opt` }))}
          at={at}
          label={label}
          onClose={() => setAt(null)}
        />
      )}
    </span>
  );
}

/** Claude Code's own login: the conversation model, the editing model and its thinking effort, as /api/models has them.
 *  A part with one choice (the conversation model) is plain text; the others are menus, saved at once — in a meeting
 *  or not (the next reply / the next edit uses it). A failure is said in the connection row (`onError`). */
function ClaudeModels({ onError, disabled }: { onError: (text: string | null) => void; disabled: boolean }) {
  const [m, setM] = useState<Models | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = () =>
      meetingApi.models().then(
        (x) => {
          if (!alive) return;
          if (x) setM(x);
          else timer = window.setTimeout(load, 4000);
        },
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
  const pick = async (part: ModelPart, v: string) => {
    setBusy(true);
    onError(null);
    try {
      setM(await meetingApi.setModel(part, v));
      window.dispatchEvent(new Event(PROFILE_EVENT)); // the lobby's status rows name the new model
    } catch {
      onError(S('settings.provider.err.failed'));
    } finally {
      setBusy(false);
    }
  };
  /** the choices; a current one that isn't among them (a test override) is listed too, so the menu never lies */
  const choices = (c?: ModelChoice): [string, string][] => {
    const o = (c?.options ?? []).map((x) => [x.id, x.label] as [string, string]);
    if (c?.current && !o.some(([id]) => id === c.current)) o.unshift([c.current, c.current]);
    return o;
  };
  const line = (id: string, part: ModelPart, label: string) => {
    const c = m?.[part];
    const ch = choices(c);
    // not read yet, or nothing to choose from: the name as text, in the menus' column
    if (ch.length < 2) {
      const name = !m ? '…' : ch[0]?.[1] ?? '—';
      return (
        <Row id={id}>
          <span className="st-fixed" title={m ? name : undefined} data-testid={`provider-${part}`}>{name}</span>
        </Row>
      );
    }
    return (
      <Row id={id}>
        <Popup wide label={label} value={c?.current ?? ''} choices={ch} disabled={busy || disabled} onChange={(v) => void pick(part, v)} />
      </Row>
    );
  };
  return (
    <>
      {line('general.fast_model', 'dialog', S('settings.general.fast_model'))}
      {line('general.main_model', 'edit', S('settings.general.main_model'))}
      {line('general.effort', 'effort', S('settings.general.effort'))}
    </>
  );
}

/** `usage`: the month's line (Settings has it; the first-run guide leaves it out) */
export function ProviderGroup({ title, usage: showUsage = true }: { title?: string; usage?: boolean } = {}) {
  const [p, setP] = useState<Provider | null>(null);
  const [down, setDown] = useState(false);
  const [kind, setKind] = useState<ProviderKind>('claude');
  const [base, setBase] = useState('');
  const [key, setKey] = useState('');
  const [fast, setFast] = useState('');
  const [main, setMain] = useState('');
  const [msg, setMsg] = useState<{ text: string; warn: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [usage, setUsage] = useState<MonthUsage | null>(null);
  /** the last model list, for the fields it was fetched with */
  const listed = useRef<{ sig: string; models: string[] } | null>(null);
  /** the fields for a source: what is saved when it is the saved one, else the provider's published defaults */
  const fill = (x: Provider, k: ProviderKind) => {
    const pre = x.presets[k];
    const same = x.kind === k;
    setBase(same && x.base_url ? x.base_url : pre?.base_url ?? '');
    setFast(same && x.fast ? x.fast : pre?.fast ?? '');
    setMain(same && x.main ? x.main : pre?.main ?? '');
    setKey('');
  };
  const loadUsage = () => meetingApi.usageMonth().then(setUsage, () => {});
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const load = () =>
      meetingApi.provider().then(
        (x) => {
          if (!alive) return;
          setP(x);
          setDown(false);
          const k = x.kind === 'unset' ? 'deepseek' : x.kind; // not picked yet: an API source, DeepSeek first
          setKind(k);
          fill(x, k);
          void loadUsage();
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
  const fmt = (k: ProviderKind) => p?.presets[k]?.format ?? 'anthropic';
  const keyRule = (k: ProviderKind) => p?.presets[k]?.key ?? 'required';
  const name = (k: ProviderKind) =>
    k === 'claude' ? S('settings.provider.claude')
      : k === 'custom' ? S('settings.provider.custom')
      : k === 'openai' ? S('settings.provider.openai')
      : p?.presets[k]?.label || LOCAL_NAMES[k] || k;
  const api = kind !== 'claude';
  const endpoint = api && kind !== 'anthropic';
  const openai = api && fmt(kind) === 'openai';
  const needsKey = api && keyRule(kind) !== 'none';
  const save = async (k: ProviderKind = kind) => {
    if (k !== 'claude' && k !== 'anthropic' && (!fast || !main)) {
      setMsg({ text: S('settings.provider.err.model_empty'), warn: true });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const x = await meetingApi.setProvider(k === 'claude' ? { kind: k } : { kind: k, base_url: base, key: keyRule(k) === 'none' ? '' : key, fast, main });
      setP(x);
      const k2 = x.kind === 'unset' ? 'deepseek' : x.kind;
      setKind(k2);
      fill(x, k2);
      setMsg({ text: S('settings.provider.saved'), warn: false });
    } catch (e) {
      const c = String((e as Error)?.message ?? e);
      const code = c === 'base_url' && fmt(k) === 'openai' ? 'base_url_oa' : PROVIDER_ERRORS.includes(c) ? c : 'failed';
      setMsg({ text: S(`settings.provider.err.${code}`), warn: true });
    } finally {
      setBusy(false);
    }
  };
  const choose = (k: ProviderKind) => {
    setKind(k);
    setMsg(null);
    listed.current = null;
    if (p) fill(p, k);
    // back to Claude Code's own login: nothing to fill in, so it is saved right away
    if (k === 'claude' && p && p.kind !== 'claude') void save('claude');
  };
  const test = async () => {
    setBusy(true);
    setMsg({ text: S('settings.provider.testing'), warn: false });
    try {
      const r = await meetingApi.testProvider();
      setMsg(r.ok ? { text: S('settings.provider.ok', ((r.ms ?? 0) / 1000).toFixed(1)), warn: false } : { text: S('settings.provider.fail', r.error ?? ''), warn: true });
    } catch (e) {
      setMsg({ text: S('settings.provider.fail', String((e as Error)?.message ?? e)), warn: true });
    } finally {
      setBusy(false);
      void loadUsage();
    }
  };
  /** the endpoint's models as the fields stand (the saved key when none is typed); a failure is said in the connection row */
  const listModels = async (): Promise<string[] | null> => {
    const sig = `${kind}|${base}|${key}`;
    if (listed.current?.sig === sig) return listed.current.models;
    let r: { models?: string[]; error?: string };
    try {
      r = await meetingApi.providerModels({ kind, base_url: base, key: needsKey ? key : '' });
    } catch (e) {
      r = { error: String((e as Error)?.message ?? e) };
    }
    if (!r.models) {
      const why = r.error === 'base_url' ? S('settings.provider.err.base_url_oa') : r.error ?? '';
      setMsg({ text: S('settings.provider.models_fail', why), warn: true });
      return null;
    }
    if (!r.models.length) {
      setMsg({ text: S('settings.provider.models_none'), warn: true });
      return null;
    }
    listed.current = { sig, models: r.models };
    setMsg(null);
    return r.models;
  };
  const setLit = async (on: boolean) => {
    if (!p) return;
    setP({ ...p, lit: on });
    try {
      setP(await meetingApi.setLit(on));
    } catch {
      setP((cur) => (cur ? { ...cur, lit: !on } : cur));
      setMsg({ text: S('settings.provider.err.failed'), warn: true });
    }
  };
  const dirty = !!p && (kind !== p.kind || !!key || (endpoint && (base !== p.base_url || fast !== p.fast || main !== p.main)));
  const keyHint = p && p.kind === kind && p.key_set ? S('settings.provider.key_saved', p.key_tail)
    : keyRule(kind) === 'optional' ? S('settings.provider.key_optional') : S('settings.provider.key_placeholder');
  const field = (v: string, set: (s: string) => void, max: number, testid: string, label: string) => (
    <input
      className="st-name st-wide"
      value={v}
      maxLength={max}
      spellCheck={false}
      disabled={!p || busy}
      aria-label={label}
      title={v || undefined}
      data-testid={testid}
      onChange={(e) => set(e.target.value.trim())}
    />
  );
  const modelField = (v: string, set: (s: string) => void, testid: string, label: string) =>
    openai ? <ModelCombo value={v} set={set} disabled={!p || busy} testid={testid} label={label} list={listModels} /> : field(v, set, MODEL_MAX, testid, label);
  return (
    <Group id="general.model_source" title={title} footer={down ? S('settings.profile.offline') : undefined}>
      <Row id="general.source" detail={p?.kind === 'unset' ? S('settings.provider.unset') : null} warn={p?.kind === 'unset'}>
        <Popup wide label={S('settings.general.source')} value={kind} choices={SOURCES.map((k) => [k, name(k)] as [ProviderKind, string])} dividers={[5, 7, 9]}
          onChange={choose} disabled={!p || busy} />
      </Row>
      {/* Claude Code's login: the models it uses, changeable here (an API source has its model fields below instead) */}
      {kind === 'claude' && p?.kind === 'claude' && (
        <ClaudeModels disabled={busy} onError={(text) => setMsg(text ? { text, warn: true } : null)} />
      )}
      {needsKey && (
        <Row id="general.api_key">
          <input
            className="st-name st-wide"
            type="password"
            value={key}
            placeholder={keyHint}
            autoComplete="off"
            spellCheck={false}
            maxLength={400}
            disabled={!p || busy}
            data-testid="provider-key"
            onChange={(e) => setKey(e.target.value.trim())}
          />
        </Row>
      )}
      {endpoint && <Row id="general.base_url">{field(base, setBase, 200, 'provider-url', S('settings.general.base_url'))}</Row>}
      {endpoint && <Row id="general.fast_model">{modelField(fast, setFast, 'provider-fast', S('settings.general.fast_model'))}</Row>}
      {endpoint && <Row id="general.main_model">{modelField(main, setMain, 'provider-main', S('settings.general.main_model'))}</Row>}
      <Row id="general.provider_check" detail={msg?.text ?? undefined} warn={!!msg?.warn}>
        {api && (
          <button className="primary" disabled={!p || busy || !dirty} onClick={() => void save()} data-testid="provider-save">
            {S('settings.provider.save')}
          </button>
        )}
        <button disabled={!p || busy || dirty} onClick={() => void test()} data-testid="provider-test">
          {S('settings.provider.test')}
        </button>
      </Row>
      {api && (
        <Row id="general.lit">
          <Switch on={p?.lit ?? true} set={(v) => void setLit(v)} label={S('settings.general.lit')} disabled={!p} />
        </Row>
      )}
      {api && showUsage && (
        <Row id="general.usage_month">
          <span className="st-usage" title={usageBreakdown(usage) || undefined} data-testid="provider-usage">
            {usage ? usageLine(usage) : '…'}
          </span>
        </Row>
      )}
    </Group>
  );
}
