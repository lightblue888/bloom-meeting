// One test per screen × language (screens.ts). A test passes when the screen could be reached and checked; what the
// checks found goes to out/findings and the report (out/report.md) — findings don't fail the run unless
// UI_AUDIT_STRICT=1 (then a blocker does).
import { expect, test } from '@playwright/test';
import { auditScreen } from './lib/audit';
import { SCREENS } from './screens';
import type { Lang } from './fixtures/world';

const LANGS: Lang[] = (process.env.UI_AUDIT_LANGS?.split(',') as Lang[]) ?? ['zh', 'en'];
const ONLY = process.env.UI_AUDIT_SCREENS?.split(',').map((s) => s.trim()).filter(Boolean);

for (const screen of SCREENS) {
  if (ONLY && !ONLY.some((o) => screen.name === o || screen.name.startsWith(o))) continue;
  for (const lang of LANGS) {
    test(`${screen.name} [${lang}]`, async ({ browser, baseURL }, info) => {
      info.annotations.push({ type: 'what', description: screen.what });
      const found = await auditScreen(browser, baseURL!, screen, lang, (s) => info.annotations.push({ type: 'log', description: s }));
      if (process.env.UI_AUDIT_STRICT === '1') {
        const blockers = found.filter((f) => ['page-overflow-x', 'offscreen-control', 'page-error', 'live-backend-request'].includes(f.check));
        expect(blockers.map((b) => `${b.check} ${b.size} ${b.theme}: ${b.detail}`)).toEqual([]);
      }
    });
  }
}
