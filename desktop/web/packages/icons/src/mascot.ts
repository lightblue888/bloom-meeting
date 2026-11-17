/* @blurt/icons/mascot — the mascot, for the web apps (the Windows / Linux overlays, the Tauri app). In Bloom it is the
   flower (BloomMark in apps/desktop/src/components/MeetingLayout.tsx: five petals, the centre), drawn by mascot.css
   from one inline SVG wherever Blurt showed Peep, the clay chick: the island's ears and toasts, the recording
   countdown, the voice note, the app window's welcome and empty states. The clip names stay Peep's, so every call site
   keeps working; each clip is a motion of the flower:

     idle       still
     listening  a slow breath (1 → 1.04 and back, 3.2 s)
     talking    with `level`: it grows with the voice (1 → 1.12); without: a soft beat of its own
     thinking   a slow turn (one round in 6 s)
     hello      it opens: from small, with a little twist, past its size and back (0.7 s)
     done       a pop (1 → 1.18 → 1, 0.5 s)
     error      a short shake (a twist each way, dying out, 0.6 s)

     mascotHtml('talking', { size: 24, level: true })     plain pages (the island); setMascotLevel(() => 0…1)
     <Mascot clip="hello" then="idle" size={108} />       React ("@blurt/icons")

   One small ticker (requestAnimationFrame at 30 fps; it stops when nothing moves) sets each `.mascot`'s --bloom-scale
   and --bloom-turn; mascot.css turns and sizes the flower by them. No sprite sheets. Reduced motion (the system's, or
   <html data-reduce-motion="1">): the flower stands still, a one-shot's `then` follows at once. The 1A round chick's
   clip names still work: happy → done, hatching → done, wobble → thinking, peck → done. */

export const PEEP_STATES = ["idle", "hello", "listening", "talking", "thinking", "done", "error"] as const;
export type PeepState = (typeof PEEP_STATES)[number];
/** The 1A chick's clip names, mapped to the state played in their place. */
export const LEGACY_CLIPS = { happy: "done", hatching: "done", wobble: "thinking", peck: "done" } as const satisfies Record<string, PeepState>;
export type MascotClip = PeepState | keyof typeof LEGACY_CLIPS;
export const MASCOT_CLIPS = [...PEEP_STATES, ...Object.keys(LEGACY_CLIPS)] as MascotClip[];

export function peepState(clip: string | undefined): PeepState {
  if (!clip) return "idle";
  if ((PEEP_STATES as readonly string[]).includes(clip)) return clip as PeepState;
  return (LEGACY_CLIPS as Record<string, PeepState>)[clip] ?? "idle";
}

export const loops = (s: PeepState) => s === "idle" || s === "listening" || s === "talking" || s === "thinking";

const SECONDS: Record<PeepState, number> = { idle: 1, hello: 0.7, listening: 3.2, talking: 0.6, thinking: 6, done: 0.5, error: 0.6 };

/** Seconds one pass of the clip takes (a loop repeats it). */
export function mascotSeconds(clip: MascotClip): number {
  return SECONDS[peepState(clip)];
}

// ------------------------------------------------------------------ the motions

/** The flower's pose: its size (1: the box) and its turn (degrees, clockwise). */
export interface BloomPose {
  scale: number;
  turn: number;
}
export const REST: BloomPose = { scale: 1, turn: 0 };

const TAU = 2 * Math.PI;
const clamp01 = (x: number) => Math.min(1, Math.max(0, x));
/** past 1 and back: an opening that overshoots a little */
const outBack = (p: number) => 1 + 2.70158 * Math.pow(p - 1, 3) + 1.70158 * Math.pow(p - 1, 2);

/** The pose `t` seconds into a clip: a loop wraps, a one-shot ends at rest (idle is always at rest). */
export function bloomPose(s: PeepState, t: number): BloomPose {
  const T = SECONDS[s];
  t = Math.max(0, t);
  const p = loops(s) ? (t % T) / T : clamp01(t / T);
  switch (s) {
    case "listening":
      return { scale: 1 + 0.04 * (0.5 - 0.5 * Math.cos(TAU * p)), turn: 0 };
    case "talking":
      return { scale: 1 + 0.05 * Math.pow(Math.sin(Math.PI * p), 2), turn: 0 };
    case "thinking":
      return { scale: 1, turn: 360 * p };
    case "hello":
      return p >= 1 ? REST : { scale: 0.5 + 0.5 * outBack(p), turn: -40 * Math.pow(1 - p, 2) };
    case "done":
      return p >= 1 ? REST : { scale: 1 + 0.18 * Math.sin(Math.PI * Math.pow(p, 0.6)), turn: 0 };
    case "error":
      return p >= 1 ? REST : { scale: 1 - 0.05 * Math.sin(Math.PI * p), turn: 12 * Math.sin(3 * TAU * p) * (1 - p) };
    default:
      return REST;
  }
}

/** Talking that follows the voice: the level (0…1) as the flower's size, 1 → 1.12, with a soft knee (quiet speech
 *  already shows). */
export function levelScale(level: number): number {
  return 1 + 0.12 * Math.pow(clamp01(level), 0.7);
}

/** A level that rises fast and falls a little slower (per tick of `dt` seconds). */
export function smoothLevel(current: number, level: number, dt: number): number {
  const rate = level > current ? 18 : 7;
  return current + (level - current) * Math.min(1, rate * Math.max(0, dt));
}

// ------------------------------------------------------------------ markup

export interface MascotOptions {
  /** px: the flower's square (an opening or a pop reaches a little past it) */
  size?: number;
  className?: string;
  /** accessible name; without it the mascot is decorative (aria-hidden) */
  title?: string;
  /** after a one-shot (hello, done, error): go on with this clip */
  then?: MascotClip;
  /** talking follows the voice level (setMascotLevel) instead of its own beat */
  level?: boolean;
}

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");

/** `<span class="mascot" data-clip="…">` — a loop keeps playing; a one-shot plays once from when it is shown. */
export function mascotHtml(clip: MascotClip, { size = 48, className, title, then, level }: MascotOptions = {}): string {
  const a11y = title ? ` role="img" aria-label="${esc(title)}"` : ` aria-hidden="true"`;
  const extra = (then ? ` data-then="${peepState(then)}"` : "") + (level ? " data-level" : "");
  return `<span class="mascot${className ? " " + esc(className) : ""}" data-clip="${peepState(clip)}"${extra} style="--mascot-size:${size}px"${a11y}></span>`;
}

/** Plays a clip again on an existing element (or switches it to another clip). */
export function playMascot(el: HTMLElement, clip?: MascotClip): void {
  if (clip) el.dataset.clip = peepState(clip);
  runs.delete(el);
  kick();
}

let levelOf: () => number = () => 0;
/** Where talking mascots (`level: true`) read the voice level, 0…1, each frame. */
export function setMascotLevel(fn: () => number): void {
  levelOf = fn;
}

// ------------------------------------------------------------------ the ticker

interface Run {
  asked: string;
  clip: PeepState;
  start: number;
  last: number;
  smoothed: number;
  ended: boolean;
  shown: string;
}

const runs = new WeakMap<HTMLElement, Run>();
let raf = 0;
let lastTick = 0;
// the system setting, or a page's own switch (<html data-reduce-motion="1">: the overlays' ?reduce-motion)
const reduced = () =>
  (typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches) || document.documentElement.dataset.reduceMotion === "1";

function kick(): void {
  if (raf || typeof document === "undefined" || typeof requestAnimationFrame !== "function") return;
  raf = requestAnimationFrame(tick);
}

function tick(now: number): void {
  raf = 0;
  // 30 fps is plenty for a slow turn or a breath (a 60 / 120 Hz screen draws each pose twice or four times)
  if (now - lastTick < 1000 / 30 - 2) {
    raf = requestAnimationFrame(tick);
    return;
  }
  lastTick = now;
  let moving = false;
  for (const el of document.querySelectorAll<HTMLElement>(".mascot[data-clip]")) moving = step(el, now / 1000) || moving;
  if (moving) raf = requestAnimationFrame(tick);
}

function begin(el: HTMLElement, clip: PeepState, now: number): Run {
  const r: Run = { asked: el.dataset.clip ?? "", clip, start: now, last: now, smoothed: 0, ended: false, shown: "" };
  runs.set(el, r);
  return r;
}

function ended(el: HTMLElement, r: Run): void {
  if (r.ended) return;
  r.ended = true;
  el.dispatchEvent(new CustomEvent("mascotend", { bubbles: false }));
}

/** Poses `el` for now; true while it still moves. */
function step(el: HTMLElement, now: number): boolean {
  let r = runs.get(el);
  if (!r || r.asked !== el.dataset.clip) r = begin(el, peepState(el.dataset.clip), now);
  const dt = now - r.last;
  r.last = now;
  let pose = REST;
  let moving = true;
  if (reduced()) {
    // standing still; a one-shot ends at once and its `then` follows
    if (!loops(r.clip)) {
      ended(el, r);
      if (el.dataset.then) r.clip = peepState(el.dataset.then);
    }
    moving = false;
  } else if (r.clip === "talking" && el.hasAttribute("data-level")) {
    r.smoothed = smoothLevel(r.smoothed, levelOf(), dt);
    pose = { scale: levelScale(r.smoothed), turn: 0 };
  } else if (r.clip === "idle") {
    moving = false;
  } else {
    const t = now - r.start;
    if (!loops(r.clip) && t >= SECONDS[r.clip]) {
      // a one-shot rests, or goes on with `then`
      ended(el, r);
      const then = el.dataset.then;
      if (then && peepState(then) !== r.clip) {
        r.clip = peepState(then);
        r.start = now;
        return step(el, now);
      }
      moving = false;
    } else {
      pose = bloomPose(r.clip, t);
    }
  }
  draw(el, r, pose);
  return moving;
}

function draw(el: HTMLElement, r: Run, pose: BloomPose): void {
  const scale = pose.scale.toFixed(4), turn = pose.turn.toFixed(2);
  const key = `${scale} ${turn}`;
  if (key === r.shown) return;
  r.shown = key;
  if (pose.scale === 1 && pose.turn === 0) {
    el.style.removeProperty("--bloom-scale");
    el.style.removeProperty("--bloom-turn");
  } else {
    el.style.setProperty("--bloom-scale", scale);
    el.style.setProperty("--bloom-turn", `${turn}deg`);
  }
}

if (typeof document !== "undefined" && typeof MutationObserver === "function") {
  // a mascot inserted by markup (innerHTML) or a clip switched on one starts the ticker
  new MutationObserver(kick).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-clip"] });
  if (typeof matchMedia === "function") matchMedia("(prefers-reduced-motion: reduce)").addEventListener?.("change", kick);
  kick();
}
