import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { TreeNode } from '../types';
import { useI18n } from '../i18n';
import './NodeActions.css';

export function NodeActions({ node, onDelete, disabled = false }: {
  node: TreeNode;
  onDelete: (id: number) => void;
  disabled?: boolean;
}) {
  const { t } = useI18n();
  const menuId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);

  function close(restoreFocus = false) {
    setPosition(null);
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  }

  useLayoutEffect(() => {
    if (!position || !menu.current) return;
    const bounds = menu.current.getBoundingClientRect();
    const left = Math.max(8, Math.min(position.left, window.innerWidth - bounds.width - 8));
    const top = Math.max(8, Math.min(position.top, window.innerHeight - bounds.height - 8));
    if (left !== position.left || top !== position.top) setPosition({ left, top });
    menu.current.querySelector<HTMLButtonElement>('button')?.focus({ preventScroll: true });
  }, [position?.left, position?.top]);

  useEffect(() => {
    if (!position) return;
    const outside = (event: PointerEvent) => {
      if (!menu.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) close();
    };
    const move = () => close();
    document.addEventListener('pointerdown', outside);
    document.addEventListener('wheel', move, true);
    document.addEventListener('scroll', move, true);
    window.addEventListener('resize', move);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('wheel', move, true);
      document.removeEventListener('scroll', move, true);
      window.removeEventListener('resize', move);
    };
  }, [!!position]);

  useEffect(() => { if (disabled) close(); }, [disabled]);

  return <>
    <button ref={trigger} type="button" className="lm-node-actions" disabled={disabled}
      title={disabled ? t('请先停止当前回答，再删除。', 'Stop the current response before deleting.') : t('节点操作', 'Node actions')}
      aria-label={t('节点操作：{title}', 'Node actions: {title}', { title: node.title })}
      aria-haspopup="menu" aria-expanded={!!position} aria-controls={position ? menuId : undefined}
      onClick={() => {
        if (position) { close(true); return; }
        const bounds = trigger.current?.getBoundingClientRect();
        if (bounds) setPosition({ left: bounds.right - 168, top: bounds.bottom + 4 });
      }}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" /></svg>
    </button>
    {position && createPortal(<div id={menuId} ref={menu} className="node-actions-menu" role="menu" aria-label={t('节点操作', 'Node actions')}
      style={position} onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); }
        else if (event.key === 'Tab') close();
        else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) { event.preventDefault(); menu.current?.querySelector<HTMLButtonElement>('button')?.focus(); }
      }}>
      <button type="button" role="menuitem" onClick={() => { close(true); onDelete(node.id); }}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" /></svg>
        {node.parent_id == null ? t('删除主题', 'Delete topic') : t('删除分支', 'Delete branch')}
      </button>
    </div>, document.body)}
  </>;
}
