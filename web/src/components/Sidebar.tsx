import type { Tree } from '../types';
import { useI18n } from '../i18n';
import { TopicList } from './TopicList';
import brandIcon from '../assets/learning-tree.svg';
import './Sidebar.css';

interface Props {
  trees: Tree[]; activeTreeId: number | null;
  onSelect: (id: number) => void; onNew: () => void; onDelete: (id: number) => void;
  onRename: (id: number, title: string) => Promise<void>;
  onArchive: (id: number, archived: boolean) => Promise<void>;
  onOpenSettings: () => void; onImport: () => void;
}

export function Sidebar({ trees, activeTreeId, onSelect, onNew, onDelete, onRename, onArchive, onOpenSettings, onImport }: Props) {
  const { locale, t } = useI18n();
  const activeTopics = trees.filter(tree => !tree.archived);
  return <aside className="sidebar" aria-label={t('学习主题', 'Learning topics')}>
    <div className="brand-row"><div className="brand"><img className="brand-mark" src={brandIcon} alt="" aria-hidden="true" />{t('学习树', 'LearningTree')}</div>{locale === 'zh-CN' && <span className="brand-sub" style={{ textTransform: 'none', letterSpacing: '0.04em' }}>LearningTree</span>}</div>
    <button className="new-topic" onClick={onNew}><span aria-hidden="true">＋</span> {t('新的学习', 'New topic')}</button>
    <div className="side-label">{t('我的主题', 'My topics')} <span>{activeTopics.length || ''}</span></div>
    <TopicList trees={activeTopics} activeTreeId={activeTreeId} onSelect={onSelect} onDelete={onDelete} onRename={onRename} onArchive={onArchive} />
    <div className="sidebar-bottom">
      <button className="sidebar-link" onClick={onImport}>{t('↥ 导入学习记录', '↥ Import learning records')}</button>
      <button className="sidebar-link" onClick={onOpenSettings}>{t('⚙ 设置', '⚙ Settings')}</button>
    </div>
  </aside>;
}
