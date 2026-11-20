// 第 7 批, 回顾 → 编辑: the host fixes a past meeting's transcript by hand (zuhui/transcript_edit.py). With 编辑 on, a
// click on a line opens it in place (Enter saves, Esc puts it back, emptied it goes), a click on a guest's name renames
// them in every line; 撤销 takes the last edit back. The words as said stay on disk untouched. The tools sit at the right
// end of the transcript's head, laid over it: they never change its height. (替换, a word in the whole meeting, was taken
// out on 2026-11-19: "不需要保留 没意义")
import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '@blurt/icons';
import { isImeKey } from '../ime';
import { t } from '../i18n';
import {
  EDIT_LINE_MAX,
  GUEST_NAME_MAX,
  liveError,
  meetingApi,
  type Edits,
  type MeetingDetail,
} from '../meetingApi';
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
      undo: async () => {
        await quiet({ op: 'undo' });
      },
    };
  }, [id, can, edits]);
}

// ---------------------------------------------------------------- the head's tools (回顾)
/** 撤销 · 编辑/完成, at the right end of the transcript's head */
export function EditTools({ editing, on, onToggle }: {
  editing: TranscriptEditing;
  on: boolean;
  onToggle: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const last = editing.edits?.last ?? null;
  const undoTip = !last
    ? undefined
    : last.op === 'replace'                 // a replace made before 替换 was taken out can still be undone
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
  // the tips sit on a box round each button: a greyed (disabled) button takes no hover, the box does
  const busyTip = editing.can ? undefined : t('meeting.tredit.busy');
  return (
    <div className="mt-trtools" data-testid="tredit-tools">
      <span className="mt-trtip" data-tip={busyTip ?? undoTip}>
        <button className="mt-trbtn" disabled={!editing.can || !editing.edits?.n || busy} onClick={() => void undo()} data-testid="tredit-undo">
          <Icon name="undo" size={12} />
          {t('meeting.tredit.undo')}
        </button>
      </span>
      {/* 编辑 and 完成 share one box as wide as the wider */}
      <span className="mt-trtip" data-tip={busyTip}>
        <button className={`mt-trbtn ${on ? 'is-on' : ''}`} disabled={!editing.can} aria-pressed={on} onClick={onToggle}
          data-testid="tredit-toggle">
          <Icon name="pencil" size={12} />
          <span className="mt-wstack">
            <span className={on ? 'is-ghost' : undefined} aria-hidden={on || undefined}>{t('meeting.tredit.edit')}</span>
            <span className={on ? undefined : 'is-ghost'} aria-hidden={!on || undefined}>{t('meeting.tredit.done')}</span>
          </span>
        </button>
      </span>
    </div>
  );
}

// ---------------------------------------------------------------- editing in place
/** a line's words in a field laid over the line itself: the paragraph stays (its words drawn again unseen give it its
 *  size, so it grows as you type), the field covers it with the same padding and font, so not one word moves. Enter or
 *  leaving it saves, Esc puts it back, Shift+Enter starts a new line in it */
export function LineEditor({ text, onSave, onDone, pick, typed }: {
  text: string;
  onSave: (text: string) => Promise<boolean>;
  onDone: () => void;
  /** the line could be played (its box has the playable line's padding) */
  pick?: boolean;
  /** the line was typed, not said: its "打字" tag stays in front of the words (the field starts after it) */
  typed?: string;
}) {
  const [v, setV] = useState(text);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const tag = useRef<HTMLSpanElement>(null);
  // the field's first line starts where the words started (after the tag)
  const [indent, setIndent] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const g = tag.current;
    if (g) {
      const cs = getComputedStyle(el);
      const left = el.getBoundingClientRect().left + parseFloat(cs.paddingLeft);
      setIndent(g.getBoundingClientRect().right + parseFloat(getComputedStyle(g).marginRight) - left);
    }
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);
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
    <p className={`mt-line-text is-editable is-editing ${pick ? 'is-pick' : ''}`}>
      {/* a last empty line (Shift+Enter) or no words at all still takes a line's height */}
      <span className="mt-line-mirror" aria-hidden="true">
        {typed && <span className="mt-typed" ref={tag}>{typed}</span>}
        {!v || v.endsWith('\n') ? `${v}​` : v}
      </span>
      <textarea
        ref={ref}
        className="mt-line-edit"
        style={indent ? { textIndent: indent } : undefined}
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
    </p>
  );
}

/** a guest's name in a field in the name button's own box (transcript-edit.css): the name doesn't move. Enter or
 *  leaving it saves, Esc puts it back */
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
