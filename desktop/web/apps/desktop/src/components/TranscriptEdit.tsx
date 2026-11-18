// 第 7 批, 回顾 → 编辑: the host fixes a past meeting's transcript by hand (zuhui/transcript_edit.py). With 编辑 on, a
// click on a line opens it in place (Enter saves, Esc puts it back, emptied it goes), a click on a guest's name renames
// them in every line; 替换 changes a word in the whole meeting (and can add the new one to the custom words); 撤销 takes
// the last edit back. The words as said stay on disk untouched. The tools sit at the right end of the transcript's head,
// laid over it: they never change its height; the replace box floats.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { Icon } from '@blurt/icons';
import { isImeKey } from '../ime';
import { t } from '../i18n';
import {
  EDIT_LINE_MAX,
  EDIT_WORD_MAX,
  GUEST_NAME_MAX,
  liveError,
  meetingApi,
  type Edits,
  type Line,
  type MeetingDetail,
} from '../meetingApi';
import { useDismiss } from './useDismiss';
import './transcript-edit.css';

/** what 回顾 gives the transcript to edit it by */
export interface TranscriptEditing {
  /** off while the meeting is on or its post-meeting work runs (the tools show, greyed) */
  can: boolean;
  edits: Edits | null;
  /** a line's new words ('' takes it out); true once saved and fetched again */
  line: (i: number, text: string) => Promise<boolean>;
  /** a guest's new name in every line of theirs */
  rename: (from: string, to: string) => Promise<boolean>;
  /** null when done; else the failure, worded */
  replace: (find: string, repl: string, hot: boolean) => Promise<string | null>;
  undo: () => Promise<void>;
}

const EDIT_ERRORS = ['no_match', 'taken', 'same', 'busy', 'live', 'nothing'];
/** a refusal's code, worded ("没找到", "与会上其他人重名"); the shared ones as the meeting's other actions say them */
export const editError = (e: unknown): string => {
  const c = String((e as Error)?.message ?? e);
  return EDIT_ERRORS.includes(c) ? t(`meeting.tredit.err.${c}`) : liveError(c);
};

/** one spelling of what the backend keeps: runs of spaces as one, no spaces at the ends */
const squeeze = (s: string) => s.replace(/\s+/g, ' ').trim();
const SPEAKS = new Set(['teacher', 'student', 'guest']);
/** how many times `find` is in the meeting's spoken lines (the backend's count, non-overlapping) */
export const countIn = (lines: Line[], find: string) =>
  find ? lines.reduce((n, l) => n + (SPEAKS.has(l.who) ? l.text.split(find).length - 1 : 0), 0) : 0;

/** 回顾's editing for one past meeting: each edit is sent, then the meeting is fetched again */
export function useTranscriptEdit(d: MeetingDetail, onRefresh: (id: string) => unknown, onToast: (msg: string) => void): TranscriptEditing {
  const id = d.meeting.id;
  const can = d.meeting.state !== 'live' && !d.processing;
  const edits = d.edits ?? null;
  const refresh = useRef(onRefresh);
  refresh.current = onRefresh;
  const toast = useRef(onToast);
  toast.current = onToast;
  return useMemo<TranscriptEditing>(() => {
    const send = async (body: Parameters<typeof meetingApi.editTranscript>[1]) => {
      const r = await meetingApi.editTranscript(id, body);
      await refresh.current(id);
      return r;
    };
    const quiet = async (body: Parameters<typeof meetingApi.editTranscript>[1]) => {
      try {
        await send(body);
        return true;
      } catch (e) {
        if (String((e as Error)?.message) === 'same') return true;
        toast.current(t('meeting.tredit.failed', { a: editError(e) }));
        return false;
      }
    };
    return {
      can,
      edits,
      line: (i, text) => quiet({ op: 'line', i, text }),
      rename: (from, to) => quiet({ op: 'speaker', from, to }),
      replace: async (find, repl, hot) => {
        try {
          const r = await send({ op: 'replace', find, repl, hot });
          const n = r.count ?? 0;
          toast.current(
            r.hot === 'added' ? t('meeting.tredit.replaced_hot', { n }) : r.hot === 'full' ? t('meeting.tredit.replaced_full', { n }) : t('meeting.tredit.replaced', { n }),
          );
          return null;
        } catch (e) {
          return t('meeting.tredit.replace_failed', { a: editError(e) });
        }
      },
      undo: async () => {
        await quiet({ op: 'undo' });
      },
    };
  }, [id, can, edits]);
}

// ---------------------------------------------------------------- the head's tools (回顾)
/** 撤销 · 替换 · 编辑/完成, at the right end of the transcript's head */
export function EditTools({ editing, on, onToggle, lines }: {
  editing: TranscriptEditing;
  on: boolean;
  onToggle: () => void;
  lines: Line[];
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useDismiss(open, () => setOpen(false), box);
  useEffect(() => {
    if (!editing.can) setOpen(false);
  }, [editing.can]);
  const last = editing.edits?.last ?? null;
  const undoTip = !last
    ? undefined
    : last.op === 'replace'
      ? t('meeting.tredit.undo_replace', { a: last.find, b: last.repl })
      : last.op === 'speaker'
        ? t('meeting.tredit.undo_speaker', { a: last.from, b: last.to })
        : t('meeting.tredit.undo_line');
  const undo = async () => {
    setBusy(true);
    try {
      await editing.undo();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-trtools" ref={box} data-testid="tredit-tools">
      <button className="mt-trbtn" disabled={!editing.can || !editing.edits?.n || busy} onClick={() => void undo()} data-tip={undoTip} data-testid="tredit-undo">
        <Icon name="undo" size={12} />
        {t('meeting.tredit.undo')}
      </button>
      <button className={`mt-trbtn ${open ? 'is-on' : ''}`} disabled={!editing.can} onClick={() => setOpen((v) => !v)} aria-expanded={open} data-testid="tredit-replace">
        {t('meeting.tredit.replace')}
      </button>
      {/* 编辑 and 完成 share one box as wide as the wider */}
      <button className={`mt-trbtn ${on ? 'is-on' : ''}`} disabled={!editing.can} aria-pressed={on} onClick={onToggle}
        data-tip={editing.can ? undefined : t('meeting.tredit.busy')} data-testid="tredit-toggle">
        <Icon name="pencil" size={12} />
        <span className="mt-wstack">
          <span className={on ? 'is-ghost' : undefined} aria-hidden={on || undefined}>{t('meeting.tredit.edit')}</span>
          <span className={on ? undefined : 'is-ghost'} aria-hidden={!on || undefined}>{t('meeting.tredit.done')}</span>
        </span>
      </button>
      {open && <ReplaceBox lines={lines} onReplace={editing.replace} onClose={() => setOpen(false)} />}
    </div>
  );
}

/** 替换: a word in the whole meeting, how many there are, and optionally the new one into the custom words. Its
 *  message row keeps two lines' room: a failure never moves the buttons */
function ReplaceBox({ lines, onReplace, onClose }: { lines: Line[]; onReplace: TranscriptEditing['replace']; onClose: () => void }) {
  const [find, setFind] = useState('');
  const [repl, setRepl] = useState('');
  const [hot, setHot] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const f = squeeze(find);
  const r = squeeze(repl);
  const n = useMemo(() => countIn(lines, f), [lines, f]);
  const ok = !!f && n > 0 && f !== r && !busy;
  const go = async () => {
    if (!ok) return;
    setBusy(true);
    setErr(null);
    const e = await onReplace(f, r, hot && !!r);
    setBusy(false);
    if (e) setErr(e);
    else onClose();
  };
  const keys = (e: ReactKeyboardEvent) => {
    if (isImeKey(e)) return;
    if (e.key === 'Enter') {
      e.preventDefault();
      void go();
    }
  };
  return (
    <div className="mt-trpop" role="dialog" aria-label={t('meeting.tredit.replace')} data-testid="tredit-pop">
      <label className="mt-trpop-row">
        <span>{t('meeting.tredit.find')}</span>
        <input value={find} maxLength={EDIT_WORD_MAX} autoFocus spellCheck={false} onChange={(e) => (setFind(e.target.value), setErr(null))} onKeyDown={keys} data-testid="tredit-find" />
      </label>
      <label className="mt-trpop-row">
        <span>{t('meeting.tredit.with')}</span>
        <input value={repl} maxLength={EDIT_WORD_MAX} spellCheck={false} onChange={(e) => (setRepl(e.target.value), setErr(null))} onKeyDown={keys} data-testid="tredit-with" />
      </label>
      <label className="mt-trpop-check">
        <input type="checkbox" checked={hot && !!r} disabled={!r} onChange={(e) => setHot(e.target.checked)} data-testid="tredit-hot" />
        <span>{t('meeting.tredit.hot')}</span>
      </label>
      <p className={`mt-trpop-msg ${err ? 'is-err' : ''}`} role={err ? 'alert' : undefined} data-testid="tredit-msg">
        {err ?? (f ? t('meeting.tredit.count', { n }) : '')}
      </p>
      <div className="mt-trpop-foot">
        <button className="mt-trbtn" onClick={onClose}>{t('meeting.tredit.cancel')}</button>
        <button className="mt-trbtn is-primary" disabled={!ok} aria-busy={busy || undefined} onClick={() => void go()} data-testid="tredit-go">
          {t('meeting.tredit.replace_all')}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- editing in place
/** a line's words in a box laid where the line was (same font, same padding): Enter or leaving it saves, Esc puts it
 *  back, Shift+Enter starts a new line in it */
export function LineEditor({ text, onSave, onDone, pick }: {
  text: string;
  onSave: (text: string) => Promise<boolean>;
  onDone: () => void;
  /** the line could be played (its box has the playable line's padding) */
  pick?: boolean;
}) {
  const [v, setV] = useState(text);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const fit = () => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  };
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    fit();
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);
  useLayoutEffect(fit, [v]);
  // saved or put back once; never twice (leaving the box after Enter)
  const done = useRef(false);
  const inflight = useRef(false);
  const save = async () => {
    if (done.current || inflight.current) return;
    if (v.trim() === text.trim()) {
      done.current = true;
      return onDone();
    }
    inflight.current = true;
    setBusy(true);
    const ok = await onSave(v);
    inflight.current = false;
    setBusy(false);
    if (ok) {
      done.current = true;
      onDone();
    }
  };
  return (
    <textarea
      ref={ref}
      className={`mt-line-text mt-line-edit ${pick ? 'is-pick' : ''}`}
      value={v}
      rows={1}
      maxLength={EDIT_LINE_MAX}
      spellCheck={false}
      readOnly={busy}
      aria-busy={busy || undefined}
      aria-label={t('meeting.tredit.line')}
      data-testid="tredit-line"
      onChange={(e) => setV(e.target.value)}
      onBlur={() => void save()}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (isImeKey(e)) return;
        if (e.key === 'Escape') {
          e.preventDefault();
          done.current = true;
          onDone();
        } else if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          void save();
        }
      }}
    />
  );
}

/** a guest's name in a field over where it was: Enter or leaving it saves, Esc puts it back */
export function NameEditor({ name, color, onSave, onDone }: { name: string; color?: string; onSave: (to: string) => Promise<boolean>; onDone: () => void }) {
  const [v, setV] = useState(name);
  const [busy, setBusy] = useState(false);
  const done = useRef(false);
  const inflight = useRef(false);
  const save = async () => {
    if (done.current || inflight.current) return;
    const to = squeeze(v);
    if (!to || to === name) {
      done.current = true;
      return onDone();
    }
    inflight.current = true;
    setBusy(true);
    const ok = await onSave(to);
    inflight.current = false;
    setBusy(false);
    if (ok) {
      done.current = true;
      onDone();
    }
  };
  return (
    <input
      className="mt-name-edit"
      value={v}
      maxLength={GUEST_NAME_MAX}
      autoFocus
      spellCheck={false}
      readOnly={busy}
      aria-busy={busy || undefined}
      style={color ? { color } : undefined}
      aria-label={t('meeting.tredit.rename')}
      data-testid="tredit-name"
      onFocus={(e) => e.currentTarget.select()}
      onChange={(e) => setV(e.target.value)}
      onBlur={() => void save()}
      onKeyDown={(e) => {
        if (isImeKey(e)) return;
        if (e.key === 'Escape') {
          e.preventDefault();
          done.current = true;
          onDone();
        } else if (e.key === 'Enter') {
          e.preventDefault();
          void save();
        }
      }}
    />
  );
}
