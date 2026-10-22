import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { localizeError } from "../i18n/workspace";
import { LEARNING_PRESETS_DOCS, presetDescription, presetTitle, questionTag, STYLE_TEXT, styleLabel, styleOptions, type StyleOption } from "../lib/learningPresets";
import type { LearningPresetList } from "../types";
import "./LearningStyle.css";

/**
 * The composer's learning-style control for the open topic. Choosing saves the topic's style for its next
 * questions; arrow keys only move focus, a click, Enter or Space chooses.
 */
export function LearningStyleMenu({ value, presets, failed, disabled = false, onOpen, onChange }: {
  value: string | null;
  presets: LearningPresetList | null;
  /** The list could not be loaded. */
  failed: boolean;
  disabled?: boolean;
  /** Read the list again: custom files may have changed. */
  onOpen: () => void;
  /** Settles once saved or rolled back; the caller reports a failure. */
  onChange: (id: string | null) => Promise<void>;
}) {
  const { t, locale } = useI18n();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const options = styleOptions(presets, value);
  const label = styleLabel(value, presets, locale);
  const unavailable = !!value && !!presets && !presets.presets.some(preset => preset.id === value);
  const errors = presets?.errors ?? [];

  function close(restoreFocus = false) {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  }
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!wrapper.current?.contains(event.target as Node)) setOpen(false); };
    // Escape closes the menu wherever focus is (its button, or nowhere after a click on its text),
    // and only the menu: the handlers behind it never see this key press.
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      close(true);
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  // Opening focuses the current choice, so arrow keys start from it.
  useLayoutEffect(() => {
    if (!open) return;
    const items = [...(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]') ?? [])];
    (items.find(item => item.getAttribute("aria-checked") === "true") ?? items[0])?.focus({ preventScroll: true });
  }, [open]);

  async function choose(option: StyleOption) {
    if (option.unavailable || saving) return;
    close(true);
    if (option.id === value) return;
    setSaving(true);
    try { await onChange(option.id); } finally { setSaving(false); }
  }
  function move(event: React.KeyboardEvent) {
    const items = [...(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]') ?? [])];
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    const targets: Record<string, number> = { ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: items.length - 1 };
    const target = targets[event.key];
    if (target === undefined || !items.length) return;
    event.preventDefault();
    items[(target + items.length) % items.length]?.focus();
  }

  const describe = (option: StyleOption) => option.id === null ? [t(...STYLE_TEXT.defaultOption), t(...STYLE_TEXT.defaultDescription)]
    : option.preset ? [presetTitle(option.preset, locale), presetDescription(option.preset, locale)]
    : [t(...STYLE_TEXT.unavailable), t("「{id}」已删除或无法读取，提问时按默认方式讲解。", "“{id}” was removed or cannot be read; questions are answered the default way.", { id: option.id })];
  const buttonTitle = unavailable ? t("这个主题的学习方式已不可用，提问时按默认方式讲解", "This topic's learning style is unavailable; questions are answered the default way") : t("学习方式：这个主题之后的问题怎样讲解", "Learning style: how the next questions in this topic are taught");

  return <div className="chat-style-menu" ref={wrapper} onBlur={event => {
    // Tabbing out closes it; moving within (to the docs link) keeps it open. A click that moves focus
    // nowhere (Safari) is left to the outside-pointer check.
    const next = event.relatedTarget as Node | null;
    if (open && next && !wrapper.current?.contains(next)) setOpen(false);
  }}>
    <button ref={trigger} type="button" className={`chat-style-button${value && !unavailable ? " is-active" : ""}${unavailable ? " is-unavailable" : ""}`} disabled={disabled}
      aria-label={t("学习方式：{name}", "Learning style: {name}", { name: label })} title={buttonTitle}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? `${id}-menu` : undefined} aria-busy={saving || undefined}
      onClick={() => {
        // Stays focusable while a choice saves, so keyboard focus returns here; it opens again once saved.
        if (saving) return;
        if (open) { close(); return; }
        onOpen();
        setOpen(true);
      }}>
      <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 5.5c2.7-1.4 5.7-1.4 9 .7 3.3-2.1 6.3-2.1 9-.7V19c-2.7-1.4-5.7-1.4-9 .7-3.3-2.1-6.3-2.1-9-.7z" /><path d="M12 6.2v13.5" /></svg>
      <span className="chat-style-current">{label}</span>
    </button>
    {open && <div className="chat-style-dropdown">
      <span className="chat-menu-label" id={`${id}-label`}>{t("学习方式 · 用于这个主题之后的问题", "Learning style · for the next questions in this topic")}</span>
      <div id={`${id}-menu`} ref={menu} role="menu" aria-labelledby={`${id}-label`} onKeyDown={move}>
        {options.map((option, index) => {
          const [title, description] = describe(option);
          const checked = option.id === value;
          return <button key={option.id ?? ""} type="button" role="menuitemradio" tabIndex={-1} aria-checked={checked} aria-disabled={option.unavailable || undefined}
            aria-label={title} aria-describedby={description ? `${id}-option-${index}` : undefined} className={`chat-style-option${option.unavailable ? " is-unavailable" : ""}`}
            onClick={() => void choose(option)}>
            <span className="chat-style-title">{title}</span>
            {description && <span className="chat-style-description" id={`${id}-option-${index}`}>{description}</span>}
          </button>;
        })}
      </div>
      <div className="chat-style-notes">
        {failed && <p role="alert">{t("学习方式列表暂时无法读取，可稍后重新打开。", "The learning styles could not be loaded. Open this menu again later.")}</p>}
        {errors.length > 0 && <div><span>{t("这些自定义文件无法使用：", "These custom files cannot be used:")}</span><ul>{errors.map(error => <li key={error.file}><code>{error.file}</code> {localizeError(error.reason, locale)}</li>)}</ul></div>}
        <p>{t("添加自己的学习方式：", "Add your own styles: ")}<a href={LEARNING_PRESETS_DOCS} target="_blank" rel="noopener noreferrer">docs/learning-presets.md</a></p>
      </div>
    </div>}
  </div>;
}

/** A small tag in the question bubble naming the style a question was asked with; none for the default way. */
export function LearningStyleTag({ id, presets }: { id: string | null | undefined; presets: LearningPresetList | null }) {
  const { t, locale } = useI18n();
  const tag = questionTag(id, presets, locale);
  if (!tag) return null;
  const title = tag.unavailable
    ? t("提问时使用的学习方式「{id}」已不可用", "The learning style this question used (“{id}”) is no longer available", { id: id ?? "" })
    : t("学习方式：{name}", "Learning style: {name}", { name: tag.text });
  return <span className={`chat-style-tag${tag.unavailable ? " is-unavailable" : ""}`} title={title}><span className="chat-sr-only">{t("学习方式：", "Learning style: ")}</span>{tag.text}</span>;
}
