// The guest's voice in the meeting (LiveKit, loaded as the UMD global `LivekitClient` by guest.html), ported from the
// first guest page (relay/index.html): a fresh token on every (re)join, automatic rejoin with 1 / 2 / 4 s back-off
// (at most 6), the microphone as it was before a drop, a few events reported to the relay's diag log (never the token).
/* eslint-disable @typescript-eslint/no-explicit-any */

declare global {
  interface Window {
    LivekitClient?: any;
  }
}

export type ConnKind = '' | 'busy' | 'retry' | 'info';

export interface VoiceSnapshot {
  joined: boolean;
  connected: boolean;
  micOn: boolean;
  /** a line for the bar above the columns: reconnecting, cut off … ('' = nothing to say) */
  conn: string;
  connKind: ConnKind;
  /** the browser blocked the sound until a click */
  audioBlocked: boolean;
  /** who is talking now (their names; the host's and Claude's voices come in under the meeting's own identities) */
  speaking: string[];
}

const REASONS = ['UNKNOWN_REASON', 'CLIENT_INITIATED', 'DUPLICATE_IDENTITY', 'SERVER_SHUTDOWN', 'PARTICIPANT_REMOVED', 'ROOM_DELETED',
  'STATE_MISMATCH', 'JOIN_FAILURE', 'MIGRATION', 'SIGNAL_CLOSE', 'ROOM_CLOSED', 'USER_UNAVAILABLE', 'USER_REJECTED', 'SIP_TRUNK_FAILURE',
  'CONNECTION_TIMEOUT', 'MEDIA_FAILURE'];
const NO_AUTO: Record<string, 1> = { PARTICIPANT_REMOVED: 1, ROOM_DELETED: 1, ROOM_CLOSED: 1 };
const RJ_MAX = 6;

export class GuestVoice {
  room: any = null;
  name = '';
  token = '';
  private tokenAt = 0;
  private joined = false;
  private wantMic = false;
  private micLater = false;
  private quietUntil = 0;
  private gen = 0;
  private rj = { n: 0, timer: 0 as number, busy: false, stop: '' };
  private connMsg = '';
  private connKind: ConnKind = '';
  private audioBlocked = false;
  private lastQ = '';
  private diagLeft = 400;
  private t0 = 0;

  constructor(
    private readonly ROOM: string,
    private readonly L: (zh: string, en: string) => string,
    private readonly live: () => boolean,
    private readonly onChange: () => void,
    private readonly onData: (topic: string, msg: any) => void,
    private readonly toast: (msg: string) => void,
  ) {
    document.addEventListener('visibilitychange', () => {
      if (!this.joined) return;
      this.diag('visibilitychange', document.visibilityState);
      if (document.hidden) return;
      if (this.micLater && this.room) void this.pubMic(false).then(() => this.onChange());
      this.wake();
    });
    window.addEventListener('pagehide', (e) => { if (this.joined) this.diag('pagehide', (e as PageTransitionEvent).persisted ? 'persisted' : ''); });
    window.addEventListener('pageshow', (e) => { if (this.joined && (e as PageTransitionEvent).persisted) { this.diag('pageshow', 'persisted'); this.wake(); } });
    window.addEventListener('beforeunload', () => { if (this.room) this.room.disconnect(); });
  }

  snapshot(): VoiceSnapshot {
    const LK = window.LivekitClient;
    let micOn = false;
    const speaking: string[] = [];
    if (this.room && LK) {
      const pub = this.room.localParticipant.getTrackPublication(LK.Track.Source.Microphone);
      micOn = !!(pub && !pub.isMuted);
      for (const p of [this.room.localParticipant, ...Array.from(this.room.remoteParticipants.values()) as any[]]) {
        if (p.isSpeaking) speaking.push(String(p.name || p.identity || ''));
      }
    }
    return { joined: this.joined, connected: !!this.room, micOn, conn: this.connMsg, connKind: this.connKind,
      audioBlocked: this.audioBlocked, speaking };
  }

  /** a token for this room in this name: the voice's own when fresh, else a new one (the guest page's actions use it too) */
  async getToken(): Promise<string> {
    if (this.token && Date.now() - this.tokenAt < 5 * 3600 * 1000) return this.token;
    const d = await this.tokenFor(this.name);
    return d.token as string;
  }

  /** the key the relay gave this browser for this name in this room: the name stays ours for the meeting (a refresh, a
   *  rejoin bring it along; someone else typing the same name is turned away), see relay/token_server.py _claim.
   *  Kept under a hash of room + name: the room number is the meeting's password, never written into storage as it is */
  private claims: Record<string, string> = {};
  private async claimKey(name: string): Promise<string> {
    const plain = `${this.ROOM}\n${name.split(/\s+/).filter(Boolean).join(' ').toLowerCase()}`;
    try {
      const h = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(plain)));
      return 'bloom.claim.' + Array.from(h.slice(0, 12), (b) => b.toString(16).padStart(2, '0')).join('');
    } catch {
      return '';                                     // no WebCrypto (not https): kept in memory only
    }
  }

  private async tokenFor(name: string): Promise<any> {
    const ck = await this.claimKey(name);
    let claim = this.claims[name] || '';
    try { claim = claim || (ck && localStorage.getItem(ck)) || ''; } catch { /* private mode: kept in memory only */ }
    const r = await fetch(`/token?room=${encodeURIComponent(this.ROOM)}&name=${encodeURIComponent(name)}${claim ? `&claim=${encodeURIComponent(claim)}` : ''}`,
      { cache: 'no-store' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e: any = new Error(d.error === 'name_taken' ? this.L('与会上其他人重名', 'Someone in the meeting already uses this name') : (d.error || `HTTP ${r.status}`));
      e.tokenStatus = d.error === 'name_taken' ? 403 : r.status;
      throw e;
    }
    if (typeof d.claim === 'string' && d.claim) {
      this.claims[name] = d.claim;
      try { if (ck) localStorage.setItem(ck, d.claim); } catch { /* private mode */ }
    }
    this.token = d.token;
    this.tokenAt = Date.now();
    return d;
  }

  private setConn(msg: string, kind: ConnKind = '') {
    this.connMsg = msg;
    this.connKind = msg ? kind : '';
    this.onChange();
  }

  diag(ev: string, why?: unknown, extra?: Record<string, unknown>) {
    if (!this.ROOM || this.diagLeft <= 0) return;
    this.diagLeft--;
    try {
      let w = why == null ? '' : String(why);
      w = w.replace(/eyJ[\w-]+\.[\w-]+(\.[\w-]*)?/g, '[jwt]').replace(/(access_token=)[^&\s]*/g, '$1-');
      const o: Record<string, unknown> = { ev, id: this.name, why: w.slice(0, 120), vis: document.visibilityState === 'hidden' ? 'h' : 'v',
        st: this.room ? String(this.room.state || '') : '-', n: this.rj.n, t: this.t0 ? Math.round((Date.now() - this.t0) / 1000) : 0, ui: 2, ...extra };
      let body = JSON.stringify(o);
      if (new TextEncoder().encode(body).length > 1000) { o.ua = String(o.ua || '').slice(0, 160); o.why = w.slice(0, 40); body = JSON.stringify(o); }
      const url = `/view/${encodeURIComponent(this.ROOM)}/diag`;
      let sent = false;
      try { sent = !!(navigator.sendBeacon && navigator.sendBeacon(url, body)); } catch { /* */ }
      if (!sent) fetch(url, { method: 'POST', body, keepalive: true, headers: { 'Content-Type': 'text/plain;charset=UTF-8' } }).catch(() => {});
    } catch { /* never let diagnostics break the page */ }
  }

  private reasonName(r: unknown): string {
    if (r == null) return 'NONE';
    const t = window.LivekitClient?.DisconnectReason;
    return (t && typeof t[r as any] === 'string' && t[r as any]) || REASONS[r as number] || String(r);
  }

  join(name: string, mic: boolean) {
    this.gen++;
    this.name = name;
    this.t0 = Date.now();
    this.joined = true;
    this.wantMic = mic;
    this.micLater = false;
    this.rj = { n: 0, timer: 0, busy: false, stop: '' };
    this.diag('join', '', { ua: navigator.userAgent.slice(0, 300), lk: window.LivekitClient?.version || 'none' });
    this.voice(false);
  }

  leave(cause = 'user') {
    if (this.joined) this.diag('leave', cause);
    this.gen++;
    clearTimeout(this.rj.timer);
    this.rj = { n: 0, timer: 0, busy: false, stop: '' };
    this.micLater = false;
    const r = this.room;
    this.room = null;
    this.joined = false;
    if (r) r.disconnect();
    document.querySelectorAll('audio[data-sid]').forEach((el) => el.remove());
    this.setConn('');
  }

  async toggleMic(): Promise<boolean> {
    const LK = window.LivekitClient;
    if (!this.room || !LK) {
      this.toast(this.joined && (this.rj.busy || this.rj.timer) ? this.L('正在重新连接…', 'Reconnecting…')
        : this.L('语音未连接', 'Voice not connected'));
      return false;
    }
    const r = this.room;
    const pub = r.localParticipant.getTrackPublication(LK.Track.Source.Microphone);
    const on = !!(pub && !pub.isMuted);
    try {
      await r.localParticipant.setMicrophoneEnabled(!on);
      if (r === this.room) { this.wantMic = !on; this.micLater = false; }
    } catch {
      this.toast(this.L('未获得麦克风权限', 'No microphone permission'));
    }
    this.onChange();
    return this.snapshot().micOn;
  }

  startAudio() {
    if (this.room) this.room.startAudio().catch(() => {});
  }

  retry() {
    if (!this.joined || this.room || this.rj.busy) return;
    this.rj.stop = '';
    clearTimeout(this.rj.timer);
    this.rj.timer = 0;
    this.rj.n = 0;
    this.diag('rejoin_click');
    this.voice(true);
  }

  publish(obj: unknown) {
    try {
      this.room?.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(obj)), { reliable: true, topic: 'bloom' });
    } catch { /* not connected: nothing to tell */ }
  }

  private wire(r: any) {
    const LK = window.LivekitClient;
    const RE = LK.RoomEvent;
    const CQ = LK.ConnectionQuality || {};
    const mine = () => r === this.room;
    const isBloom = (p: any) => { try { return !!JSON.parse(p.metadata || '{}').bloom_host; } catch { return false; } };
    r.on(RE.ParticipantConnected, (p: any) => {
      if (mine() && !isBloom(p) && Date.now() > this.quietUntil) this.toast(`${p.name || p.identity} ${this.L('加入会议', 'joined')}`);
      this.onChange();
    })
      .on(RE.ParticipantDisconnected, (p: any) => { if (mine() && !isBloom(p)) this.toast(`${p.name || p.identity} ${this.L('离开会议', 'left')}`); this.onChange(); })
      .on(RE.ActiveSpeakersChanged, () => this.onChange())
      .on(RE.TrackMuted, () => this.onChange())
      .on(RE.TrackUnmuted, () => this.onChange())
      .on(RE.TrackSubscribed, (track: any, pub: any) => {
        if (track.kind === 'audio') {
          const el = track.attach();
          el.dataset.sid = pub.trackSid;
          document.body.appendChild(el);
        }
        this.onChange();
      })
      .on(RE.TrackUnsubscribed, (track: any) => { track.detach().forEach((el: HTMLElement) => el.remove()); this.onChange(); })
      .on(RE.AudioPlaybackStatusChanged, () => {
        if (!mine()) return;
        this.audioBlocked = !r.canPlaybackAudio;
        if (this.audioBlocked) this.diag('AudioPlaybackStatusChanged', 'blocked');
        this.onChange();
      })
      .on(RE.DataReceived, (payload: Uint8Array, _p: any, _kind: any, topic: string) => {
        if (topic !== 'bloom' && topic !== 'bloom-s') return;
        let m: any;
        try { m = JSON.parse(new TextDecoder().decode(payload)); } catch { return; }
        this.onData(topic, m);
      })
      .on(RE.SignalReconnecting, () => { if (!mine()) return; this.diag('SignalReconnecting'); this.setConn(this.L('网络不稳定，正在重连…', 'Unstable network, reconnecting…'), 'busy'); })
      .on(RE.Reconnecting, () => { if (!mine()) return; this.diag('Reconnecting'); this.setConn(this.L('网络不稳定，正在重连…', 'Unstable network, reconnecting…'), 'busy'); })
      .on(RE.Reconnected, () => { if (!mine()) return; this.diag('Reconnected'); this.setConn(''); })
      .on(RE.ConnectionQualityChanged, (q: string, p: any) => {
        if (!mine() || !p || p !== r.localParticipant || q === this.lastQ) return;
        this.lastQ = q;
        if (q === (CQ.Poor || 'poor') || q === (CQ.Lost || 'lost')) this.diag('ConnectionQualityChanged', q);
      })
      .on(RE.Disconnected, (reason: unknown) => this.onDisconnected(r, reason));
  }

  private onDisconnected(r: any, reason: unknown) {
    const why = this.reasonName(reason);
    this.diag('Disconnected', why + (r === this.room ? '' : '/old'));
    if (r !== this.room) return;
    this.room = null;
    this.lastQ = '';
    document.querySelectorAll('audio[data-sid]').forEach((el) => el.remove());
    this.onChange();
    if (!this.joined) return;
    if (why === 'DUPLICATE_IDENTITY') {
      this.rj.stop = why;
      this.setConn(this.L('语音已断开：有人用相同名字入会，请换名字后重新加入', 'Voice disconnected: someone joined with the same name. Rejoin with another name.'), 'info');
      return;
    }
    if (NO_AUTO[why]) { this.rj.stop = why; this.setConn(this.L('语音已断开', 'Voice disconnected'), 'retry'); return; }
    this.rejoinSoon();
  }

  private rejoinSoon() {
    if (!this.joined || this.room || this.rj.busy || this.rj.timer || this.rj.stop) return;
    if (!this.live()) { this.setConn(''); return; }
    if (this.rj.n >= RJ_MAX) { this.diag('rejoin_giveup'); this.setConn(this.L('语音已断开', 'Voice disconnected'), 'retry'); return; }
    this.setConn(this.L('正在重新连接…', 'Reconnecting…'), 'busy');
    this.rj.timer = window.setTimeout(() => { this.rj.timer = 0; this.voice(true); }, Math.min(4000, 1000 * 2 ** this.rj.n));
  }

  private async pubMic(first: boolean) {
    const r = this.room;
    if (!r || !this.wantMic) return;
    if (document.hidden) { this.micLater = true; return; }
    this.micLater = false;
    try {
      await r.localParticipant.setMicrophoneEnabled(true);
    } catch (e: any) {
      if (r !== this.room) return;
      this.wantMic = false;
      this.diag('mic_fail', e && (e.name || e.message));
      this.toast(first ? this.L('未获得麦克风权限，已静音入会', 'No microphone permission: joined muted')
        : this.L('麦克风开启失败，请手动打开', 'Microphone failed to turn on: turn it on by hand'));
    }
    this.onChange();
  }

  private voice(again: boolean) {
    if (!this.joined || this.room || this.rj.busy) return;
    if (!this.live()) { this.setConn(''); return; }
    const LK = window.LivekitClient;
    if (!LK) { this.setConn(this.L('语音加载失败，请刷新重试', 'Voice failed to load. Refresh to try again.'), 'info'); return; }
    const g = this.gen;
    let r: any = null;
    const t1 = Date.now();
    this.rj.busy = true;
    if (again) { this.rj.n++; this.diag('rejoin'); this.setConn(this.L('正在重新连接…', 'Reconnecting…'), 'busy'); }
    this.tokenFor(this.name)
      .then((d) => {
        if (g !== this.gen) throw { abort: 1 };
        r = new LK.Room({ adaptiveStream: true, dynacast: true });
        this.wire(r);
        this.room = r;
        this.lastQ = '';
        return r.connect((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host, d.token);
      })
      .then(
        () => {
          if (g !== this.gen) { try { r.disconnect(); } catch { /* */ } return; }
          this.rj.busy = false;
          if (r !== this.room) { try { r.disconnect(); } catch { /* */ } this.rejoinSoon(); return; }
          this.diag(again ? 'rejoined' : 'connected', `${Date.now() - t1}ms`);
          this.rj.n = 0;
          this.quietUntil = again ? Date.now() + 2500 : 0;
          this.setConn('');
          void this.pubMic(!again).then(() => { if (g === this.gen) this.onChange(); });
        },
        (e: any) => {
          if (g !== this.gen || (e && e.abort)) return;
          this.rj.busy = false;
          if (r && r === this.room) { this.room = null; try { r.disconnect(); } catch { /* */ } }
          const msg = (e && e.message) || String(e);
          this.diag(again ? 'rejoin_fail' : 'connect_fail', msg);
          this.onChange();
          if (!this.joined) return;
          if (e && e.tokenStatus === 403) { this.setConn(this.L(`语音连接失败：${msg}`, `Voice connection failed: ${msg}`), 'info'); return; }
          this.rejoinSoon();
        },
      );
  }

  private wake() {
    if (!this.joined || this.room || this.rj.busy || this.rj.stop) return;
    clearTimeout(this.rj.timer);
    this.rj.timer = 0;
    this.rj.n = 0;
    this.voice(true);
  }
}
