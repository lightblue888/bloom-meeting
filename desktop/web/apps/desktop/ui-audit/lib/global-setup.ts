// Before the tests: clear the last run's findings and screenshots, then open both pages once so Vite has compiled and
// pre-bundled everything (a dependency discovered while four workers are mid-test makes Vite reload their pages).
import { rmSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium, type FullConfig } from '@playwright/test';
import { open, OUT } from './audit';
import { SCREENS } from '../screens';

export default async function globalSetup(config: FullConfig) {
  for (const d of ['findings', 'shots', 'report.md', 'findings.json']) rmSync(resolve(OUT, d), { recursive: true, force: true });
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
