// 第 6 批 · 历史版本 (the paper menu on the meeting page): a paper's saved versions across meetings (before / after each
// meeting, before a restore; zuhui/paper_versions.py). The list on the left, newest first under 现在; the right side is
// the diff page between the picked version and the one in 对比 (现在 by default), drawn by the same diff generator as the
// meeting's own. 恢复到这一版 puts the picked one back (the paper as it is now is saved first); not during a meeting.
// One size: the list scrolls by itself, the buttons and the message line never move.
import { useCallback, useEffect, useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react';
import { Icon } from '@blurt/icons';
import { i18n, t } from '../i18n';
import { meetingApi, type PaperVersion, type PaperVersions } from '../meetingApi';
import { tidy, useMenu } from './Menu';
import './paper-history.css';

const ERR = ['live', 'busy', 'not_found', 'gone', 'failed'];

/** "2026-11-18T10:05:00" → "11-18 10:05" (the app's own clock: these are local times) */
const when = (iso?: string) => (iso ? iso.slice(5, 16).replace('T', ' ') : '');

function kindLabel(v: PaperVersion | null): string {
  if (!v) return '';
  if (v.v === 'cur') return t('paper.history.now');
  return t(`paper.history.kind.${['before', 'after', 'backup', 'restore'].includes(v.kind) ? v.kind : 'after'}`);
}

export function PaperHistory({ paper, name, onClose, onToast }: {
  /** the paper's id (meetings.paper_id) */
  paper: string;
  name: string;
  onClose: () => void;
  onToast?: (msg: string) => void;
}) {
  const [data, setData] = useState<PaperVersions | null>(null);
  const [loadErr, setLoadErr] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [cmp, setCmp] = useState('cur');
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const menu = useMenu();

  const load = useCallback(async (pick?: 'newest') => {
    try {
      const d = await meetingApi.paperVersions(paper);
      setData(d);
      setLoadErr(false);
      setSel((s) => {
        if (s && !pick && (s === 'cur' || d.items.some((x) => x.v === s))) return s;
        const first = d.items.find((x) => !x.same) ?? d.items[0];
        return first ? first.v : 'cur';
      });
    } catch {
      setLoadErr(true);
    }
  }, [paper]);
  useEffect(() => void load(), [load]);

  // Esc closes (the diff page inside has its own keys: Esc there reaches us as {zhKey})
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    const m = (e: MessageEvent) => (e.data as { zhKey?: string } | null)?.zhKey === 'Escape' && onClose();
    window.addEventListener('keydown', k);
    window.addEventListener('message', m);
    return () => {
      window.removeEventListener('keydown', k);
      window.removeEventListener('message', m);
    };
  }, [onClose]);

  const items = data?.items ?? [];
  const now: PaperVersion = useMemo(() => ({ v: 'cur', kind: 'cur', at: '', chars: data?.current?.chars }), [data?.current?.chars]);
  const all = useMemo(() => [now, ...items], [now, items]);
  const find = (v: string | null) => all.find((x) => x.v === v) ?? null;
  /** a restore row: which version it went back to ("开会前 · 11-17 10:00") */
  const ofLabel = (x: PaperVersion) => {
    const o = x.of ? find(x.of) : null;
    return o ? `${kindLabel(o)} · ${when(o.at)}` : '';
  };
  const a = sel ?? 'cur';
  const b = cmp === a ? (a === 'cur' ? items[0]?.v ?? 'cur' : 'cur') : cmp;
  const src = a !== b ? meetingApi.versionDiffUrl(paper, a, b) : '';
  useEffect(() => setReady(false), [src]);
  useEffect(() => {
    setConfirm(false);
    setMsg(null);
  }, [sel]);

  const picked = find(a);
  const canRestore = !!picked && picked.v !== 'cur' && !picked.same && !data?.live && !busy;
  const restore = async () => {
    if (!picked || !canRestore) return;
    if (!confirm) return setConfirm(true);
    setBusy(true);
    setMsg(null);
    try {
      await meetingApi.restoreVersion(paper, picked.v);
      setConfirm(false);
      setCmp('cur');
      await load('newest');
      onToast?.(t('paper.history.restored'));
    } catch (e) {
      const code = String((e as Error)?.message ?? e);
      setMsg(t('paper.history.restore_failed', { a: t(`paper.history.err.${ERR.includes(code) ? code : 'failed'}`) }));
    } finally {
      setBusy(false);
    }
  };

  const openCmp = (e: ReactMouseEvent<HTMLButtonElement>) =>
    menu.open(
      tidy(all.map((x) => ({
        label: x.v === 'cur' ? kindLabel(x) : `${kindLabel(x)} · ${when(x.at)}`,
        checked: x.v === b,
        disabled: x.v === a,
        run: () => setCmp(x.v),
        testId: `ph-cmp-${x.v}`,
      }))),
      { x: 0, y: 0, anchor: e.currentTarget, align: 'start', minWidth: 240 },
      t('paper.history.compare'),
    );

  const lang = i18n().lang;
  const fmtN = (n?: number) => (n == null ? '' : t('paper.history.chars', { n: new Intl.NumberFormat(lang).format(n) }));
  return (
    <div className="ph-veil" onMouseDown={(e) => e.target === e.currentTarget && onClose()} data-testid="paper-history">
      <div className="ph-dlg" role="dialog" aria-modal="true" aria-label={t('paper.history.title')}>
        <header className="ph-head">
          <h2>{t('paper.history.title')}</h2>
          <span className="ph-name" title={name}>{name}</span>
          <button className="ph-x" onClick={onClose} aria-label={t('paper.history.close')} data-testid="ph-close">
            <Icon name="x" size={14} />
          </button>
        </header>
        <div className="ph-body">
          <aside className="ph-side">
            <ul className="ph-list" role="listbox" aria-label={t('paper.history.title')}>
              {all.map((x) => (
                <li key={x.v}>
                  <button
                    className={`ph-row ${x.v === a ? 'is-sel' : ''} ${x.v === b ? 'is-cmp' : ''}`}
                    role="option"
                    aria-selected={x.v === a}
                    onClick={() => setSel(x.v)}
                    data-testid={`ph-row-${x.v}`}
                  >
                    <span className="ph-r1">
                      <span className="ph-kind">{kindLabel(x)}</span>
                      <span className="ph-when">{x.v === 'cur' ? '' : when(x.at)}</span>
                    </span>
                    <span className="ph-r2">
                      <span className="ph-title" title={x.title ?? undefined}>{x.v === 'cur' ? name : x.title || ofLabel(x) || ' '}</span>
                      <span className="ph-n">{fmtN(x.chars)}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <div className="ph-foot">
              <button className="mt-btn ph-cmp" onClick={openCmp} disabled={all.length < 2} aria-haspopup="menu" data-testid="ph-compare">
                <span className="ph-cmp-t">{t('paper.history.compare')}</span>
                <span className="ph-cmp-v">{kindLabel(find(b))}{b !== 'cur' ? ` · ${when(find(b)?.at)}` : ''}</span>
                <Icon name="chevron-down" size={12} />
              </button>
              <button className={`mt-btn ph-restore ${confirm ? 'is-confirm' : ''}`} onClick={() => void restore()} disabled={!canRestore}
                data-testid="ph-restore">
                {/* both labels share one box as wide as the wider: 恢复到这一版 → 确定恢复 never changes its width */}
                <span className="ph-swap">
                  <span className={confirm ? 'is-ghost' : undefined} aria-hidden={confirm || undefined}>{t('paper.history.restore')}</span>
                  <span className={confirm ? undefined : 'is-ghost'} aria-hidden={!confirm || undefined}>{t('paper.history.confirm')}</span>
                </span>
              </button>
              <p className={`ph-msg ${!msg && !loadErr ? 'is-hint' : ''}`} role="status" title={msg ?? undefined}>
                {msg ?? (loadErr ? t('paper.history.load_failed') : data?.live ? t('paper.history.err.live') : '')}
              </p>
            </div>
          </aside>
          <section className="ph-view">
            {src ? (
              <iframe key={src} className="ph-frame" src={src} title={t('paper.history.title')} onLoad={() => setReady(true)} data-testid="ph-diff" />
            ) : (
              <div className="ph-empty">{data && !items.length ? t('paper.history.none') : ''}</div>
            )}
            {src && !ready && (
              <div className="ph-wait" role="status">
                <span className="mw-spinner" aria-hidden="true" />
              </div>
            )}
          </section>
        </div>
      </div>
      {menu.node}
    </div>
  );
}
