// Settings → Storage: what Bloom keeps on this PC, in Bloom's terms. The lead is the total and the disk's free space,
// then one thin bar split into recordings / models / meeting records / other with its legend. Sections:
//   录屏        meeting recordings (video and audio; transcripts, screenshots, summaries and revision history stay),
//               recordings no meeting points at, and automatic deletion (asks first when it would take something now)
//   模型        each model Bloom really uses, by what it is for; the ones nobody uses as one row
//   会议记录    the meetings' records and the imported papers
//   缓存和日志  the voice cache and temporary files (the temporary part can be cleared), the logs
// Every row: an icon, a title, one line, ⋯ (also a right-click) and the size; its actions live in that menu. Moving
// to the Recycle Bin is Windows' own undo. Sizes and clean-ups come from the meeting backend (zuhui/storage.py via
// storageApi.ts); which Blurt-side speech model the app's speech settings use comes from `transcribe.py models`.
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Icon, type IconName } from '@blurt/icons';
import { api, type AppStatus, type AsrModel, type Settings } from '../api';
import { i18n } from '../i18n';
import { Group, Page, Popup, Row, S } from '../settings/kit';
import { useMenu, type MenuAt, type MenuEntry } from './Menu';
import { storageApi, type Retention, type StorageInfo, type UsedModel } from '../storageApi';
import { cleanTitle, fmtWhen } from '../meetingApi';
import './storage.css';

/** "412 MB", "1.6 GB" — one formatter for every row (decimal units, like File Explorer's "size on disk" wording). */
export function size(bytes: number): string {
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = Math.max(0, bytes) / 1000;
  let u = 0;
  while (v >= 1000 && u < units.length - 1) {
    v /= 1000;
    u++;
  }
  return `${i18n().formatNumber(v, { maximumFractionDigits: v >= 100 || u === 0 ? 0 : 1 })} ${units[u]}`;
}
const P = (key: string, n: number) => i18n().plural(`st.settings.storage.n.${key}`, n);
const AGES = [30, 90, 180];
/** 第 8 批: how long old meetings keep their transcripts and notes */
const RET_AGES = [30, 90, 180, 365];
const norm =(p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();

type Part = 'recordings' | 'models' | 'records' | 'other';
const PARTS: { key: Part; label: string }[] = [
  { key: 'recordings', label: 'settings.storage.recordings' },
  { key: 'models', label: 'settings.storage.models' },
  { key: 'records', label: 'settings.storage.records' },
  { key: 'other', label: 'settings.storage.other' },
];
const ROLE_ICON: Record<UsedModel['role'], IconName> = { live: 'captions', after: 'waveform', addr: 'character-bubble', voice: 'voice', turn: 'listening', asr: 'waveform' };

/** The split under the lead: an 8 px bar (no text, a 2 px gap between parts) and its legend, names and sizes. */
function Split({ info }: { info: StorageInfo | null }) {
  const total = info ? PARTS.reduce((a, p) => a + info.split[p.key], 0) : 0;
  const pct = (b: number) => (total ? `${i18n().formatNumber((b / total) * 100, { maximumFractionDigits: b / total < 0.1 ? 1 : 0 })}%` : '');
  const parts = PARTS.map((p) => ({ ...p, name: S(p.label), bytes: info?.split[p.key] ?? 0 }));
  return (
    <div className="sto-split">
      <div className="sto-bar" role="img" aria-label={info ? parts.map((p) => S('settings.storage.part', p.name, size(p.bytes), pct(p.bytes))).join(i18n().lang === 'zh-Hans' ? '；' : '; ') : S('settings.storage.counting')}>
        {parts.filter((p) => p.bytes > 0).map((p) => (
          <span key={p.key} className={`sto-seg sto-${p.key}`} style={{ flexGrow: p.bytes }} data-tip={S('settings.storage.part', p.name, size(p.bytes), pct(p.bytes))} />
        ))}
      </div>
      <ul className="sto-legend">
        {parts.map((p) => (
          <li key={p.key}>
            <i className={`sto-dot sto-${p.key}`} />
            <span>{p.name}</span>
            <span className="sto-legend-n">{info ? size(p.bytes) : '—'}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** A row: icon, title and one line (both cut with … when long, the whole text on hover), ⋯ and the size. */
function Item({ icon, title, detail, bytes, actions, onMenu, flash }: {
  icon: IconName;
  title: string;
  detail: string;
  bytes: number;
  actions: MenuEntry[];
  onMenu: (entries: MenuEntry[], at: MenuAt, label: string) => void;
  flash?: string;
}) {
  const more = useRef<HTMLButtonElement>(null);
  return (
    <div className="st-row sto-row" data-setting={flash}
      onContextMenu={(e) => {
        if (!actions.length) return;
        e.preventDefault();
        onMenu(actions, { x: e.clientX, y: e.clientY }, title);
      }}>
      <span className="st-icon"><Icon name={icon} size={16} /></span>
      <div className="st-text">
        <div className="st-row-title"><span className="sto-cut" data-tip={title} data-tip-trunc>{title}</span></div>
        <div className="st-detail sto-cut" data-tip={detail} data-tip-trunc>{detail}</div>
      </div>
      <div className="st-ctl">
        <button ref={more} type="button" className="icon-btn sto-more" aria-label={`${S('settings.storage.more')}: ${title}`} disabled={!actions.length}
          onClick={() => more.current && onMenu(actions, { x: 0, y: 0, anchor: more.current, align: 'end' }, title)}>
          <Icon name="more" size={14} />
        </button>
        <span className="st-size">{size(bytes)}</span>
      </div>
    </div>
  );
}

export function StoragePane({ s, save, onToast }: { status: AppStatus; s: Settings; save: (patch: Record<string, unknown>) => Promise<void>; onToast: (m: string) => void }) {
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [failed, setFailed] = useState(false);
  const [asr, setAsr] = useState<AsrModel[] | null>(null);
  const [ask, setAsk] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  // rows that come and go with the data (recordings with no meeting, unused models) are decided on the first scan,
  // so cleaning them up leaves the row in place (reading "None") instead of pulling the rows below it up
  const shown = useRef<{ others: boolean; unused: boolean } | null>(null);
  const menu = useMenu();

  const load = useCallback(
    () =>
      storageApi.get().then(
        (r) => {
          shown.current ??= { others: r.recordings.others.count > 0, unused: r.models.unused.length > 0 };
          setInfo(r);
          setFailed(false);
        },
        () => setFailed(true),
      ),
    [],
  );
  useEffect(() => {
    load();
    api.asrModels().then((r) => setAsr(r.models ?? []), () => setAsr([]));
  }, [load]);

  const run = async (job: () => Promise<string>) => {
    setBusy(true);
    try {
      onToast(await job());
    } catch (e) {
      onToast(S('settings.storage.failed', String((e as Error)?.message ?? e)));
    }
    setBusy(false);
    load();
  };
  const show = (p: string | null | undefined, folder = true) => p && (folder ? storageApi.open(p) : storageApi.reveal(p)).catch((e) => onToast(String(e)));
  const trashed = (what: string, bytes: number) => (bytes || what ? S('settings.storage.trashed', what, size(bytes)) : S('settings.storage.trash_none'));

  // the app's speech settings may use another model on the Blurt side: whatever `transcribe.py models` says has a job
  // stays out of "unused" and is listed as speech recognition
  const asrUsed = (asr ?? []).filter((m) => typeof m.path === 'string' && Array.isArray(m.purposes) && (m.purposes as unknown[]).length > 0);
  const isAsrUsed = (p: string) => asrUsed.some((m) => norm(String(m.path)) === norm(p));
  const used: UsedModel[] = info ? [...info.models.used] : [];
  const unused = (info?.models.unused ?? []).filter((u) => !isAsrUsed(u.path));
  for (const u of (info?.models.unused ?? []).filter((x) => isAsrUsed(x.path))) {
    const m = asrUsed.find((a) => norm(String(a.path)) === norm(u.path));
    used.push({ role: 'asr', name: String(m?.name ?? u.name), paths: [u.path], bytes: u.bytes, langs: [] });
  }
  const unusedBytes = unused.reduce((a, b) => a + b.bytes, 0);

  const rec = info?.recordings;
  const preview = (d: number) => rec?.auto_preview[String(d)] ?? { count: 0, bytes: 0 };
  const setAuto = async (d: number) => {
    if (d && preview(d).count && ask !== d) return setAsk(d);
    setAsk(null);
    // Blurt's own "delete videos automatically" (organized takes only) is gone from this page: this one covers them
    const old = s.config?.record?.delete_videos_after_days;
    if (old) save({ record: { ...(s.config.record ?? {}), delete_videos_after_days: null } }).catch(() => {});
    await run(async () => {
      const r = await storageApi.setAuto(d);
      return r.count ? trashed(P('recordings', r.count), r.bytes) : d ? S('settings.storage.auto_set', d) : S('settings.storage.auto_off');
    });
  };

  // 第 8 批: turning on deleting old transcripts and notes lists the meetings it would take now and asks first
  const ret = info?.records.retention;
  const [askRet, setAskRet] = useState<number | null>(null);
  const retPreview = (d: number): Retention['preview'][string] => ret?.preview[String(d)] ?? { count: 0, bytes: 0, meetings: [] };
  const setRetention = async (d: number, sure = false) => {
    if (d && retPreview(d).count && !sure) return setAskRet(d);
    setAskRet(null);
    await run(async () => {
      const r = await storageApi.setRetention(d);
      return r.count ? S('settings.storage.records.done', P('meetings', r.count)) : d ? S('settings.storage.records.set', d) : S('settings.storage.records.off');
    });
  };

  const recActions = (): MenuEntry[] => {
    if (!rec) return [];
    const by = (k: string) => rec.older[k] ?? { count: 0, bytes: 0 };
    const item = (k: string, label: string, days: number | null): MenuEntry => ({
      label,
      keys: by(k).count ? size(by(k).bytes) : undefined,
      disabled: busy || !by(k).count,
      run: () => run(async () => {
        const r = await storageApi.trashRecordings(days, 'meetings');
        return trashed(r.count ? P('recordings', r.count) : '', r.bytes);
      }),
    });
    return [
      ...(rec.folder ? [{ label: S('settings.storage.show'), run: () => show(rec.folder) } as MenuEntry, { kind: 'separator' } as MenuEntry] : []),
      { kind: 'header', label: S('settings.storage.trash') },
      ...AGES.map((d) => item(String(d), S('settings.storage.older', d), d)),
      item('all', S('settings.storage.all'), null),
    ];
  };

  const lead: ReactNode = failed ? (
    <span className="sto-warn"><Icon name="warning" size={12} />{S('settings.storage.offline')}</span>
  ) : info ? (
    <span className="st-lead-split">
      <span className="st-num">{S('settings.storage.uses', size(info.total))}</span>
      {typeof info.free === 'number' && <span className="st-dim st-num">{S('settings.storage.available', size(info.free))}</span>}
    </span>
  ) : (
    S('settings.storage.counting')
  );
  const acc = (b: number | undefined) => (info ? <span className="st-size">{size(b ?? 0)}</span> : undefined);

  return (
    <Page id="storage" lead={lead}>
      <Split info={info} />
      {info && rec && (
        <>
          <Group id="storage.recordings" accessory={acc(info.split.recordings)}>
            <Item icon="video" title={S('settings.storage.rec.meetings')} bytes={rec.bytes} onMenu={menu.open}
              detail={rec.meetings ? P('meetings', rec.meetings) : S('settings.storage.none')} actions={recActions()} />
            {shown.current?.others && (
              <Item icon="film" title={S('settings.storage.rec.others')} bytes={rec.others.bytes} onMenu={menu.open}
                detail={rec.others.count ? P('clips', rec.others.count) : S('settings.storage.none')}
                actions={[
                  ...(rec.folder ? [{ label: S('settings.storage.show'), run: () => show(rec.folder) } as MenuEntry, { kind: 'separator' } as MenuEntry] : []),
                  {
                    label: S('settings.storage.trash'),
                    keys: rec.others.ready.count ? size(rec.others.ready.bytes) : undefined,
                    disabled: busy || !rec.others.ready.count,
                    run: () => run(async () => {
                      const r = await storageApi.trashRecordings(null, 'others');
                      return trashed(r.count ? P('recordings', r.count) : '', r.bytes);
                    }),
                  },
                ]} />
            )}
            <Row id="storage.auto_delete" icon="history" warn={!!ask}
              detail={ask ? S('settings.storage.auto.confirm', P('recordings', preview(ask).count), size(preview(ask).bytes)) : undefined}>
              {ask ? (
                <span className="sto-confirm">
                  <button type="button" onClick={() => setAsk(null)}>{S('action.cancel')}</button>
                  <button type="button" className="danger" disabled={busy} onClick={() => setAuto(ask)}>{S('settings.storage.turn_on')}</button>
                </span>
              ) : (
                <Popup label={S('settings.storage.rec.auto')} value={rec.auto_days} dividers={[1]} disabled={busy}
                  choices={[[0, S('settings.storage.never')], ...AGES.map((d) => [d, S('settings.storage.after', d)] as [number, string])]} onChange={setAuto} />
              )}
            </Row>
          </Group>

          <Group id="storage.models" accessory={acc(info.split.models)}>
            {used.map((m) => (
              <Item key={m.role + m.paths[0]} icon={ROLE_ICON[m.role]} title={S(`settings.storage.model.${m.role}`)} bytes={m.bytes} onMenu={menu.open}
                detail={m.role === 'addr' && m.langs.length ? i18n().formatList(m.langs.map((l) => S(`settings.storage.lang.${l === 'en' ? 'en' : 'zh'}`))) : m.name}
                actions={[{ label: S('settings.storage.show'), run: () => show(m.paths[0], false) }]} />
            ))}
            {shown.current?.unused && (
              <Item icon="archive" title={S('settings.storage.model.unused')} bytes={unusedBytes} onMenu={menu.open}
                detail={unused.length ? unused.map((u) => u.name).join(i18n().lang === 'zh-Hans' ? '、' : ', ') : S('settings.storage.none')}
                actions={unused.length ? [
                  { label: S('settings.storage.show'), run: () => show(unused[0].path, false) },
                  { kind: 'separator' },
                  {
                    label: S('settings.storage.trash'),
                    keys: size(unusedBytes),
                    disabled: busy || asr === null,
                    run: () => run(async () => {
                      const r = await storageApi.trashModels(unused.map((u) => u.path));
                      return trashed(r.count ? P('models', r.count) : '', r.bytes);
                    }),
                  },
                ] : []} />
            )}
          </Group>

          <Group id="storage.records" accessory={acc(info.split.records)}>
            <Item icon="list-box" title={S('settings.storage.meetings')} bytes={info.records.meetings.bytes} onMenu={menu.open}
              detail={info.records.meetings.count ? P('meeting_records', info.records.meetings.count) : S('settings.storage.none')}
              actions={[{ label: S('settings.storage.show'), run: () => show(info.records.meetings.path) }]} />
            <Item icon="document" title={S('settings.storage.papers')} bytes={info.records.papers.bytes} onMenu={menu.open}
              detail={info.records.papers.count ? P('papers', info.records.papers.count) : S('settings.storage.none')}
              actions={[{ label: S('settings.storage.show'), run: () => show(info.records.papers.path) }]} />
            {ret && (
              <Row id="storage.records_auto" icon="history">
                <Popup label={S('settings.storage.records.auto')} value={ret.days} dividers={[1]} disabled={busy}
                  choices={[[0, S('settings.storage.never')], ...RET_AGES.map((d) => [d, S('settings.storage.after', d)] as [number, string])]}
                  onChange={(d) => void setRetention(d)} />
              </Row>
            )}
          </Group>

          <Group id="storage.cleanup" accessory={acc(info.caches.bytes + info.logs.bytes)}>
            <Item icon="sync" title={S('settings.storage.caches')} detail={S('settings.storage.caches.detail')} bytes={info.caches.bytes} onMenu={menu.open}
              actions={[
                { label: S('settings.storage.show'), run: () => show(info.caches.path) },
                { kind: 'separator' },
                {
                  label: S('settings.storage.clear'),
                  keys: info.caches.clearable ? size(info.caches.clearable) : undefined,
                  disabled: busy || !info.caches.clearable,
                  run: () => run(async () => S('settings.storage.cleared', size((await storageApi.clearCaches()).bytes))),
                },
              ]} />
            <Item icon="lines" title={S('settings.storage.logs')} detail="" bytes={info.logs.bytes} onMenu={menu.open}
              actions={[{ label: S('settings.storage.show'), run: () => show(info.logs.path) }]} />
          </Group>
        </>
      )}
      {menu.node}
      {askRet !== null && (
        <RetentionAsk days={askRet} p={retPreview(askRet)} busy={busy} onCancel={() => setAskRet(null)} onOk={() => void setRetention(askRet, true)} />
      )}
    </Page>
  );
}

/** 第 8 批: before old transcripts and notes go, the meetings they belong to (a dialog of one size, the list scrolls) */
function RetentionAsk({ days, p, busy, onCancel, onOk }: {
  days: number;
  p: Retention['preview'][string];
  busy: boolean;
  onCancel: () => void;
  onOk: () => void;
}) {
  const lang = i18n().lang;
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    box.current?.querySelector<HTMLButtonElement>('.sto-dlg-no')?.focus();
    const k = (e: KeyboardEvent) => e.key === 'Escape' && onCancel();
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onCancel]);
  const more = p.count - p.meetings.length;
  return (
    <div className="sto-dlg-veil" onMouseDown={(e) => e.target === e.currentTarget && onCancel()}>
      <div className="sto-dlg" role="dialog" aria-modal="true" aria-label={S('settings.storage.records.auto')} ref={box} data-testid="retention-ask">
        <header className="sto-dlg-head">
          <h2>{S('settings.storage.records.auto')}</h2>
          <span className="st-dim">{S('settings.storage.after', days)}</span>
        </header>
        <p className="sto-dlg-line">{S('settings.storage.records.ask', P('meetings', p.count))}</p>
        <ul className="sto-dlg-list" data-testid="retention-list">
          {p.meetings.map((m) => {
            const name = cleanTitle(m.title) || m.id;
            return (
              <li key={m.id}>
                <span className="sto-cut" data-tip={name} data-tip-trunc>{name}</span>
                <span className="sto-dlg-when">{fmtWhen(m.started, lang)}</span>
              </li>
            );
          })}
          {more > 0 && <li className="sto-dlg-more">{S('settings.storage.records.ask_more', more)}</li>}
        </ul>
        <p className="sto-dlg-keep">{S('settings.storage.records.auto.detail')}</p>
        <footer className="sto-dlg-foot">
          <button type="button" className="sto-dlg-no" onClick={onCancel}>{S('action.cancel')}</button>
          <button type="button" className="danger" disabled={busy} onClick={onOk} data-testid="retention-ok">{S('settings.storage.turn_on')}</button>
        </footer>
      </div>
    </div>
  );
}
