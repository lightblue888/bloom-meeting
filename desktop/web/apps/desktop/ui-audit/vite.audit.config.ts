// The audit's own dev server for the app's two pages (index.html, guest.html) — separate from `vite` (port 1420) so it
// can run while the app is being worked on: its own port, its own dependency cache, no HMR / file watching (a run sees
// the source as it was when it started). Started by playwright.config.ts (webServer).
import { resolve } from 'node:path';
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

const PORT = Number(process.env.UI_AUDIT_PORT || 41497);
const SRC = resolve(import.meta.dirname, '../src');

/** src/api.ts's `./mock` (the Tauri commands outside the app) → fixtures/tauri-mock.ts, driven by the test's world */
function auditTauriMock(): Plugin {
  return {
    name: 'ui-audit-tauri-mock',
    enforce: 'pre',
    resolveId(source, importer) {
      if (source === './mock' && importer && resolve(importer) === resolve(SRC, 'api.ts')) return resolve(import.meta.dirname, 'fixtures/tauri-mock.ts');
      return null;
    },
  };
}

export default defineConfig({
  root: resolve(import.meta.dirname, '..'),
  plugins: [auditTauriMock(), react()],
  clearScreen: false,
  logLevel: 'warn',
  cacheDir: resolve(import.meta.dirname, 'out/.vite'),
  server: {
    host: '127.0.0.1',
    port: PORT,
    strictPort: true,
    hmr: false,
    watch: { ignored: ['**/*'] },
  },
  // pre-bundle everything both pages import up front: a dependency found mid-run would make Vite reload the page
  optimizeDeps: {
    entries: ['index.html', 'guest.html'],
    include: ['react', 'react-dom', 'react-dom/client', 'react/jsx-runtime', 'react/jsx-dev-runtime', '@tauri-apps/api/core', '@tauri-apps/api/event', '@tauri-apps/api/window', '@tauri-apps/api/webview'],
  },
});
