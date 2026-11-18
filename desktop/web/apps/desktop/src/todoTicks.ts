// 回顾 → 修改清单: a tick is saved with the meeting (its carry.json, key "note:<task id>"), the same file the lobby's
// left-overs read (zuhui/past.py). Before 第 3 批 the ticks lived in this browser (localStorage "bloom-todos-<id>"):
// moved to carry.json once, then the old copy goes.

/** the browser's old key for a meeting's ticks */
export const todoKey = (meetingId: string | null) => `bloom-todos-${meetingId ?? ''}`;

/** the old ticks not in carry.json yet (to save once); anything that isn't a task id is dropped */
export function ticksToMove(local: unknown, done: readonly string[]): string[] {
  const ids = Array.isArray(local) ? local.filter((x): x is string => typeof x === 'string' && /^[A-Za-z0-9_.-]{1,40}$/.test(x)) : [];
  return [...new Set(ids)].filter((id) => !done.includes(`note:${id}`));
}
