// Swapping in a new version of the paper's page (the meeting page and the guest page keep two iframe slots: the new
// version loads in the hidden one). 2026-11-21: the paper jumped back while you were reading, or while you scrolled as
// an edit landed. Before the swap the page on screen is asked where it is (the paragraph at the top, how far into it)
// and whether you are busy in it (scrolling, holding the mouse, a selection, a comment being written, a paragraph being
// edited); the new page is put on that same paragraph, and only then shown. Busy: ask again a moment later.
//   shell → old page   {zhReadAsk: id}
//   old page → shell   {zhRead: {id, at, busy}}       at: zuhui/view.py zhReading(), with the paper's id
//   shell → new page   {zhGo: at, id}
//   new page → shell   {zhGone: id}
// A page that doesn't answer (an older backend) is swapped as before.

/** the part of `window` used here (tests pass their own) */
export interface SwapHost {
  addEventListener(type: 'message', fn: (e: MessageEvent) => void): void;
  removeEventListener(type: 'message', fn: (e: MessageEvent) => void): void;
  setTimeout(fn: () => void, ms: number): number;
  clearTimeout(id: number): void;
}

/** how long a page gets to answer */
export const ANSWER_MS = 300;
/** you are busy in the old page: ask again after this */
export const BUSY_MS = 300;
/** the new page didn't say it is in place (still loading): try again this many times, then swap anyway */
const GO_TRIES = 8;

interface ReadMsg {
  zhRead?: { id?: string; at?: unknown; busy?: string | boolean };
  zhGone?: string;
}

let seq = 0;

/** Ask `oldWin` where it is, put `newWin` there, then `swap()`. Returns a cancel function (unmount, a new paper). */
export function handOff(oldWin: Window | null | undefined, newWin: Window | null | undefined, swap: () => void, host: SwapHost = window as unknown as SwapHost): () => void {
  const id = `s${Date.now().toString(36)}${(seq += 1)}`;
  let timer = 0;
  let done = false;
  let tries = 0;
  const stop = () => {
    done = true;
    host.clearTimeout(timer);
    host.removeEventListener('message', on);
  };
  const finish = () => {
    if (done) return;
    stop();
    swap();
  };
  const ask = () => {
    if (done) return;
    if (!oldWin) return finish();
    try {
      oldWin.postMessage({ zhReadAsk: id }, '*');
    } catch {
      return finish();
    }
    host.clearTimeout(timer);
    timer = host.setTimeout(finish, ANSWER_MS);
  };
  const on = (e: MessageEvent) => {
    if (done) return;
    const d = (e.data ?? null) as ReadMsg | null;
    if (d?.zhRead && d.zhRead.id === id && e.source === oldWin) {
      host.clearTimeout(timer);
      if (d.zhRead.busy) {
        timer = host.setTimeout(ask, BUSY_MS);
        return;
      }
      if (!newWin || !d.zhRead.at) return finish();
      try {
        newWin.postMessage({ zhGo: d.zhRead.at, id }, '*');
      } catch {
        return finish();
      }
      // no word back: the new page is still loading (it reloaded itself for a newer version) — ask again
      timer = host.setTimeout(() => ((tries += 1) >= GO_TRIES ? finish() : ask()), ANSWER_MS);
    } else if (d?.zhGone === id && e.source === newWin) {
      finish();
    }
  };
  host.addEventListener('message', on);
  ask();
  return stop;
}
