// One screen × one language: open it in Edge with the mocked backend, then
//   pass 1 — for each theme × window size: the geometry checks (inpage.js), a screenshot, axe (at AXE_SIZE);
//   pass 2 — for each SHIFT_SIZE: replay the screen's transitions, measuring what moved, then check the new state.
// Findings go to out/findings/<screen>.<lang>.json; the report reporter (report.ts) groups them at the end.
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import AxeBuilder from '@axe-core/playwright';
import type { Browser, BrowserContext, Page } from '@playwright/test';
import { baseWorld, NOW_MS, TZ, type Lang, type World } from '../fixtures/world';
import type { Ctx, Screen } from '../screens';
import { installMocks, Recorder } from './mock-backend';

export const OUT = resolve(import.meta.dirname, '../out');
const INPAGE = resolve(import.meta.dirname, 'inpage.js');

/** UI_AUDIT_MOBILE=1: a phone pass instead (390×844, touch, a phone browser's user agent) — for the guest page,
 *  which people open from the invite link on their phones: npx playwright test -c ui-audit/playwright.config.ts --grep guest */
export const MOBILE = process.env.UI_AUDIT_MOBILE === '1';
const PHONE = { w: 390, h: 844 };
export const SIZES = MOBILE
  ? [PHONE]
  : [
      { w: 1024, h: 640 },
      { w: 1280, h: 800 },
      { w: 1600, h: 900 },
      { w: 1920, h: 1080 },
    ];
export const THEMES = ['dark', 'light'] as const;
export type Theme = (typeof THEMES)[number];
/** axe runs once per theme at this size (contrast doesn't change with the window) */
export const AXE_SIZE = MOBILE ? '390x844' : '1280x800';
/** the transitions are replayed at these sizes, dark theme */
export const SHIFT_SIZES = MOBILE
  ? [PHONE]
  : [
      { w: 1024, h: 640 },
      { w: 1600, h: 900 },
    ];
const sizeName = (s: { w: number; h: number }) => `${s.w}x${s.h}`;

export interface Finding {
  check: string;
  detail: string;
  screen: string;
  lang: Lang;
  theme: Theme;
  size: string;
  /** set for findings made after a transition */
  transition?: string;
  el?: string;
  text?: string;
  user?: boolean;
  classes?: string[];
  testids?: string[];
  shot?: string;
  [k: string]: unknown;
}

interface Opened {
  context: BrowserContext;
  page: Page;
  rec: Recorder;
  world: World;
  ctx: Ctx;
}

const rel = (p: string) => p.slice(resolve(OUT, '..').length + 1).replace(/\\/g, '/');

export async function open(browser: Browser, origin: string, screen: Screen, lang: Lang, theme: Theme, size: { w: number; h: number }): Promise<Opened> {
  const world = baseWorld(lang, !!screen.stress);
  screen.world?.(world);
  const context = await browser.newContext({
    viewport: { width: size.w, height: size.h },
    deviceScaleFactor: 1,
    ...(MOBILE
      ? {
          isMobile: true,
          hasTouch: true,
          userAgent: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36',
        }
      : {}),
    locale: lang === 'zh' ? 'zh-CN' : 'en-US',
    timezoneId: TZ,
    colorScheme: theme,
    serviceWorkers: 'block',
  });
  const rec = new Recorder();
  await installMocks(context, origin, () => world, rec);
  await context.addInitScript({ path: INPAGE });
  // phone pass: the guest page opens on the tab this pass looks at (it remembers the last tab in bloom.mtab)
  if (MOBILE) await context.addInitScript((tab) => { try { localStorage.setItem('bloom.mtab', tab); } catch { /* fine */ } }, process.env.UI_AUDIT_MTAB === 'paper' ? 'paper' : 'talk');
  await context.addInitScript((t) => {
    (window as unknown as { __uiAuditTauri: unknown }).__uiAuditTauri = t;
  }, world.tauri);
  const page = await context.newPage();
  page.on('console', (m) => {
    if (m.type() === 'error' || m.type() === 'warning') rec.console.push({ type: m.type(), text: m.text().slice(0, 400), url: m.location()?.url });
  });
  page.on('pageerror', (e) => rec.pageErrors.push(String(e?.stack || e).slice(0, 600)));
  page.on('requestfailed', (r) => {
    const err = r.failure()?.errorText ?? '';
    if (/ERR_ABORTED/.test(err)) return; // a page/iframe navigating away, a stream closed: not a failure
    rec.failed.push({ url: r.url(), error: err });
  });
  page.on('response', (r) => {
    if (r.status() >= 400) rec.badStatus.push({ url: r.url(), status: r.status() });
  });
  await page.clock.setFixedTime(new Date(NOW_MS));
  const url =
    screen.page === 'guest'
      ? `${origin}/guest.html?room=k3J9xQ2mZp`
      : `${origin}/index.html?zuhui=${encodeURIComponent(origin + '/__zuhui')}&lang=${lang === 'zh' ? 'zh-Hans' : 'en'}`;
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.locator('#root > *').first().waitFor({ timeout: 30_000 });
  const settle = async (ms = 250) => {
    await page.evaluate((m) => (window as unknown as { __uiAudit: { settle: (m: number) => Promise<void> } }).__uiAudit.settle(m), ms);
  };
  const ctx: Ctx = {
    page,
    lang,
    world,
    T: (zh, en) => (lang === 'zh' ? zh : en),
    settle,
    push: async (stream, data) => {
      const n = await page.evaluate(([p, d]) => (window as unknown as { __uiAudit: { push: (p: string, d: unknown) => number } }).__uiAudit.push(p as string, d), [
        stream === 'mic' ? '/api/mic/stream' : '/api/brain/stream',
        data,
      ] as const);
      if (!n) throw new Error(`no open ${stream} stream to push to`);
      await settle(300);
    },
    waitPoll: async () => {
      // two polls: the first may have been in flight before the change
      for (let i = 0; i < 2; i++) await page.waitForResponse((r) => r.url().includes('/api/current'), { timeout: 10_000 });
      await settle(400);
    },
    bumpRelay: async (lines = false) => {
      world.relay.sv += 1;
      if (lines && world.relay.state) world.relay.state.lv = String(Number(world.relay.state.lv ?? 0) + 1);
      await page.waitForResponse((r) => r.url().includes('/state?sv='), { timeout: 10_000 });
      if (lines) await page.waitForResponse((r) => r.url().includes('/lines'), { timeout: 10_000 });
      await settle(400);
    },
  };
  if (screen.go) await screen.go(ctx);
  let ready = screen.ready;
  if (MOBILE) {
    // the guest page on a phone shows the paper or the transcript, one at a time (a tab bar at the bottom):
    // UI_AUDIT_MTAB=paper|talk picks which one this pass looks at (default talk, where the typing is). The AI's status
    // box isn't on phones at all, so a screen that waits for it waits for the page instead.
    const tab = process.env.UI_AUDIT_MTAB === 'paper' ? 'paper' : 'talk';
    const bar = page.getByTestId(`guest-tab-${tab}`);
    await page.locator('.g-app.is-in, [data-testid="guest-join"], .g-center').first().waitFor({ timeout: 20_000 }).catch(() => {});
    if (await bar.isVisible().catch(() => false)) await bar.click();
    if (/claude-now|composer|transcript|ask-input/.test(ready) && tab === 'paper') ready = '[data-testid="meeting"]';
    else if (/claude-now/.test(ready)) ready = '[data-testid="transcript"]';
  }
  await page.locator(ready).first().waitFor({ state: 'visible', timeout: 20_000 });
  await settle(500);
  return { context, page, rec, world, ctx };
}

async function check(o: Opened, screen: Screen): Promise<Omit<Finding, 'screen' | 'lang' | 'theme' | 'size'>[]> {
  return o.page.evaluate(
    (opts) => (window as unknown as { __uiAudit: { check: (o: unknown) => never[] } }).__uiAudit.check(opts),
    {
      userStrings: o.world.userStrings,
      appShell: screen.page === 'main',
      // the paper inside the iframe is the backend's page (a stub here): its own tests cover it
      ignore: [...(screen.ignore ?? [])],
      scope: screen.scope,
    },
  );
}

async function axe(page: Page, scope?: string): Promise<Omit<Finding, 'screen' | 'lang' | 'theme' | 'size'>[]> {
  let b = new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).exclude('iframe');
  if (scope) b = b.include(scope);
  const res = await b.analyze();
  const out: Omit<Finding, 'screen' | 'lang' | 'theme' | 'size'>[] = [];
  for (const v of res.violations) {
    for (const n of v.nodes.slice(0, 12)) {
      const data = (n.any?.[0]?.data ?? {}) as { fgColor?: string; bgColor?: string; contrastRatio?: number; expectedContrastRatio?: string; fontSize?: string };
      const classes = [...String(n.html).matchAll(/class="([^"]*)"/g)].flatMap((m) => m[1].split(/\s+/)).filter(Boolean);
      const testids = [...String(n.html).matchAll(/data-testid="([^"]*)"/g)].map((m) => m[1]);
      out.push({
        check: `axe:${v.id}`,
        impact: v.impact,
        detail: v.id === 'color-contrast' && data.contrastRatio ? `contrast ${data.contrastRatio}:1 (needs ${data.expectedContrastRatio}) — ${data.fgColor} on ${data.bgColor}, ${data.fontSize}` : `${v.help}`,
        ratio: data.contrastRatio,
        el: (Array.isArray(n.target) ? n.target.join(' ') : String(n.target)).slice(0, 160),
        text: String(n.html).replace(/\s+/g, ' ').slice(0, 160),
        classes,
        testids,
        helpUrl: v.helpUrl,
      });
    }
  }
  return out;
}

function wireFindings(rec: Recorder, screen: Screen, base: { screen: string; lang: Lang; theme: Theme; size: string; transition?: string }): Finding[] {
  const out: Finding[] = [];
  const net = screen.expectNetErrors;
  for (const e of rec.pageErrors) out.push({ ...base, check: 'page-error', detail: e.split('\n')[0], stack: e });
  for (const c of rec.console) {
    if (/Download the React DevTools|\[vite\]/.test(c.text)) continue;
    // the browser's own line for any 4xx/5xx answer: the http-error check below reports those (with the URL)
    if (/Failed to load resource/.test(c.text)) continue;
    if (net && /ERR_CONNECTION_REFUSED|Failed to fetch/.test(c.text)) continue;
    out.push({ ...base, check: c.type === 'error' ? 'console-error' : 'console-warning', detail: c.text.split('\n')[0].slice(0, 240), stack: c.text, url: c.url });
  }
  for (const f of rec.failed) {
    if (net && /CONNECTION_REFUSED/.test(f.error)) continue;
    if (/BLOCKED_BY_CLIENT/.test(f.error)) continue; // reported as external / leak below
    out.push({ ...base, check: 'request-failed', detail: `${f.error} ${f.url}`, url: f.url });
  }
  for (const b of rec.badStatus) {
    // answered so on purpose: no photo of your own (404), a backend that can't switch models (503), a name the
    // "name rejected" transition makes the backend refuse (400)
    if (/\/avatars\/me/.test(b.url) || /\/__zuhui\/api\/models/.test(b.url) || (b.status === 400 && /\/__zuhui\/api\/profile$/.test(b.url))) continue;
    if (/not mocked/.test(b.url)) continue;
    out.push({ ...base, check: 'http-error', detail: `HTTP ${b.status} ${b.url.replace(/^https?:\/\/[^/]+/, '')}`, url: b.url });
  }
  for (const u of [...new Set(rec.unmocked)]) out.push({ ...base, check: 'unmocked', detail: `the mock backend doesn't know ${u}` });
  for (const u of [...new Set(rec.external)]) out.push({ ...base, check: 'external-request', detail: `blocked a request to another host: ${u}` });
  for (const u of [...new Set(rec.leaks)]) out.push({ ...base, check: 'live-backend-request', detail: `the page tried the real backend (blocked): ${u}` });
  rec.reset();
  rec.unmocked = [];
  rec.external = [];
  rec.leaks = [];
  return out;
}

export async function auditScreen(browser: Browser, origin: string, screen: Screen, lang: Lang, log: (s: string) => void = () => {}): Promise<Finding[]> {
  const findings: Finding[] = [];
  const shots = resolve(OUT, 'shots', screen.name);
  mkdirSync(shots, { recursive: true });
  const sizes = screen.sizes ? SIZES.filter((s) => screen.sizes!.includes(sizeName(s))) : SIZES;

  // ---------------------------------------------------------------- pass 1: every theme × size
  const o = await open(browser, origin, screen, lang, 'dark', sizes[0]);
  try {
    for (const theme of THEMES) {
      await o.page.emulateMedia({ colorScheme: theme });
      for (const s of sizes) {
        const size = sizeName(s);
        await o.page.setViewportSize({ width: s.w, height: s.h });
        await o.ctx.settle(300);
        if (screen.reapply) await screen.reapply(o.ctx);
        const shot = resolve(shots, `${lang}-${theme}-${size}.png`);
        const base = { screen: screen.name, lang, theme, size, shot: rel(shot) };
        const res = await check(o, screen);
        await o.page.screenshot({ path: shot, animations: 'disabled', caret: 'hide' });
        findings.push(...res.map((f) => ({ ...base, ...f }) as Finding));
        if (size === AXE_SIZE || (!sizes.some((x) => sizeName(x) === AXE_SIZE) && s === sizes[0])) {
          const a = await axe(o.page, screen.scope);
          findings.push(...a.map((f) => ({ ...base, ...f }) as Finding));
        }
        log(`${screen.name} ${lang} ${theme} ${size}: ${res.length} geometry findings`);
      }
    }
    findings.push(...wireFindings(o.rec, screen, { screen: screen.name, lang, theme: 'dark', size: 'any' }));
  } finally {
    await o.context.close();
  }

  // ---------------------------------------------------------------- pass 2: transitions (layout shift)
  if (screen.transitions?.length) {
    for (const s of SHIFT_SIZES) {
      const size = sizeName(s);
      const t = await open(browser, origin, screen, lang, 'dark', s);
      try {
        for (const [i, tr] of screen.transitions.entries()) {
          const shot = resolve(shots, `${lang}-shift-${size}-${String(i + 1).padStart(2, '0')}-${tr.name.replace(/[^\w一-鿿-]+/g, '_')}.png`);
          const base = { screen: screen.name, lang, theme: 'dark' as Theme, size, transition: tr.name, shot: rel(shot) };
          await t.page.evaluate(() => (window as unknown as { __uiAudit: { mark: () => number } }).__uiAudit.mark());
          try {
            await tr.act(t.ctx);
          } catch (e) {
            findings.push({ ...base, check: 'audit-step-failed', detail: `couldn't do "${tr.name}": ${String((e as Error)?.message ?? e).split('\n')[0]}` });
            continue;
          }
          await t.ctx.settle(600);
          const d = (await t.page.evaluate(
            (expect) => (window as unknown as { __uiAudit: { diff: (o: unknown) => unknown } }).__uiAudit.diff({ expect }),
            tr.expect ?? [],
          )) as {
            moved: { el: string; text: string; dx: number; dy: number; interactive: boolean; classes: string[]; testids: string[] }[];
            resized: { el: string; text: string; dw: number; dh: number; classes: string[]; testids: string[] }[];
            appeared: string[];
            gone: string[];
            cls: number;
            shiftSources: unknown[];
          };
          await t.page.screenshot({ path: shot, animations: 'disabled', caret: 'hide' });
          for (const m of d.moved) {
            findings.push({
              ...base,
              check: 'layout-shift',
              detail: `"${tr.name}" moved ${m.el}${m.text ? ` "${m.text}"` : ''} by ${m.dx ? `${m.dx > 0 ? '+' : ''}${m.dx}px x` : ''}${m.dx && m.dy ? ', ' : ''}${m.dy ? `${m.dy > 0 ? '+' : ''}${m.dy}px y` : ''}`,
              el: m.el,
              text: m.text,
              dx: m.dx,
              dy: m.dy,
              interactive: m.interactive,
              classes: m.classes,
              testids: m.testids,
              appeared: d.appeared,
              gone: d.gone,
              cls: d.cls,
              note: tr.note,
              byDesign: tr.byDesign,
            });
          }
          for (const r of d.resized) {
            findings.push({ ...base, check: 'control-resized', detail: `"${tr.name}" changed the size of ${r.el} "${r.text}" by ${r.dw}px × ${r.dh}px`, el: r.el, text: r.text, classes: r.classes, testids: r.testids, note: tr.note, byDesign: tr.byDesign });
          }
          if (d.cls > 0.001 && !d.moved.length) {
            findings.push({ ...base, check: 'layout-shift-cls', detail: `"${tr.name}": layout-shift score ${d.cls} (browser's own measure)`, sources: d.shiftSources, note: tr.note });
          }
          // the state after the change: same geometry checks
          const res = await check(t, screen);
          findings.push(...res.map((f) => ({ ...base, ...f }) as Finding));
          log(`${screen.name} ${lang} ${size} · ${tr.name}: moved ${d.moved.length}, cls ${d.cls}`);
        }
        findings.push(...wireFindings(t.rec, screen, { screen: screen.name, lang, theme: 'dark', size, transition: '(transitions)' }));
      } finally {
        await t.context.close();
      }
    }
  }

  mkdirSync(resolve(OUT, 'findings'), { recursive: true });
  writeFileSync(resolve(OUT, 'findings', `${screen.name}.${lang}.json`), JSON.stringify(findings, null, 1));
  return findings;
}
