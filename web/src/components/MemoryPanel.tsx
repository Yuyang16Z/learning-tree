import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api";
import { useI18n } from "../i18n";
import type { MemoryFact, MemoryFactsPage, PreferenceProfile } from "../types";
import { PreferenceSupplements } from "./PreferenceSupplements";
import "./MemoryPanel.css";

interface Props {
  onDirtyChange: (dirty: boolean) => void;
  onBusyChange: (busy: boolean) => void;
  onOpenSource: (treeId: number, nodeId: number | null) => void;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isConflict(error: unknown): boolean {
  return typeof error === "object" && error !== null && "status" in error && error.status === 409;
}

export function MemoryPanel({ onDirtyChange, onBusyChange, onOpenSource }: Props) {
  const { t } = useI18n();
  const profileId = useId();
  const profileEditorRef = useRef<HTMLTextAreaElement>(null);
  const [supplementDirty, setSupplementDirty] = useState(false);
  const [supplementBusy, setSupplementBusy] = useState(false);
  const [profile, setProfile] = useState<PreferenceProfile | null>(null);
  const [draft, setDraft] = useState("");
  const [profileReload, setProfileReload] = useState(0);
  const [profileLoading, setProfileLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [profileError, setProfileError] = useState("");
  const [profileConflict, setProfileConflict] = useState(false);
  const [saved, setSaved] = useState(false);
  const [facts, setFacts] = useState<MemoryFactsPage | null>(null);
  const [query, setQuery] = useState("");
  const [settledQuery, setSettledQuery] = useState("");
  const [topic, setTopic] = useState("all");
  const [page, setPage] = useState(1);
  const [factsReload, setFactsReload] = useState(0);
  const [factsLoading, setFactsLoading] = useState(true);
  const [factsError, setFactsError] = useState("");
  const [selected, setSelected] = useState<number[]>([]);
  const [expanded, setExpanded] = useState<number[]>([]);
  const [editing, setEditing] = useState<MemoryFact | null>(null);
  const [factDraft, setFactDraft] = useState("");
  const [mutation, setMutation] = useState(false);
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const profileDirty = profile !== null && draft !== profile.content;
  const factDirty = editing !== null && factDraft !== editing.content;

  useEffect(() => {
    onDirtyChange(profileDirty || factDirty || supplementDirty);
  }, [profileDirty, factDirty, supplementDirty, onDirtyChange]);

  useEffect(() => {
    onBusyChange(saving || mutation || supplementBusy);
  }, [saving, mutation, supplementBusy, onBusyChange]);

  useEffect(() => {
    const controller = new AbortController();
    setProfileLoading(true);
    setProfileError("");
    void api.getPreferences(controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setProfile(result);
      setDraft(result.content);
      setProfileConflict(false);
      setSaved(false);
    }).catch(error => {
      if (!controller.signal.aborted) setProfileError(errorMessage(error));
    }).finally(() => {
      if (!controller.signal.aborted) setProfileLoading(false);
    });
    return () => controller.abort();
  }, [profileReload]);

  useEffect(() => {
    const timer = setTimeout(() => setSettledQuery(query.trim()), 200);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    setFactsLoading(true);
    setFactsError("");
    void api.listFacts({ q: settledQuery || undefined, tree_id: topic === "all" ? undefined : Number(topic), page, page_size: 10 }, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        const lastPage = Math.max(1, Math.ceil(result.total / result.page_size));
        if (page > lastPage) { setPage(lastPage); return; }
        setFacts(result);
      }).catch(error => {
        if (!controller.signal.aborted) setFactsError(errorMessage(error));
      }).finally(() => {
        if (!controller.signal.aborted) setFactsLoading(false);
      });
    return () => controller.abort();
  }, [settledQuery, topic, page, factsReload]);

  function reloadProfile() {
    if (profileDirty && !window.confirm(t("读取最新版本会放弃这里未保存的偏好修改，继续吗？", "Load the latest version and discard your unsaved preference changes?"))) return;
    setProfileReload(value => value + 1);
  }

  async function saveProfile() {
    if (!profile || saving) return;
    setSaving(true);
    setProfileError("");
    setProfileConflict(false);
    setSaved(false);
    try {
      const result = await api.savePreferences({ content: draft, revision: profile.revision });
      setProfile(result);
      setDraft(result.content);
      setSaved(true);
    } catch (error) {
      setProfileError(errorMessage(error));
      setProfileConflict(isConflict(error));
    } finally { setSaving(false); }
  }

  function clearSelection() {
    setSelected([]);
    setExpanded([]);
    setActionError("");
    setActionNotice("");
  }

  async function saveFact() {
    if (!editing || mutation || !factDraft.trim()) return;
    setMutation(true);
    setActionError("");
    try {
      await api.updateMemory(editing.id, { content: factDraft, expected_content: editing.content });
      setEditing(null);
      setFactsReload(value => value + 1);
      setActionNotice(t("记忆已更新", "Memory updated"));
    } catch (error) {
      setActionError(isConflict(error)
        ? t("这条记忆已在别处修改。你的草稿仍在；请复制草稿，取消编辑并刷新后重试。", "This memory changed elsewhere. Your draft is safe here; copy it, cancel editing, and refresh before trying again.")
        : errorMessage(error));
    } finally { setMutation(false); }
  }

  async function deleteFacts(ids: number[]) {
    if (!ids.length || mutation) return;
    if (!window.confirm(t("删除选中的 {count} 条话题记忆？此操作不可撤销，原始对话会保留。", "Delete {count} selected topic memories? This cannot be undone. Original conversations will remain.", { count: ids.length }))) return;
    setMutation(true);
    setActionError("");
    try {
      const result = await api.deleteMemories(ids);
      setSelected([]);
      setFactsReload(value => value + 1);
      setActionNotice(t("已删除 {count} 条记忆", "Deleted {count} memories", { count: result.deleted }));
    } catch (error) { setActionError(errorMessage(error)); }
    finally { setMutation(false); }
  }

  const waiting = factsLoading || query.trim() !== settledQuery;
  const pages = Math.max(1, Math.ceil((facts?.total ?? 0) / 10));
  const allSelected = !!facts?.items.length && facts.items.every(item => selected.includes(item.id));

  return (
    <div className="memory-manager" data-testid="memory-management">
      <section className="memory-section" aria-labelledby={profileId}>
        <div className="memory-heading">
          <label id={profileId} htmlFor={`${profileId}-input`}>{t("关于你", "About you")}</label>
          <span className="memory-caption">{t("偏好 / 习惯", "Preferences / habits")}</span>
        </div>
        {profileLoading ? <div className="memory-empty" role="status">{t("正在读取…", "Loading…")}</div> : profile && (
          <>
            <textarea ref={profileEditorRef} id={`${profileId}-input`} data-testid="preference-editor" className="memory-profile-input" value={draft}
              disabled={saving} onChange={event => { setDraft(event.target.value); setSaved(false); }}
              placeholder={t("写下你希望 AI 记住的偏好，例如：先用简单的例子解释，再介绍术语。", "What should AI remember about you? For example: explain with a simple example before introducing terminology.")} />
            <div className="memory-editor-footer">
              <span className="memory-caption">{profile.managed ? t("由你管理，AI 不会改写。", "Managed by you. AI will not rewrite it.") : t("保存后由你管理，AI 不会改写。", "After saving, you manage this. AI will not rewrite it.")}</span>
              {(profileDirty || !profile.managed || saving) ? <div className="memory-actions">
                {profileDirty && <button className="btn memory-small-btn" disabled={saving} onClick={() => { setDraft(profile.content); setProfileError(""); setProfileConflict(false); setSaved(false); }}>{t("取消", "Cancel")}</button>}
                <button className="btn memory-save" aria-label={t("保存偏好", "Save preferences")} disabled={saving || draft.length > 6000} onClick={() => void saveProfile()}>{saving ? t("保存中…", "Saving…") : t("保存", "Save")}</button>
              </div> : saved && <span className="memory-saved" role="status">{t("已保存", "Saved")}</span>}
            </div>
          </>
        )}
        {draft.length > 5400 && <div className={draft.length > 6000 ? "memory-error" : "memory-caption"}>{t("{count} / 6000 字符", "{count} / 6000 characters", { count: draft.length })}{draft.length > 6000 && t(" · 请精简后再保存，原内容仍保留。", " · Shorten this before saving. Your original content is preserved.")}</div>}
        {profileError && <div className="memory-error" role="alert">{profileConflict ? t("偏好已在别处更新，当前草稿仍保留。请读取最新版本后再修改。", "Preferences changed in another window. Your draft is still here. Load the latest version before editing again.") : profileError}
          <button className="memory-text-btn" disabled={profileLoading} onClick={reloadProfile}>{profileConflict ? t("读取最新版本", "Load latest version") : t("重新读取", "Reload")}</button>
        </div>}
      </section>

      <PreferenceSupplements onDirtyChange={setSupplementDirty} onBusyChange={setSupplementBusy} onOpenSource={onOpenSource}
        profileBusy={saving || profileLoading} onEditProfile={() => {
          profileEditorRef.current?.focus();
          profileEditorRef.current?.scrollIntoView({ block: "center" });
        }} />

      <section className="memory-section memory-facts" aria-label={t("话题记忆", "Topic memories")}>
        <div className="memory-heading">
          <span>{t("话题记忆", "Topic memories")}</span>
          <span className="memory-caption">{facts ? t("{count} 条", "{count} items", { count: facts.total }) : ""}</span>
        </div>
        <div className="memory-filters">
          <input type="search" maxLength={300} value={query} disabled={!!editing || mutation} aria-label={t("搜索记忆", "Search memories")} placeholder={t("搜索记忆…", "Search memories…")}
            onChange={event => { setQuery(event.target.value); setPage(1); clearSelection(); }} />
          <select value={topic} disabled={!!editing || mutation} aria-label={t("按话题筛选", "Filter by topic")}
            onChange={event => { setTopic(event.target.value); setPage(1); clearSelection(); }}>
            <option value="all">{t("所有话题", "All topics")}</option>
            {facts?.topics.map(item => <option key={item.tree_id ?? "unknown"} value={item.tree_id ?? "unknown"} disabled={item.tree_id === null}>{item.title || t("未归属话题", "Unassigned topic")} · {item.count}</option>)}
          </select>
        </div>
        {factsError ? <div className="memory-error" role="alert">{factsError} <button className="memory-text-btn" onClick={() => setFactsReload(value => value + 1)}>{t("重试", "Retry")}</button></div>
          : waiting ? <div className="memory-empty" role="status">{t("正在查找…", "Loading memories…")}</div>
          : facts?.items.length ? <>
            <div className="memory-bulk-bar">
              <label><input type="checkbox" checked={allSelected} disabled={!!editing || mutation} onChange={() => setSelected(allSelected ? [] : facts.items.map(item => item.id))} /> {selected.length ? t("已选 {count} 条", "{count} selected", { count: selected.length }) : t("选择本页", "Select page")}</label>
              {selected.length > 0 && <button className="memory-text-btn memory-delete" disabled={mutation || !!editing} onClick={() => void deleteFacts(selected)}>{mutation ? t("处理中…", "Working…") : t("删除所选", "Delete selected")}</button>}
              {selected.length === 0 && <button className="memory-text-btn" disabled={mutation || !!editing} onClick={() => setFactsReload(value => value + 1)}>{t("刷新", "Refresh")}</button>}
            </div>
            <div className="memory-list">
              {facts.items.map(fact => {
                const isExpanded = expanded.includes(fact.id);
                const isEditing = editing?.id === fact.id;
                return <div className={`memory-fact-row${isEditing ? " editing" : ""}`} key={fact.id} data-testid="memory-fact">
                  <input type="checkbox" checked={selected.includes(fact.id)} disabled={!!editing || mutation} aria-label={t("选择记忆：{content}", "Select memory: {content}", { content: fact.content.slice(0, 80) })}
                    onChange={() => setSelected(values => values.includes(fact.id) ? values.filter(id => id !== fact.id) : [...values, fact.id])} />
                  <div className="memory-fact-body">
                    {isEditing ? <>
                      <textarea className="memory-fact-input" maxLength={4000} aria-label={t("编辑记忆", "Edit memory")} value={factDraft} disabled={mutation} onChange={event => setFactDraft(event.target.value)} autoFocus />
                      <div className="memory-actions">
                        <button className="btn memory-small-btn" disabled={mutation} onClick={() => { setEditing(null); setActionError(""); }}>{t("取消", "Cancel")}</button>
                        <button className="btn memory-save" disabled={mutation || !factDraft.trim() || !factDirty} onClick={() => void saveFact()}>{mutation ? t("保存中…", "Saving…") : t("保存", "Save")}</button>
                      </div>
                    </> : <>
                      <button className={`memory-fact-preview${isExpanded ? " expanded" : ""}`} aria-expanded={isExpanded} title={t("展开 / 收起记忆", "Expand / collapse memory")}
                        onClick={() => setExpanded(values => isExpanded ? values.filter(id => id !== fact.id) : [...values, fact.id])}>{fact.content}</button>
                      <div className="memory-fact-footer">
                        <span className="memory-topic" title={fact.tree_title ?? undefined}>{fact.tree_title || t("未归属话题", "Unassigned topic")}</span>
                        <div className="memory-actions">
                          {fact.tree_id !== null && <button className="memory-text-btn" aria-label={t("查看来源", "View source")} disabled={mutation || !!editing} onClick={() => onOpenSource(fact.tree_id!, fact.source_node_id)}>{t("来源 ↗", "Source ↗")}</button>}
                          <button className="memory-text-btn" aria-label={t("编辑记忆", "Edit memory")} disabled={mutation || !!editing} onClick={() => { setEditing(fact); setFactDraft(fact.content); setActionError(""); setActionNotice(""); }}>{t("编辑", "Edit")}</button>
                          <button className="memory-text-btn memory-delete" disabled={mutation || !!editing} onClick={() => void deleteFacts([fact.id])}>{t("删除", "Delete")}</button>
                        </div>
                      </div>
                    </>}
                  </div>
                </div>;
              })}
            </div>
            <div className="memory-pagination">
              <span className="memory-caption">{t("第 {page} / {pages} 页", "Page {page} of {pages}", { page, pages })}</span>
              <div className="memory-actions">
                <button className="btn memory-small-btn" disabled={page <= 1 || !!editing || mutation} onClick={() => { setPage(value => value - 1); clearSelection(); }}>{t("上一页", "Previous")}</button>
                <button className="btn memory-small-btn" disabled={page >= pages || !!editing || mutation} onClick={() => { setPage(value => value + 1); clearSelection(); }}>{t("下一页", "Next")}</button>
              </div>
            </div>
          </> : <div className="memory-empty">{query || topic !== "all" ? t("没有找到匹配的记忆。", "No memories match your search.") : t("对话中值得保留的结论会出现在这里。", "Useful facts from your conversations will appear here.")}</div>}
        {actionError && <div className="memory-error" role="alert">{actionError}</div>}
        {actionNotice && <div className="memory-saved" role="status">{actionNotice}</div>}
      </section>
    </div>
  );
}
