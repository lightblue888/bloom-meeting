// In the meeting (第 3 批, zuhui/inmeeting.py): what the transcript column holds besides the transcript, live and on the
// guest page alike —
//   the box at the foot       — the host and co-hosts type to the AI (the same as saying it: in the transcript, answered
//                               aloud); 标记 (everyone) marks this moment (a short note can follow).
//                               A participant's box holds 标记 only;
//   laid over the transcript  — the passage selected in the paper (the next words to the AI are about it; it clears
//                               itself).
// Everything that comes and goes floats; the box keeps one height whatever it shows, live or ended.
import { memo, useEffect, useRef, useState } from 'react';
import { Icon } from '@blurt/icons';
import { isImeKey } from '../ime';
import { t } from '../i18n';
import {
  ASK_TEXT_MAX,
  BOOKMARK_NOTE_MAX,
  liveError,
  meetingApi,
  QUIET_S,
  type AnchorSel,
  type Bookmark,
} from '../meetingApi';
import { useDismiss } from './useDismiss';
import './meeting-tools.css';

/** what the page around the meeting gives the tools: who is looking, the live state, how to report back */
export interface LiveTools {
  /** the host (everything), a co-host (types to the AI, points at a passage), a participant (标记 only) */
  role: 'host' | 'cohost' | 'guest';
  /** the guest's own name (whose selection is whose) */
  me?: string | null;
  /** the meeting is on (after it ends everything stays where it was, switched off) */
  live: boolean;
  anchor: AnchorSel | null;
  /** the host's: seconds nobody has spoken */
  quietS?: number | null;
  /** a failure, worded ("发送失败：太长了"): the page shows it where it shows its errors */
  onError: (msg: string) => void;
  /** the host's 散会 (the quiet reminder offers it) */
  onEnd?: (() => void) | null;
  /** something was done: ask the backend again now rather than at the next poll */
  onChanged?: () => void;
}

// ---------------------------------------------------------------- the box at the foot
export const Composer = memo(function Composer({ tools }: { tools: LiveTools }) {
  // typing to the AI: the host and co-hosts; a participant's box is 标记 alone
  const canAsk = tools.role !== 'guest';
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [marking, setMarking] = useState(false);
  const [mark, setMark] = useState<Bookmark | null>(null);
  const live = tools.live;
  const send = async () => {
    const q = text.trim();
    if (!q || sending || !live || !canAsk) return;
    setSending(true);
    try {
      await meetingApi.ask(q);
      setText('');
      tools.onChanged?.();
    } catch (e) {
      tools.onError(t('meeting.live.ask_failed', { a: liveError(e) }));
    } finally {
      setSending(false);
    }
  };
  const doMark = async () => {
    if (!live || marking) return;
    setMarking(true);
    try {
      const r = await meetingApi.bookmark('');
      setMark(r.bookmark);
      tools.onChanged?.();
    } catch (e) {
      tools.onError(t('meeting.live.mark_failed', { a: liveError(e) }));
    } finally {
      setMarking(false);
    }
  };
  return (
    // one row: the question, send, then 标记 (a participant who can't ask the AI gets 标记 alone, at the row's end)
    <div className={`mt-compose ${live ? '' : 'is-off'} ${canAsk ? '' : 'is-mark-only'}`} data-testid="composer">
      <div className="mt-compose-row">
      {canAsk ? (
        <>
        <textarea
          className="mt-compose-in"
          rows={1}
          value={text}
          maxLength={ASK_TEXT_MAX}
          placeholder={t('meeting.live.ask_ph')}
          aria-label={t('meeting.live.ask_ph')}
          disabled={!live}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !isImeKey(e)) {
              e.preventDefault();
              void send();
            }
          }}
          data-testid="ask-input"
        />
        <button className="mt-compose-send" onClick={() => void send()} disabled={!live || sending || !text.trim()} aria-label={t('meeting.live.send')}
          data-tip={t('meeting.live.send')} data-testid="ask-send">
          {sending ? <span className="mw-spinner sm" aria-hidden="true" /> : <Icon name="send" size={14} />}
        </button>
        </>
      ) : (
        <span className="mt-compose-gap" />
      )}
        <span className="mt-compose-mark">
          <button className="mt-tbtn" onClick={() => void doMark()} disabled={!live || marking} data-testid="bookmark">
            <Icon name="flag" size={13} />
            {t('meeting.live.mark')}
          </button>
          {mark && <MarkNote mark={mark} onClose={() => setMark(null)} onError={tools.onError} onChanged={tools.onChanged} />}
        </span>
      </div>
    </div>
  );
});

/** right after 标记: "已标记 12:34" and a field for a short note (saved on Enter or 保存; closing keeps the mark) */
function MarkNote({ mark, onClose, onError, onChanged }: { mark: Bookmark; onClose: () => void; onError: (m: string) => void; onChanged?: () => void }) {
  const [note, setNote] = useState(mark.note ?? '');
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useDismiss(true, onClose, box);
  useEffect(() => {
    box.current?.querySelector('input')?.focus();
  }, []);
  const save = async () => {
    if (busy) return;
    if (note.trim() === (mark.note ?? '')) return onClose();
    setBusy(true);
    try {
      await meetingApi.bookmark(note.trim(), mark.id);
      onChanged?.();
      onClose();
    } catch (e) {
      onError(t('meeting.live.mark_failed', { a: liveError(e) }));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-marknote" ref={box} role="dialog" aria-label={t('meeting.live.marked', { t: mark.t })} data-testid="bookmark-note">
      <span className="mt-marknote-h">
        <Icon name="check" size={12} />
        {t('meeting.live.marked', { t: mark.t })}
      </span>
      <span className="mt-marknote-row">
        <input value={note} maxLength={BOOKMARK_NOTE_MAX} placeholder={t('meeting.live.mark_note_ph')} aria-label={t('meeting.live.mark_note_ph')}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !isImeKey(e)) {
              e.preventDefault();
              void save();
            }
          }}
          data-testid="bookmark-note-input" />
        <button className="mt-tbtn is-primary" onClick={() => void save()} disabled={busy} data-testid="bookmark-note-save">{t('meeting.live.save')}</button>
      </span>
    </div>
  );
}

// ---------------------------------------------------------------- laid over the transcript
/** the selected passage, over the transcript's foot (the host sees any, a co-host their own) */
export const ToolsFloat = memo(function ToolsFloat({ tools }: { tools: LiveTools }) {
  const host = tools.role === 'host';
  const a = tools.anchor;
  const mine = !!a && (host || (!!a.by && a.by === tools.me));
  const clearAnchor = async () => {
    try {
      await meetingApi.anchor(null);
      tools.onChanged?.();
    } catch (e) {
      tools.onError(t('meeting.live.anchor_failed', { a: liveError(e) }));
    }
  };
  if (!mine || !a) return null;
  return (
    <div className="mt-float">
      <div className="mt-anchor" role="dialog" aria-label={t('meeting.live.anchor')} aria-live="polite" data-testid="anchor-chip">
        <Icon name="quote" size={13} />
        <span className="mt-anchor-k">{t('meeting.live.anchor')}</span>
        <span className="mt-anchor-t" title={a.text}>{a.text}</span>
        <button className="mt-anchor-x" onClick={() => void clearAnchor()} aria-label={t('meeting.live.anchor_clear')} data-tip={t('meeting.live.anchor_clear')}
          data-testid="anchor-clear">
          <Icon name="close" size={11} />
        </button>
      </div>
    </div>
  );
});

/** the host's reminder: nobody has spoken for 10 minutes (散会 right there; nothing ends by itself). Closed, it stays
 *  away until someone speaks and the quiet starts again */
export function QuietNote({ tools }: { tools: LiveTools }) {
  const q = tools.quietS ?? 0;
  const [shut, setShut] = useState(false);
  useEffect(() => {
    if (q < QUIET_S) setShut(false);
  }, [q]);
  if (tools.role !== 'host' || !tools.live || q < QUIET_S || shut) return null;
  return (
    <div className="mt-quiet" role="dialog" aria-label={t('meeting.live.quiet', { n: String(Math.floor(q / 60)) })} aria-live="polite" data-testid="quiet-note">
      <Icon name="info" size={13} />
      <span>{t('meeting.live.quiet', { n: String(Math.floor(q / 60)) })}</span>
      {tools.onEnd && (
        <button className="mt-quiet-end" onClick={tools.onEnd} data-testid="quiet-end">
          {t('meeting.action.end')}
        </button>
      )}
      <button className="mt-quiet-x" onClick={() => setShut(true)} data-testid="quiet-close">{t('meeting.live.close')}</button>
    </div>
  );
}
