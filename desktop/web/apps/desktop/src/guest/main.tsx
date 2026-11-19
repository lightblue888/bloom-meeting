// The guest page's entry (guest.html → dist-guest, served by the relay at example.com/room/<id>; vite.guest.config.ts).
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@blurt/ui/tokens.css';
import '@blurt/ui/base.css';
import '../styles.css';
import '../components/main-window.css';
import '../components/meeting.css';
import './guest.css';
import { GuestApp } from './GuestApp';
import { trackKeyboardNav } from '../kbnav';

trackKeyboardNav();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <GuestApp />
  </StrictMode>,
);
