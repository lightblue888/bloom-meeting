// The main window's strings (K10): this package's catalogs over @blurt/i18n's common words. The language comes
// from the app (`app_status.lang`, which follows config `lang` / the system); dates and numbers use its locale.
import { createI18n, resolveLang, type I18n, type Params } from '@blurt/i18n';
import EN from './locales/en.json';
import ZH from './locales/zh-Hans.json';
// Settings' words, from the Mac's own (Settings/Dictation/Capture/Speech/Models .strings) under "st.<the Mac's key>"
import SET_EN from './locales/settings.en.json';
import SET_ZH from './locales/settings.zh-Hans.json';

export const CATALOGS = { en: { ...EN, ...SET_EN }, 'zh-Hans': { ...ZH, ...SET_ZH } };

let current: I18n = createI18n(CATALOGS, resolveLang(null));

export function setLang(lang: string | null | undefined) {
  current = createI18n(CATALOGS, resolveLang(lang));
  if (typeof document !== 'undefined') document.documentElement.lang = current.lang;
  return current;
}

export const i18n = () => current;
export const t = (key: string, p?: Params) => current.t(key, p);

// The meeting area (transcript cards, steps, task board, summary) follows the meeting's own language, not the app's:
// an English meeting under a Chinese UI showed "思考 / 出声 4.0 秒 / 被打断了" on English lines (2026-11-13, the user).
// The open meeting view sets it (MeetingLayout, only while on screen); null = follow the app's language.
let meetingI18n: I18n | null = null;
let meetingLang: string | null = null;

export function setMeetingLang(lang: string | null | undefined) {
  const want = lang === 'en' || lang === 'zh' || lang === 'zh-Hans' ? lang : null;
  if (want === meetingLang) return;
  meetingLang = want;
  meetingI18n = want ? createI18n(CATALOGS, resolveLang(want)) : null;
}

export const mi18n = () => meetingI18n ?? current;
// Only the words that annotate a single transcript line follow the meeting's language: 随口 / 思考, "出声 3 秒", "查了 16 秒",
// the grey step lines, "被打断了", the speaker labels. Everything else on the meeting screen — buttons, tabs, the
// paper toolbar, the status strip, the task board, 修改清单, summary headings, the state and the date — follows the app's
// language (2026-11-14: a Chinese meeting under the English UI showed 修订标记 / 上一处 / 已结束 / 11月14日 / 修改清单).
const LINE_WORDS = /^meeting\.(?:ai\.(?:cat\.|lat$|took$|step\.|cut$|musing$|unspoken_chip$|stale_chip$|deep_secs$|edit_live$|deep_wait$)|who\.|line\.)/;
export const mt = (key: string, p?: Params) => (LINE_WORDS.test(key) ? mi18n() : current).t(key, p);
