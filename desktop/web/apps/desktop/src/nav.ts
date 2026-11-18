// Moving around the main window from inside a page: the lobby's "去设置" and its recent meetings. App.tsx listens for
// NAV_EVENT; a meeting asked for before 回顾 is mounted waits in `pending` until it is.
export const NAV_EVENT = 'bloom-nav';
export type Nav = { to: 'settings'; link?: string } | { to: 'history'; meeting?: string };

let pending: string | null = null;
export const nav = (n: Nav) => {
  if (n.to === 'history' && n.meeting) pending = n.meeting;
  window.dispatchEvent(new CustomEvent<Nav>(NAV_EVENT, { detail: n }));
};
/** the meeting 回顾 should open (once) */
export const takePendingMeeting = (): string | null => {
  const m = pending;
  pending = null;
  return m;
};
