// Tencent Meeting's narrow rail: the user's avatar, 会议 and 回顾 as icon-over-label, the gear at the foot.
import { useEffect, useState } from 'react';
import { Icon } from '@blurt/icons';
import { t } from '../i18n';
import { ZUHUI_BASE } from '../meetingApi';

/** 会议 (the meeting under way), 回顾 (the meetings held) or Dev (the simulated meetings Bloom is tested with) */
export type Page = 'meeting' | 'history' | 'dev';

export function Sidebar({ page, onGo, onSettings }: { page: Page | null; onGo: (p: Page) => void; onSettings: () => void }) {
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
        <a role="option" aria-selected={page === 'dev'} className={`mt-rail-item ${page === 'dev' ? 'sel' : ''}`} onClick={() => onGo('dev')} data-testid="nav-dev">
          <Icon name="code" size={22} />
          <span>{t('meeting.nav.dev')}</span>
        </a>
      </div>
      <div className="mt-rail-foot">
        <button className="mt-rail-btn" onClick={onSettings} aria-label={t('mw.settings')} data-tip={t('mw.settings')} data-testid="nav-settings">
          <Icon name="settings-gear" size={18} />
        </button>
      </div>
    </nav>
  );
}

function RailAvatar() {
  const [ok, setOk] = useState(true);
  const [n, setN] = useState(0);
  // the backend may come up after the window: try again every few seconds until the picture loads
  useEffect(() => {
    if (ok) return;
    const h = setTimeout(() => {
      setOk(true);
      setN((k) => k + 1);
    }, 4000);
    return () => clearTimeout(h);
  }, [ok]);
  if (!ok) return <span className="mt-rail-me is-empty" aria-hidden="true" />;
  return <img className="mt-rail-me" src={`${ZUHUI_BASE}/avatars/me?n=${n}`} alt="" draggable={false} onError={() => setOk(false)} />;
}
