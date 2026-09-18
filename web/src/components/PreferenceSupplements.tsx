import { useEffect, useId, useState } from "react";
import { api } from "../api";
import { useI18n } from "../i18n";
import type { PreferenceLearning, PreferenceSupplement, PreferenceSupplementsPage } from "../types";

interface Props {
  onDirtyChange: (dirty: boolean) => void;
  onBusyChange: (busy: boolean) => void;
  onOpenSource: (treeId: number, nodeId: number | null) => void;
  onEditProfile: () => void;
  profileBusy: boolean;
}

function conflict(error: unknown): boolean {
  return typeof error === "object" && error !== null && "status" in error && error.status === 409;
}

export function PreferenceSupplements({ onDirtyChange, onBusyChange, onOpenSource, onEditProfile, profileBusy }: Props) {
  const { t } = useI18n();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [learning, setLearning] = useState<PreferenceLearning | null>(null);
  const [learningReload, setLearningReload] = useState(0);
  const [learningError, setLearningError] = useState("");
  const [learningBusy, setLearningBusy] = useState(false);
  const [data, setData] = useState<PreferenceSupplementsPage | null>(null);
  const [page, setPage] = useState(1);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [editing, setEditing] = useState<PreferenceSupplement | null>(null);
  const [draft, setDraft] = useState("");
  const [expanded, setExpanded] = useState<number[]>([]);
  const [mutation, setMutation] = useState(false);
  const [actionError, setActionError] = useState("");
  const dirty = editing !== null && draft !== editing.content;
  const busy = mutation || learningBusy;

  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);

  useEffect(() => {
    const controller = new AbortController();
    setLearningError("");
    void api.getPreferenceLearning(controller.signal).then(result => {
      if (!controller.signal.aborted) setLearning(result);
    }).catch(() => {
      if (!controller.signal.aborted) setLearningError("load");
    });
    return () => controller.abort();
  }, [learningReload]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(false);
    void api.listPreferenceSupplements(page, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      const last = Math.max(1, Math.ceil(result.total / result.page_size));
      if (page > last) { setPage(last); return; }
      setData(result);
    }).catch(() => {
      if (!controller.signal.aborted) setLoadError(true);
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [page, reload]);

  async function toggleLearning(enabled: boolean) {
    if (!learning || busy) return;
    setLearningBusy(true);
    setLearningError("");
    try {
      setLearning(await api.setPreferenceLearning({ enabled, revision: learning.revision }));
    } catch (error) {
      setLearningError(conflict(error) ? "conflict" : "save");
    } finally { setLearningBusy(false); }
  }

  async function save() {
    if (!editing || busy || !draft.trim() || draft.length > 4000) return;
    setMutation(true);
    setActionError("");
    try {
      await api.updatePreferenceSupplement(editing.id, { content: draft, revision: editing.revision });
      setEditing(null);
      setReload(value => value + 1);
    } catch (error) {
      setActionError(conflict(error)
        ? t("这条补充已在别处更新，草稿仍保留。请复制草稿，取消编辑后刷新。", "This addition changed elsewhere. Your draft is kept; copy it, cancel editing, and refresh.")
        : t("暂时无法保存，草稿仍保留。", "Could not save. Your draft is still here."));
    } finally { setMutation(false); }
  }

  async function remove(item: PreferenceSupplement) {
    if (busy || editing) return;
    if (!window.confirm(t("删除这条 AI 补充？原始对话会保留，此操作不可撤销。", "Delete this AI addition? Original conversations will remain. This cannot be undone."))) return;
    setMutation(true);
    setActionError("");
    try {
      await api.deletePreferenceSupplement(item.id, item.revision);
      setExpanded(values => values.filter(value => value !== item.id));
      setReload(value => value + 1);
    } catch (error) {
      setActionError(conflict(error)
        ? t("这条补充已在别处更新，请刷新后重试。", "This addition changed elsewhere. Refresh before trying again.")
        : t("暂时无法删除，请稍后重试。", "Could not delete this addition. Try again shortly."));
    } finally { setMutation(false); }
  }

  const pages = Math.max(1, Math.ceil((data?.total ?? 0) / (data?.page_size ?? 10)));
  return <section className="memory-section preference-supplements" data-testid="preference-supplements">
    <button className="memory-supplements-heading" aria-expanded={open} aria-controls={id} disabled={!!editing || busy} onClick={() => setOpen(value => !value)}>
      <span aria-hidden="true">{open ? "⌄" : "›"}</span>
      <span>{t("AI 补充", "AI additions")} · {data?.total ?? "…"}</span>
    </button>
    {open && <div id={id} className="memory-supplements-content">
      <div className="memory-supplements-tools">
        <label className="memory-learning-toggle" title={t("仅影响今后的自动记录，已有补充会保留。", "Changes future learning only; existing additions remain.")}>
          <input type="checkbox" checked={learning?.enabled ?? false} disabled={!learning || busy || !!editing || !!learningError} onChange={event => void toggleLearning(event.target.checked)} />
          <span>{t("允许自动记住偏好", "Remember preferences automatically")}</span>
        </label>
        <button className="memory-text-btn" aria-label={t("刷新 AI 补充", "Refresh AI additions")} disabled={loading || busy || !!editing} onClick={() => { setReload(value => value + 1); setLearningReload(value => value + 1); setActionError(""); }}>{t("刷新", "Refresh")}</button>
      </div>
      {learningError && <div className="memory-error" role="alert">
        {learningError === "conflict" ? t("此设置已在别处更新，请刷新后重试。", "This setting changed elsewhere. Refresh before trying again.") : t("暂时无法读取或保存自动记录设置。", "Could not read or save the automatic learning setting.")}
        <button className="memory-text-btn" disabled={learningBusy} onClick={() => setLearningReload(value => value + 1)}>{t("重新读取", "Reload")}</button>
      </div>}
      {loadError ? <div className="memory-error" role="alert">{t("暂时无法读取 AI 补充。", "Could not load AI additions.")} <button className="memory-text-btn" onClick={() => setReload(value => value + 1)}>{t("重试", "Retry")}</button></div>
        : loading ? <div className="memory-empty" role="status">{t("正在读取…", "Loading…")}</div>
        : data?.items.length ? <>
          <div className="memory-supplements-list" role="region" aria-label={t("AI 补充列表", "AI additions list")} tabIndex={0}>
            {data.items.map(item => {
              const isEditing = editing?.id === item.id;
              const isExpanded = expanded.includes(item.id);
              return <div className="memory-supplement-row" key={item.id} data-testid="preference-supplement" data-status={item.status}>
                <div className="memory-supplement-meta">
                  <span className="memory-scope" title={item.scope === "topic" ? item.tree_title ?? undefined : undefined}>{item.scope === "global" ? t("全局", "Global") : t("主题内", "Topic only")}</span>
                  {item.scope === "topic" && <span className="memory-topic" title={item.tree_title ?? undefined}>{item.tree_title || t("未归属话题", "Unassigned topic")}</span>}
                  {item.status === "pending" && <span className="memory-pending">{t("待确认", "Pending")}</span>}
                  {item.user_edited && <span className="memory-caption">{t("由你编辑", "Edited by you")}</span>}
                </div>
                {isEditing ? <>
                  <textarea className="memory-fact-input memory-supplement-input" aria-label={t("编辑 AI 补充", "Edit AI addition")} value={draft} disabled={mutation} onChange={event => setDraft(event.target.value)} autoFocus />
                  {draft.length > 3600 && <div className={draft.length > 4000 ? "memory-error" : "memory-caption"}>{t("{count} / 4000 字符", "{count} / 4000 characters", { count: draft.length })}</div>}
                  <div className="memory-actions">
                    <button className="btn memory-small-btn" disabled={mutation} onClick={() => { setEditing(null); setActionError(""); }}>{t("取消", "Cancel")}</button>
                    <button className="btn memory-save" disabled={mutation || !draft.trim() || !dirty || draft.length > 4000} onClick={() => void save()}>{mutation ? t("保存中…", "Saving…") : t("保存", "Save")}</button>
                  </div>
                </> : <>
                  <button className={`memory-fact-preview${isExpanded ? " expanded" : ""}`} aria-expanded={isExpanded} title={t("展开 / 收起补充", "Expand / collapse addition")} onClick={() => setExpanded(values => isExpanded ? values.filter(value => value !== item.id) : [...values, item.id])}>{item.content}</button>
                  {isExpanded && item.evidence && <blockquote className="memory-supplement-evidence">{item.evidence}</blockquote>}
                  <div className="memory-fact-footer">
                    <span />
                    <div className="memory-actions">
                      {item.source_tree_id !== null && <button className="memory-text-btn" aria-label={t("查看来源", "View source")} title={item.evidence} disabled={busy || !!editing} onClick={() => onOpenSource(item.source_tree_id!, item.source_node_id)}>{t("来源 ↗", "Source ↗")}</button>}
                      <button className="memory-text-btn" aria-label={t("编辑 AI 补充", "Edit AI addition")} disabled={busy || !!editing} onClick={() => { setEditing(item); setDraft(item.content); setActionError(""); }}>{t("编辑", "Edit")}</button>
                      <button className="memory-text-btn memory-delete" disabled={busy || !!editing} onClick={() => void remove(item)}>{t("删除", "Delete")}</button>
                    </div>
                  </div>
                </>}
                {item.status === "pending" && <div className="memory-pending-note">
                  <span>{t("尚未生效，请在上方偏好中确认取舍。", "Not used yet. Resolve this in your preferences above.")}</span>
                  <button className="memory-text-btn" disabled={busy || !!editing || profileBusy} onClick={onEditProfile}>{t("编辑偏好", "Edit preferences")}</button>
                </div>}
              </div>;
            })}
          </div>
          <div className="memory-pagination">
            <span className="memory-caption">{t("第 {page} / {pages} 页", "Page {page} of {pages}", { page, pages })}</span>
            <div className="memory-actions">
              <button className="btn memory-small-btn" disabled={page <= 1 || !!editing || busy} onClick={() => { setPage(value => value - 1); setExpanded([]); setActionError(""); }}>{t("上一页", "Previous")}</button>
              <button className="btn memory-small-btn" disabled={page >= pages || !!editing || busy} onClick={() => { setPage(value => value + 1); setExpanded([]); setActionError(""); }}>{t("下一页", "Next")}</button>
            </div>
          </div>
        </> : <div className="memory-empty">{t("明确的长期偏好会记录在这里。", "Explicit long-term preferences will appear here.")}</div>}
      {actionError && <div className="memory-error" role="alert">{actionError}</div>}
    </div>}
  </section>;
}
