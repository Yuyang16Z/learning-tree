import { formatMessage, type Locale } from "../i18n/locale";
import type { LearningPreset, LearningPresetList } from "../types";

/** Where custom styles are explained (format, limits, location). */
export const LEARNING_PRESETS_DOCS = "https://github.com/Yuyang16Z/learning-tree/blob/main/docs/learning-presets.md";

/** Interface text as [zh, en] pairs for `t(...)`. */
export const STYLE_TEXT: Record<"menu" | "defaultShort" | "defaultOption" | "defaultDescription" | "unavailable", [string, string]> = {
  menu: ["学习方式", "Learning style"],
  defaultShort: ["默认", "Default"],
  defaultOption: ["默认（直接讲解）", "Default (direct explanation)"],
  defaultDescription: ["不指定教学方式，按平常的方式讲解", "No teaching style; answers explain as usual"],
  unavailable: ["已不可用", "Unavailable"],
};

/**
 * What a stored style id means now: the default way (no id), a usable style, one that no longer
 * exists (a deleted custom file), or not yet known because the list is still loading.
 */
export type PresetChoice =
  | { kind: "default" }
  | { kind: "preset"; preset: LearningPreset }
  | { kind: "unavailable"; id: string }
  | { kind: "loading"; id: string };

export function presetChoice(id: string | null | undefined, list: LearningPresetList | null): PresetChoice {
  if (!id) return { kind: "default" };
  if (!list) return { kind: "loading", id };
  const preset = list.presets.find(item => item.id === id);
  return preset ? { kind: "preset", preset } : { kind: "unavailable", id };
}

export function presetTitle(preset: LearningPreset, locale: Locale): string {
  return locale === "en" ? preset.title_en : preset.title_zh;
}

/** A custom file may describe itself in one language only; show that one rather than nothing. */
export function presetDescription(preset: LearningPreset, locale: Locale): string {
  return locale === "en" ? preset.description_en || preset.description_zh : preset.description_zh || preset.description_en;
}

/** The menu's entries: the default way first, then the styles in the server's display order, then the current choice when it no longer exists. */
export interface StyleOption { id: string | null; preset?: LearningPreset; unavailable?: boolean }
export function styleOptions(list: LearningPresetList | null, current: string | null | undefined): StyleOption[] {
  const presets = list?.presets ?? [];
  const options: StyleOption[] = [{ id: null }, ...presets.map(preset => ({ id: preset.id, preset }))];
  if (current && list && !presets.some(preset => preset.id === current)) options.push({ id: current, unavailable: true });
  return options;
}

/** The composer button's text for the open topic's style. */
export function styleLabel(current: string | null | undefined, list: LearningPresetList | null, locale: Locale): string {
  const choice = presetChoice(current, list);
  if (choice.kind === "default") return formatMessage(locale, ...STYLE_TEXT.defaultShort);
  if (choice.kind === "preset") return presetTitle(choice.preset, locale);
  if (choice.kind === "unavailable") return formatMessage(locale, ...STYLE_TEXT.unavailable);
  return "…";
}

/**
 * What a question sends as `learning_preset`: the topic's style while it exists, otherwise null, so a deleted
 * custom file answers in the default way instead of failing. Before the list has loaded, the stored id is sent
 * and the server checks it.
 */
export function requestedPreset(current: string | null | undefined, list: LearningPresetList | null): string | null {
  if (!current) return null;
  if (!list) return current;
  return list.presets.some(preset => preset.id === current) ? current : null;
}

/** A question's tag: none for the default way (or while the list loads), else the style's title or "Unavailable". */
export function questionTag(id: string | null | undefined, list: LearningPresetList | null, locale: Locale): { text: string; unavailable: boolean } | null {
  const choice = presetChoice(id, list);
  if (choice.kind === "preset") return { text: presetTitle(choice.preset, locale), unavailable: false };
  if (choice.kind === "unavailable") return { text: formatMessage(locale, ...STYLE_TEXT.unavailable), unavailable: true };
  return null;
}

type Styled = { id: number; learning_preset?: string | null };

/** Optimistic update of one topic's style; the same array comes back when nothing changes. */
export function withTopicPreset<T extends Styled>(trees: readonly T[], treeId: number, value: string | null): T[] {
  let changed = false;
  const next = trees.map(tree => {
    if (tree.id !== treeId || (tree.learning_preset ?? null) === value) return tree;
    changed = true;
    return { ...tree, learning_preset: value };
  });
  return changed ? next : (trees as T[]);
}

/** Roll back a failed save, but only while the topic still shows the attempted style, so a newer value from another window stays. */
export function restoreTopicPreset<T extends Styled>(trees: readonly T[], treeId: number, attempted: string | null, previous: string | null): T[] {
  const current = trees.find(tree => tree.id === treeId);
  return current && (current.learning_preset ?? null) === attempted ? withTopicPreset(trees, treeId, previous) : (trees as T[]);
}
