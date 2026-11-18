// API usage as people read it: "12.3 万 token", "约 ¥0.42" (zh) / "123K tokens", "~$0.10" (en). Money is in the
// provider's own currency (CNY for the Chinese ones, USD for Anthropic), from the backend's estimate (zuhui/usage.py).
import { i18n, t } from './i18n';
import type { Usage, UsageModel } from './meetingApi';

const loc = () => (i18n().lang === 'zh-Hans' ? 'zh-Hans' : 'en');

/** a token count, compact: 850 / 1.2 万 / 12.3 万 (zh), 850 / 1.2K / 12.3K (en) */
export function fmtTokenCount(n: number): string {
  return new Intl.NumberFormat(loc(), { notation: 'compact', maximumSignificantDigits: 3 }).format(Math.max(0, n));
}

export const fmtTokens = (n: number) => t('usage.tokens', { n: fmtTokenCount(n) });

/** an amount in its currency; under a cent: "<¥0.01" */
export function fmtMoney(v: number, currency: string): string {
  let f: Intl.NumberFormat;
  try {
    f = new Intl.NumberFormat(loc(), { style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2 });
  } catch {
    return `${v.toFixed(2)} ${currency}`;
  }
  return v > 0 && v < 0.01 ? `<${f.format(0.01)}` : f.format(v);
}

/** "约 ¥0.42" / "约 ¥0.42 + US$0.10"; '' when nothing could be priced */
export function fmtCosts(costs: Record<string, number> | null | undefined): string {
  const parts = Object.entries(costs ?? {}).map(([c, v]) => fmtMoney(v, c));
  return parts.length ? t('usage.cost', { a: parts.join(' + ') }) : '';
}

/** one model's split, for its hover: "输入 1.2 万 · 缓存 3.4 万 · 输出 5,600 · 12 次" */
export function usageDetail(m: UsageModel): string {
  const bits = [t('usage.in', { n: fmtTokenCount(m.in) })];
  if (m.cache_read || m.cache_write) bits.push(t('usage.cache', { n: fmtTokenCount(m.cache_read + m.cache_write) }));
  bits.push(t('usage.out', { n: fmtTokenCount(m.out) }), t('usage.calls', { n: i18n().formatNumber(m.calls) }));
  return bits.join(' · ');
}

/** the whole as one line: "约 ¥1.23 · 45.6 万 token" (only tokens when nothing could be priced) */
export function usageLine(u: Usage | null | undefined): string {
  if (!u) return fmtTokens(0);
  const c = fmtCosts(u.costs);
  return c ? `${c} · ${fmtTokens(u.tokens)}` : fmtTokens(u.tokens);
}

/** every model on its own line, for a hover */
export function usageBreakdown(u: Usage | null | undefined): string {
  const colon = loc() === 'zh-Hans' ? '：' : ': ';
  return (u?.models ?? [])
    .map((m) => `${m.pretty || m.model}${colon}${fmtTokens(m.tokens)}${m.cost != null && m.currency ? ` · ${fmtMoney(m.cost, m.currency)}` : ''}`)
    .join('\n');
}
