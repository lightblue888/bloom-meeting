// Tencent Meeting's narrow rail: the user's avatar, 会议 and 回顾 as icon-over-label, the gear at the foot.
import { useEffect, useState } from 'react';
import { Icon } from '@blurt/icons';
import { t } from '../i18n';
import { meetingApi, PROFILE_EVENT, ZUHUI_BASE } from '../meetingApi';
import { Flower, HOST_COLOR } from './Flower';

/** 会议 (the meeting under way), 回顾 (the meetings held) or Dev (the simulated meetings Bloom is tested with) */
export type Page = 'meeting' | 'history' | 'dev';

export function Sidebar({ page, dev, onGo, onSettings }: { page: Page | null; dev: boolean; onGo: (p: Page) => void; onSettings: () => void }) {
  return (
    <nav className="mw-sidebar mt-rail" aria-label={t('main.sidebar.label')}>
      <RailAvatar />
      <div className="mt-rail-list" role="listbox" aria-label={t('main.sidebar.label')}>
        <a role="option" aria-selected={page === 'meeting'} className={`mt-rail-item ${page === 'meeting' ? 'sel' : ''}`} onClick={() => onGo('meeting')} data-testid="nav-meeting">
          <Icon name="video" size={22} />
          <span>{t('meeting.nav.current')}</span>
        </a>
        <a role="option" aria-selected={page === 'history'} className={`mt-rail-item ${page === 'history' ? 'sel' : ''}`} onClick={() => onGo('history')} data-testid="nav-history">
          <Icon name="history" size={22} />
          <span>{t('meeting.nav.history')}</span>
        </a>
        {dev && (
          <a role="option" aria-selected={page === 'dev'} className={`mt-rail-item ${page === 'dev' ? 'sel' : ''}`} onClick={() => onGo('dev')} data-testid="nav-dev">
            <Icon name="code" size={22} />
            <span>{t('meeting.nav.dev')}</span>
          </a>
        )}
      </div>
      <div className="mt-rail-foot">
        <button className="mt-rail-btn" onClick={onSettings} aria-label={t('mw.settings')} data-tip={t('mw.settings')} data-testid="nav-settings">
          <Icon name="settings-gear" size={18} />
        </button>
      </div>
    </nav>
  );
}

/** your photo, or Bloom's flower in the host's colour when you have none (a fresh install). The backend may come up after
 *  the window: until it answers, the flower, asked again every few seconds. Settings changing the photo loads it anew. */
function RailAvatar() {
  const [has, setHas] = useState(false);
  const [n, setN] = useState(0);
  const [color, setColor] = useState(HOST_COLOR);
  useEffect(() => {
    const again = () => setN((k) => k + 1);
    window.addEventListener(PROFILE_EVENT, again);
    return () => window.removeEventListener(PROFILE_EVENT, again);
  }, []);
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const look = () =>
      fetch(`${ZUHUI_BASE}/avatars/me?n=${n}`, { cache: 'no-store' }).then(
        (r) => {
          void r.body?.cancel();
          if (alive) setHas(r.ok);
          // the flower in your own colour (Settings → Profile)
          if (!r.ok) meetingApi.profile().then((x) => alive && setColor(x.color || HOST_COLOR), () => {});
        },
        () => {
          if (!alive) return;
          setHas(false);
          timer = window.setTimeout(look, 4000);
        },
      );
    void look();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [n]);
  if (!has)
    return (
      <span className="mt-rail-me is-flower" aria-hidden="true">
        <Flower color={color} />
      </span>
    );
  return <img className="mt-rail-me" src={`${ZUHUI_BASE}/avatars/me?n=${n}`} alt="" draggable={false} onError={() => setHas(false)} />;
}
