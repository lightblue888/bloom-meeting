// Moving around the main window from inside a page: the lobby's "去设置" and its recent meetings. App.tsx listens for
// NAV_EVENT; a meeting asked for before 回顾 is mounted waits in `pending` until it is.
export const NAV_EVENT = 'bloom-nav';
/** history: `meeting` opens it; `at` (seconds into it) also plays it from there and shows that transcript line */
export type Nav = { to: 'settings'; link?: string } | { to: 'history'; meeting?: string; at?: number | null };

let pending: { id: string; at: number | null } | null = null;
export const nav = (n: Nav) => {
  if (n.to === 'history' && n.meeting) pending = { id: n.meeting, at: typeof n.at === 'number' ? n.at : null };
  window.dispatchEvent(new CustomEvent<Nav>(NAV_EVENT, { detail: n }));
};
/** the meeting 回顾 should open (once) */
export const takePendingMeeting = (): string | null => {
  const m = pending;
  pendingAt = m?.at ?? null;
  pending = null;
  return m?.id ?? null;
};
let pendingAt: number | null = null;
/** where in that meeting to go (read right after takePendingMeeting) */
export const takePendingAt = (): number | null => {
  const a = pendingAt;
  pendingAt = null;
  return a;
};
