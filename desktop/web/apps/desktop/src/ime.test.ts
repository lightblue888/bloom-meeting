import { describe, expect, it } from 'vitest';
import { checkCatalogs } from '@blurt/i18n';
import { isImeKey, _setCompositionEnd } from './ime';
import { CATALOGS } from './i18n';

describe('IME guard (C6)', () => {
  it('ignores composing / 229 / right after compositionend', () => {
    _setCompositionEnd(-1e9);
    expect(isImeKey({ isComposing: true }, 1000)).toBe(true);
    expect(isImeKey({ keyCode: 229 }, 1000)).toBe(true);
    expect(isImeKey({ keyCode: 13 }, 1000)).toBe(false);
    _setCompositionEnd(1000);
    expect(isImeKey({ keyCode: 13 }, 1030)).toBe(true);
    expect(isImeKey({ keyCode: 13 }, 1060)).toBe(false);
  });
});

describe('catalogs (K10)', () => {
  it('en and zh-Hans have the same keys and placeholders', () => {
    expect(checkCatalogs(CATALOGS.en, CATALOGS['zh-Hans'])).toEqual([]);
  });
});
