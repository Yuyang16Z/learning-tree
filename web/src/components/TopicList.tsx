import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import type { Tree } from '../types';
import { useI18n } from '../i18n';
import { RenameTreeModal } from './RenameTreeModal';
import './TopicList.css';

export interface TopicListProps {
  trees: Tree[];
  activeTreeId: number | null;
  onSelect: (id: number) => void;
  onDelete: (id: number) => void;
  onRename: (id: number, title: string) => Promise<void>;
  onArchive: (id: number, archived: boolean) => Promise<void>;
  ariaLabel?: string;
  emptyLabel?: string;
}

function ActionIcon({ type }: { type: 'more' | 'rename' | 'archive' | 'restore' | 'delete' }) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {type === 'more' && <><circle cx="5" cy="12" r="1" fill="currentColor" /><circle cx="12" cy="12" r="1" fill="currentColor" /><circle cx="19" cy="12" r="1" fill="currentColor" /></>}
    {type === 'rename' && <path d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15v5Z" />}
    {type === 'archive' && <path d="M4 8h16v12H4zM3 4h18v4H3zM9 12h6" />}
    {type === 'restore' && <path d="M4 8h16v12H4zM3 4h18v4H3zM12 17v-6m-3 3 3-3 3 3" />}
    {type === 'delete' && <path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" />}
  </svg>;
}

export function TopicList({ trees, activeTreeId, onSelect, onDelete, onRename, onArchive, ariaLabel, emptyLabel }: TopicListProps) {
  const { t } = useI18n();
  const menuId = useId();
  const listRef = useRef<HTMLElement>(null);
  const [menu, setMenu] = useState<{ id: number; left: number; top: number } | null>(null);
  const [menuBusy, setMenuBusy] = useState(false);
  const [menuError, setMenuError] = useState('');
  const [renaming, setRenaming] = useState<Tree | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const busyRef = useRef(false);
  const menuTree = menu ? trees.find(tree => tree.id === menu.id) : undefined;

  function closeMenu(restoreFocus = true) {
    if (busyRef.current) return;
    setMenu(null);
    setMenuError('');
    if (restoreFocus) triggerRef.current?.focus();
  }

  useLayoutEffect(() => {
    if (!menu || !menuRef.current) return;
    const element = menuRef.current;
    const bounds = element.getBoundingClientRect();
    const left = Math.max(8, Math.min(menu.left, window.innerWidth - bounds.width - 8));
    const top = Math.max(8, Math.min(menu.top, window.innerHeight - bounds.height - 8));
    if (left !== menu.left || top !== menu.top) setMenu({ ...menu, left, top });
  }, [menu?.id, menuError]);

  useLayoutEffect(() => {
    menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
  }, [menu?.id]);

  useEffect(() => {
    if (menuBusy) menuRef.current?.focus();
    else if (menuError) menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')[1]?.focus();
  }, [menuBusy, menuError]);

  useEffect(() => {
    if (!menu) return;
    function outside(event: PointerEvent) {
      if (busyRef.current || menuRef.current?.contains(event.target as Node) || triggerRef.current?.contains(event.target as Node)) return;
      setMenu(null);
      setMenuError('');
      triggerRef.current?.focus({ preventScroll: true });
    }
    function closeOnMove(event: Event) {
      if (busyRef.current || menuRef.current?.contains(event.target as Node)) return;
      setMenu(null);
    }
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', closeOnMove);
    document.addEventListener('scroll', closeOnMove, true);
    return () => {
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('resize', closeOnMove);
      document.removeEventListener('scroll', closeOnMove, true);
    };
  }, [menu?.id]);

  function menuKeys(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeMenu(); return; }
    if (event.key === 'Tab') { closeMenu(); return; }
    const buttons = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? []);
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    let next: number;
    if (event.key === 'ArrowDown') next = (current + 1) % buttons.length;
    else if (event.key === 'ArrowUp') next = (current - 1 + buttons.length) % buttons.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = buttons.length - 1;
    else return;
    event.preventDefault();
    buttons[next]?.focus();
  }

  async function toggleArchive(tree: Tree) {
    if (busyRef.current) return;
    busyRef.current = true;
    setMenuBusy(true);
    setMenuError('');
    try {
      await onArchive(tree.id, !tree.archived);
      setMenu(null);
      listRef.current?.focus({ preventScroll: true });
    } catch (error) {
      setMenuError(error instanceof Error ? error.message : t('操作失败，请重试。', 'Something went wrong. Please try again.'));
    } finally {
      busyRef.current = false;
      setMenuBusy(false);
    }
  }

  return <>
    <nav ref={listRef} tabIndex={-1} className="topic-list" aria-label={ariaLabel ?? t('知识树列表', 'Learning tree list')}>
      {trees.length === 0 && <div className="side-empty">{emptyLabel ?? t('从一个好奇的问题开始。', 'Start with a question.')}</div>}
        {trees.map(tree => <div key={tree.id} className={`tree-item${tree.id === activeTreeId ? ' active' : ''}`}>
          <button className="topic-select" onClick={() => onSelect(tree.id)} aria-current={tree.id === activeTreeId ? 'page' : undefined} title={tree.title}><span className="topic-dot" /><span className="topic-title">{tree.title}</span></button>
          <button className="topic-actions-trigger" title={t('主题操作', 'Topic actions')} aria-label={t('主题操作：{title}', 'Topic actions: {title}', { title: tree.title })} aria-haspopup="menu" aria-expanded={menu?.id === tree.id} aria-controls={menu?.id === tree.id ? menuId : undefined} onClick={event => {
            if (busyRef.current) return;
            if (menu?.id === tree.id) { closeMenu(); return; }
            triggerRef.current = event.currentTarget;
            const rect = event.currentTarget.getBoundingClientRect();
            setMenuError('');
            setMenu({ id: tree.id, left: rect.right - 168, top: rect.bottom + 5 });
          }}><ActionIcon type="more" /></button>
        </div>)}
    </nav>
    {menu && menuTree && createPortal(<div id={menuId} ref={menuRef} tabIndex={-1} className="topic-actions-menu" role="menu" aria-label={t('主题操作', 'Topic actions')} aria-busy={menuBusy} style={{ left: menu.left, top: menu.top }} onKeyDown={menuKeys}>
      <button role="menuitem" disabled={menuBusy} onClick={() => { setRenaming(menuTree); closeMenu(false); }}><ActionIcon type="rename" />{t('改名', 'Rename')}</button>
      <button role="menuitem" disabled={menuBusy} onClick={() => void toggleArchive(menuTree)}><ActionIcon type={menuTree.archived ? 'restore' : 'archive'} />{menuTree.archived ? t('取消归档', 'Restore') : t('归档', 'Archive')}</button>
      <div className="topic-actions-divider" role="separator" />
      <button role="menuitem" className="topic-action-delete" disabled={menuBusy} onClick={() => {
        closeMenu();
        if (window.confirm(t('永久删除「{title}」？其中的对话、分支、附件和话题记忆都会删除，且无法撤销。', 'Permanently delete “{title}”? Its conversations, branches, attachments, and topic memories will be deleted. This cannot be undone.', { title: menuTree.title }))) onDelete(menuTree.id);
      }}><ActionIcon type="delete" />{t('删除', 'Delete')}</button>
      {menuError && <div className="topic-action-error" role="alert">{menuError}</div>}
    </div>, document.body)}
    {renaming && createPortal(<RenameTreeModal key={renaming.id} title={renaming.title} onSave={title => onRename(renaming.id, title)} onClose={() => { setRenaming(null); triggerRef.current?.focus(); }} />, document.body)}
  </>;
}
