import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { useI18n } from '../i18n';

interface Props {
  title: string;
  onSave: (title: string) => Promise<void>;
  onClose: () => void;
}

export function RenameTreeModal({ title, onSave, onClose }: Props) {
  const { t } = useI18n();
  const [draft, setDraft] = useState(title);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const busyRef = useRef(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLFormElement>(null);

  useEffect(() => { inputRef.current?.focus(); inputRef.current?.select(); }, []);

  useEffect(() => {
    if (saving) dialogRef.current?.focus();
    else if (error) inputRef.current?.focus();
  }, [saving, error]);

  async function save(event: FormEvent) {
    event.preventDefault();
    const value = draft.trim();
    if (!value || value.length > 300 || busyRef.current) return;
    busyRef.current = true;
    setSaving(true);
    setError('');
    try {
      await onSave(value);
      onClose();
    } catch (error) {
      setError(error instanceof Error ? error.message : t('改名失败，请重试。', 'Could not rename this topic. Please try again.'));
    } finally {
      busyRef.current = false;
      setSaving(false);
    }
  }

  function keys(event: KeyboardEvent<HTMLFormElement>) {
    if (event.nativeEvent.isComposing || event.keyCode === 229) {
      if (event.key === 'Enter') event.preventDefault();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      if (!busyRef.current) onClose();
    }
    if (event.key === 'Tab') {
      const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('input:not(:disabled), button:not(:disabled)') ?? []);
      if (!controls.length) { event.preventDefault(); return; }
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }

  return <div className="overlay topic-rename-overlay" onClick={event => { if (event.target === event.currentTarget && !busyRef.current) onClose(); }}>
    <form ref={dialogRef} tabIndex={-1} className="modal modal-sm topic-rename-modal" role="dialog" aria-modal="true" aria-label={t('修改主题名称', 'Rename topic')} aria-busy={saving} onSubmit={event => void save(event)} onKeyDown={keys}>
      <h3>{t('修改主题名称', 'Rename topic')}</h3>
      <label htmlFor="topic-rename-input">{t('主题名称', 'Topic name')}</label>
      <input ref={inputRef} id="topic-rename-input" className="topic-rename-input" autoComplete="off" maxLength={300} value={draft} disabled={saving} onChange={event => { setDraft(event.target.value); setError(''); }} aria-invalid={Boolean(error)} aria-describedby={error ? 'topic-rename-error' : undefined} />
      {error && <div id="topic-rename-error" className="topic-action-error" role="alert">{error}</div>}
      <div className="topic-rename-actions">
        <button type="button" className="btn" disabled={saving} onClick={onClose}>{t('取消', 'Cancel')}</button>
        <button type="submit" className="btn topic-rename-save" disabled={saving || !draft.trim() || draft.trim().length > 300}>{saving ? t('保存中…', 'Saving…') : t('保存', 'Save')}</button>
      </div>
    </form>
  </div>;
}
