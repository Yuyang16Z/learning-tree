import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { formatMessage, isLocale, LOCALE_STORAGE_KEY, persistLocale, readLocale, resolveLocale, type Locale, type MessageParams } from './locale';

export { formatMessage } from './locale';
export type { Locale, MessageParams } from './locale';

function browserLanguages(): readonly string[] {
  return typeof navigator === 'undefined' ? [] : navigator.languages?.length ? navigator.languages : [navigator.language];
}

function browserStorage(): Storage | undefined {
  try { return typeof window === 'undefined' ? undefined : window.localStorage; } catch { return undefined; }
}

let activeLocale: Locale | undefined;
export function getLocale(): Locale {
  return activeLocale ?? readLocale(browserStorage(), browserLanguages());
}

/** For event handlers and API helpers outside the React component tree. */
export function translate(zh: string, en: string, params?: MessageParams): string {
  return formatMessage(getLocale(), zh, en, params);
}

interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (zh: string, en: string, params?: MessageParams) => string;
}
const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, updateLocale] = useState<Locale>(() => {
    const initial = readLocale(browserStorage(), browserLanguages());
    activeLocale = initial;
    return initial;
  });
  const setLocale = useCallback((next: Locale) => {
    if (!isLocale(next)) return;
    activeLocale = next;
    persistLocale(browserStorage(), next);
    updateLocale(next);
  }, []);
  useEffect(() => {
    document.documentElement.lang = locale;
    document.title = formatMessage(locale, '学习树', 'LearningTree');
  }, [locale]);
  useEffect(() => {
    function handleStorage(event: StorageEvent) {
      if (event.key !== LOCALE_STORAGE_KEY && event.key !== null) return;
      const next = resolveLocale(event.newValue, browserLanguages());
      activeLocale = next;
      updateLocale(next);
    }
    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);
  const t = useCallback((zh: string, en: string, params?: MessageParams) => formatMessage(locale, zh, en, params), [locale]);
  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) throw new Error('useI18n must be used inside I18nProvider');
  return context;
}
