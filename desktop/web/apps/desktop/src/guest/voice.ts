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
  level: number;
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
  private meter: { ctx: AudioContext | null; an: AnalyserNode | null; src: MediaStreamAudioSourceNode | null; timer: number; track: MediaStreamTrack | null; lv: number } =
    { ctx: null, an: null, src: null, timer: 0, track: null, lv: 0 };

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
      audioBlocked: this.audioBlocked, speaking, level: this.meter.lv };
  }

  /** a token for this room in this name: the voice's own when fresh, else a new one (the guest page's actions use it too) */
  async getToken(): Promise<string> {
    if (this.token && Date.now() - this.tokenAt < 5 * 3600 * 1000) return this.token;
    const d = await this.tokenFor(this.name);
    return d.token as string;
  }

  private async tokenFor(name: string): Promise<any> {
    const r = await fetch(`/token?room=${encodeURIComponent(this.ROOM)}&name=${encodeURIComponent(name)}&role=guest`, { cache: 'no-store' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e: any = new Error(d.error || `HTTP ${r.status}`);
      e.tokenStatus = r.status;
      throw e;
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
    this.meterStop();
    document.querySelectorAll('audio[data-sid]').forEach((el) => el.remove());
    this.setConn('');
  }

  async toggleMic(): Promise<boolean> {
    const LK = window.LivekitClient;
    if (!this.room || !LK) {
      this.toast(this.joined && (this.rj.busy || this.rj.timer) ? this.L('正在重新连接，连上后麦克风照原样', 'Reconnecting: the microphone comes back as it was')
        : this.L('语音没连上', 'Voice is not connected'));
      return false;
    }
    const r = this.room;
    const pub = r.localParticipant.getTrackPublication(LK.Track.Source.Microphone);
    const on = !!(pub && !pub.isMuted);
    try {
      await r.localParticipant.setMicrophoneEnabled(!on);
      if (r === this.room) { this.wantMic = !on; this.micLater = false; }
    } catch {
      this.toast(this.L('没拿到麦克风权限', 'No microphone permission'));
    }
    this.micUi();
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
      if (mine() && !isBloom(p) && Date.now() > this.quietUntil) this.toast(`${p.name || p.identity} ${this.L('进来了', 'joined')}`);
      this.onChange();
    })
      .on(RE.ParticipantDisconnected, (p: any) => { if (mine() && !isBloom(p)) this.toast(`${p.name || p.identity} ${this.L('离开了', 'left')}`); this.onChange(); })
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
      .on(RE.SignalReconnecting, () => { if (!mine()) return; this.diag('SignalReconnecting'); this.setConn(this.L('网络不稳，正在重连…', 'Unstable network, reconnecting…'), 'busy'); })
      .on(RE.Reconnecting, () => { if (!mine()) return; this.diag('Reconnecting'); this.setConn(this.L('网络不稳，正在重连…', 'Unstable network, reconnecting…'), 'busy'); })
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
    this.micUi();
    if (!this.joined) return;
    if (why === 'DUPLICATE_IDENTITY') {
      this.rj.stop = why;
      this.setConn(this.L('有人用同一个名字进了会，你这边的语音断开了。换个名字刷新再进', 'Someone joined with your name, so your voice was cut. Refresh and join with another name.'), 'info');
      return;
    }
    if (NO_AUTO[why]) { this.rj.stop = why; this.setConn(this.L('语音断开了，论文和逐字稿照常更新', 'Voice disconnected; the paper and transcript still update'), 'retry'); return; }
    this.rejoinSoon();
  }

  private rejoinSoon() {
    if (!this.joined || this.room || this.rj.busy || this.rj.timer || this.rj.stop) return;
    if (!this.live()) { this.setConn(''); return; }
    if (this.rj.n >= RJ_MAX) { this.diag('rejoin_giveup'); this.setConn(this.L('语音断开了，论文和逐字稿照常更新', 'Voice disconnected; the paper and transcript still update'), 'retry'); return; }
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
      this.toast(first ? this.L('没拿到麦克风权限，先以静音进会', 'No microphone permission: joined muted')
        : this.L('麦克风没重新打开，点一下麦克风按钮再开', 'The microphone did not come back: click the microphone to turn it on'));
    }
    this.micUi();
  }

  private voice(again: boolean) {
    if (!this.joined || this.room || this.rj.busy) return;
    if (!this.live()) { this.setConn(''); return; }
    const LK = window.LivekitClient;
    if (!LK) { this.setConn(this.L('语音模块没加载上：听不到、也说不了，论文和逐字稿照常更新。刷新一下可以重试。', 'The voice module did not load: you can’t hear or talk, the paper and transcript still update. Refresh to try again.'), 'info'); return; }
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
          this.micUi();
          if (!this.joined) return;
          if (e && e.tokenStatus === 403) { this.setConn(this.L(`语音没连上（${msg}），论文和逐字稿照常更新。`, `Voice did not connect (${msg}); the paper and transcript still update.`), 'info'); return; }
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

  // ---------- the level beside one's own microphone (25 times a second is smooth enough) ----------
  private micUi() {
    if (this.snapshot().micOn) this.meterStart();
    else this.meterStop();
    this.onChange();
  }

  private meterStop() {
    clearInterval(this.meter.timer);
    this.meter.timer = 0;
    try { this.meter.src?.disconnect(); } catch { /* */ }
    this.meter.src = null;
    this.meter.an = null;
    this.meter.track = null;
    this.meter.lv = 0;
  }

  private meterStart() {
    try {
      const LK = window.LivekitClient;
      const pub = this.room && this.room.localParticipant.getTrackPublication(LK.Track.Source.Microphone);
      const mt: MediaStreamTrack | undefined = pub && pub.track && pub.track.mediaStreamTrack;
      if (!mt || this.meter.track === mt) return;
      this.meterStop();
      const AC = window.AudioContext || (window as any).webkitAudioContext;
      if (!AC) return;
      this.meter.ctx = this.meter.ctx || new AC();
      if (this.meter.ctx.state === 'suspended') this.meter.ctx.resume().catch(() => {});
      this.meter.src = this.meter.ctx.createMediaStreamSource(new MediaStream([mt]));
      const an = this.meter.ctx.createAnalyser();
      an.fftSize = 512;
      this.meter.src.connect(an);
      this.meter.an = an;
      this.meter.track = mt;
      const buf = new Uint8Array(an.fftSize);
      let last = -1;
      this.meter.timer = window.setInterval(() => {
        if (!this.meter.an) return;
        this.meter.an.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) { const x = (buf[i] - 128) / 128; sum += x * x; }
        const db = 20 * Math.log10(Math.sqrt(sum / buf.length) + 1e-6);
        const v = Math.min(1, Math.max(0, (db + 52) / 36));
        this.meter.lv = v > this.meter.lv ? v : this.meter.lv * 0.82 + v * 0.18;
        const k = Math.round(this.meter.lv * 20);
        if (k !== last) { last = k; this.onChange(); }
      }, 40);
    } catch { /* no level: the button still works */ }
  }
}
