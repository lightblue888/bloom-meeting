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
    // the lobby's readiness card asks this every 10 s (the model names on the status rows)
    case 'GET /api/ready':
      return json(route, clone(w.current.ready ?? {}));
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
    case 'POST /api/provider/models':
      return json(route, w.providerModels ? { models: clone(w.providerModels) } : { error: 'Ollama 回了 404（接口地址或型号不对）：not found' });
    case 'POST /api/provider/lit':
      w.provider.lit = !!(body as { on?: boolean }).on;
      return json(route, clone(w.provider));
    case 'GET /api/usage':
      return json(route, clone(w.usageMonth));
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
    // Settings → General → Invites (relay/setup.sh's connection code)
    case 'GET /api/relay':
      return json(route, { mode: 'public', configured: true, invite_on: !!w.invite?.armed, host: 'relay.example.org', key_tail: '', managed: false });
    case 'POST /api/relay': {
      const b = body as { invite_on?: boolean; mode?: string; code?: string };
      if (typeof b.invite_on === 'boolean' && w.invite) w.invite.armed = b.invite_on;
      const mode = b.code ? 'own' : b.mode ?? 'public';
      return json(route, { mode, configured: mode !== 'off', invite_on: !!w.invite?.armed, host: mode === 'own' ? 'meet.example-university.edu.cn' : 'relay.example.org', key_tail: mode === 'own' ? '3f9a' : '', managed: mode === 'own' });
    }
    case 'POST /api/relay/test':
      return json(route, { ok: false, error: 'unreachable' });
    case 'GET /api/storage':
      return json(route, clone(w.storage));
    case 'POST /api/storage/recordings':
    case 'POST /api/storage/models':
      return json(route, { count: 1, bytes: 900_000_000 });
    case 'POST /api/storage/auto':
      return json(route, { count: 0, bytes: 0, auto_days: Number((body as { days?: number }).days ?? 0) });
    case 'POST /api/storage/caches':
      return json(route, { bytes: 250_000_000 });
    // 第 8 批: delete old transcripts and notes after N days; what was older goes at once (the preview said how many)
    case 'POST /api/storage/retention': {
      const days = Number((body as { days?: number }).days ?? 0);
      if (![0, 30, 90, 180, 365].includes(days)) return json(route, { error: 'days' }, 400);
      const st = w.storage as { records?: { retention?: { days: number; preview: Record<string, { count: number; bytes: number }> } } };
      const ret = st.records?.retention;
      const took = days && ret ? ret.preview[String(days)] ?? { count: 0, bytes: 0 } : { count: 0, bytes: 0 };
      if (ret) ret.days = days;
      return json(route, { ok: true, days, count: took.count, bytes: took.bytes });
    }
    case 'POST /api/recording/save':
      return json(route, { ok: true, path: 'C:\\Users\\maya\\Videos\\meeting.webm' });
    // the lobby: what the last meetings on this paper left undone; a tick is saved (and stays listed until a reload)
    case 'GET /api/carry':
      return json(route, { paper: w.current.paper?.name ?? null, items: clone(w.carry) });
    case 'POST /api/carry':
      return json(route, { ok: true, id: (body as { id?: string }).id ?? '', done: !!(body as { done?: boolean }).done });
    // 回顾 → 提问 (300 characters at most): the world's answer, or its error
    case 'POST /api/past/ask': {
      const q = String((body as { question?: string }).question ?? '').trim();
      if (!q) return json(route, { ok: false, error: 'empty' }, 400);
      if ([...q].length > 300) return json(route, { ok: false, error: 'too_long' }, 400);
      if (w.askError) return json(route, { ok: false, error: w.askError }, 502);
      return json(route, { ok: true, ...clone(w.askAnswer) });
    }
    // 回顾 → 阶段汇总: the saved ones; a new one starts and stays running
    case 'GET /api/digests':
      return json(route, w.digests.map(({ result: _r, markdown: _m, ...brief }) => clone(brief)));
    case 'POST /api/digests': {
      const mids = ((body as { mids?: string[] }).mids ?? []).filter(Boolean);
      if (mids.length < 2) return json(route, { ok: false, error: 'too_few' }, 400);
      // oldest first, as the backend keeps them
      const ms = w.meetings.filter((m) => mids.includes(m.id)).map((m) => ({ id: m.id, title: m.title, started: m.started }))
        .sort((a, b) => a.started.localeCompare(b.started));
      const d = { id: `d-${String(w.digests.length + 1).padStart(3, '0')}`, created: '2026-11-18T10:30:00', state: 'running' as const, error: null, mids, meetings: ms, title: '', result: null, markdown: '' };
      w.digests = [d, ...w.digests];
      return json(route, { ok: true, digest: clone(d) });
    }
    // 第 3 批, in the meeting: typed questions (500 characters), bookmarks, a selected passage (zuhui/live_api.py)
    case 'POST /api/ask': {
      const b = body as { text?: string };
      const q = String(b.text ?? '').trim();
      if (!q) return json(route, { ok: false, error: 'empty' }, 400);
      if ([...q].length > 500) return json(route, { ok: false, error: 'too_long' }, 400);
      if (w.current.meeting?.state !== 'live') return json(route, { ok: false, error: 'not_live' }, 409);
      w.current.transcript.push({ t: '20:31', sec: 1231, who: 'teacher', text: q, typed: true });
      return json(route, { ok: true });
    }
    case 'POST /api/bookmark': {
      const b = body as { note?: string; id?: string };
      if (String(b.note ?? '').length > 100) return json(route, { ok: false, error: 'too_long' }, 400);
      const list = (w.current.bookmarks ??= []);
      if (b.id) {
        const hit = list.find((x) => x.id === b.id);
        if (!hit) return json(route, { ok: false, error: 'not_found' }, 404);
        hit.note = String(b.note ?? '');
        return json(route, { ok: true, bookmark: clone(hit) });
      }
      const mk = { id: `b${list.length + 1}`, sec: 1231, t: '20:31', note: String(b.note ?? ''), by: w.current.participants?.find((x) => x.id === 'me')?.name ?? w.profile.name, host: true };
      list.push(mk);
      return json(route, { ok: true, bookmark: clone(mk) });
    }
    case 'POST /api/anchor': {
      const b = body as { text?: string; section?: string | null; clear?: boolean };
      w.current.anchor = b.clear ? null : { text: String(b.text ?? ''), section: b.section ?? null, by: null, at: NOW_S, left_s: 60 };
      return json(route, { ok: true, anchor: clone(w.current.anchor) });
    }
    // 第 5 批: Settings → General → 散会后自动保存 (a folder of more than dir_max characters is refused)
    case 'GET /api/autosave':
      return json(route, clone(w.autosave));
    case 'POST /api/autosave': {
      const b = body as { on?: boolean; dir?: string; items?: World['autosave']['items'] };
      if (typeof b.dir === 'string' && b.dir.length > w.autosave.dir_max) return json(route, { error: 'too_long' }, 400);
      const next = { ...w.autosave, ...(typeof b.dir === 'string' ? { dir: b.dir } : {}), ...(b.items ? { items: b.items } : {}), ...(typeof b.on === 'boolean' ? { on: b.on } : {}) };
      if (next.on && !next.dir) return json(route, { error: 'empty' }, 400);
      if (!next.items.length) return json(route, { error: 'items' }, 400);
      w.autosave = next;
      return json(route, clone(w.autosave));
    }
    // Settings → General → Notes: the format and the group's own requirements (2000 characters at most)
    case 'GET /api/summary/prefs':
      return json(route, clone(w.summaryPrefs));
    case 'POST /api/summary/prefs': {
      const b = body as { template?: World['summaryPrefs']['template']; custom?: string };
      if (typeof b.custom === 'string' && b.custom.trim().length > w.summaryPrefs.custom_max) return json(route, { error: 'too_long' }, 400);
      if (b.template) w.summaryPrefs.template = b.template;
      if (typeof b.custom === 'string') w.summaryPrefs.custom = b.custom.trim();
      return json(route, clone(w.summaryPrefs));
    }
    // 第 6 批: Settings → General → 论文 (Word 写回), the paper menu's 历史版本 (list, diff page, restore), comments
    case 'GET /api/paper/prefs':
      return json(route, { docx_mode: w.paperPrefs?.docx_mode ?? 'track' });
    case 'POST /api/paper/prefs': {
      const b = body as { docx_mode?: string };
      if (b.docx_mode !== 'track' && b.docx_mode !== 'direct') return json(route, { ok: false, error: 'docx_mode' }, 400);
      w.paperPrefs = { docx_mode: b.docx_mode };
      return json(route, { ok: true, docx_mode: b.docx_mode });
    }
    case 'GET /api/paper/versions':
      return json(route, clone(w.paperVersions ?? { paper: { id: w.current.active_paper ?? 'p1', name: w.current.paper?.name ?? '' }, items: [], current: null }));
    case 'GET /api/paper/versions/diff':
      return html(route, paperPage(w));
    case 'POST /api/paper/versions/restore': {
      const v = w.paperVersions;
      if (!v) return json(route, { ok: false, error: 'not_found' }, 404);
      if (v.live) return json(route, { ok: false, error: 'live' }, 409);
      return json(route, { ok: true, ...clone(v) });
    }
    case 'POST /api/comment':
      return json(route, { ok: true, comments: clone(w.current.comments ?? []) });
  }
  if ((m = path.match(/^\/api\/meetings\/([^/]+)$/)) && method === 'GET') {
    const d = w.details[decodeURIComponent(m[1])];
    return d ? json(route, clone(d)) : json(route, { error: 'not found' }, 404);
  }
  if ((m = path.match(/^\/api\/digests\/([^/]+)$/)) && method === 'GET') {
    const d = w.digests.find((x) => x.id === decodeURIComponent(m![1]));
    return d ? json(route, clone(d)) : json(route, { error: 'not_found' }, 404);
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
  // 第 7 批: 回顾 → 编辑 — a line, a word in the whole meeting, a guest's name, 撤销 (the world's copy changes in place)
  if ((m = path.match(/^\/api\/meetings\/([^/]+)\/transcript$/)) && method === 'POST') {
    const d = w.details[decodeURIComponent(m[1])];
    if (!d) return json(route, { ok: false, error: 'not_found' }, 404);
    if (d.processing || d.meeting.state === 'live') return json(route, { ok: false, error: d.processing ? 'busy' : 'live' }, 409);
    const b = body as { op?: string; i?: number; text?: string; find?: string; repl?: string; hot?: boolean; from?: string; to?: string };
    const ed = d.edits ?? { n: 0, last: null };
    const speaks = (l: { who: string }) => l.who !== 'system';
    if (b.op === 'undo') {
      if (!ed.n) return json(route, { ok: false, error: 'nothing' }, 400);
      d.edits = { n: ed.n - 1, last: null };
      return json(route, { ok: true, ...clone(d.edits) });
    }
    let last: NonNullable<typeof ed.last>;
    let count: number | undefined;
    if (b.op === 'line') {
      const l = d.transcript.find((x) => x.i === b.i);
      if (!l) return json(route, { ok: false, error: 'not_found' }, 404);
      if ((b.text ?? '').length > 2000) return json(route, { ok: false, error: 'too_long' }, 400);
      if (b.text?.trim()) Object.assign(l, { text: b.text.trim(), edited: true });
      else d.transcript = d.transcript.filter((x) => x !== l);
      last = { op: 'line', i: b.i!, text: String(b.text ?? '') };
    } else if (b.op === 'replace') {
      const f = String(b.find ?? '');
      count = d.transcript.reduce((n, l) => n + (speaks(l) ? l.text.split(f).length - 1 : 0), 0);
      if (!f || !count) return json(route, { ok: false, error: 'no_match' }, 400);
      for (const l of d.transcript) if (speaks(l) && l.text.includes(f)) Object.assign(l, { text: l.text.split(f).join(String(b.repl ?? '')), edited: true });
      last = { op: 'replace', find: f, repl: String(b.repl ?? '') };
    } else if (b.op === 'speaker') {
      if (!b.to?.trim()) return json(route, { ok: false, error: 'empty' }, 400);
      if (b.to === w.profile.name || b.to === w.profile.assistant) return json(route, { ok: false, error: 'taken' }, 400);
      for (const l of d.transcript) if (l.who === 'guest' && l.name === b.from) l.name = b.to;
      last = { op: 'speaker', from: String(b.from ?? ''), to: b.to };
    } else return json(route, { ok: false, error: 'bad' }, 400);
    d.edits = { n: ed.n + 1, last };
    return json(route, { ok: true, ...clone(d.edits), ...(count != null ? { count, hot: b.hot ? 'added' : undefined } : {}) });
  }
  // 第 5 批: 回顾 → 保存 → 复制（带格式） (the 纪要 as HTML and text) and 存成 Word / PDF / 导出会议包 (written where picked)
  if ((m = path.match(/^\/api\/meetings\/([^/]+)\/minutes$/)) && method === 'GET') {
    const d = w.details[decodeURIComponent(m[1])];
    if (!d?.summary) return json(route, { ok: false, error: d ? 'no_summary' : 'not_found' }, d ? 400 : 404);
    return json(route, { ok: true, html: `<h1>${d.summary.title}</h1><p>${d.summary.abstract}</p>`, text: `${d.summary.title}\n\n${d.summary.abstract}\n`, name: 'notes.docx' });
  }
  if ((m = path.match(/^\/api\/meetings\/([^/]+)\/save$/)) && method === 'POST') {
    const kind = String((body as { kind?: string }).kind ?? '');
    return json(route, { ok: true, path: `C:\\Users\\maya\\Documents\\meeting.${kind === 'pack' ? 'html' : kind}` });
  }
  // 回顾 → 纪要 → 重新生成: the job starts and stays running (a transition can make it fail)
  if ((m = path.match(/^\/api\/meetings\/([^/]+)\/summary$/)) && method === 'POST') {
    const d = w.details[decodeURIComponent(m[1])];
    if (!d) return json(route, { ok: false, error: 'not_found' }, 404);
    const job = { state: 'running' as const, template: (body as { template?: World['summaryPrefs']['template'] }).template ?? 'default', started: NOW_S, error: null };
    d.summary_job = job;
    return json(route, { ok: true, job });
  }
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
  if (what === 'act') {
    // what the guest asked for: the host's Bloom answers it (here, the world does); the next relay poll shows the change
    const a = (() => {
      try {
        return (req.postDataJSON() ?? {}) as { path?: string; body?: Record<string, unknown> };
      } catch {
        return {};
      }
    })();
    const res: Record<string, unknown> = { ok: true };
    if (a.path === '/api/bookmark' && r.state) {
      const mk = { id: `b${(r.state.bookmarks?.length ?? 0) + 1}`, sec: 1231, t: '20:31', note: String(a.body?.note ?? ''), by: 'guest', host: false };
      r.state.bookmarks = [...(r.state.bookmarks ?? []), mk];
      res.bookmark = mk;
      r.sv += 1;
    }
    r.lastAct = res;
    return json(route, { id: 'act-1' });
  }
  if (what.startsWith('result/')) return json(route, { done: true, result: r.lastAct ?? { ok: true } });
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
