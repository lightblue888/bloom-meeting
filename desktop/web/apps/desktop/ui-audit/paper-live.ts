// 论文区 (2026-11-21): the meeting page's paper with the real page in it (zuhui/view.py, made by fixtures/paper_live.py
// before the run), not the stub. The audit's own checks don't look inside the paper's iframe, so these screens check it
// themselves and throw (→ "audit-step-failed" in the report) when it goes wrong:
//   · 「引用」 after a selection: comes up next to where the selection ends, inside the paper, pushes nothing; clicking
//     it is what quotes (the chip over the transcript's foot);
//   · the box to write a comment: compact, an error or the count near the limit doesn't change its size;
//   · an edit lands (a new version of the page swaps in): the paper stays on the paragraph you were reading, also when
//     you are scrolling right then.
import type { ElementHandle, Frame } from '@playwright/test';
import type { World } from './fixtures/world';
import type { LiveWorld } from './lib/mock-backend';
import type { Ctx, Screen } from './screens';

const VISIBLE = 'iframe.mt-diff:not(.is-staging)';
const live = (w: World) => {
  (w as LiveWorld).paperLive = 1;
  // the paper's own words (a quoted passage ends in "…" in the chip over the transcript): fixtures/paper_live.py
  w.userStrings.push(w.lang === 'zh' ? '这里描述一个编出来的步骤' : 'a made-up step with');
};

/** the paper on screen (the visible slot), once its paragraphs are there */
async function paper(c: Ctx): Promise<{ f: Frame; el: ElementHandle<HTMLElement> }> {
  const loc = c.page.locator(VISIBLE);
  await loc.waitFor();
  const el = (await loc.elementHandle()) as ElementHandle<HTMLElement>;
  const f = await el.contentFrame();
  if (!f) throw new Error('the paper has no frame');
  await f.locator('.docs .row').first().waitFor({ timeout: 20_000 });
  if (!(await f.evaluate(() => /var BUILD = \d/.test(document.documentElement.innerHTML)))) {
    throw new Error('the paper is the stub, not the real page (fixtures/paper_live.py did not run: see the log of lib/global-setup.ts)');
  }
  return { f, el };
}

/** the paragraph at the top of the paper: which one, its first words, how far it is from the top; `at` = paragraph
 *  number + how much of it has scrolled by, 0 while a heading or the gap above it is at the top (only grows while you
 *  scroll down) */
const TOP = () => {
  const rows = document.querySelectorAll('.docs .row');
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i].getBoundingClientRect();
    if (r.bottom > 8 && r.height > 0) {
      const p = rows[i].querySelector('.para');
      return { i, y: Math.round(r.top), at: i + Math.min(1, Math.max(0, (8 - r.top) / r.height)), text: (p?.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 16) };
    }
  }
  return { i: -1, y: 0, at: -1, text: '' };
};
type Top = ReturnType<typeof TOP>;
const say = (t: Top) => `paragraph ${t.i} "${t.text}" at ${t.y}px`;

/** drag across paragraph `i` (its first line, into the second): what someone does before 引用 */
async function select(c: Ctx, i: number) {
  const { f, el } = await paper(c);
  await f.evaluate(() => getSelection()?.removeAllRanges());
  const box = (await el.boundingBox())!;
  const r = await f.evaluate((k) => {
    const p = document.querySelectorAll('.docs .row')[k].querySelector('.para')!;
    p.scrollIntoView({ block: 'center' });
    const b = p.getBoundingClientRect();
    return { x: b.x, y: b.y, w: b.width };
  }, i);
  const before = await f.evaluate((k) => document.querySelectorAll('.docs .row')[k + 1].getBoundingClientRect().top, i);
  const y = box.y + r.y + 12;
  await c.page.mouse.move(box.x + r.x + 24, y);
  await c.page.mouse.down();
  await c.page.mouse.move(box.x + r.x + Math.min(360, r.w - 40), y + 32, { steps: 8 });
  await c.page.mouse.up();
  await f.locator('#zh-quote.show').waitFor({ timeout: 5000 });
  // 引用 floats: the next paragraph is where it was; the button is inside the paper and not on the line it ends on
  const q = await f.evaluate((k) => {
    const b = document.getElementById('zh-quote')!.getBoundingClientRect();
    return { next: document.querySelectorAll('.docs .row')[k + 1].getBoundingClientRect().top, l: b.left, r: b.right, t: b.top, b: b.bottom,
      vw: document.documentElement.clientWidth, vh: innerHeight };
  }, i);
  if (Math.abs(q.next - before) > 0.5) throw new Error(`引用 pushed the next paragraph by ${q.next - before}px`);
  if (q.l < 0 || q.t < 0 || q.r > q.vw || q.b > q.vh) throw new Error(`引用 is outside the paper: ${JSON.stringify(q)}`);
  const end = y + 32 - box.y;
  if (q.t < end && q.b > end) throw new Error(`引用 covers the line the selection ends on (${Math.round(q.t)}–${Math.round(q.b)}, line at ${Math.round(end)})`);
}

/** hover a paragraph, open its comment box (and type `text`) */
async function commentBox(c: Ctx, i: number, text: string) {
  const { f } = await paper(c);
  await f.evaluate((k) => document.querySelectorAll('.docs .row')[k].scrollIntoView({ block: 'center' }), i);
  await c.page.mouse.move(2, 2);
  await f.locator('.docs .row').nth(i).hover();
  await f.locator('#zh-cm-add.show').waitFor({ timeout: 5000 });
  await f.locator('#zh-cm-add').click();
  await f.locator('#zh-cm-card textarea').waitFor();
  if (text) await f.locator('#zh-cm-card textarea').fill(text);
  return f;
}
const cardSize = (f: Frame) => f.evaluate(() => {
  const r = document.getElementById('zh-cm-card')!.getBoundingClientRect();
  return { w: Math.round(r.width), h: Math.round(r.height) };
});

/** wait until the version `n` page is the one on screen, then a moment for it to settle */
async function onScreen(c: Ctx, n: number) {
  const t0 = Date.now();
  for (;;) {
    const { f } = await paper(c).catch(() => ({ f: null as Frame | null }));
    if (f && (await f.evaluate((k) => document.documentElement.innerHTML.includes(`var BUILD = ${k};`), n).catch(() => false))) break;
    if (Date.now() - t0 > 15_000) throw new Error(`version ${n} of the paper never came up`);
    await c.page.waitForTimeout(150);
  }
  await c.settle(800);
}

export const PAPER_LIVE_SCREENS: Screen[] = [
  {
    name: 'paper-live-quote',
    what: 'In a meeting: a selection in the paper, 「引用」 next to where it ends (nothing is quoted until it is clicked)',
    page: 'main',
    world: live,
    go: (c) => select(c, 4),
    // a new window size closes it (as a click elsewhere would): select again
    reapply: (c) => select(c, 4),
    ready: VISIBLE,
    transitions: [
      {
        name: 'click 引用',
        act: async (c) => {
          const { f } = await paper(c);
          if (await c.page.getByTestId('anchor-chip').count()) throw new Error('a selection quoted by itself (before 引用 was clicked)');
          await f.locator('#zh-quote button').click();
          await c.page.getByTestId('anchor-chip').waitFor({ timeout: 8000 });
          if (await f.locator('#zh-quote.show').count()) throw new Error('引用 stayed up after it was clicked');
        },
        expect: ['[data-testid="anchor-chip"]', '.mt-float'],
      },
      {
        name: 'select again, then Esc',
        act: async (c) => {
          await select(c, 7);
          await c.page.keyboard.press('Escape');
          const { f } = await paper(c);
          if (await f.locator('#zh-quote.show').count()) throw new Error('Esc left 引用 up');
        },
      },
    ],
  },
  {
    name: 'paper-live-comment',
    what: 'In a meeting: the box to write a comment on a paragraph (one line, send on the right; the count only near 500)',
    page: 'main',
    world: live,
    go: async (c) => {
      await commentBox(c, 5, c.T('这一段要补一个例子，说明窗口长度怎么选。', 'This paragraph needs an example of how the window is chosen.'));
    },
    ready: VISIBLE,
    transitions: [
      {
        name: 'sending fails',
        act: async (c) => {
          const { f } = await paper(c);
          const before = await cardSize(f);
          await c.page.route('**/__zuhui/api/comment', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: false, error: 'failed' }) }));
          await f.locator('#zh-cm-card textarea').press('Enter');
          await f.locator('.zh-cm-err:not(.off)').waitFor({ timeout: 5000 });
          await c.page.unroute('**/__zuhui/api/comment');
          const after = await cardSize(f);
          if (before.w !== after.w || before.h !== after.h) throw new Error(`the error changed the box: ${JSON.stringify(before)} → ${JSON.stringify(after)}`);
        },
      },
      {
        name: 'near the 500 limit',
        act: async (c) => {
          const { f } = await paper(c);
          await f.locator('#zh-cm-card textarea').fill(c.T('长'.repeat(430), 'long '.repeat(86)));
          await f.locator('.zh-cm-n:not(.off)').waitFor({ timeout: 5000 });
          const n = await f.locator('.zh-cm-n').innerText();
          if (!/^43\d\/500$/.test(n)) throw new Error(`the count says "${n}"`);
        },
      },
    ],
  },
  {
    name: 'paper-live-scroll',
    what: 'In a meeting, reading the middle of the paper: an edit lands above (a new version swaps in), the paper stays put',
    page: 'main',
    world: live,
    go: async (c) => {
      const { f } = await paper(c);
      await f.evaluate(() => {
        const r = document.querySelectorAll('.docs .row')[16];
        window.scrollTo(0, r.getBoundingClientRect().top + scrollY - 40);
      });
      await c.settle(400);
    },
    ready: VISIBLE,
    transitions: [
      {
        name: 'an edit lands above where you read',
        act: async (c) => {
          const was = await (await paper(c)).f.evaluate(TOP);
          (c.world as LiveWorld).paperLive = 2;
          await onScreen(c, 2);
          const now = await (await paper(c)).f.evaluate(TOP);
          if (now.text !== was.text || Math.abs(now.y - was.y) > 2) throw new Error(`the paper moved: was ${say(was)}, now ${say(now)}`);
        },
        expect: ['.mt-diff'],
      },
      {
        name: 'an edit lands while you scroll',
        act: async (c) => {
          const { el } = await paper(c);
          const box = (await el.boundingBox())!;
          await c.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
          (c.world as LiveWorld).paperLive = 1;
          // scrolling down the whole time: where you are only goes forward (a swap that puts you back where you were a
          // second ago shows as a step back)
          let last = await (await paper(c)).f.evaluate(TOP);
          for (let k = 0; k < 16; k++) {
            await c.page.mouse.wheel(0, 60);
            await c.page.waitForTimeout(110);
            const t = await (await paper(c)).f.evaluate(TOP);
            if (t.at < last.at - 0.02) throw new Error(`the paper jumped back while scrolling: from ${say(last)} to ${say(t)}`);
            last = t;
          }
          await c.page.waitForTimeout(250);
          const stopped = await (await paper(c)).f.evaluate(TOP);
          await onScreen(c, 1);
          const now = await (await paper(c)).f.evaluate(TOP);
          if (now.text !== stopped.text || Math.abs(now.y - stopped.y) > 2) throw new Error(`the paper moved under the scrolling: stopped at ${say(stopped)}, now ${say(now)}`);
        },
        expect: ['.mt-diff'],
      },
    ],
  },
];
