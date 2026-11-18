// npm run test:ui  (from apps/desktop) — see README.md.
// Microsoft Edge (Chromium, the same engine as the app's WebView2) is used as installed: no browser download.
import { resolve } from 'node:path';
import { defineConfig } from '@playwright/test';

const PORT = Number(process.env.UI_AUDIT_PORT || 41497);
const HERE = import.meta.dirname;

export default defineConfig({
  testDir: HERE,
  testMatch: /audit\.spec\.ts$/,
  outputDir: resolve(HERE, 'out/pw-results'),
  fullyParallel: true,
  workers: Number(process.env.UI_AUDIT_WORKERS || 4),
  // one test = one screen in one language, at every size × theme plus its transitions
  timeout: 8 * 60_000,
  retries: 0,
  globalSetup: resolve(HERE, 'lib/global-setup.ts'),
  reporter: [['list'], [resolve(HERE, 'lib/progress-reporter.ts')], [resolve(HERE, 'lib/report.ts')]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: process.env.UI_AUDIT_BROWSER || 'msedge',
    headless: process.env.UI_AUDIT_HEADED !== '1',
    actionTimeout: 15_000,
  },
  webServer: {
    command: `node node_modules/vite/bin/vite.js --config ui-audit/vite.audit.config.ts`,
    cwd: resolve(HERE, '..'),
    url: `http://127.0.0.1:${PORT}/index.html`,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: 'ignore',
    stderr: 'pipe',
    env: { UI_AUDIT_PORT: String(PORT) },
  },
});
