// Bloom's main window: the rail (avatar, 会议, 回顾, the gear), and the page — 会议 (MeetingCurrent: the paper, the
// tools, the transcript, the people), 回顾 (MeetingHistory: the meetings held) or Settings. A thin toolbar keeps the
// rail's toggle and the "…" menu (Windows has no app menu). Keys: Ctrl+1 / Ctrl+2 the two pages, Ctrl+, Settings,
// Ctrl+F the page's own find, Ctrl+Shift+S the rail, Ctrl+W closes the window (Bloom stays in the tray).

import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '@blurt/icons';
import { applyPlatform, applyTheme } from '@blurt/ui';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { api, inTauri, listen, type AppStatus } from './api';
import { i18n, setLang, t } from './i18n';
import { isImeKey } from './ime';
import { SidebarGlyph } from './components/glyphs';
import { tidy, useMenu, type MenuEntry } from './components/Menu';
import { SettingsView } from './components/SettingsView';
import { Sidebar, type Page } from './components/Sidebar';
import { Tooltip } from './components/Tooltip';
import { MeetingCurrent } from './components/MeetingCurrent';
import { MeetingHistory } from './components/MeetingHistory';
import { DevMeetings } from './components/DevMeetings';
import './components/main-window.css';
import './components/meeting.css';

/** 会议 / 回顾 once opened stay mounted (the one not shown is hidden in place): switching between them, or back from
 *  Settings, doesn't reload the paper (its iframe, MathJax), the recording, or the lists */
const SEEN = { meeting: false, history: false, dev: false };
const STORE = 'blurt.main-window';
/** the rail's width (main-window.css .mt-rail) */
const RAIL_W = 68;

interface Saved {
  place?: string;
  sidebar?: boolean;
}
const loadSaved = (): Saved => {
  try {
    return JSON.parse(localStorage.getItem(STORE) ?? '{}') as Saved;
  } catch {
    return {};
  }
};

type Screen = 'main' | 'settings';
interface Toast {
  msg: string;
  id: number;
}

export default function App() {
  const saved = useRef(loadSaved()).current;
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [, setLangTick] = useState(0);
  const [page, setPage] = useState<Page>(saved.place === 'history' || saved.place === 'dev' ? saved.place : 'meeting');
  const [screen, setScreen] = useState<Screen>('main');
  const [sideHidden, setSideHidden] = useState(!!saved.sidebar);
  const [toast, setToastState] = useState<Toast | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const menu = useMenu();

  const showToast = useCallback((msg: string) => {
    clearTimeout(toastTimer.current);
    const id = Date.now();
    setToastState({ msg, id });
    toastTimer.current = setTimeout(() => setToastState((x) => (x?.id === id ? null : x)), 4200);
  }, []);
  const relang = useCallback((l: string) => {
    setLang(l);
    setLangTick((n) => n + 1);
  }, []);

  // 回顾 is built in the background once the window has settled (hidden), so even the first switch to it is a repaint
  // and not a fresh page
  const [, warmPages] = useState(0);
  useEffect(() => {
    const go = () => {
      if (SEEN.history || !SEEN.meeting) return;
      SEEN.history = true;
      warmPages((n) => n + 1);
    };
    const ric = (window as unknown as { requestIdleCallback?: (f: () => void, o?: { timeout: number }) => number }).requestIdleCallback;
    const h = setTimeout(() => (ric ? ric(go, { timeout: 3000 }) : go()), 3000);
    return () => clearTimeout(h);
  }, []);

  // ---------------------------------------------------------------- start, events
  useEffect(() => {
    api.status().then((s) => {
      relang(s.lang);
      setStatus(s);
      applyPlatform(s.platform);
      applyTheme('auto');
    });
    const offs = [
      listen<{ ev: string; code?: string }>('capture://status', (p) => {
        if (p.ev === 'error' && p.code) showToast(t(`error.${p.code}`) === `error.${p.code}` ? t('error.unknown') : t(`error.${p.code}`));
      }),
      // the tray's Settings… / its speech model's Show download (the pane: SettingsView)
      listen<{ view?: string }>('nav', (p) => {
        if (p.view === 'settings') setScreen('settings');
      }),
    ];
    return () => offs.forEach((o) => o());
  }, []);

  // the window state, a moment after it changes
  useEffect(() => {
    const h = setTimeout(() => {
      try {
        localStorage.setItem(STORE, JSON.stringify({ place: page, sidebar: sideHidden } satisfies Saved));
      } catch {
        /* a private window: nothing kept */
      }
    }, 600);
    return () => clearTimeout(h);
  }, [page, sideHidden]);

  const go = (p: Page) => {
    setScreen('main');
    setPage(p);
  };
  const toggleSide = () => setSideHidden((h) => !h);

  // ---------------------------------------------------------------- the "…" menu
  const moreMenu = (): MenuEntry[] =>
    tidy([
      { label: t(sideHidden ? 'menu.show_sidebar' : 'menu.hide_sidebar'), icon: <SidebarGlyph size={14} />, keys: 'Ctrl+Shift+S', run: toggleSide },
      { kind: 'separator' },
      { label: t('mw.settings_menu'), icon: <Icon name="settings" size={14} />, keys: 'Ctrl+,', run: () => setScreen('settings') },
    ]);

  // ---------------------------------------------------------------- keyboard
  const onKey = (e: KeyboardEvent) => {
    if (e.defaultPrevented || isImeKey(e) || menu.isOpen) return;
    const ctrl = e.ctrlKey || e.metaKey;
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const stop = (f: () => void) => {
      e.preventDefault();
      f();
    };
    if (ctrl && !e.altKey && !e.shiftKey) {
      if (key === 'f') return stop(() => window.dispatchEvent(new CustomEvent('bloom-find')));
      if (key === ',') return stop(() => setScreen('settings'));
      // the window's own close (Bloom stays in the notification area)
      if (key === 'w' && inTauri) return stop(() => void getCurrentWindow().close().catch(() => {}));
      if (key === '1') return stop(() => go('meeting'));
      if (key === '2') return stop(() => go('history'));
      if (key === '3') return stop(() => go('dev'));
    }
    if (ctrl && e.shiftKey && !e.altKey && key === 's') return stop(toggleSide);
  };
  const keyRef = useRef(onKey);
  keyRef.current = onKey;
  useEffect(() => {
    const h = (e: KeyboardEvent) => keyRef.current(e);
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

  // ---------------------------------------------------------------- render
  const lang = i18n().lang;
  const platform = status?.platform ?? 'win';
  const mainScreen = screen === 'main';
  if (mainScreen) SEEN[page] = true;
  const sideOff = sideHidden || screen === 'settings';
  const sideIn = sideOff ? 0 : RAIL_W;

  return (
    <div className="app mw" data-platform={platform} lang={lang} style={{ gridTemplateColumns: `${sideIn}px 1fr 0px` }}>
      <header className="mw-toolbar">
        <button className="mw-tool" onClick={toggleSide} aria-pressed={!sideHidden} aria-label={t('menu.toggle_sidebar')} title={`${t('menu.toggle_sidebar')} (Ctrl+Shift+S)`} data-testid="toggle-sidebar">
          <SidebarGlyph size={16} />
        </button>
        <span className="grow" />
        <button className="mw-tool" onClick={(e) => menu.open(moreMenu(), { x: 0, y: 0, anchor: e.currentTarget }, t('mw.more'))} aria-label={t('mw.more')} title={t('mw.more')} aria-haspopup="menu" data-testid="more">
          <Icon name="more" size={16} />
        </button>
      </header>

      <div className={`mw-side-col ${sideOff ? 'hidden' : ''}`}>
        {!sideOff && <Sidebar page={mainScreen ? page : null} onGo={go} onSettings={() => setScreen('settings')} />}
      </div>

      {screen === 'settings' && status && (
        <main className="mw-wide content">
          <section className="pad st-host">
            <SettingsView status={status} onToast={showToast} onBack={() => setScreen('main')} onLang={(l) => (l === 'auto' ? api.status().then((s) => relang(s.lang)) : relang(l))} />
          </section>
        </main>
      )}
      {(SEEN.meeting || SEEN.history || SEEN.dev) && (
        <main className={`mw-wide content mt-wide ${mainScreen ? '' : 'is-away'}`} data-testid="meeting-page" aria-hidden={!mainScreen || undefined}>
          {SEEN.meeting && (
            <div className={`mt-page ${page === 'meeting' ? 'is-on' : ''}`} aria-hidden={page !== 'meeting' || undefined}>
              <MeetingCurrent active={mainScreen && page === 'meeting'} onToast={showToast} />
            </div>
          )}
          {SEEN.history && (
            <div className={`mt-page ${page === 'history' ? 'is-on' : ''}`} aria-hidden={page !== 'history' || undefined}>
              <MeetingHistory active={mainScreen && page === 'history'} onToast={showToast} />
            </div>
          )}
          {SEEN.dev && (
            <div className={`mt-page ${page === 'dev' ? 'is-on' : ''}`} aria-hidden={page !== 'dev' || undefined}>
              <DevMeetings active={mainScreen && page === 'dev'} />
            </div>
          )}
        </main>
      )}

      {menu.node}
      <Tooltip />
      {toast && (
        <div className="shell-toast mw-toast" role="status" style={{ left: sideIn + (window.innerWidth - sideIn) / 2 }} data-testid="toast">
          <span>{toast.msg}</span>
        </div>
      )}
    </div>
  );
}

