import { useEffect, useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type ContextStatus } from "../api";
import { useI18n } from "../i18n";
import { localizeError, mcpDisplayName } from "../i18n/workspace";
import { clearAcceptedDraft, isEmptyDraft, moveFollowupDraft, type ChatDraft as Draft } from "../lib/chatDrafts";
import { documentMetadata, DocumentUploadQueue } from "../lib/documentUploads";
import { ATTACHMENT_ACCEPT, attachmentKind, ImageAttachmentQueue, MAX_IMAGES, readImage } from "../lib/attachments";
import { copyText } from "../lib/clipboard";
import { hasManagedWebSearch, selectedChatTools } from "../lib/chatTools";
import { matchesDeletedWorkspace, onWorkspaceDeletion, type WorkspaceDeletion } from "../lib/workspace";
import { fullResultSource, toolStepLabel, type FullResultSource } from "../lib/toolSteps";
import { diagnosticsText, roundLines } from "../lib/answerDiagnostics";
import { sourceLink, type SourceView } from "../lib/sourceLinks";
import { locatePassage } from "../lib/passage";
import { DocumentCards } from "./DocumentCards";
import { ChatTurnNavigator, turnKey } from "./ChatTurnNavigator";
import { markTarget, toggleUnderstanding, UNDERSTANDING_LEVELS, UNDERSTANDING_TEXT } from "../lib/understanding";
import { followReturnTrip, hasReturnContent, returnTitle, type ReturnTrip } from "../lib/returnTrip";
import type { AnswerDiagnostics, AnswerSource, DocumentSummary, LearningPresetList, McpServer, ModelCfg, ReturnExcerptCategory, ReturnSummary, ThreadNode, ToolConfirmation, ToolStep, Understanding } from "../types";
import { LearningStyleMenu, LearningStyleTag } from "./LearningStyle";
import "./ChatPane.css";

type Anchor = { source_message_id?: number; source_start?: number; source_end?: number; learning_note?: string };
type Destination = { messageId?: number; start?: number; end?: number; text?: string };
type Ask = { question: string; images?: string[]; document_ids?: string[]; documents?: DocumentSummary[]; tools?: string[]; deep?: boolean; mode?: "continue" | "retry" | "revise"; nodeId?: number; question_message_id?: number; onAccepted?: (nodeId: number) => void };
interface Props {
  thread: ThreadNode[];
  treeTitle: string;
  hasNode: boolean;
  loading?: boolean;
  activeNodeId: number | null;
  activeTreeId: number | null;
  focusedSeed: string | null;
  live: string;
  liveReasoning: string;
  liveSteps: ToolStep[];
  /** A tool call of the running answer waiting for approval, and how to answer it. */
  liveConfirmation?: ToolConfirmation | null;
  onConfirmTool?: (index: number, allow: boolean) => Promise<void>;
  liveDiagnostics?: AnswerDiagnostics | null;
  /** Memory records the running answer was given; shown as a compact "Referenced" line. */
  liveSources?: AnswerSource[];
  /** Open a memory source after checking that it still exists; it may live in another topic. */
  onOpenSource?: (treeId: number, nodeId: number | null) => void;
  /** Topics and open-map nodes that exist, so chips for deleted sources render disabled. */
  topicIds?: ReadonlySet<number>;
  mapNodeIds?: ReadonlySet<number>;
  contextStatus: ContextStatus | null;
  /** The local answer's connection dropped and is being reattached. */
  reconnecting?: boolean;
  pendingQuestion: string | null;
  sendingImages: string[];
  sendingDocuments: DocumentSummary[];
  streaming: boolean;
  showStream: boolean;
  err: string | null;
  models: ModelCfg[];
  activeModelId: number | null;
  onModelChange: (id: number) => void;
  onOpenSettings: () => void;
  onAsk: (p: Ask) => Promise<boolean>;
  onStop: () => void;
  /** Stop an answer that another window or device is generating. */
  onStopNode?: (nodeId: number) => void;
  /** Save the learner's mark for a node; rejects (after restoring the old mark) when it fails. */
  onUnderstandingChange?: (nodeId: number, value: Understanding | null) => Promise<void>;
  onBranch: (seed: string, fromNodeId: number, anchor?: Anchor, onCreated?: (nodeId: number) => void) => Promise<void>;
  onDeleteNode: (id: number) => void;
  onNavigate: (id: number, anchor?: Destination) => Promise<void>;
  navigationAnchor?: (Destination & { nodeId: number; nonce: number }) | null;
  rightOpen: boolean;
  onToggleRight: () => void;
  onAddMock: () => void;
  mcpServers: McpServer[];
  onTestMcp: (id: number) => Promise<{ ok: boolean; detail: string; tools: string[] }>;
  onExport?: () => void;
  onNew?: () => void;
  /** Built-in and custom learning styles; null until loaded. */
  learningPresets?: LearningPresetList | null;
  learningPresetsFailed?: boolean;
  /** The open topic's style for its next questions; null = the default way. */
  topicPreset?: string | null;
  /** Save the open topic's style; rejects (after restoring the old one) when it fails. */
  onLearningPresetChange?: (id: string | null) => Promise<void>;
  onReloadPresets?: () => void;
  /** The style of the question being sent, for its tag. */
  sendingPreset?: string | null;
}

type SelectionCard = {
  text: string; nodeId: number; messageId?: number; start: number; end: number; x: number; y: number;
  state: "selected" | "loading" | "ready" | "error"; explanation?: string; error?: string;
};
const draftKeyFor = (tree: number | null, node: number | null) => `bl-draft-v2:${tree}:${node}`;
const scrollKeyFor = (tree: number | null, node: number | null) => `bl-scroll-v2:${tree}:${node}`;
const markdownPlugins = [remarkGfm];
const NO_IDS: ReadonlySet<number> = new Set();
const draftCache = new Map<string, Draft>();
const noteDraftCache = new Map<string, string>();
const deletedWorkspaces: WorkspaceDeletion[] = [];
const deletedDraft = (key: string) => deletedWorkspaces.some(scope => matchesDeletedWorkspace(key, scope));


function readDraft(key: string): Draft {
  if (deletedDraft(key)) return { key, text: "", images: [] };
  const cached = draftCache.get(key);
  if (cached) return cached;
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? "null");
    if (value && typeof value.text === "string") return { ...value, key, images: Array.isArray(value.images) ? value.images : [], documents: documentMetadata(value.documents),
      previous: value.previous ? { text: value.previous.text ?? "", images: value.previous.images ?? [], documents: documentMetadata(value.previous.documents) } : undefined };
  } catch { /* Storage may be unavailable; editing still works in memory. */ }
  return { key, text: "", images: [] };
}
function writeDraft(draft: Draft) {
  if (deletedDraft(draft.key)) return;
  draft = { ...draft, documents: documentMetadata(draft.documents),
    previous: draft.previous ? { ...draft.previous, documents: documentMetadata(draft.previous.documents) } : undefined };
  draftCache.set(draft.key, draft);
  if (!draft.text && !draft.images.length && !draft.documents?.length && !draft.revisionId) localStorage.removeItem(draft.key);
  else localStorage.setItem(draft.key, JSON.stringify(draft));
}

function Markdown({ text }: { text: string }) {
  return <ReactMarkdown remarkPlugins={markdownPlugins} components={{ a: ({ children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a> }}>{text}</ReactMarkdown>;
}
function ReasoningBlock({ text }: { text: string }) {
  const { t } = useI18n();
  if (!text) return null;
  return <details className="chat-reasoning"><summary>{t("思考过程", "Reasoning")}</summary><div>{text}</div></details>;
}
/** A truncated step's preview, replaced by the stored complete text page by page on request. */
function FullToolResult({ preview, source }: { preview: string; source: FullResultSource }) {
  const { t, locale } = useI18n();
  const [text, setText] = useState<string | null>(null);
  const [next, setNext] = useState<number | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function load(start: number) {
    setLoading(true);
    setError(null);
    try {
      const page = await api.toolResult(source.nodeId, source.callIndex, source.requestId, start);
      setText(current => (start === 0 ? "" : current ?? "") + page.text);
      setNext(page.next_start);
      setTotal(page.total_chars);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("完整结果暂时无法读取。", "The full result could not be loaded."));
    } finally {
      setLoading(false);
    }
  }
  const busy = loading ? t("读取中…", "Loading…") : null;
  return <>
    <div>{text ?? preview}</div>
    <p className="chat-tool-full">
      {text === null && <button type="button" className="btn" disabled={loading} onClick={() => void load(0)}>{busy ?? t("查看完整结果", "View full result")}</button>}
      {text !== null && next !== null && <button type="button" className="btn" disabled={loading} onClick={() => void load(next)}>{busy ?? t("继续加载（已显示 {shown}/{total} 字）", "Load more ({shown} of {total} characters)", { shown: text.length, total })}</button>}
      {error && <span className="test-bad" role="alert">{localizeError(error, locale)}</span>}
    </p>
  </>;
}
function ToolSteps({ steps, mcpServers, nodeId }: { steps: ToolStep[]; mcpServers: McpServer[]; nodeId?: number | null }) {
  const { t } = useI18n();
  const status = (step: ToolStep) => step.result === null ? t(" · 进行中", " · Running")
    : step.confirmation === "declined" ? t(" · 未允许，没有运行", " · Not allowed, not run")
    : step.confirmation === "expired" ? t(" · 确认超时，没有运行", " · Not confirmed in time, not run")
    : step.confirmation === "allowed" ? t(" · 已允许 · 已完成", " · Allowed · Done")
    : t(" · 已完成", " · Done");
  return <>{steps.map((step, i) => {
    const source = fullResultSource(step, nodeId);
    return <details className="chat-reasoning" key={i}><summary>{toolStepLabel(step.tool, t, mcpServers)}{status(step)}</summary>{step.result !== null && (source ? <FullToolResult preview={step.result} source={source} /> : <div>{step.result}</div>)}</details>;
  })}</>;
}
/** Arguments of a call waiting for approval, as readable JSON (shortened when long). */
function confirmationArgs(args: unknown): string {
  if (args == null || (typeof args === "object" && !Array.isArray(args) && !Object.keys(args).length)) return "";
  let text: string;
  try { text = JSON.stringify(args, null, 2) ?? ""; } catch { text = String(args); }
  return text.length > 1200 ? text.slice(0, 1200) + "…" : text;
}
/** The tool call the running answer waits on: what would run, and the learner's decision. */
function ToolConfirmCard({ request, mcpServers, onDecide }: { request: ToolConfirmation; mcpServers: McpServer[]; onDecide?: (index: number, allow: boolean) => Promise<void> }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => { setBusy(false); setFailed(false); }, [request.index]);
  const args = confirmationArgs(request.args);
  async function decide(allow: boolean) {
    if (busy || !onDecide) return;
    setBusy(true); setFailed(false);
    // On success the stream's outcome closes the card; a failed send can be retried.
    try { await onDecide(request.index, allow); } catch { setFailed(true); setBusy(false); }
  }
  return <div className="chat-confirm" role="group" aria-label={t("工具运行确认", "Tool approval")}>
    <div className="chat-confirm-title">{t("这个回答要运行「{tool}」，允许吗？", "This answer wants to run “{tool}”. Allow it?", { tool: toolStepLabel(request.name, t, mcpServers) })}</div>
    {args && <pre className="chat-confirm-args">{args}</pre>}
    {failed && <div className="test-bad" role="alert">{t("没有送达，请重试。", "Not delivered. Please try again.")}</div>}
    <div className="chat-confirm-actions"><button type="button" className="btn btn-primary" onClick={() => void decide(true)} disabled={busy}>{t("允许运行", "Allow")}</button><button type="button" className="btn" onClick={() => void decide(false)} disabled={busy}>{t("不允许", "Don't allow")}</button></div>
  </div>;
}
/** Numbers only, collapsed by default: how the requests were budgeted and what the provider reported. */
function DiagnosticsBlock({ data }: { data?: AnswerDiagnostics | null }) {
  const { t, locale } = useI18n();
  if (!data || !data.timings_ms) return null;
  const n = (value: number | null | undefined) => value == null ? "–" : value.toLocaleString(locale === "en" ? "en-US" : "zh-CN");
  // Context is one request's input; with tool rounds the sum over requests is a line of its own.
  const { summary, total } = diagnosticsText(data, t, n);
  const rounds = roundLines(data, t, n);
  const timing = (label: string, ms: number | undefined) => `${label} ${((ms ?? 0) / 1000).toFixed(2)} s`;
  return <details className="chat-reasoning chat-diagnostics"><summary>{summary}</summary><div>
    {total && <>{total}{"\n"}</>}
    {rounds.map((line, index) => <span key={index}>{line}{"\n"}</span>)}
    {t("输出 {tokens} tokens", "Output {tokens} tokens", { tokens: n(data.output_tokens) })} · {data.compacted ? t("历史已压缩", "History compacted") : t("历史完整", "Full history")} · {t("校准系数 {ratio}", "Calibration ratio {ratio}", { ratio: (data.calibration_ratio ?? 1).toFixed(2) })}{typeof data.target_input_tokens === "number" ? t(" · 单次目标 {tokens} tokens", " · Request target {tokens} tokens", { tokens: n(data.target_input_tokens) }) : ""}{data.summary_blocks === 1 ? t(" · 历史摘要 1 段", " · 1 history summary") : data.summary_blocks ? t(" · 历史摘要 {count} 段", " · {count} history summaries", { count: data.summary_blocks }) : ""}{"\n"}
    {[timing(t("检索", "Retrieval"), data.timings_ms.retrieval), timing(t("整理", "Summary"), data.timings_ms.summary), timing(t("模型", "Model"), data.timings_ms.model), timing(t("工具", "Tools"), data.timings_ms.tools), timing(t("总计", "Total"), data.timings_ms.total)].join(" · ")}
  </div></details>;
}
function SourcesLine({ sources, view, onNavigate, onOpenSource }: { sources: AnswerSource[]; view: SourceView; onNavigate: (id: number) => void; onOpenSource?: (treeId: number, nodeId: number | null) => void }) {
  const { t } = useI18n();
  if (!sources.length) return null;
  return <div className="chat-sources"><span>{t("参考了：", "Referenced: ")}</span>{sources.map(source => {
    const label = source.scope === "other"
      ? t("「{title}」记忆 {id}", "“{title}” memory {id}", { title: source.tree_title, id: source.memory_id })
      : source.scope === "tree" ? t("其他分支记忆 {id}", "Other-branch memory {id}", { id: source.memory_id }) : t("记忆 {id}", "Memory {id}", { id: source.memory_id });
    const link = sourceLink(source, view);
    const title = link.kind === "open" ? t("打开来源节点 {node}", "Open source node {node}", { node: link.nodeId })
      : link.kind === "deleted" ? t("来源已删除", "Source deleted") : t("没有可打开的来源节点", "No source node to open");
    // The opener checks the source again, since it may be deleted after this list was drawn.
    return <button type="button" key={source.memory_id} className={`chat-source-link${link.kind === "deleted" ? " is-deleted" : ""}`} disabled={link.kind !== "open"} title={title} onClick={() => {
      if (link.kind !== "open") return;
      if (onOpenSource) onOpenSource(link.treeId, link.nodeId);
      else onNavigate(link.nodeId);
    }}>{label}</button>;
  })}</div>;
}
/** The learner's own mark for a node. Arrow keys move focus only; a click or Space saves. */
function UnderstandingControl({ value, disabled, saving, onChange }: { value: Understanding | null; disabled: boolean; saving: boolean; onChange: (value: Understanding | null) => void }) {
  const { t } = useI18n();
  const options = useRef<(HTMLButtonElement | null)[]>([]);
  const focusable = value ? UNDERSTANDING_LEVELS.indexOf(value) : 0;
  const move = (event: React.KeyboardEvent, index: number) => {
    const last = UNDERSTANDING_LEVELS.length - 1;
    const targets: Record<string, number> = { ArrowRight: index + 1, ArrowDown: index + 1, ArrowLeft: index - 1, ArrowUp: index - 1, Home: 0, End: last };
    const target = targets[event.key];
    if (target === undefined) return;
    event.preventDefault();
    options.current[(target + last + 1) % (last + 1)]?.focus();
  };
  return <div className="chat-understanding" role="radiogroup" aria-label={t("理解", "Understanding")} aria-busy={saving || undefined}>
    <span className="chat-understanding-label" aria-hidden="true">{t("理解", "Understanding")}</span>
    {UNDERSTANDING_LEVELS.map((level, index) => <button key={level} ref={element => { options.current[index] = element; }} type="button" role="radio" aria-checked={value === level} tabIndex={index === focusable ? 0 : -1} disabled={disabled} className={`u-${level}${value === level ? " is-checked" : ""}`} title={value === level ? t("再次点击可取消标记", "Click again to clear the mark") : undefined} onKeyDown={event => move(event, index)} onClick={() => onChange(toggleUnderstanding(value, level))}>{t(...UNDERSTANDING_TEXT[level])}</button>)}
  </div>;
}
/** Shown above the composer after returning from a branch; a local extract, never an AI summary. */
function ReturnCard({ summary, onDismiss }: { summary: ReturnSummary; onDismiss: () => void }) {
  const { t } = useI18n();
  const categories: Record<ReturnExcerptCategory, string> = { correction: t("纠正", "Correction"), condition: t("条件", "Condition"), definition: t("定义", "Definition"), question: t("待解决", "Open question"), explanation: t("解释", "Explanation") };
  return <section className="chat-return-card" aria-labelledby="chat-return-title">
    <div className="chat-return-head"><h2 id="chat-return-title">{t("从分支「{title}」回来", "Back from “{title}”", { title: returnTitle(summary, t("新分支", "New branch")) })}</h2>{summary.understanding && <span className={`chat-return-mark u-${summary.understanding}`}>{t(...UNDERSTANDING_TEXT[summary.understanding])}</span>}<button type="button" className="chat-return-dismiss" onClick={onDismiss} aria-label={t("关闭分支回顾", "Dismiss branch recap")} title={t("关闭", "Dismiss")}>×</button></div>
    {summary.excerpts.length > 0 && <ul>{summary.excerpts.map((excerpt, index) => <li key={`${excerpt.message_id}:${index}`}><span className="chat-return-category">{categories[excerpt.category] ?? excerpt.category}</span>{excerpt.text}</li>)}</ul>}
    {summary.note && <p className="chat-return-note"><span>{t("我的理解", "My reflection")}</span>{summary.note}</p>}
    <p className="chat-return-hint">{t("从分支回答原文中本地摘录，不是 AI 总结。", "Sentences picked locally from the branch’s answers; not an AI summary.")}</p>
  </section>;
}
function ImageStrip({ images }: { images: string[] }) {
  const { t } = useI18n();
  return <div className="chat-images">{images.map((src, i) => <a href={src} target="_blank" rel="noreferrer" key={i}><img src={src} alt={t("附图 {number}", "Attachment {number}", { number: i + 1 })} /></a>)}</div>;
}
function Icon({ name }: { name: "tree" | "attachment" | "tools" | "export" | "arrow" | "copy" | "check" }) {
  const paths = { tree: <><path d="M6 4v14m0-9h8a4 4 0 0 0 4-4" /><circle cx="6" cy="19" r="2" /><circle cx="18" cy="4" r="2" /></>, attachment: <path d="m21 11-8.4 8.4a6 6 0 0 1-8.5-8.5L13 2a4 4 0 0 1 5.7 5.7l-8.8 8.8a2 2 0 0 1-2.8-2.8l8.1-8.1" />, tools: <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="2" fill="var(--bg)" /><circle cx="15" cy="17" r="2" fill="var(--bg)" /></>, export: <><path d="M12 3v12m-4-4 4 4 4-4M4 15v5h16v-5" /></>, arrow: <><path d="M12 19V5m-5 5 5-5 5 5" /></>, copy: <><rect x="8" y="8" width="12" height="13" rx="2" /><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" /></>, check: <path d="m5 12 4 4L19 6" /> };
  return <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

/** Shows or hides the learning map; also in App's fallback for a conversation that cannot render. */
export function MapToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const { t } = useI18n();
  const label = open ? t("收起学习树", "Hide learning tree") : t("展开学习树", "Show learning tree");
  return <button className={`chat-icon-button ${open ? "is-active" : ""}`} onClick={onToggle} aria-label={label} title={label}><Icon name="tree" /></button>;
}

function CopyButton({ text, label, visibleLabel }: { text: string; label: string; visibleLabel?: string }) {
  const { t } = useI18n();
  const [state, setState] = useState<"idle" | "copying" | "copied" | "error">("idle");
  useEffect(() => {
    if (state !== "copied") return;
    const timer = window.setTimeout(() => setState("idle"), 2000);
    return () => window.clearTimeout(timer);
  }, [state]);
  const title = state === "copied" ? t("已复制", "Copied") : label;
  return <span className="chat-copy-control"><button type="button" className={`chat-copy-button ${state === "copied" ? "is-copied" : ""} ${visibleLabel ? "chat-copy-labeled" : ""}`} title={title} aria-label={title} disabled={state === "copying"} onClick={async () => {
    setState("copying");
    try { await copyText(text); setState("copied"); }
    catch { setState("error"); }
  }}><Icon name={state === "copied" ? "check" : "copy"} />{visibleLabel && <span>{state === "copied" ? t("已复制", "Copied") : visibleLabel}</span>}</button><span className="chat-sr-only" role="status">{state === "copied" ? t("已复制", "Copied") : ""}</span>{state === "error" && <span className="chat-copy-error" role="alert">{t("复制失败，请选中文字复制。", "Couldn't copy. Select the text to copy it.")}</span>}</span>;
}

/** DOM offsets intentionally refer to visible answer text, not Markdown source. */
function textRange(element: HTMLElement, start: number, end: number) {
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  let offset = 0;
  const range = document.createRange();
  let began = false;
  let current: Node | null;
  while ((current = walker.nextNode())) {
    const length = current.textContent?.length ?? 0;
    if (!began && start <= offset + length) {
      range.setStart(current, Math.max(0, start - offset));
      began = true;
    }
    if (began && end <= offset + length) {
      range.setEnd(current, Math.max(0, end - offset));
      return range;
    }
    offset += length;
  }
  return null;
}
function markSource(element: HTMLElement, anchor: Destination) {
  const passage = locatePassage(element.textContent ?? "", anchor);
  const range = passage ? textRange(element, passage.start, passage.end) : null;
  if (range) {
    const highlightApi = window as unknown as { Highlight?: new (...ranges: Range[]) => unknown; CSS?: { highlights?: Map<string, unknown> } };
    if (highlightApi.Highlight && highlightApi.CSS?.highlights) highlightApi.CSS.highlights.set("learning-source", new highlightApi.Highlight(range));
    else { const selection = window.getSelection(); selection?.removeAllRanges(); selection?.addRange(range); }
    const rect = range.getBoundingClientRect();
    const scroller = element.closest(".chat-scroll") as HTMLElement | null;
    if (scroller) scroller.scrollTop += rect.top - scroller.getBoundingClientRect().top - scroller.clientHeight * .35;
  } else element.scrollIntoView({ block: "center" });
  element.classList.add("chat-source-flash");
  window.setTimeout(() => element.classList.remove("chat-source-flash"), 3500);
}

export function ChatPane(props: Props) {
  const { locale, t } = useI18n();
  const translationRef = useRef(t);
  translationRef.current = t;
  const { thread, treeTitle, hasNode, loading = false, activeNodeId, activeTreeId, focusedSeed, live, liveReasoning, liveSteps, liveDiagnostics = null, liveConfirmation = null, onConfirmTool, liveSources = [], onOpenSource, topicIds = NO_IDS, mapNodeIds = NO_IDS, contextStatus, reconnecting = false, pendingQuestion, sendingImages, sendingDocuments, streaming, showStream, err, models, activeModelId, onModelChange, onOpenSettings, onAsk, onStop, onStopNode, onUnderstandingChange, onBranch, onNavigate, navigationAnchor, rightOpen, onToggleRight, onAddMock, mcpServers, onExport, onNew, learningPresets = null, learningPresetsFailed = false, topicPreset = null, onLearningPresetChange, onReloadPresets, sendingPreset = null } = props;
  const openSource = (id: number) => void onNavigate(id);
  const sourceView: SourceView = { activeTreeId, topicIds, nodeIds: mapNodeIds, canOpenTopics: !!onOpenSource };
  const nearestBranch = [...thread].reverse().find(node => node.seed_text && (node.source_node_id ?? node.parent_id) != null);
  const noteKey = `bl-note-draft-v2:${activeTreeId}:${nearestBranch?.node_id ?? "none"}`;
  const draftKey = draftKeyFor(activeTreeId, activeNodeId);
  const scrollKey = scrollKeyFor(activeTreeId, activeNodeId);
  const [draft, setDraft] = useState<Draft>(() => readDraft(draftKey));
  const [localError, setLocalError] = useState<string | null>(null);
  const [storageError, setStorageError] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [useSearch, setUseSearch] = useState(false);
  const [useFetch, setUseFetch] = useState(false);
  const [selectedMcp, setSelectedMcp] = useState<number[]>([]);
  const hasWebSearchMcp = hasManagedWebSearch(mcpServers);
  const selectedTools = selectedChatTools(mcpServers, { useSearch, useFetch, selectedMcp });
  useEffect(() => {
    if (!hasWebSearchMcp) return;
    // Hidden legacy choices must not revive if this MCP is later disabled.
    setUseSearch(false);
    setUseFetch(false);
  }, [hasWebSearchMcp]);
  const [selection, setSelection] = useState<SelectionCard | null>(null);
  // Touch screens dock selection actions away from the system menu and handles.
  const [touchScreen] = useState(() => window.matchMedia("(pointer: coarse)").matches);
  const selectionStateRef = useRef<SelectionCard["state"] | null>(null);
  selectionStateRef.current = selection?.state ?? null;
  const cardPressRef = useRef(0);
  const [branching, setBranching] = useState(false);
  const [retryingNodeId, setRetryingNodeId] = useState<number | null>(null);
  const [awayFromBottom, setAwayFromBottom] = useState(false);
  const [noteOpen, setNoteOpen] = useState(false);
  const [noteDraft, setNoteDraft] = useState("");
  const [noteSaving, setNoteSaving] = useState(false);
  const [noteError, setNoteError] = useState<string | null>(null);
  const [savedNotes, setSavedNotes] = useState<Record<string, string>>({});
  const [savingMarks, setSavingMarks] = useState<number[]>([]);
  const savingMarksRef = useRef(new Set<number>());
  const [returnTrip, setReturnTrip] = useState<ReturnTrip | null>(null);
  const returnCountRef = useRef(0);
  const [returnSummary, setReturnSummary] = useState<{ nonce: number; summary: ReturnSummary } | null>(null);
  const [, setUploadVersion] = useState(0);
  const activeNoteKeyRef = useRef(noteKey);
  activeNoteKeyRef.current = noteKey;
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const documentUploadsRef = useRef<DocumentUploadQueue | null>(null);
  const imageUploadsRef = useRef<ImageAttachmentQueue | null>(null);
  const toolMenuRef = useRef<HTMLDivElement>(null);
  const selectionRef = useRef<HTMLDivElement>(null);
  const draftRef = useRef(draft);
  const activeKeyRef = useRef(draftKey);
  const selectionRequestRef = useRef(0);
  const restoreKeyRef = useRef<string | null>(null);
  const restoredRef = useRef<string | null>(null);
  const anchorNonceRef = useRef<number | null>(null);
  const stickToBottom = useRef(true);
  const composingRef = useRef(false);
  const compEndAt = useRef(0);
  const sendingRef = useRef(false);
  const followupRef = useRef<{ sourceKey: string; targetKey: string } | null>(null);
  const branchFocusRef = useRef<{ sourceKey: string; targetKey: string } | null>(null);
  const uploadKeysRef = useRef(new Set<string>());
  activeKeyRef.current = draftKey;
  if (!documentUploadsRef.current) documentUploadsRef.current = new DocumentUploadQueue({
    documents: key => readDraft(key).documents ?? [],
    upload: api.uploadDocument,
    complete: (key, document) => {
      const current = readDraft(key);
      commitDraft({ ...current, documents: documentMetadata([...(current.documents ?? []), document]) });
    },
    changed: () => setUploadVersion(version => version + 1),
  });
  const documentUploads = documentUploadsRef.current;
  if (!imageUploadsRef.current) imageUploadsRef.current = new ImageAttachmentQueue({
    images: key => readDraft(key).images,
    read: readImage,
    complete: (key, image) => {
      const current = readDraft(key);
      commitDraft({ ...current, images: [...current.images, image] });
    },
    failed: (key, file) => {
      if (activeKeyRef.current === key) setLocalError(translationRef.current("无法读取图片「{name}」，请重新添加。", "Could not read image “{name}”. Add it again.", { name: file.name }));
    },
    changed: () => setUploadVersion(version => version + 1),
  });
  const imageUploads = imageUploadsRef.current;

  useEffect(() => onWorkspaceDeletion(scope => {
    deletedWorkspaces.push(scope);
    for (const key of draftCache.keys()) if (matchesDeletedWorkspace(key, scope)) draftCache.delete(key);
    for (const key of noteDraftCache.keys()) if (matchesDeletedWorkspace(key, scope)) noteDraftCache.delete(key);
    for (const key of uploadKeysRef.current) {
      if (!matchesDeletedWorkspace(key, scope)) continue;
      documentUploads.cancel(key);
      imageUploads.cancel(key);
      uploadKeysRef.current.delete(key);
    }
    setSavedNotes(current => Object.fromEntries(Object.entries(current).filter(([key]) => !matchesDeletedWorkspace(key, scope))));
    if (matchesDeletedWorkspace(draftRef.current.key, scope)) {
      const empty = { key: draftRef.current.key, text: "", images: [] };
      draftRef.current = empty;
      setDraft(empty);
      setSelection(null);
      selectionRequestRef.current++;
      followupRef.current = null;
    }
    if (matchesDeletedWorkspace(activeNoteKeyRef.current, scope)) { setNoteDraft(""); setNoteOpen(false); }
  }), []);

  useEffect(() => {
    let savedDraft: string | null = null;
    try { savedDraft = localStorage.getItem(noteKey); } catch { /* Keep the in-memory draft available. */ }
    setNoteDraft(noteDraftCache.get(noteKey) ?? savedDraft ?? savedNotes[noteKey] ?? nearestBranch?.learning_note ?? "");
    setNoteOpen(false);
    setNoteError(null);
  }, [noteKey, nearestBranch?.learning_note]);

  // A return from a branch lands on its source node; going anywhere else ends it.
  useEffect(() => {
    setReturnTrip(trip => followReturnTrip(trip, { treeId: activeTreeId, nodeId: activeNodeId }));
  }, [activeTreeId, activeNodeId]);

  // One local read per return. A failed read shows no card: the navigation itself already happened.
  const returnNonce = returnTrip?.nonce;
  const returnFrom = returnTrip?.fromNodeId;
  useEffect(() => {
    setReturnSummary(null);
    if (returnNonce === undefined || returnFrom === undefined) return;
    const controller = new AbortController();
    api.returnSummary(returnFrom, controller.signal).then(
      summary => { if (!controller.signal.aborted) setReturnSummary({ nonce: returnNonce, summary }); },
      () => { /* Optional card; nothing to report. */ },
    );
    return () => controller.abort();
  }, [returnNonce, returnFrom]);

  useLayoutEffect(() => {
    const followup = followupRef.current;
    if (followup?.targetKey === draftKey) {
      const moved = moveFollowupDraft(readDraft(followup.sourceKey), readDraft(draftKey));
      if (moved) { persistDraft(moved.source); persistDraft(moved.target); }
      followupRef.current = null;
    } else if (followup && followup.sourceKey !== draftKey) followupRef.current = null;
    const next = readDraft(draftKey);
    draftRef.current = next;
    setDraft(next);
    setLocalError(null);
    setSelection(null);
    selectionRequestRef.current++;
    setToolsOpen(false);
    restoreKeyRef.current = scrollKey;
    restoredRef.current = null;
    stickToBottom.current = false;
  }, [draftKey, scrollKey]);

  useLayoutEffect(() => {
    const pending = branchFocusRef.current;
    if (!pending) return;
    if (draftKey !== pending.sourceKey && draftKey !== pending.targetKey) {
      branchFocusRef.current = null;
      return;
    }
    if (draftKey === pending.targetKey && !loading && textareaRef.current) {
      const input = textareaRef.current;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      branchFocusRef.current = null;
    }
  }, [draftKey, loading]);

  useEffect(() => {
    const flush = () => { try { writeDraft(draftRef.current); } catch { /* Existing records stay untouched. */ } };
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, []);

  useEffect(() => {
    if (loading) restoreKeyRef.current = scrollKey;
  }, [loading, scrollKey]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (thread[thread.length - 1]?.node_id !== activeNodeId) { restoreKeyRef.current = scrollKey; return; }
    if (restoredRef.current !== scrollKey || restoreKeyRef.current === scrollKey) {
      let saved: string | null = null;
      try { saved = localStorage.getItem(scrollKey); } catch { /* Default to latest. */ }
      el.scrollTop = saved == null ? el.scrollHeight : Number(saved) || 0;
      stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
      setAwayFromBottom(!stickToBottom.current);
      restoredRef.current = scrollKey;
      restoreKeyRef.current = null;
    }
    if (navigationAnchor && navigationAnchor.nodeId === activeNodeId && navigationAnchor.nonce !== anchorNonceRef.current) {
      const selector = navigationAnchor.messageId ? `[data-message-id="${navigationAnchor.messageId}"]` : `[data-answer-node="${activeNodeId}"]`;
      // A passage this conversation does not show falls back to the node's turn, so the
      // anchor is used once instead of firing on a later, unrelated reload.
      const answer = el.querySelector<HTMLElement>(selector) ?? el.querySelector<HTMLElement>(`[data-turn-node="${activeNodeId}"]`);
      if (answer) {
        let parent = answer.parentElement;
        while (parent && parent !== el) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement; }
        markSource(answer, navigationAnchor);
        anchorNonceRef.current = navigationAnchor.nonce;
        stickToBottom.current = false;
        setAwayFromBottom(el.scrollHeight - el.scrollTop - el.clientHeight > 100);
      }
    }
  }, [thread, activeNodeId, scrollKey, navigationAnchor]);

  useLayoutEffect(() => {
    if (showStream && stickToBottom.current && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [live, liveReasoning, liveSteps, showStream]);

  // An answer from another window or device grows through background reloads.
  const answeringElsewhere = thread.some(node => node.status === "pending");
  useLayoutEffect(() => {
    if (answeringElsewhere && !showStream && stickToBottom.current && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [thread, answeringElsewhere, showStream]);

  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (el) { el.style.height = "auto"; el.style.height = `${Math.min(160, el.scrollHeight)}px`; }
  }, [draft.text]);

  useEffect(() => {
    const onPointer = (event: PointerEvent) => {
      if (toolMenuRef.current && !toolMenuRef.current.contains(event.target as Node)) setToolsOpen(false);
      if (selectionRef.current && !selectionRef.current.contains(event.target as Node) && !(event.target as Element).closest?.("[data-answer-node]")) { setSelection(null); selectionRequestRef.current++; }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setSelection(null); setToolsOpen(false); selectionRequestRef.current++; }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("pointerdown", onPointer); document.removeEventListener("keydown", onKey); };
  }, []);

  // iOS hands a long-press selection to the system: no pointerup follows, and
  // dragging the selection handles fires no page events. Read it once it settles.
  const captureRef = useRef(captureSelection);
  captureRef.current = captureSelection;
  useEffect(() => {
    if (!touchScreen) return;
    let timer: number | undefined;
    const settle = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        // Tapping an open card can clear the text selection: keep an explanation, and
        // keep the actions briefly after a press. A closed card follows every selection.
        const state = selectionStateRef.current;
        if (state !== null && (state !== "selected" || Date.now() - cardPressRef.current < 800)) return;
        captureRef.current();
      }, 350);
    };
    document.addEventListener("selectionchange", settle);
    return () => { window.clearTimeout(timer); document.removeEventListener("selectionchange", settle); };
  }, [touchScreen]);

  function persistDraft(next: Draft) {
    try { writeDraft(next); setStorageError(false); }
    catch { setStorageError(true); }
  }
  function commitDraft(next: Draft) {
    if (deletedDraft(next.key)) return;
    // Update the ref and cache together. A later node-change effect must never re-save an old render.
    persistDraft(next);
    if (next.key === activeKeyRef.current) { draftRef.current = next; setDraft(next); }
  }
  function updateDraft(change: Partial<Draft>) {
    const current = draftRef.current.key === draftKey ? draftRef.current : readDraft(draftKey);
    commitDraft({ ...current, ...change });
  }
  function addImage(file: File): string | null {
    if (deletedDraft(draftKey)) return null;
    uploadKeysRef.current.add(draftKey);
    const error = imageUploads.enqueue(draftKey, file);
    return error === "limit" ? t("每次提问最多附加 4 张图片，请先移除一张。", "Attach up to 4 images per question. Remove one first.")
      : error === "size" ? t("每张图片不能超过 4 MB。", "Each image must be 4 MB or smaller.")
      : error === "empty" ? t("这个图片文件是空的，请重新选择。", "This image file is empty. Choose another one.") : null;
  }
  function addDocument(file: File): string | null {
    if (activeNodeId == null || loading || deletedDraft(draftKey)) return null;
    uploadKeysRef.current.add(draftKey);
    const error = documentUploads.enqueue(draftKey, activeNodeId, file);
    return error === "limit" ? t("每次提问最多附加 4 份文档，请先移除一份。", "Attach up to 4 documents per question. Remove one first.")
      : error === "size" ? t("每份文档不能超过 10 MB。", "Each document must be 10 MB or smaller.")
      : error === "empty" ? t("这个文件是空的，请选择有内容的文档。", "This file is empty. Choose a document with content.")
      : error === "type" ? t("支持图片、PDF、Word（.docx）、TXT、Markdown、CSV、TSV、JSON 和 LOG。旧版 .doc 请另存为 .docx。", "Use images, PDF, Word (.docx), TXT, Markdown, CSV, TSV, JSON or LOG. Save older .doc files as .docx first.") : null;
  }
  function addAttachments(files: File[]) {
    if (activeNodeId == null || loading) return;
    const errors = files.map(file => attachmentKind(file) === "image" ? addImage(file) : addDocument(file)).filter((error): error is string => error !== null);
    setLocalError([...new Set(errors)].join(" ") || null);
  }
  function captureSelection() {
    const selected = window.getSelection();
    if (!selected || selected.isCollapsed || !selected.rangeCount) { setSelection(null); selectionRequestRef.current++; return; }
    const range = selected.getRangeAt(0);
    const asElement = (node: Node) => node.nodeType === Node.ELEMENT_NODE ? node as Element : node.parentElement;
    const answer = asElement(range.startContainer)?.closest<HTMLElement>("[data-answer-node]");
    if (!answer || !answer.contains(range.endContainer) || asElement(range.endContainer)?.closest("[data-answer-node]") !== answer) { setSelection(null); return; }
    const value = range.toString();
    const text = value.trim();
    if (!text || text.length > 3000) { setSelection(null); return; }
    const prefix = document.createRange();
    prefix.selectNodeContents(answer);
    prefix.setEnd(range.startContainer, range.startOffset);
    const start = prefix.toString().length + value.indexOf(text);
    const rect = range.getBoundingClientRect();
    const width = Math.min(352, window.innerWidth - 24);
    selectionRequestRef.current++;
    setSelection({ text, nodeId: Number(answer.dataset.answerNode), messageId: answer.dataset.messageId ? Number(answer.dataset.messageId) : undefined, start, end: start + text.length, x: Math.max(12, Math.min(window.innerWidth - width - 12, rect.left)), y: Math.max(64, Math.min(window.innerHeight - 220, rect.bottom + 8)), state: "selected" });
  }
  async function explainSelection() {
    if (!selection || !activeModelId) return;
    const current = { ...selection, y: Math.max(12, Math.min(selection.y, window.innerHeight - Math.min(420, window.innerHeight - 24) - 12)) };
    const request = ++selectionRequestRef.current;
    setSelection({ ...current, state: "loading", error: undefined });
    try {
      const result = await api.explain(current.nodeId, current.text, activeModelId, current.messageId);
      if (request === selectionRequestRef.current) setSelection({ ...current, state: "ready", explanation: result.explanation });
    } catch (error) {
      if (request === selectionRequestRef.current) setSelection({ ...current, state: "error", error: error instanceof Error ? error.message : t("暂时无法解释，请重试。", "Could not explain this selection. Try again.") });
    }
  }
  async function branchSelection() {
    if (!selection || branching || streaming) return;
    const selected = selection;
    const selectionRequest = selectionRequestRef.current;
    const sourceKey = draftKey;
    const treeId = activeTreeId;
    let targetKey: string | null = null;
    setBranching(true);
    try {
      await onBranch(selected.text, selected.nodeId, { source_message_id: selected.messageId, source_start: selected.start, source_end: selected.end }, nodeId => {
        targetKey = draftKeyFor(treeId, nodeId);
        // Seed the new node before navigation reads its draft; keep the source draft intact.
        const existing = readDraft(targetKey);
        if (isEmptyDraft(existing)) commitDraft({ ...existing, text: selected.text });
        if (activeKeyRef.current === sourceKey) branchFocusRef.current = { sourceKey, targetKey };
      });
      if (activeKeyRef.current === targetKey) {
        setSelection(null);
        window.getSelection()?.removeAllRanges();
      }
    } catch (error) {
      if (activeKeyRef.current === sourceKey && selectionRequestRef.current === selectionRequest)
        setSelection(current => current ? { ...current, state: "error", error: error instanceof Error ? error.message : t("分支创建失败，请重试。", "Could not create the branch. Try again.") } : null);
    }
    finally { setBranching(false); }
  }
  async function branchAnswer(node: ThreadNode) {
    if (branching || streaming || loading) return;
    const answer = scrollRef.current?.querySelector<HTMLElement>(`[data-message-id="${node.answer_message_id}"]`)
      ?? scrollRef.current?.querySelector<HTMLElement>(`[data-answer-node="${node.node_id}"]`);
    const content = answer?.textContent ?? node.answer ?? "";
    const seed = Array.from(content.trim()).slice(0, 120).join("");
    if (!seed) return;
    const start = content.indexOf(seed);
    setBranching(true);
    setLocalError(null);
    try {
      await onBranch(seed, node.node_id, { source_message_id: node.answer_message_id ?? undefined,
        ...(start >= 0 && node.answer_message_id != null ? { source_start: start, source_end: start + seed.length } : {}) });
      textareaRef.current?.focus();
    } catch (error) { setLocalError(error instanceof Error ? error.message : t("分支创建失败，请重试。", "Could not create the branch. Try again.")); }
    finally { setBranching(false); }
  }

  function editNode(node: ThreadNode) {
    if (documentUploads.pending(draftKey) || imageUploads.count(draftKey)) return;
    updateDraft({ text: node.question ?? "", images: node.images ?? [], documents: node.documents ?? [], revisionId: node.node_id, revisionMessageId: node.question_message_id ?? undefined, previous: draft.previous ?? { text: draft.text, images: draft.images, documents: draft.documents } });
    textareaRef.current?.focus();
  }
  function cancelRevision() {
    updateDraft({ text: draft.previous?.text ?? "", images: draft.previous?.images ?? [], documents: draft.previous?.documents ?? [], revisionId: undefined, revisionMessageId: undefined, previous: undefined });
  }
  async function send(retry?: ThreadNode) {
    const submitted = { ...draftRef.current };
    let question = retry ? retry.question ?? "" : submitted.text.trim();
    const images = retry ? retry.images ?? [] : submitted.images;
    const documents = retry ? retry.documents ?? [] : submitted.documents ?? [];
    if (!question && documents.length) question = t("请概括所附文档的主要内容，并注明文件名及页码或段落来源。", "Summarize the attached documents and cite file names and page or paragraph sources.");
    if ((!question && !images.length && !documents.length) || sendingRef.current || streaming || loading || focusBusy || !activeModelId || documentUploads.list(submitted.key).length > 0 || imageUploads.count(submitted.key)) return;
    const tools = selectedTools;
    sendingRef.current = true;
    let accepted = false;
    setLocalError(null);
    setReturnTrip(null);  // Asking a question ends the return recap.
    if (retry) setRetryingNodeId(retry.node_id);
    stickToBottom.current = true;
    setAwayFromBottom(false);
    try {
      await onAsk({ question, images: images.length ? images : undefined, documents,
        document_ids: retry ? undefined : documents.map(document => document.id), tools: tools.length ? tools : undefined,
        mode: retry ? "retry" : submitted.revisionId ? "revise" : "continue", nodeId: retry?.node_id ?? submitted.revisionId,
        question_message_id: retry?.question_message_id ?? submitted.revisionMessageId,
        onAccepted: nodeId => {
          if (accepted || retry) return;
          accepted = true;
          const current = readDraft(submitted.key);
          commitDraft(clearAcceptedDraft(current, submitted));
          const targetKey = draftKeyFor(activeTreeId, nodeId);
          if (activeKeyRef.current === submitted.key && targetKey !== submitted.key)
            followupRef.current = { sourceKey: submitted.key, targetKey };
        },
      });
    } catch (error) {
      if (activeKeyRef.current === submitted.key)
        setLocalError(error instanceof Error ? error.message : accepted ? t("回答未完成，可在聊天记录中重试。", "The response is incomplete. You can retry it in the conversation.") : t("发送失败，草稿已保留。", "Could not send. Your draft has been kept."));
    } finally { sendingRef.current = false; setRetryingNodeId(null); }
  }

  const branchSource = nearestBranch ? nearestBranch.source_node_id ?? nearestBranch.parent_id : null;
  const branchIndex = nearestBranch ? thread.indexOf(nearestBranch) : thread.length;
  const lastAnswer = thread.slice(branchIndex).reverse().find(node => node.answer)?.answer;
  const personalNote = savedNotes[noteKey] ?? nearestBranch?.learning_note ?? "";
  const learningNote = personalNote || (lastAnswer ? Array.from(lastAnswer.replace(/[#*`>]/g, "")).slice(0, 400).join("") : "");
  function updateNote(value: string) {
    if (deletedDraft(noteKey)) return;
    setNoteDraft(value);
    noteDraftCache.set(noteKey, value);
    try { localStorage.setItem(noteKey, value); }
    catch { setNoteError(t("这段草稿暂时保留在当前页面，请保存后再关闭。", "This draft is only available on this page. Save it before closing.")); }
  }
  async function savePersonalNote() {
    if (!nearestBranch || noteSaving) return;
    const key = noteKey;
    const content = noteDraft.trim();
    setNoteSaving(true);
    setNoteError(null);
    try {
      await api.saveNote(nearestBranch.node_id, content);
      if (deletedDraft(key)) return;
      setSavedNotes(current => ({ ...current, [key]: content }));
      noteDraftCache.delete(key);
      try { localStorage.removeItem(key); } catch { /* The durable server copy is already saved. */ }
      if (activeNoteKeyRef.current === key) setNoteOpen(false);
    } catch (error) {
      if (activeNoteKeyRef.current === key) setNoteError(error instanceof Error ? error.message : t("保存失败，理解草稿已保留。", "Could not save. Your reflection draft has been kept."));
    } finally { setNoteSaving(false); }
  }
  async function markUnderstanding(nodeId: number, value: Understanding | null) {
    if (!onUnderstandingChange || savingMarksRef.current.has(nodeId)) return;
    const key = draftKey;
    savingMarksRef.current.add(nodeId);
    setSavingMarks([...savingMarksRef.current]);
    try { await onUnderstandingChange(nodeId, value); }
    catch { if (activeKeyRef.current === key) setLocalError(t("理解标记没有保存，已恢复原来的标记。", "Your understanding mark was not saved; the previous mark is back.")); }
    finally {
      savingMarksRef.current.delete(nodeId);
      setSavingMarks([...savingMarksRef.current]);
    }
  }
  async function chooseLearningStyle(id: string | null) {
    if (!onLearningPresetChange) return;
    const key = draftKey;
    const notSaved = t("学习方式没有保存，已恢复原来的选择。", "The learning style was not saved; the previous choice is back.");
    // A new choice replaces the report about an earlier one that failed, and no other message.
    setLocalError(current => current === notSaved ? null : current);
    try { await onLearningPresetChange(id); }
    catch { if (activeKeyRef.current === key) setLocalError(notSaved); }
  }
  async function returnToSource(withNote = false) {
    if (!nearestBranch || branchSource == null) return;
    if (withNote && learningNote) {
      const sourceKey = draftKeyFor(activeTreeId, branchSource);
      const prior = readDraft(sourceKey);
      const next = { ...prior, text: [prior.text, t("关于「{quote}」，{intro}：\n{note}\n\n请接着解释原来的问题。", "About “{quote}”, {intro}:\n{note}\n\nPlease continue explaining the original question.", { quote: nearestBranch.seed_text ?? "", intro: personalNote ? t("我的理解是", "my understanding is") : t("这是一份整理草稿（请帮我检查）", "here is a draft summary (please check it)"), note: learningNote })].filter(Boolean).join("\n\n") };
      try { writeDraft(next); } catch { setStorageError(true); return; }
    }
    // Remember the branch being left; its recap card appears once the source is shown.
    setReturnTrip({ nonce: ++returnCountRef.current, treeId: activeTreeId, fromNodeId: activeNodeId ?? nearestBranch.node_id, branchNodeId: nearestBranch.node_id, sourceNodeId: branchSource, arrived: false });
    await onNavigate(branchSource, { messageId: nearestBranch.source_message_id ?? undefined, start: nearestBranch.source_start ?? undefined, end: nearestBranch.source_end ?? undefined, text: nearestBranch.seed_text ?? undefined });
    if (withNote) textareaRef.current?.focus();
  }
  const noModel = !models.length || !activeModelId;
  const toolCount = selectedTools.length;
  const displayDraft = draft.key === draftKey ? draft : readDraft(draftKey);
  const draftUploads = documentUploads.list(draftKey);
  const pendingImages = imageUploads.count(draftKey);
  const attachmentsBusy = draftUploads.length > 0 || pendingImages > 0;
  const visibleThread = thread.filter(node => node.question !== null && !(showStream && (node.node_id === retryingNodeId || (node.status === "pending" && node.node_id === activeNodeId))));
  const focusBusy = !streaming && thread[thread.length - 1]?.node_id === activeNodeId && thread[thread.length - 1]?.status === "pending";
  // One mark, for the open question, in the source bar area: inside a branch's source bar,
  // else in a bar of its own.
  const markFor = onUnderstandingChange ? markTarget(thread, activeNodeId, showStream ? retryingNodeId : null) : null;
  const markSaving = !!markFor && savingMarks.includes(markFor.row.node_id);
  const marks = markFor ? <UnderstandingControl value={markFor.row.understanding ?? null} disabled={markFor.answering || markSaving} saving={markSaving} onChange={value => void markUnderstanding(markFor.row.node_id, value)} /> : null;
  const recap = returnTrip?.arrived && returnSummary?.nonce === returnTrip.nonce && hasReturnContent(returnSummary.summary) ? returnSummary.summary : null;

  return <main className="center chat-pane">
    <header className="chat-header">
      <div className="chat-heading"><span className="chat-heading-dot" /><h1>{hasNode ? treeTitle : t("学习空间", "Learning space")}</h1>{nearestBranch && <span className="chat-mode-label">{t("分支", "Branch")}</span>}</div>
      <div className="chat-header-actions">{onExport && hasNode && <button className="chat-icon-button" onClick={onExport} aria-label={t("导出学习记录", "Export learning history")} title={t("导出学习记录", "Export learning history")}><Icon name="export" /></button>}<MapToggle open={rightOpen} onToggle={onToggleRight} /></div>
    </header>
    {nearestBranch && <div className={`chat-source-bar${marks ? " has-marks" : ""}`}><span title={nearestBranch.seed_text ?? ""}>↳ {nearestBranch.seed_text}</span><button onClick={() => void returnToSource()}>{t("返回出处 ↗", "Back to source ↗")}</button><button onClick={() => setNoteOpen(open => !open)} aria-expanded={noteOpen}>{t("我的理解", "My reflection")}{personalNote ? " ·" : ""}</button>{learningNote && <button className="chat-note-return" onClick={() => void returnToSource(true)} title={t("把这段理解放回原问题的草稿，可继续编辑", "Add this reflection to the original question’s draft to continue editing")}>{t("带着理解继续", "Continue with reflection")}</button>}{marks}</div>}
    {!nearestBranch && marks && <div className="chat-mark-bar">{marks}</div>}
    {nearestBranch && noteOpen && <div className="chat-note-panel"><label htmlFor="personal-learning-note">{t("我的理解", "My reflection")}</label><textarea id="personal-learning-note" disabled={noteSaving} value={noteDraft} onChange={event => updateNote(event.target.value)} rows={3} maxLength={4000} placeholder={t("用自己的话，记下这次弄懂的事…", "Write what you learned in your own words…")} /><div className="chat-note-actions">{noteError && <span role="alert">{localizeError(noteError, locale)}</span>}<button onClick={() => setNoteOpen(false)}>{t("收起", "Collapse")}</button><button className="chat-explain-button" onClick={() => void savePersonalNote()} disabled={noteSaving || noteDraft.trim() === personalNote.trim()}>{noteSaving ? t("保存中…", "Saving…") : t("保存理解", "Save reflection")}</button></div></div>}
    {(err || localError || storageError) && <div className="chat-error" role="alert">{localizeError(err || localError || t("浏览器存储空间不足，当前草稿尚未保存。可移除附图后重试。", "Browser storage is full. This draft has not been saved. Remove attachments and try again."), locale)}</div>}
    {!hasNode ? <div className="chat-empty"><div className="chat-empty-mark"><Icon name="tree" /></div><h2>{t("从一个好奇开始", "Start with curiosity")}</h2><p>{t("把不懂的地方，慢慢学明白。", "Follow your questions, one idea at a time.")}</p>{onNew && <button className="chat-primary" onClick={onNew}>{t("新建学习树", "New learning tree")}</button>}</div> : <>
      <div className={`chat-reading${visibleThread.length > 1 ? ' has-turn-nav' : ''}`}>
        <div className="chat-scroll" ref={scrollRef} onScroll={() => {
          const el = scrollRef.current;
          if (!el) return;
          stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
          setAwayFromBottom(!stickToBottom.current);
          if (!deletedDraft(scrollKey) && restoredRef.current === scrollKey && thread[thread.length - 1]?.node_id === activeNodeId) { try { localStorage.setItem(scrollKey, String(el.scrollTop)); } catch { /* Scroll restoration is optional. */ } }
        }}>
          <div className="chat-messages" onPointerUp={captureSelection} onKeyUp={(event) => { if (event.shiftKey) captureSelection(); }}>
            {!loading && !visibleThread.length && !showStream && <div className="chat-welcome"><span className="chat-eyebrow">{focusedSeed ? t("从这里展开", "Explore from here") : t("新的起点", "A fresh start")}</span><h2>{focusedSeed ? `「${focusedSeed}」` : treeTitle}</h2><p>{t("想先弄懂什么？", "What would you like to understand first?")}</p></div>}
            {visibleThread.map((node, index) => <section className="chat-turn" data-turn-node={node.node_id} data-turn-key={turnKey(node, index)} key={turnKey(node, index)}>
              <div className="chat-question"><LearningStyleTag id={node.learning_preset} presets={learningPresets} /><ImageStrip images={node.images ?? []} /><DocumentCards documents={node.documents ?? []} /><div data-message-id={node.question_message_id ?? undefined}>{node.question}</div><div className="chat-question-actions"><CopyButton text={node.question ?? ""} label={t("复制问题", "Copy question")} /><button onClick={() => editNode(node)} disabled={streaming || attachmentsBusy || node.status === "pending"} title={t("编辑后生成新版本，保留原有问答与分支", "Create a new version while keeping the original conversation and branches")}>{t("编辑", "Edit")}</button></div></div>
              {node.answer != null && <div className="chat-answer"><div className="chat-answer-label"><span className="chat-answer-dot" />{node.answered_by ?? "AI"}{node.status === "error" || node.status === "interrupted" ? <span>{t("· 未完成", "· Incomplete")}</span> : null}</div><ReasoningBlock text={node.reasoning ?? ""} /><ToolSteps steps={node.steps ?? []} mcpServers={mcpServers} nodeId={node.node_id} /><div className="chat-markdown" data-answer-node={node.node_id} data-message-id={node.answer_message_id ?? undefined} tabIndex={0}><Markdown text={node.answer} /></div><SourcesLine sources={node.sources ?? []} view={sourceView} onNavigate={openSource} onOpenSource={onOpenSource} />{node.answer.trim() && <div className="chat-answer-actions"><CopyButton text={node.answer} label={t("复制回答", "Copy answer")} /><button onClick={() => void branchAnswer(node)} disabled={branching || streaming || loading} title={t("从这条回答展开一个新的问题", "Explore a new question from this answer")}><Icon name="tree" />{t("新分支", "New branch")}</button></div>}<DiagnosticsBlock data={node.diagnostics} /></div>}
              {node.status === "pending" && <div className="chat-retry chat-remote-answer" role="status"><span className="chat-answer-dot is-loading" /><span>{t("正在回答，完成前会自动更新", "Responding. This updates automatically until it finishes.")}</span>{onStopNode && <button onClick={() => onStopNode(node.node_id)}>{t("停止", "Stop")}</button>}</div>}
              {!!node.attempts?.length && <details className="chat-reasoning chat-prior-attempts"><summary>{t("之前的未完成回答（{count}）", "Previous incomplete responses ({count})", { count: node.attempts.length })}</summary>{node.attempts.map(attempt => <div className="chat-markdown" data-answer-node={node.node_id} data-message-id={attempt.message_id} key={attempt.message_id}><Markdown text={attempt.content || t("未收到内容", "No content received")} /></div>)}</details>}
              {(node.status === "error" || node.status === "interrupted") && <div className="chat-retry"><span>{node.status === "interrupted" ? t("回答已停止", "Response stopped") : t("这次回答未完成", "This response is incomplete")}</span><button onClick={() => void send(node)} disabled={streaming || noModel}>{t("重试", "Retry")}</button>{node.error && <details><summary>{t("详情", "Details")}</summary><p>{localizeError(node.error, locale)}</p></details>}</div>}
            </section>)}
            {showStream && <section className="chat-turn chat-live"><div className="chat-question"><LearningStyleTag id={sendingPreset} presets={learningPresets} /><ImageStrip images={sendingImages} /><DocumentCards documents={sendingDocuments} /><div>{pendingQuestion}</div></div><div className="chat-answer"><div className="chat-answer-label" role="status"><span className="chat-answer-dot is-loading" />{reconnecting ? t("连接中断，正在重新连接…", "Connection lost. Reconnecting…") : liveConfirmation ? t("等待你确认", "Waiting for your approval") : contextStatus === "summarizing" && !live ? t("正在整理上下文…", "Organizing context…") : t("正在回答", "Responding")}</div><ReasoningBlock text={liveReasoning} /><ToolSteps steps={liveSteps} mcpServers={mcpServers} />{liveConfirmation && <ToolConfirmCard request={liveConfirmation} mcpServers={mcpServers} onDecide={onConfirmTool} />}<div className="chat-markdown" aria-live="polite" aria-busy="true">{live ? <Markdown text={live} /> : <span className="chat-typing">•••</span>}</div><SourcesLine sources={liveSources} view={sourceView} onNavigate={openSource} onOpenSource={onOpenSource} /><DiagnosticsBlock data={liveDiagnostics} /></div></section>}
          </div>
        </div>
        <ChatTurnNavigator turns={visibleThread} scrollRef={scrollRef} scopeKey={scrollKey} onJump={() => { stickToBottom.current = false; setAwayFromBottom(true); setSelection(null); selectionRequestRef.current++; }} />
        {awayFromBottom && <button className="chat-latest" onClick={() => { if (scrollRef.current) scrollRef.current.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }); stickToBottom.current = true; }}>{t("↓ 最新", "↓ Latest")}</button>}
      </div>
      <div className="chat-compose-area">
        {recap && <ReturnCard summary={recap} onDismiss={() => setReturnTrip(null)} />}
        {noModel && <div className="chat-model-empty"><span>{t("连接模型，开始学习", "Connect a model to start learning")}</span><button onClick={onOpenSettings}>{t("设置模型", "Model settings")}</button><button onClick={onAddMock}>{t("体验演示", "Try demo")}</button></div>}
        <div className="chat-compose-box">
          {displayDraft.revisionId && <div className="chat-revision"><span>{t("编辑为新版本", "Edit as a new version")} <small>{t("原记录会保留", "The original stays saved")}</small></span><button onClick={cancelRevision} disabled={streaming || attachmentsBusy}>{t("取消", "Cancel")}</button></div>}
          {displayDraft.images.length > 0 && <div className="chat-pending-images">{displayDraft.images.map((src, index) => <div key={index}><img src={src} alt={t("待发送附图 {number}", "Pending attachment {number}", { number: index + 1 })} /><button onClick={() => updateDraft({ images: displayDraft.images.filter((_, i) => i !== index) })} aria-label={t("移除附图 {number}", "Remove attachment {number}", { number: index + 1 })}>×</button></div>)}</div>}
          {pendingImages > 0 && <div className="chat-document-hint" role="status">{t("正在读取图片…", "Reading images…")}</div>}
          <DocumentCards documents={displayDraft.documents ?? []} onRemove={id => updateDraft({ documents: (displayDraft.documents ?? []).filter(document => document.id !== id) })} />
          {draftUploads.map(upload => <div className="chat-document-upload" key={upload.id}>
            <div><span className="chat-document-name">{upload.file.name}</span><span role={upload.status === "error" ? "alert" : "status"}>{upload.status === "error" ? localizeError(upload.error ?? "", locale) : upload.status === "queued" ? t("等待上传…", "Queued…") : upload.progress >= 100 ? t("正在提取文字…", "Extracting text…") : t("上传中 {percent}%", "Uploading {percent}%", { percent: upload.progress })}</span></div>
            {upload.status !== "error" && <progress value={upload.progress} max={100} aria-label={t("文档上传进度", "Document upload progress")} />}
            <div className="chat-document-actions">{upload.status === "error" && <button onClick={() => { documentUploads.remove(draftKey, upload.id); addAttachments([upload.file]); }}>{t("重试上传", "Retry upload")}</button>}<button onClick={() => documentUploads.remove(draftKey, upload.id)} aria-label={t("移除文档 {name}", "Remove document {name}", { name: upload.file.name })}>{t("移除", "Remove")}</button></div>
          </div>)}
          {((displayDraft.documents?.length ?? 0) > 0 || draftUploads.length > 0) && <div className="chat-document-hint">{t("在本机提取文字；发送问题时将相关内容提供给所选模型。扫描件需先 OCR。", "Text is extracted locally; relevant content is sent to your selected model with the question. Scans need OCR first.")}</div>}
          <textarea ref={textareaRef} disabled={loading} aria-label={t("输入问题", "Enter a question")} rows={1} value={displayDraft.text} onChange={event => updateDraft({ text: event.target.value })} onPaste={event => { const files = Array.from(event.clipboardData.items).filter(item => item.type.startsWith("image/")); if (files.length) { event.preventDefault(); addAttachments(files.map(item => item.getAsFile()).filter((file): file is File => file !== null)); } }} onCompositionStart={() => { composingRef.current = true; }} onCompositionEnd={() => { composingRef.current = false; compEndAt.current = Date.now(); }} onKeyDown={event => { if (event.key !== "Enter" || event.shiftKey || composingRef.current || event.nativeEvent.isComposing || event.keyCode === 229 || Date.now() - compEndAt.current < 120) return; event.preventDefault(); void send(); }} />
          <div className="chat-compose-controls"><div className="chat-compose-left">
            <button className="chat-icon-button" onClick={() => fileRef.current?.click()} aria-label={t("添加附件", "Add attachments")} title={t("添加图片或文档（图片最多 4 张，每张 4 MB；文档最多 4 份，每份 10 MB）", "Add images or documents (up to 4 images, 4 MB each; up to 4 documents, 10 MB each)")} disabled={loading || (displayDraft.images.length + pendingImages >= MAX_IMAGES && (displayDraft.documents?.length ?? 0) + draftUploads.filter(upload => upload.status !== "error").length >= 4)}><Icon name="attachment" /></button><input hidden ref={fileRef} data-testid="document-upload-input" type="file" accept={ATTACHMENT_ACCEPT} multiple onChange={event => { addAttachments(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
            <div className="chat-tool-menu" ref={toolMenuRef}><button className={`chat-icon-button ${toolCount ? "is-active" : ""}`} onClick={() => setToolsOpen(!toolsOpen)} aria-label={t("选择工具", "Choose tools")} aria-expanded={toolsOpen} title={t("工具", "Tools")}><Icon name="tools" />{toolCount > 0 && <span className="chat-tool-count">{toolCount}</span>}</button>{toolsOpen && <div className="chat-tool-dropdown"><span className="chat-menu-label">{t("这次提问使用", "Tools for this question")}</span>{!hasWebSearchMcp && <><label><input type="checkbox" checked={useSearch} onChange={event => setUseSearch(event.target.checked)} />{t("联网搜索", "Web search")}</label><label><input type="checkbox" checked={useFetch} onChange={event => setUseFetch(event.target.checked)} />{t("读取网页", "Read webpage")}</label></>}{mcpServers.filter(server => server.enabled).map(server => <label key={server.id}><input type="checkbox" checked={selectedMcp.includes(server.id)} onChange={() => setSelectedMcp(current => current.includes(server.id) ? current.filter(id => id !== server.id) : [...current, server.id])} />{mcpDisplayName(server, locale)}</label>)}<button onClick={() => { setToolsOpen(false); onOpenSettings(); }}>{t("管理模型与工具 ↗", "Manage models & tools ↗")}</button></div>}</div>
            {onLearningPresetChange && <LearningStyleMenu value={topicPreset} presets={learningPresets} failed={learningPresetsFailed} disabled={loading} onOpen={() => onReloadPresets?.()} onChange={chooseLearningStyle} />}
          </div><div className="chat-compose-right"><select aria-label={t("选择模型", "Choose model")} value={activeModelId ?? ""} onChange={event => onModelChange(Number(event.target.value))} disabled={!models.length}>{!models.length && <option value="">{t("未连接模型", "No model connected")}</option>}{models.map(model => <option value={model.id} key={model.id}>{model.label}</option>)}</select>{streaming ? <button className="chat-send is-stop" onClick={onStop} aria-label={t("停止回答", "Stop response")} title={t("停止回答", "Stop response")}>■</button> : <button className="chat-send" onClick={() => void send()} disabled={loading || noModel || attachmentsBusy || focusBusy || (!displayDraft.text.trim() && !displayDraft.images.length && !displayDraft.documents?.length)} aria-label={t("发送问题", "Send question")} title={focusBusy ? t("这个问题还在回答，完成后可以继续提问", "This question is still being answered. Ask a follow-up when it finishes.") : t("发送 · Enter", "Send · Enter")}><Icon name="arrow" /></button>}</div></div>
        </div>
      </div>
    </>}
    {selection && <div className={`chat-selection-card ${selection.state === "selected" ? "is-compact" : ""}${touchScreen ? " is-docked" : ""}`} ref={selectionRef} style={touchScreen ? undefined : { left: selection.x, top: selection.y }} role="dialog" aria-label={t("解释选中文字", "Explain selected text")} onPointerDown={event => { cardPressRef.current = Date.now(); event.stopPropagation(); }}>
      {selection.state === "selected" ? <><CopyButton text={selection.text} label={t("复制选中文字", "Copy selected text")} visibleLabel={t("复制", "Copy")} /><button className="chat-explain-button" disabled={noModel} onClick={() => void explainSelection()}>{t("解释一下", "Explain")}</button><button className="chat-selection-branch" onClick={() => void branchSelection()} disabled={branching || streaming} title={t("围绕选中文字创建分支", "Create a branch from the selected text")}><Icon name="tree" />{t("新分支", "New branch")}</button></> : <><div className="chat-selection-heading"><span>{selection.text}</span><button onClick={() => { setSelection(null); selectionRequestRef.current++; }} aria-label={t("关闭解释", "Close explanation")}>×</button></div><div className="chat-selection-content">{selection.state === "loading" ? <span className="chat-explaining">{t("正在解释…", "Explaining…")}</span> : selection.explanation ? <div className="chat-markdown"><Markdown text={selection.explanation} /></div> : null}{selection.error && <div className="chat-selection-error" role="alert">{localizeError(selection.error, locale)}<button onClick={() => void explainSelection()}>{t("重试解释", "Try again")}</button></div>}</div><div className="chat-selection-footer"><CopyButton text={selection.text} label={t("复制选中文字", "Copy selected text")} visibleLabel={t("复制", "Copy")} /><button onClick={() => { setSelection(null); selectionRequestRef.current++; }}>{t("明白了", "Got it")}</button><button className="chat-explain-button" onClick={() => void branchSelection()} disabled={branching || streaming || selection.state === "loading"}>{branching ? t("创建中…", "Creating…") : t("新分支 ↗", "New branch ↗")}</button></div></>}
    </div>}
  </main>;
}
