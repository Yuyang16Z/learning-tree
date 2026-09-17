import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import type { Tree } from '../types';
import { useI18n } from '../i18n';
import { RenameTreeModal } from './RenameTreeModal';
import brandIcon from '../assets/learning-tree.svg';
import './Sidebar.css';

interface Props {
  trees: Tree[]; activeTreeId: number | null;
  onSelect: (id: number) => void; onNew: () => void; onDelete: (id: number) => void;
  onRename: (id: number, title: string) => Promise<void>;
  onArchive: (id: number, archived: boolean) => Promise<void>;
  onOpenSettings: () => void; onImport: () => void;
}

function ActionIcon({ type }: { type: 'more' | 'rename' | 'archive' | 'restore' | 'delete' | 'back' }) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {type === 'more' && <><circle cx="5" cy="12" r="1" fill="currentColor" /><circle cx="12" cy="12" r="1" fill="currentColor" /><circle cx="19" cy="12" r="1" fill="currentColor" /></>}
    {type === 'rename' && <path d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15v5Z" />}
    {type === 'archive' && <path d="M4 8h16v12H4zM3 4h18v4H3zM9 12h6" />}
    {type === 'restore' && <path d="M4 8h16v12H4zM3 4h18v4H3zM12 17v-6m-3 3 3-3 3 3" />}
    {type === 'delete' && <path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" />}
    {type === 'back' && <path d="m10 6-6 6 6 6M4 12h16" />}
  </svg>;
}

export function Sidebar({ trees, activeTreeId, onSelect, onNew, onDelete, onRename, onArchive, onOpenSettings, onImport }: Props) {
  const { locale, t } = useI18n();
  const [showArchived, setShowArchived] = useState(false);
  const [menu, setMenu] = useState<{ id: number; left: number; top: number } | null>(null);
  const [menuBusy, setMenuBusy] = useState(false);
  const [menuError, setMenuError] = useState('');
  const [renaming, setRenaming] = useState<Tree | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const archiveToggleRef = useRef<HTMLButtonElement>(null);
  const busyRef = useRef(false);
  const activeTree = trees.find(tree => tree.id === activeTreeId);
  const archivedCount = trees.filter(tree => tree.archived).length;
  const visibleTrees = trees.filter(tree => Boolean(tree.archived) === showArchived);
  const menuTree = menu ? trees.find(tree => tree.id === menu.id) : undefined;

  useEffect(() => { setShowArchived(Boolean(activeTree?.archived)); }, [activeTreeId, activeTree?.archived]);

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
    if (event.key === 'Escape') { event.preventDefault(); closeMenu(); return; }
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
      archiveToggleRef.current?.focus();
    } catch (error) {
      setMenuError(error instanceof Error ? error.message : t('操作失败，请重试。', 'Something went wrong. Please try again.'));
    } finally {
      busyRef.current = false;
      setMenuBusy(false);
    }
  }

  return <>
    <aside className="sidebar" aria-label={t('学习主题', 'Learning topics')}>
      <div className="brand-row"><div className="brand"><img className="brand-mark" src={brandIcon} alt="" aria-hidden="true" />{t('学习树', 'LearningTree')}</div>{locale === 'zh-CN' && <span className="brand-sub" style={{ textTransform: 'none', letterSpacing: '0.04em' }}>LearningTree</span>}</div>
      <button className="new-topic" onClick={() => { setShowArchived(false); onNew(); }}><span aria-hidden="true">＋</span> {t('新的学习', 'New topic')}</button>
      <div className="side-label">{showArchived ? t('已归档', 'Archived') : t('我的主题', 'My topics')} <span>{visibleTrees.length || ''}</span></div>
      <nav className="topic-list" aria-label={showArchived ? t('已归档主题', 'Archived topics') : t('知识树列表', 'Learning tree list')}>
        {visibleTrees.length === 0 && <div className="side-empty">{showArchived ? t('还没有归档的主题。', 'No archived topics yet.') : t('从一个好奇的问题开始。', 'Start with a question.')}</div>}
        {visibleTrees.map(tree => <div key={tree.id} className={`tree-item${tree.id === activeTreeId ? ' active' : ''}`}>
          <button className="topic-select" onClick={() => onSelect(tree.id)} aria-current={tree.id === activeTreeId ? 'page' : undefined} title={tree.title}><span className="topic-dot" /><span className="topic-title">{tree.title}</span></button>
          <button className="topic-actions-trigger" title={t('主题操作', 'Topic actions')} aria-label={t('主题操作：{title}', 'Topic actions: {title}', { title: tree.title })} aria-haspopup="menu" aria-expanded={menu?.id === tree.id} aria-controls={menu?.id === tree.id ? 'topic-actions-menu' : undefined} onClick={event => {
            if (busyRef.current) return;
            if (menu?.id === tree.id) { closeMenu(); return; }
            triggerRef.current = event.currentTarget;
            const rect = event.currentTarget.getBoundingClientRect();
            setMenuError('');
            setMenu({ id: tree.id, left: rect.right - 168, top: rect.bottom + 5 });
          }}><ActionIcon type="more" /></button>
        </div>)}
      </nav>
      <div className="sidebar-bottom">
        <button ref={archiveToggleRef} className="sidebar-link sidebar-archive-link" aria-label={showArchived ? t('返回我的主题', 'Back to topics') : t('已归档', 'Archived')} onClick={() => { closeMenu(false); setShowArchived(value => !value); }}><ActionIcon type={showArchived ? 'back' : 'archive'} /><span>{showArchived ? t('返回我的主题', 'Back to topics') : t('已归档', 'Archived')}</span>{!showArchived && archivedCount > 0 && <span className="sidebar-archive-count">{archivedCount}</span>}</button>
        <button className="sidebar-link" onClick={onImport}>{t('↥ 导入学习记录', '↥ Import learning records')}</button>
        <button className="sidebar-link" onClick={onOpenSettings}>{t('⚙ 设置', '⚙ Settings')}</button>
      </div>
    </aside>
    {menu && menuTree && createPortal(<div id="topic-actions-menu" ref={menuRef} tabIndex={-1} className="topic-actions-menu" role="menu" aria-label={t('主题操作', 'Topic actions')} aria-busy={menuBusy} style={{ left: menu.left, top: menu.top }} onKeyDown={menuKeys}>
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
