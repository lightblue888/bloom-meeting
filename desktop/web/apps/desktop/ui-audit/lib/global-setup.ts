// Before the tests: clear the last run's findings and screenshots, then open both pages once so Vite has compiled and
// pre-bundled everything (a dependency discovered while four workers are mid-test makes Vite reload their pages).
import { spawnSync } from 'node:child_process';
import { rmSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium, type FullConfig } from '@playwright/test';
import { open, OUT } from './audit';
import { LIVE_DIR } from './mock-backend';
import { SCREENS } from '../screens';

export default async function globalSetup(config: FullConfig) {
  for (const d of ['findings', 'shots', 'report.md', 'findings.json']) rmSync(resolve(OUT, d), { recursive: true, force: true });
  // the paper column's screens (paper-live-*) look at the real paper page: zuhui/view.py renders a made-up paper here
  // (a few seconds, no network, a throwaway ZUHUI_HOME)
  const only = process.env.UI_AUDIT_SCREENS?.split(',').map((s) => s.trim()).filter(Boolean);
  if (!only || only.some((o) => 'paper-live'.startsWith(o) || o.startsWith('paper-live'))) {
    const py = spawnSync(process.env.UI_AUDIT_PYTHON || 'python', ['-X', 'utf8', resolve(import.meta.dirname, '../fixtures/paper_live.py'), LIVE_DIR], {
      encoding: 'utf8',
      timeout: 120_000,
    });
    if (py.status !== 0) console.warn(`ui-audit: the real paper page wasn't made, paper-live-* will fail: ${String(py.stderr || py.error?.message || '').trim().split('\n').slice(-2).join(' ')}`);
  }
  const use = config.projects[0].use;
  const channel = (use.channel as string) || 'msedge';
  let browser;
  try {
    browser = await chromium.launch({ channel, headless: true });
  } catch (e) {
    throw new Error(`ui-audit: can't start the browser "${channel}" (set UI_AUDIT_BROWSER=chrome to use Chrome instead): ${String(e).split('\n')[0]}`);
  }
  try {
    const origin = use.baseURL as string;
    for (const name of ['meeting-live', 'guest-join']) {
      const s = SCREENS.find((x) => x.name === name);
      if (!s) continue;
      const o = await open(browser, origin, s, 'zh', 'dark', { w: 1280, h: 800 });
      await o.context.close();
    }
  } finally {
    await browser.close();
  }
}
