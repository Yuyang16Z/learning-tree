import { useState } from "react";
import { api } from "../api";
import { useI18n } from "../i18n";
import { localizeError } from "../i18n/workspace";
import type { DocumentDetail, DocumentSummary } from "../types";

function DocumentCard({ document, onRemove }: { document: DocumentSummary; onRemove?: (id: string) => void }) {
  const { locale, t } = useI18n();
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function preview() {
    if (loading) return;
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (detail) return;
    setLoading(true); setError(null);
    try { setDetail(await api.getDocument(document.id)); }
    catch (error) { setError(error instanceof Error ? error.message : t("无法读取文档，请重试。", "Could not read the document. Try again.")); }
    finally { setLoading(false); }
  }
  return <div className="chat-document-card">
    <div className="chat-document-heading"><span className="chat-document-name" title={document.name}>{document.name}</span>
      {onRemove && <button onClick={() => onRemove(document.id)} aria-label={t("移除文档 {name}", "Remove document {name}", { name: document.name })}>×</button>}
    </div>
    <div className="chat-document-meta">{Math.max(1, Math.ceil(document.size / 1024))} KB · {t("已提取 {count} 字符", "{count} characters extracted", { count: document.characters })}</div>
    <div className="chat-document-actions"><button onClick={() => void preview()} disabled={loading} aria-expanded={open}>{loading ? t("读取中…", "Loading…") : open ? t("收起文字", "Hide text") : t("预览文字", "Preview text")}</button><a href={api.documentDownloadUrl(document.id)} download={document.name}>{t("下载", "Download")}</a></div>
    {!!document.warnings?.length && <div className="chat-document-warning">{document.warnings.map((warning, i) => <div key={i}>{localizeError(warning, locale)}</div>)}</div>}
    {open && <div className="chat-document-preview">{error ? <div role="alert">{localizeError(error, locale)}<button onClick={() => { setOpen(false); setError(null); }}>{t("关闭后重试", "Close and try again")}</button></div> : detail?.sections.map((section, index) => <section key={index}><h3>{section.label}</h3><pre>{section.text}</pre></section>)}</div>}
  </div>;
}

export function DocumentCards({ documents, onRemove }: { documents: DocumentSummary[]; onRemove?: (id: string) => void }) {
  return documents.length ? <div className="chat-documents">{documents.map(document => <DocumentCard key={document.id} document={document} onRemove={onRemove} />)}</div> : null;
}
