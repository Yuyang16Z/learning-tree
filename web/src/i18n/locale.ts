export type Locale = 'zh-CN' | 'en';
export type MessageParams = Record<string, string | number>;
export const LOCALE_STORAGE_KEY = 'learning-tree.locale';

export function isLocale(value: unknown): value is Locale {
  return value === 'zh-CN' || value === 'en';
}

/** Explicit preference wins; browser language is only used on first visit. */
export function resolveLocale(saved: unknown, browserLanguages: readonly string[]): Locale {
  if (isLocale(saved)) return saved;
  const primary = browserLanguages.find(language => language.trim());
  return primary ? (/^zh(?:-|$)/i.test(primary) ? 'zh-CN' : 'en') : 'zh-CN';
}

export function readLocale(storage: Pick<Storage, 'getItem'> | undefined, browserLanguages: readonly string[]): Locale {
  let saved: string | null = null;
  try { saved = storage?.getItem(LOCALE_STORAGE_KEY) ?? null; } catch { /* Private browsing may deny storage. */ }
  return resolveLocale(saved, browserLanguages);
}

export function persistLocale(storage: Pick<Storage, 'setItem'> | undefined, locale: Locale): void {
  try { storage?.setItem(LOCALE_STORAGE_KEY, locale); } catch { /* Language still changes for this session. */ }
}

export function formatMessage(locale: Locale, zh: string, en: string, params?: MessageParams): string {
  const message = locale === 'zh-CN' ? zh : en;
  return message.replace(/\{(\w+)\}/g, (placeholder, key: string) =>
    params && Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : placeholder,
  );
}
