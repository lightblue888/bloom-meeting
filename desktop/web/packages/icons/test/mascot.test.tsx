import { readFileSync } from "node:fs";
import { join } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { MASCOT_CLIPS, Mascot, PEEP_STATES, REST, bloomPose, levelScale, loops, mascotHtml, mascotSeconds, peepState, smoothLevel } from "../src";

const src = (f: string) => readFileSync(join(import.meta.dirname, "../src", f), "utf8");

it("is Bloom's flower (BloomMark's petals, centre and colours), one inline SVG, no sprite sheets", () => {
  const css = decodeURIComponent(src("mascot.css"));
  expect(css).toContain("data:image/svg+xml,");
  expect(css).toContain("translate(120,120)");
  expect(css.match(/<ellipse cx='0' cy='-46' rx='22' ry='40'/g)).toHaveLength(5);
  for (const k of [0, 72, 144, 216, 288]) expect(css).toContain(`transform='rotate(${k})'`);
  expect(css.match(/fill='#3B82F6'/g)).toHaveLength(3);
  expect(css.match(/fill='#60A5FA'/g)).toHaveLength(2);
  expect(css).toContain("<circle r='26' fill='#1D4ED8'/>");
  expect(css).toContain("<circle r='9' fill='#EFF6FF'/>");
  expect(css).toContain("scale(var(--bloom-scale, 1))");
  expect(css).not.toMatch(/webp|--peep-sheet/);
  expect(src("mascot.ts")).not.toMatch(/import .*\.(webp|json)/);
});

it("maps the 1A chick's clip names to the state played there", () => {
  for (const c of ["idle", "listening", "thinking", "wobble", "hatching", "happy", "peck"]) expect(MASCOT_CLIPS).toContain(c);
  expect([peepState("happy"), peepState("hatching"), peepState("wobble"), peepState("peck")]).toEqual(["done", "done", "thinking", "done"]);
  expect(peepState("nonsense")).toBe("idle");
});

it("moves per clip: idle still, listening breathes, thinking turns, one-shots pop and end at rest", () => {
  for (const s of PEEP_STATES) expect(mascotSeconds(s)).toBeGreaterThan(0.3);
  for (const t of [0, 0.4, 3]) expect(bloomPose("idle", t)).toEqual(REST);
  // listening: a slow breath, 1 → 1.04 and back, never turning
  const breath = [0, 0.8, 1.6, 2.4].map((t) => bloomPose("listening", t));
  for (const b of breath) {
    expect(b.turn).toBe(0);
    expect(b.scale).toBeGreaterThanOrEqual(1);
    expect(b.scale).toBeLessThanOrEqual(1.04 + 1e-9);
  }
  expect(breath[2].scale).toBeCloseTo(1.04);
  // thinking: one round in 6 s, wrapping
  expect(bloomPose("thinking", 1.5).turn).toBeCloseTo(90);
  expect(bloomPose("thinking", 7.5).turn).toBeCloseTo(90);
  expect(bloomPose("thinking", 1.5).scale).toBe(1);
  // the one-shots: done pops past 1.15 and settles, hello opens from small, error shakes both ways; all end at rest
  const peak = (s: "done" | "hello" | "error", f: (p: { scale: number; turn: number }) => number) =>
    Math.max(...Array.from({ length: 60 }, (_, i) => f(bloomPose(s, (i / 60) * mascotSeconds(s)))));
  expect(peak("done", (p) => p.scale)).toBeGreaterThan(1.15);
  expect(peak("done", (p) => p.scale)).toBeLessThanOrEqual(1.18 + 1e-9);
  expect(bloomPose("hello", 0).scale).toBeLessThan(0.6);
  expect(peak("hello", (p) => p.scale)).toBeGreaterThan(1);
  expect(peak("error", (p) => p.turn)).toBeGreaterThan(5);
  expect(peak("error", (p) => -p.turn)).toBeGreaterThan(5);
  for (const s of ["done", "hello", "error"] as const) {
    expect(loops(s)).toBe(false);
    expect(bloomPose(s, mascotSeconds(s))).toEqual(REST);
    expect(bloomPose(s, mascotSeconds(s) - 0.001).scale).toBeCloseTo(1, 2);
  }
});

it("talking with a level grows with the voice, 1 → 1.12, softly; the level rises fast and falls slower", () => {
  expect(levelScale(0)).toBe(1);
  expect(levelScale(1)).toBeCloseTo(1.12);
  expect(levelScale(4)).toBeCloseTo(1.12);
  expect(levelScale(0.2)).toBeGreaterThan(1 + 0.12 * 0.2); // the soft knee: quiet speech shows
  expect(levelScale(0.5)).toBeGreaterThan(levelScale(0.2));
  expect(smoothLevel(0, 1, 1 / 30)).toBeGreaterThan(1 - smoothLevel(1, 0, 1 / 30));
});

it("renders decorative unless titled; legacy names come out as states", () => {
  expect(mascotHtml("happy", { size: 40 })).toBe('<span class="mascot" data-clip="done" style="--mascot-size:40px" aria-hidden="true"></span>');
  expect(mascotHtml("talking", { size: 24, level: true })).toContain('data-clip="talking" data-level');
  const html = renderToStaticMarkup(<Mascot clip="hello" then="idle" size={64} title="Bloom" />);
  expect(html).toContain('data-clip="hello"');
  expect(html).toContain('data-then="idle"');
  expect(html).toContain('aria-label="Bloom"');
  expect(html).toContain("--mascot-size:64px");
});

it("respects reduced motion (the ticker stands it still) and needs no keyframes", () => {
  expect(src("mascot.ts")).toContain("prefers-reduced-motion: reduce");
  expect(src("mascot.css")).not.toContain("@keyframes");
});
