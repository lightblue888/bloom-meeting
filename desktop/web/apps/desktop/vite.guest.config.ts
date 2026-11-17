// The guest page (guest.html): the same meeting page as the app's, for people who join by link in a browser.
// Built on its own into dist-guest; the relay serves it at example.com/room/<id> with its files under /g/.
//   pnpm exec vite build --config vite.guest.config.ts
// Dev: vite --config vite.guest.config.ts, with a relay on 127.0.0.1:8787 (relay/token_server.py) behind /token,
// /view, /push and LiveKit's /rtc.
import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const RELAY = process.env.BLOOM_RELAY || 'http://127.0.0.1:8787';

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  base: '/g/',
  server: {
    port: 1431,
    strictPort: true,
    proxy: { '/token': RELAY, '/view': RELAY, '/push': RELAY },
  },
  build: {
    target: 'es2020',
    outDir: 'dist-guest',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
    rollupOptions: { input: resolve(import.meta.dirname, 'guest.html') },
  },
});
