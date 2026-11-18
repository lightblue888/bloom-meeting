// Served in place of /livekit-client.umd.min.js (and its CDN fallback) on the guest page: a LiveKit stand-in that
// "connects" at once, so the guest page shows its normal in-meeting state (voice connected) with no network.
// The audit can make it misbehave: __uiAudit.lk.emit('Reconnecting'), __uiAudit.lk.emit('Reconnected'),
// __uiAudit.lk.speak(['Emma Wang']).
(() => {
  const RoomEvent = new Proxy({}, { get: (_, k) => String(k) });
  const Track = { Source: { Microphone: 'microphone', Camera: 'camera' } };
  class Room {
    constructor() {
      this.h = {};
      this.state = 'disconnected';
      this.canPlaybackAudio = true;
      this.remoteParticipants = new Map();
      const room = this;
      this.localParticipant = {
        identity: 'me',
        name: '',
        isSpeaking: false,
        mic: null,
        getTrackPublication() {
          return this.mic;
        },
        async setMicrophoneEnabled(on) {
          this.mic = { isMuted: !on };
          room.emit(on ? 'TrackUnmuted' : 'TrackMuted');
        },
        publishData() {},
      };
    }
    on(e, f) {
      (this.h[e] = this.h[e] || []).push(f);
      return this;
    }
    emit(e, ...a) {
      (this.h[e] || []).forEach((f) => f(...a));
    }
    async connect() {
      this.state = 'connected';
      const A = window.__uiAudit;
      if (A) {
        A.lk = this;
        A.lk.speak = (names) => {
          for (const n of names) if (!this.remoteParticipants.has(n)) this.remoteParticipants.set(n, { name: n, identity: n, isSpeaking: false, metadata: '{}' });
          for (const [n, p] of this.remoteParticipants) p.isSpeaking = names.includes(n);
          this.emit('ActiveSpeakersChanged');
        };
      }
    }
    disconnect() {
      if (this.state === 'disconnected') return;
      this.state = 'disconnected';
      this.emit('Disconnected', 'CLIENT_INITIATED');
    }
    async startAudio() {}
  }
  window.LivekitClient = { Room, RoomEvent, Track, ConnectionQuality: { Poor: 'poor', Lost: 'lost' }, DisconnectReason: {}, version: 'ui-audit-fake' };
})();
