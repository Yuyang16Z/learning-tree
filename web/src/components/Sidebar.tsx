import type { Tree } from '../types';
import { useI18n } from '../i18n';
interface Props {
  trees: Tree[]; activeTreeId: number | null;
  onSelect: (id: number) => void; onNew: () => void; onDelete: (id: number) => void;
  onOpenSettings: () => void; onImport: () => void;
  recoveryAvailable: boolean; onRecover: () => void;
}
export function Sidebar({ trees, activeTreeId, onSelect, onNew, onDelete, onOpenSettings, onImport, recoveryAvailable, onRecover }: Props) {
  const { locale, t } = useI18n();
  return <aside className="sidebar" aria-label={t("学习主题", "Learning topics")}>
    <div className="brand-row"><div className="brand"><span className="brand-mark" aria-hidden="true">⑂</span>{t("学习树", "LearningTree")}</div>{locale === "zh-CN" && <span className="brand-sub" style={{ textTransform: "none", letterSpacing: "0.04em" }}>LearningTree</span>}</div>
    <button className="new-topic" onClick={onNew}><span aria-hidden="true">＋</span> {t("新的学习", "New topic")}</button>
    <div className="side-label">{t("我的主题", "My topics")} <span>{trees.length || ''}</span></div>
    <nav className="topic-list" aria-label={t("知识树列表", "Learning tree list")}>
      {trees.length === 0 && <div className="side-empty">{t("从一个好奇的问题开始。", "Start with a question.")}</div>}
      {trees.map(tree => <div key={tree.id} className={`tree-item${tree.id === activeTreeId ? ' active' : ''}`}>
        <button className="topic-select" onClick={() => onSelect(tree.id)} aria-current={tree.id === activeTreeId ? 'page' : undefined} title={tree.title}><span className="topic-dot" />{tree.title}</button>
        <button className="tree-del" title={t("删除「{title}」", "Delete “{title}”", { title: tree.title })} aria-label={t("删除「{title}」", "Delete “{title}”", { title: tree.title })} onClick={() => {
          if (window.confirm(t("删除「{title}」？会保留一份整树备份，可在左下角恢复。", "Delete “{title}”? A backup will be kept. Restore it from the sidebar.", { title: tree.title }))) onDelete(tree.id);
        }}>×</button>
      </div>)}
    </nav>
    <div className="sidebar-bottom">
      {recoveryAvailable && <button className="sidebar-link" onClick={onRecover}>{t("↶ 恢复上次删除", "↶ Restore last deletion")}</button>}
      <button className="sidebar-link" onClick={onImport}>{t("↥ 导入学习记录", "↥ Import learning records")}</button>
      <button className="sidebar-link" onClick={onOpenSettings}>{t("⚙ 设置", "⚙ Settings")}</button>
    </div>
  </aside>;
}
