// Every request the page makes outside the Vite dev server is answered here, from the test's World:
//   · the Bloom backend (src/meetingApi.ts ZUHUI_BASE) — the page is opened with ?zuhui=<this origin>/__zuhui, so its
//     calls stay on the audit's own origin and never reach the owner's live backend on 127.0.0.1:47330;
//   · the guest relay (/view/<room>/…, /token) and the LiveKit script the guest page loads;
//   · anything else (another host, the real backend port) is refused and recorded.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { deflateSync } from 'node:zlib';
import type { BrowserContext, Route } from '@playwright/test';
import { clone, NOW_S, type World } from '../fixtures/world';

const HERE = import.meta.dirname;
const PAPER = readFileSync(resolve(HERE, '../fixtures/paper.html'), 'utf8');
const FAKE_LK = readFileSync(resolve(HERE, 'fake-livekit.js'), 'utf8');
const VIDEO = (() => {
  try {
    return readFileSync(resolve(HERE, '../fixtures/rec.webm'));
  } catch {
    return null;
  }
})();

/** what happened on the wire during a test: the report lists these */
export class Recorder {
  console: { type: string; text: string; url?: string }[] = [];
  pageErrors: string[] = [];
  failed: { url: string; error: string }[] = [];
  badStatus: { url: string; status: number }[] = [];
  unmocked: string[] = [];
  external: string[] = [];
  leaks: string[] = [];
  reset() {
    this.console = [];
    this.pageErrors = [];
    this.failed = [];
    this.badStatus = [];
  }
}

/** a 64×64 PNG in one colour (avatars) */
function png(rgb: [number, number, number]): Buffer {
  const w = 64;
  const h = 64;
  const raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) {
    raw[y * (w * 3 + 1)] = 0;
    for (let x = 0; x < w; x++) {
      const o = y * (w * 3 + 1) + 1 + x * 3;
      const d = (x - 32) ** 2 + (y - 26) ** 2 < 140 || ((x - 32) ** 2) / 1.2 + (y - 64) ** 2 < 700;
      raw[o] = d ? 255 : rgb[0];
      raw[o + 1] = d ? 255 : rgb[1];
      raw[o + 2] = d ? 255 : rgb[2];
    }
  }
  const crcTable = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc = (b: Buffer) => {
    let c = 0xffffffff;
    for (const x of b) c = crcTable[(c ^ x) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type: string, data: Buffer) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(type), data]);
    const c = Buffer.alloc(4);
    c.writeUInt32BE(crc(td));
    return Buffer.concat([len, td, c]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8;
  ihdr[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}
const AVATARS = [png([52, 120, 200]), png([180, 90, 140]), png([60, 150, 110])];

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) });
const sse = (route: Route, ...msgs: unknown[]) =>
  route.fulfill({ status: 200, contentType: 'text/event-stream; charset=utf-8', headers: { 'Cache-Control': 'no-store' }, body: msgs.map((m) => `data: ${JSON.stringify(m)}\n\n`).join('') });
const html = (route: Route, body: string) => route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body });

function paperPage(w: World) {
  return PAPER.replace('<script>', `<script>history.replaceState(null, '', location.pathname + '?st=' + encodeURIComponent(${JSON.stringify(JSON.stringify(w.paper))}));</script><script>`);
}

/** the brain as the push channel sends it: "now" is the audit's frozen clock */
const brainNow = (w: World) => (w.current.brain ? { ...clone(w.current.brain), now: NOW_S } : null);

async function zuhui(route: Route, w: World, rec: Recorder) {
  const req = route.request();
  const u = new URL(req.url());
  const path = u.pathname.replace(/^\/__zuhui/, '') || '/';
  const method = req.method();
  const body = (() => {
    try {
      return req.postDataJSON() ?? {};
    } catch {
      return {};
    }
  })();
  if (!w.backendUp) return route.abort('connectionrefused');

  // the revision page (live) and a past meeting's snapshot: the stub
  if (path === '/' || path.startsWith('/snap/')) return html(route, paperPage(w));
  if (path.startsWith('/avatars/')) {
    if (path === '/avatars/me' && !w.avatarMe) return route.fulfill({ status: 404, body: '' });
    const k = path.length % AVATARS.length;
    return route.fulfill({ status: 200, contentType: 'image/png', body: AVATARS[k] });
  }

  let m: RegExpMatchArray | null;
  switch (`${method} ${path}`) {
    case 'GET /api/current':
      return json(route, clone(w.current));
    case 'GET /api/meetings':
      return json(route, clone(w.meetings));
    case 'GET /api/dev/runs':
      return json(route, []);
    case 'GET /api/models':
      return w.models ? json(route, clone(w.models)) : json(route, { error: 'unavailable' }, 503);
    case 'POST /api/models': {
      if (!w.models) return json(route, { ok: false, error: 'unavailable' }, 503);
      const part = (body as { part?: 'dialog' | 'edit' | 'effort' }).part;
      const c = part ? w.models[part] : undefined;
      if (c) c.current = String((body as { value?: string }).value);
      return json(route, { ok: true, ...clone(w.models) });
    }
    case 'GET /api/devices':
      return json(route, clone(w.devices));
    case 'POST /api/mic/mute':
      w.mic.muted = !!(body as { muted?: boolean }).muted;
      return json(route, { muted: w.mic.muted });
    case 'GET /api/mic/stream':
      return sse(route, { muted: w.mic.muted, talk: w.mic.talk });
    case 'GET /api/brain/stream':
      return sse(route, brainNow(w));
    case 'GET /api/profile':
      return json(route, clone(w.profile));
    case 'POST /api/profile': {
      if (w.profileError) {
        const e = w.profileError;
        w.profileError = null;
        return json(route, { error: e }, 400);
      }
      const b = body as Record<string, string>;
      if (typeof b.name === 'string') w.profile.name = b.name.trim();
      if (typeof b.assistant === 'string') {
        w.profile.assistant = b.assistant.trim() || w.profile.assistant_default;
        w.profile.assistant_custom = !!b.assistant.trim();
      }
      if (b.color) w.profile.color = b.color;
      if (b.ai_color) w.profile.ai_color = b.ai_color;
      return json(route, clone(w.profile));
    }
    case 'POST /api/profile/avatar':
      if ((body as { remove?: boolean }).remove) Object.assign(w.profile, { avatar: null, custom: false });
      else Object.assign(w.profile, { avatar: '/avatars/me', custom: true });
      w.avatarMe = !!w.profile.avatar;
      return json(route, clone(w.profile));
    case 'GET /api/provider':
      return json(route, clone(w.provider));
    case 'POST /api/provider': {
      const b = body as Partial<World['provider']> & { key?: string };
      Object.assign(w.provider, { kind: b.kind ?? w.provider.kind, base_url: b.base_url ?? w.provider.base_url, fast: b.fast ?? w.provider.fast, main: b.main ?? w.provider.main });
      if (b.key) Object.assign(w.provider, { key_set: true, key_tail: b.key.slice(-4) });
      return json(route, clone(w.provider));
    }
    case 'POST /api/provider/test':
      return json(route, { ok: true, ms: 820 });
    case 'GET /api/invite':
      return json(route, clone(w.invite));
    case 'POST /api/roles':
    case 'POST /api/roles/dismiss':
      return json(route, { ok: true });
    case 'GET /api/paper/status':
      return json(route, { busy: false });
    case 'POST /api/paper':
      return json(route, { ok: true, paper: { ...clone(w.current.paper), added: true } });
    case 'POST /api/paper/switch':
      return json(route, { ok: true, paper: clone(w.current.paper) });
    case 'POST /api/meeting/end':
      return json(route, { ok: true });
    case 'GET /api/setup':
      return json(route, clone(w.setup));
    case 'POST /api/setup': {
      const b = body as { done?: boolean; skipped?: boolean; reset?: boolean };
      if (b.reset) Object.assign(w.setup, { done: false, skipped: false });
      if (b.done) w.setup.done = true;
      if (b.skipped) w.setup.skipped = true;
      return json(route, clone(w.setup));
    }
    case 'GET /api/voice':
      return json(route, clone(w.voice));
    case 'POST /api/voice':
      Object.assign(w.voice, body);
      return json(route, clone(w.voice));
    case 'POST /api/voice/test':
      return json(route, { ok: true, engine: 'edge' });
    case 'GET /api/storage':
      return json(route, clone(w.storage));
    case 'POST /api/storage/recordings':
    case 'POST /api/storage/models':
      return json(route, { count: 1, bytes: 900_000_000 });
    case 'POST /api/storage/auto':
      return json(route, { count: 0, bytes: 0, auto_days: Number((body as { days?: number }).days ?? 0) });
    case 'POST /api/storage/caches':
      return json(route, { bytes: 250_000_000 });
    case 'POST /api/recording/save':
      return json(route, { ok: true, path: 'C:\\Users\\maya\\Videos\\meeting.webm' });
  }
  if ((m = path.match(/^\/api\/meetings\/([^/]+)$/)) && method === 'GET') {
    const d = w.details[decodeURIComponent(m[1])];
    return d ? json(route, clone(d)) : json(route, { error: 'not found' }, 404);
  }
  if ((m = path.match(/^\/api\/meetings\/([^/]+)\/video$/))) {
    if (!VIDEO) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: 'video/webm', body: VIDEO });
  }
  if ((m = path.match(/^\/api\/meetings\/([^/]+)\/title$/)) && method === 'POST') {
    const id = decodeURIComponent(m[1]);
    const t = String((body as { title?: string }).title ?? '');
    const mt = w.current.meeting?.id === id ? w.current.meeting : w.details[id]?.meeting;
    if (!mt) return json(route, { ok: false, error: 'not found' }, 404);
    mt.title = t || mt.title;
    mt.title_auto = !t;
    return json(route, { ok: true, meeting: clone(mt) });
  }
  if ((m = path.match(/^\/api\/meetings\/([^/]+)\/delete$/)) && method === 'POST') {
    const id = decodeURIComponent(m[1]);
    w.meetings = w.meetings.filter((x) => x.id !== id);
    delete w.details[id];
    return json(route, { ok: true, recording: { mb: 812, deleted: true } });
  }
  if ((m = path.match(/^\/api\/meetings\/([^/]+)\/export$/))) return json(route, { ok: true, name: 'meeting.md', text: '# meeting\n' });
  rec.unmocked.push(`${method} ${path}`);
  return json(route, { error: 'not mocked by ui-audit' }, 404);
}

async function relay(route: Route, w: World, rec: Recorder) {
  const req = route.request();
  const u = new URL(req.url());
  const parts = u.pathname.split('/').filter(Boolean); // view, <room>, what…
  const what = parts.slice(2).join('/');
  const r = w.relay;
  if (what === 'state') {
    const sv = Number(u.searchParams.get('sv'));
    if (sv === r.sv) return json(route, { same: true, sv: r.sv, pv: r.pv });
    return json(route, { sv: r.sv, pv: r.pv, state: r.state ? clone(r.state) : null });
  }
  if (what === 'lines') return json(route, { transcript: clone(r.transcript), board: r.board ? clone(r.board) : null });
  if (what === 'paper') return html(route, paperPage(w));
  if (what === 'act') return json(route, { id: 'act-1' });
  if (what.startsWith('result/')) return json(route, { done: true, result: { ok: true } });
  if (what === 'diag') return route.fulfill({ status: 204, body: '' });
  if (what.startsWith('upload')) return json(route, { id: 'up-1' });
  if (what.startsWith('avatar')) return route.fulfill({ status: 200, contentType: 'image/png', body: AVATARS[0] });
  rec.unmocked.push(`${req.method()} ${u.pathname}`);
  return json(route, { error: 'not mocked by ui-audit' }, 404);
}

/** route everything for one browser context; `world()` is read on every request (a transition may replace it) */
export async function installMocks(context: BrowserContext, origin: string, world: () => World, rec: Recorder) {
  const own = new URL(origin);
  // last registered runs first (Playwright): the catch-all goes in first
  await context.route(
    (url) => url.host !== own.host || url.protocol !== own.protocol,
    (route) => {
      const url = route.request().url();
      const u = new URL(url);
      if (/livekit-client.*\.js$/.test(u.pathname)) return route.fulfill({ status: 200, contentType: 'text/javascript', body: FAKE_LK });
      if ((u.hostname === '127.0.0.1' || u.hostname === 'localhost') && u.port === '47330') rec.leaks.push(url);
      else if (!/^(data|blob|about|chrome-extension):/.test(url)) rec.external.push(url);
      return route.abort('blockedbyclient');
    },
  );
  await context.route(`${origin}/__zuhui/**`, (route) => zuhui(route, world(), rec));
  await context.route(`${origin}/__zuhui`, (route) => zuhui(route, world(), rec));
  await context.route(`${origin}/view/**`, (route) => relay(route, world(), rec));
  await context.route(`${origin}/token**`, (route) => json(route, { token: 'ui-audit-token', url: 'ws://ui-audit.invalid' }));
  // the guest page's avatars are relay-relative (/avatars/…)
  await context.route(`${origin}/avatars/**`, (route) => route.fulfill({ status: 200, contentType: 'image/png', body: AVATARS[1] }));
  await context.route(`${origin}/livekit-client.umd.min.js`, (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: FAKE_LK }));
}
