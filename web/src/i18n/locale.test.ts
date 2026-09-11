import { describe, expect, it } from 'vitest';
import { formatMessage, LOCALE_STORAGE_KEY, persistLocale, readLocale, resolveLocale } from './locale';

describe('interface language preference', () => {
  it('detects Chinese variants and uses English for other browser languages', () => {
    expect(resolveLocale(null, ['zh-TW', 'en'])).toBe('zh-CN');
    expect(resolveLocale(null, ['ZH-cn'])).toBe('zh-CN');
    expect(resolveLocale(null, ['en-US', 'zh-CN'])).toBe('en');
    expect(resolveLocale(null, ['fr-FR'])).toBe('en');
  });
  it('prefers a saved language and recovers from invalid saved values', () => {
    expect(resolveLocale('en', ['zh-CN'])).toBe('en');
    expect(resolveLocale('zh-CN', ['en'])).toBe('zh-CN');
    expect(resolveLocale('not-a-locale', ['en-US'])).toBe('en');
    expect(resolveLocale(null, [])).toBe('zh-CN');
  });
  it('persists a choice and restores it independently of browser language', () => {
    const values = new Map<string, string>();
    const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
    persistLocale(storage, 'en');
    expect(values.get(LOCALE_STORAGE_KEY)).toBe('en');
    expect(readLocale(storage, ['zh-CN'])).toBe('en');
  });
  it('continues when browser storage is denied', () => {
    const denied = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); } };
    expect(readLocale(denied, ['en-US'])).toBe('en');
    expect(() => persistLocale(denied, 'zh-CN')).not.toThrow();
  });
});

describe('message formatting', () => {
  it('interpolates user values and numbers literally without translating content', () => {
    expect(formatMessage('en', '删除「{title}」？', 'Delete “{title}”?', { title: '机器学习 $& {count}' })).toBe('Delete “机器学习 $& {count}”?');
    expect(formatMessage('zh-CN', '{count} 个工具', '{count} tools', { count: 0 })).toBe('0 个工具');
  });
  it('leaves missing placeholders intact and ignores inherited object properties', () => {
    expect(formatMessage('en', '', '{unknown} {toString} {known}', { known: 'OK' })).toBe('{unknown} {toString} OK');
  });
});
