import { useEffect, useMemo, useRef, useState } from "react";
import { api, isStreamLost, type AskHandlers, type AskMeta, type ChangeNotice, type ContextStatus } from "./api";
import { useI18n } from "./i18n";
import { localizeError } from "./i18n/workspace";
import type {
  AnswerDiagnostics,
  AnswerSource,
  McpInput,
  McpServer,
  ModelCfg,
  ModelInput,
  ThreadNode,
  ToolStep,
  Tree,
  TreeNode,
  SourceAnchor,
  NavigationAnchor,
  DocumentSummary,
  LearningPresetList,
  Understanding,
} from "./types";
import { Sidebar } from "./components/Sidebar";
import { ChatPane, MapToggle } from "./components/ChatPane";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { LearningMap } from "./components/LearningMap";
import { NavigationGate, readSaved, saveValue, clearLegacyRecovery, removeWorkspace, onWorkspaceDeletion, orphanedWorkspaceTrees, orphanedWorkspaceNodes, deletionAffectsRequest } from "./lib/workspace";
import { mergePendingTitles, pollPendingTitles } from "./lib/titlePolling";
import { resumeAnswer } from "./lib/answerStream";
import { keepIfSame, planRefresh, watchChanges } from "./lib/changeFeed";
import { restoreUnderstanding, withUnderstanding } from "./lib/understanding";
import { askInStyle, nodeQuestionPreset, presetForQuestion, requestedPreset, restoreTopicPreset, withTopicPreset } from "./lib/learningPresets";
import { finishToolStep, startToolStep } from "./lib/toolSteps";
import { locateSource } from "./lib/sourceLinks";
import { onPairingRequired } from "./lib/phonePairing";
import { SettingsModal } from "./components/SettingsModal";
import { NewTreeModal } from "./components/NewTreeModal";
import { AddModelModal } from "./components/AddModelModal";
import { McpConfigModal } from "./components/McpConfigModal";

export default function App() {
  const { locale, t } = useI18n();
  const [models, setModels] = useState<ModelCfg[]>([]);
  const [activeModelId, setActiveModelId] = useState<number | null>(null);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [trees, setTrees] = useState<Tree[]>([]);
  const [activeTreeId, setActiveTreeId] = useState<number | null>(null);
  const [treeNodes, setTreeNodes] = useState<TreeNode[]>([]);
  const [activeNodeId, setActiveNodeId] = useState<number | null>(null);
  const [thread, setThread] = useState<ThreadNode[]>([]);
  const [rightOpen, setRightOpen] = useState(() => window.innerWidth > 760 && readSaved("bl-map-open", window.innerWidth >= 1100));
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [newTreeOpen, setNewTreeOpen] = useState(false);
  const [addModelOpen, setAddModelOpen] = useState(false);
  const [editingModel, setEditingModel] = useState<ModelCfg | null>(null);
  const [mcpConfigOpen, setMcpConfigOpen] = useState(false);
  const [mcpConfigServer, setMcpConfigServer] = useState<McpServer | null>(null);
  const [rightWidth, setRightWidth] = useState(() => Number(localStorage.getItem("bl-right-w")) || 320);
  const [streaming, setStreaming] = useState(false);
  const [live, setLive] = useState("");
  const [liveReasoning, setLiveReasoning] = useState("");
  const [liveSteps, setLiveSteps] = useState<ToolStep[]>([]);
  const [liveDiagnostics, setLiveDiagnostics] = useState<AnswerDiagnostics | null>(null);
  const [liveSources, setLiveSources] = useState<AnswerSource[]>([]);
  const [contextStatus, setContextStatus] = useState<ContextStatus | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const [sendingImages, setSendingImages] = useState<string[]>([]);
  const [sendingDocuments, setSendingDocuments] = useState<DocumentSummary[]>([]);
  const [sendingPreset, setSendingPreset] = useState<string | null>(null);
  const [learningPresets, setLearningPresets] = useState<LearningPresetList | null>(null);
  const [learningPresetsFailed, setLearningPresetsFailed] = useState(false);
  const presetsRequest = useRef(0);
  const [err, setErr] = useState<string | null>(null);
  const [theme, setTheme] = useState<"light" | "dark" | "system">(
    () => (localStorage.getItem("bl-theme") as "light" | "dark" | "system") || "system",
  );
  const abortRef = useRef<AbortController | null>(null);
  const contextStatusRequestRef = useRef<string | null>(null);
  const [streamFromId, setStreamFromId] = useState<number | null>(null);
  const focusRef = useRef<number | null>(null);
  const treeRef = useRef<number | null>(null);
  const navigation = useRef(new NavigationGate());
  const mapRevision = useRef(0);
  const [navigationAnchor, setNavigationAnchor] = useState<NavigationAnchor | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [compactLayout, setCompactLayout] = useState(() => window.matchMedia("(max-width: 760px)").matches);
  const [busyNavigation, setBusyNavigation] = useState(false);
  const streamTargetRef = useRef<number | null>(null);
  const requestRef = useRef<{ from: number; origin: number; tree: number; requestId: string } | null>(null);
  const [notice, setNoticeState] = useState<{ message: string } | null>(null);
  // Set when this phone's pairing is missing or was rotated on the computer.
  const [pairingRequired, setPairingRequired] = useState(false);
  useEffect(() => onPairingRequired(() => setPairingRequired(true)), []);
  function setNotice(message: string | null) {
    // A new event restarts the timer even when its wording is unchanged.
    setNoticeState(message === null ? null : { message });
  }
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNoticeState(current => current === notice ? null : current), 10_000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  const importRef = useRef<HTMLInputElement>(null);
  const pendingTitleIds = treeNodes.filter(node => node.title_state === "pending").map(node => node.id).join(",");
  // The change watcher outlives renders; it reads the view and handlers through refs.
  const threadRef = useRef(thread);
  threadRef.current = thread;
  const remoteChangesRef = useRef<(notice: ChangeNotice) => Promise<unknown>>(async () => {});
  remoteChangesRef.current = applyRemoteChanges;
  useEffect(() => watchChanges({ load: api.changes, apply: notice => remoteChangesRef.current(notice) }), []);

  useEffect(() => {
    const breakpoint = window.matchMedia("(max-width: 760px)");
    const resize = (event: MediaQueryListEvent) => {
      setCompactLayout(event.matches);
      setSidebarOpen(false);
      // A panel that was open beside the chat must not cover it after a resize.
      // Automatic collapsing preserves the user's saved desktop preference.
      setRightOpen(event.matches ? false : readSaved("bl-map-open", window.innerWidth >= 1100));
    };
    breakpoint.addEventListener("change", resize);
    return () => breakpoint.removeEventListener("change", resize);
  }, []);

  useEffect(() => {
    if (!compactLayout || (!rightOpen && !sidebarOpen)) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setRightOpen(false); setSidebarOpen(false); }
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [compactLayout, rightOpen, sidebarOpen]);

  useEffect(() => {
    if (activeTreeId == null || !pendingTitleIds) return;
    const treeId = activeTreeId;
    return pollPendingTitles({
      treeId,
      load: api.getTree,
      revision: () => mapRevision.current,
      isActive: id => treeRef.current === id,
      commit: (nodes, ticket) => setTreeNodes(current =>
        treeRef.current === treeId && mapRevision.current === ticket
          ? mergePendingTitles(current, nodes) : current),
    });
  }, [activeTreeId, pendingTitleIds]);

  async function loadModels() {
    const m = await api.listModels();
    setModels(keepIfSame(m));
    setActiveModelId((prev) =>
      prev && m.some((x) => x.id === prev) ? prev : m.find(x => x.id === readSaved("bl-model", 0))?.id ?? m.find((x) => x.is_default)?.id ?? m[0]?.id ?? null,
    );
  }

  async function loadTrees() {
    const t = await api.listTrees(true);
    for (const treeId of orphanedWorkspaceTrees(t.map(tree => tree.id))) removeWorkspace({ treeId }, false);
    setTrees(keepIfSame(t));
    return t;
  }

  async function loadMcp() {
    setMcpServers(keepIfSame(await api.listMcp()));
  }

  // Custom styles are local files, so the list is read again whenever the menu opens, a topic
  // changes elsewhere, or a question would use a style the loaded list does not know.
  // Resolves to the list read (newer reads still win the state), or null when it failed.
  async function loadLearningPresets(): Promise<LearningPresetList | null> {
    const ticket = ++presetsRequest.current;
    try {
      const list = await api.listLearningPresets();
      if (ticket === presetsRequest.current) {
        setLearningPresets(keepIfSame<LearningPresetList | null>(list));
        setLearningPresetsFailed(false);
      }
      return list;
    } catch {
      if (ticket === presetsRequest.current) setLearningPresetsFailed(true);
      return null;
    }
  }

  useEffect(() => {
    const resolve = (): "light" | "dark" =>
      theme === "system"
        ? window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light"
        : theme;
    const apply = () => document.documentElement.setAttribute("data-theme", resolve());
    apply();
    localStorage.setItem("bl-theme", theme);
    if (theme === "system") {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      mq.addEventListener("change", apply);
      return () => mq.removeEventListener("change", apply);
    }
  }, [theme]);

  useEffect(() => {
    clearLegacyRecovery();
    loadModels().catch((e) => setErr(String(e)));
    loadMcp().catch((e) => setErr(String(e)));
    void loadLearningPresets();
    loadTrees()
      .then((t) => {
        const saved = readSaved<number | null>("bl-tree", null);
        const initial = t.find(x => x.id === saved) ?? t.find(x => !x.archived);
        if (initial) selectTree(initial.id, t);
      })
      .catch((e) => setErr(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Open a topic at `focus` when it exists there, else at its saved or first node. */
  async function selectTree(id: number, list?: Tree[], focus?: number) {
    const ticket = navigation.current.next();
    const mapTicket = ++mapRevision.current;
    treeRef.current = id;
    focusRef.current = null;
    setActiveTreeId(id);
    setActiveNodeId(null);
    setTreeNodes([]);
    setThread([]);
    setErr(null);
    setNavigationAnchor(null);
    setSidebarOpen(false);
    setBusyNavigation(true);
    saveValue('bl-tree', id);
    try {
      const nodes = await api.getTree(id);
      if (!navigation.current.current(ticket)) return;
      const orphaned = orphanedWorkspaceNodes(id, nodes.map(node => node.id));
      if (orphaned.length) removeWorkspace({ treeId: id, nodeIds: orphaned }, false);
      if (mapTicket === mapRevision.current) setTreeNodes(nodes);
      const tr = (list ?? trees).find(t => t.id === id);
      const saved = readSaved<number | null>(`bl-node:${id}`, null);
      const root = tr?.root_node_id ?? nodes.find(n => n.parent_id == null)?.id;
      const selected = nodes.find(n => n.id === focus)?.id ?? nodes.find(n => n.id === saved)?.id ?? root ?? nodes[0]?.id;
      if (selected) await selectNode(selected);
    } catch (e) {
      if (navigation.current.current(ticket)) setErr(String((e as Error).message ?? e));
    } finally {
      if (treeRef.current === id) setBusyNavigation(false);
    }
  }

  async function selectNode(id: number, anchor?: Omit<NavigationAnchor, 'nodeId' | 'nonce'>) {
    const ticket = navigation.current.next();
    const treeId = treeRef.current;
    focusRef.current = id;
    setActiveNodeId(id);
    setThread([]);
    setErr(null);
    setBusyNavigation(true);
    setNavigationAnchor(anchor ? { ...anchor, nodeId: id, nonce: Date.now() } : null);
    if (treeId != null) saveValue(`bl-node:${treeId}`, id);
    try {
      const result = await api.getThread(id);
      if (navigation.current.current(ticket) && treeRef.current === treeId) setThread(result);
    } catch (e) {
      if (navigation.current.current(ticket)) setErr(String((e as Error).message ?? e));
    } finally {
      if (navigation.current.current(ticket)) setBusyNavigation(false);
    }
  }

  async function refreshTree(id: number) {
    const ticket = ++mapRevision.current;
    const nodes = await api.getTree(id);
    if (treeRef.current === id && ticket === mapRevision.current) {
      const orphaned = orphanedWorkspaceNodes(id, nodes.map(node => node.id));
      if (orphaned.length) removeWorkspace({ treeId: id, nodeIds: orphaned }, false);
      setTreeNodes(nodes);
    }
    return nodes;
  }

  // Another window or device changed something: reload only what this view shows.
  async function applyRemoteChanges(notice: ChangeNotice) {
    const treeId = treeRef.current;
    const plan = planRefresh(notice, { treeId, focusId: focusRef.current, threadNodeIds: threadRef.current.map(node => node.node_id) });
    await Promise.all([
      plan.models && loadModels(),
      plan.mcp && loadMcp(),
      plan.topics && syncTopics(),
      // A topic may now use a custom style added after this page read the list.
      plan.topics && loadLearningPresets(),
      plan.map && treeId !== null && syncMap(treeId),
      plan.thread && syncThread(),
    ]);
  }

  async function syncTopics() {
    const list = await loadTrees();
    const current = treeRef.current;
    if (current === null || list.some(tree => tree.id === current)) return;
    // Deleted elsewhere: continue as if it had been deleted here.
    const next = list.find(tree => !tree.archived);
    if (next) await selectTree(next.id, list);
    else clearTreeSelection(false);
  }

  async function syncMap(treeId: number) {
    // Background reloads never cancel a newer refresh or navigation.
    const ticket = mapRevision.current;
    let nodes: TreeNode[];
    try { nodes = await api.getTree(treeId); }
    catch (e) { if ((e as { status?: number }).status === 404) return; throw e; }
    if (treeRef.current !== treeId || ticket !== mapRevision.current) return;
    const orphaned = orphanedWorkspaceNodes(treeId, nodes.map(node => node.id));
    if (orphaned.length) removeWorkspace({ treeId, nodeIds: orphaned }, false);
    setTreeNodes(keepIfSame(nodes));
    const focus = focusRef.current;
    if (focus === null || abortRef.current || nodes.some(node => node.id === focus)) return;
    // The open question was deleted elsewhere; return to the start of the topic.
    const start = nodes.find(node => node.parent_id == null)?.id ?? nodes[0]?.id;
    if (start !== undefined) await selectNode(start);
  }

  async function syncThread() {
    const nodeId = focusRef.current, treeId = treeRef.current;
    // A local answer reloads its own thread when it ends.
    if (nodeId === null || abortRef.current) return;
    const ticket = navigation.current.peek();
    let rows: ThreadNode[];
    try { rows = await api.getThread(nodeId); }
    catch (e) { if ((e as { status?: number }).status === 404) return; throw e; }
    if (navigation.current.current(ticket) && focusRef.current === nodeId && treeRef.current === treeId && !abortRef.current)
      setThread(keepIfSame(rows));
  }

  // Show the learner's mark at once in the conversation and the map; restore it if the save fails.
  async function changeUnderstanding(nodeId: number, value: Understanding | null) {
    const previous = (thread.find(row => row.node_id === nodeId) ?? treeNodes.find(node => node.id === nodeId))?.understanding ?? null;
    const isRow = (row: ThreadNode) => row.node_id === nodeId;
    const isNode = (node: TreeNode) => node.id === nodeId;
    setThread(rows => withUnderstanding(rows, isRow, value));
    setTreeNodes(nodes => withUnderstanding(nodes, isNode, value));
    try { await api.setUnderstanding(nodeId, value); }
    catch (error) {
      setThread(rows => restoreUnderstanding(rows, isRow, value, previous));
      setTreeNodes(nodes => restoreUnderstanding(nodes, isNode, value, previous));
      throw error;
    }
  }

  // The open topic's style applies to its next questions at once; restore it if the save fails.
  async function changeLearningPreset(treeId: number, value: string | null) {
    const previous = trees.find(tree => tree.id === treeId)?.learning_preset ?? null;
    setTrees(current => withTopicPreset(current, treeId, value));
    try {
      const updated = await api.updateTree(treeId, { learning_preset: value });
      setTrees(current => withTopicPreset(current, treeId, updated.learning_preset ?? null));
    } catch (error) {
      setTrees(current => restoreTopicPreset(current, treeId, value, previous));
      void loadLearningPresets();  // A custom file may have been removed meanwhile.
      throw error;
    }
  }

  // Stop an answer started in another window or on another device.
  async function stopNode(nodeId: number) {
    try { await api.stop(nodeId); await syncThread(); }
    catch (e) { setErr(String((e as Error).message ?? e)); }
  }

  async function createTree(title: string) {
    setErr(null);
    try {
      const t = await api.createTree(title);
      const list = await loadTrees();
      await selectTree(t.id, list);
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    }
  }

  function clearTreeSelection(clearSaved = true) {
    navigation.current.next();
    ++mapRevision.current;
    treeRef.current = null;
    focusRef.current = null;
    setActiveTreeId(null);
    setTreeNodes([]);
    setActiveNodeId(null);
    setThread([]);
    setNavigationAnchor(null);
    setBusyNavigation(false);
    setErr(null);
    if (clearSaved) {
      try { localStorage.removeItem('bl-tree'); } catch { /* Navigation state is optional. */ }
    }
  }

  async function renameTree(id: number, title: string) {
    const updated = await api.updateTree(id, { title });
    setTrees(current => current.map(tree => tree.id === id ? { ...tree, title: updated.title } : tree));
  }

  async function archiveTree(id: number, archived: boolean) {
    const updated = await api.updateTree(id, { archived });
    setTrees(current => current.map(tree => tree.id === id ? { ...tree, archived: updated.archived } : tree));
    setNotice(archived
      ? t("已归档，可在设置的「已归档」中查看和恢复。", "Archived. View or restore it from Archived in Settings.")
      : t("已恢复到我的主题。", "Restored to My topics."));
    // Keep a generating topic open so its Stop control remains reachable.
    // Drafts stay stored under their existing node IDs when changing topics.
    const answeringHere = abortRef.current !== null && requestRef.current?.tree === id;
    if (archived && treeRef.current === id && !answeringHere) {
      const next = trees.find(tree => tree.id !== id && !tree.archived);
      if (next) await selectTree(next.id);
      else clearTreeSelection();
    }
  }

  async function deleteTree(id: number) {
    if (abortRef.current) { setErr(t("请先停止当前回答，再删除。", "Stop the current response before deleting.")); return; }
    try {
      await api.deleteTree(id);
      removeWorkspace({ treeId: id });
      const list = trees.filter(tree => tree.id !== id);
      setTrees(current => current.filter(tree => tree.id !== id));
      setNotice(t("主题已永久删除。", "Topic permanently deleted."));
      if (treeRef.current === id) {
        const next = list.find(tree => !tree.archived);
        if (next) await selectTree(next.id, list);
        else clearTreeSelection(false);
      }
    } catch (e) { setErr(String((e as Error).message ?? e)); }
  }

  useEffect(() => onWorkspaceDeletion((deletion, remote) => {
    if (!remote) return;
    if (deletionAffectsRequest(deletion, requestRef.current, streamTargetRef.current)) abortRef.current?.abort();
    if (deletion.nodeIds === undefined) {
      setTrees(current => current.filter(tree => tree.id !== deletion.treeId));
      if (treeRef.current === deletion.treeId) clearTreeSelection(false);
    } else if (treeRef.current === deletion.treeId) {
      ++mapRevision.current;
      setTreeNodes(current => current.filter(node => !deletion.nodeIds!.includes(node.id)));
      if (focusRef.current !== null && deletion.nodeIds.includes(focusRef.current)) {
        navigation.current.next();
        focusRef.current = null;
        setActiveNodeId(null);
        setThread([]);
        setNavigationAnchor(null);
        setBusyNavigation(false);
      }
      void refreshTree(deletion.treeId).catch(() => {});
    }
  }), []);

  async function onAsk(payload: { question: string; images?: string[]; document_ids?: string[]; documents?: DocumentSummary[]; tools?: string[]; deep?: boolean; mode?: 'continue' | 'retry' | 'revise'; nodeId?: number; question_message_id?: number; onAccepted?: (nodeId: number) => void }): Promise<boolean> {
    const fromId = payload.nodeId ?? focusRef.current;
    const treeId = treeRef.current;
    if (!fromId || !treeId || !activeModelId || abortRef.current || busyNavigation) return false;
    const origin = focusRef.current ?? fromId;
    const requestId = crypto.randomUUID();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    contextStatusRequestRef.current = requestId;
    ctrl.signal.addEventListener('abort', () => {
      if (contextStatusRequestRef.current === requestId) {
        contextStatusRequestRef.current = null;
        setContextStatus(null);
      }
    }, { once: true });
    requestRef.current = { from: fromId, origin, tree: treeId, requestId };
    streamTargetRef.current = null;
    // The composer's style goes with every question; a retry is answered in its node's own
    // (the server ignores the field then).
    const retry = payload.mode === 'retry';
    const topicPreset = trees.find(tree => tree.id === treeId)?.learning_preset ?? null;
    setSendingPreset(retry ? nodeQuestionPreset(threadRef.current, fromId) : requestedPreset(topicPreset, learningPresets));
    setPendingQuestion(payload.question); setSendingImages(payload.images ?? []); setSendingDocuments(payload.documents ?? []);
    setLive(''); setLiveReasoning(''); setLiveSteps([]); setLiveDiagnostics(null); setLiveSources([]); setContextStatus(null);
    setStreaming(true); setStreamFromId(origin); setErr(null);
    let succeeded = false;
    let accepted = false;
    const acceptQuestion = (nodeId: number) => {
      if (accepted) return;
      accepted = true;
      payload.onAccepted?.(nodeId);
    };
    const handlers: AskHandlers = {
      onStart: meta => { setReconnecting(false); streamTargetRef.current = meta.node_id ?? fromId; acceptQuestion(streamTargetRef.current); void refreshTree(treeId).catch(() => {}); },
      onDelta: d => setLive(p => p + d),
      onReasoning: r => setLiveReasoning(p => p + r),
      onContextStatus: status => {
        if (abortRef.current === ctrl && contextStatusRequestRef.current === requestId && !ctrl.signal.aborted)
          setContextStatus(status);
      },
      onToolStart: name => setLiveSteps(s => startToolStep(s, name)),
      onToolEnd: (name, result, reference) => setLiveSteps(s => finishToolStep(s, name, result, reference)),
      onDiagnostics: setLiveDiagnostics,
      onSources: sources => setLiveSources(sources),
    };
    let unreachable = false;
    try {
      let meta: AskMeta;
      try {
        const send = (learningPreset: string | null) => {
          if (!retry) setSendingPreset(learningPreset);
          return api.ask(fromId, {
            question: payload.question, config_id: activeModelId,
            images: payload.images, document_ids: payload.document_ids, tools: payload.tools, deep_think: payload.deep,
            mode: payload.mode, request_id: requestId, question_message_id: payload.question_message_id,
            learning_preset: learningPreset,
          }, handlers, ctrl.signal);
        };
        // A style the loaded list lacks may be newer than the list; one removed since the list was
        // read is rejected before anything is stored and asked again the default way.
        meta = retry ? await send(requestedPreset(topicPreset, learningPresets))
          : await askInStyle(await presetForQuestion(topicPreset, learningPresets, loadLearningPresets), send, loadLearningPresets);
      } catch (e) {
        if (!isStreamLost(e) || ctrl.signal.aborted) throw e;
        // The answer keeps running on the server, e.g. after a phone locks: reattach when reachable.
        setReconnecting(true);
        meta = await resumeAnswer({
          follow: after => api.follow(fromId, requestId, after, handlers, ctrl.signal),
          lost: e.meta,
          signal: ctrl.signal,
          // Without a start event the question may never have arrived; Stop ends an accepted one.
          attempts: e.meta.node_id === undefined ? 2 : Infinity,
          onLost: () => setReconnecting(true),
        });
      }
      streamTargetRef.current = meta.node_id ?? streamTargetRef.current ?? fromId;
      acceptQuestion(streamTargetRef.current);
      succeeded = true;
    } catch (e: any) {
      unreachable = isStreamLost(e);
      if (e?.meta?.node_id && e.meta.request_id === requestId) {
        streamTargetRef.current = e.meta.node_id;
        // A 409 may point to another request already running on this node.
        acceptQuestion(e.meta.node_id);
      }
      if (e?.name !== 'AbortError' && treeRef.current === treeId && focusRef.current === origin)
        setErr(unreachable
          ? t("连接中断，暂时无法确认回答状态；恢复连接后会自动更新。", "Connection lost. The response status is unknown; this page updates when the connection returns.")
          : String(e?.message ?? e));
    } finally {
      setReconnecting(false);
      if (contextStatusRequestRef.current === requestId) {
        contextStatusRequestRef.current = null;
        setContextStatus(null);
      }
      try {
        await refreshTree(treeId);
        const target = streamTargetRef.current;
        if (target && treeRef.current === treeId && (focusRef.current === origin || focusRef.current === target))
          await selectNode(target);
      } catch (e) {
        if (treeRef.current === treeId && !unreachable) setErr(t("回答已结束，刷新记录失败，请重新选择节点。", "The response ended, but the history could not be refreshed. Select the node again."));
      }
      abortRef.current = null; requestRef.current = null; streamTargetRef.current = null;
      setStreaming(false); setStreamFromId(null); setLive(''); setLiveReasoning('');
      setLiveSteps([]); setLiveDiagnostics(null); setLiveSources([]); setPendingQuestion(null); setSendingImages([]); setSendingDocuments([]); setSendingPreset(null);
    }
    return succeeded;
  }

  async function onStop() {
    const req = requestRef.current;
    const ctrl = abortRef.current;
    if (!req || !ctrl) return;
    contextStatusRequestRef.current = null;
    setContextStatus(null);
    try { const stopped = await api.stop(streamTargetRef.current ?? req.from, req.requestId); if (stopped.node_id) streamTargetRef.current = stopped.node_id; }
    catch { setErr(t("停止请求未确认，连接已断开；刷新后可查看状态。", "The stop request was not confirmed. The connection is closed; refresh to check its status.")); }
    finally { ctrl.abort(); }
  }

  async function onBranch(seed: string, fromNodeId: number, anchor?: SourceAnchor, onCreated?: (nodeId: number) => void): Promise<void> {
    const treeId = treeRef.current;
    if (!treeId) return;
    const ticket = navigation.current.next();
    try {
      const child = await api.branch(fromNodeId, seed, anchor);
      onCreated?.(child.id);
      await refreshTree(treeId);
      if (treeRef.current === treeId && navigation.current.current(ticket)) await selectNode(child.id);
    } catch (e) { if (treeRef.current === treeId) setErr(String((e as Error).message ?? e)); throw e; }
  }

  async function onDeleteNode(id: number) {
    const treeId = treeRef.current;
    if (!treeId) return;
    if (abortRef.current) { setErr(t("请先停止当前回答，再删除。", "Stop the current response before deleting.")); return; }
    const target = treeNodes.find(node => node.id === id);
    if (!target) return;
    if (target.parent_id == null) {
      const title = trees.find(tree => tree.id === treeId)?.title ?? target.title;
      if (window.confirm(t("永久删除「{title}」？其中的对话、分支、附件和话题记忆都会删除，且无法撤销。", "Permanently delete “{title}”? Its conversations, branches, attachments, and topic memories will be deleted. This cannot be undone.", { title }))) await deleteTree(treeId);
      return;
    }
    const descendants = new Set<number>([id]);
    for (let changed = true; changed;) {
      changed = false;
      for (const node of treeNodes) if (node.parent_id != null && descendants.has(node.parent_id) && !descendants.has(node.id)) { descendants.add(node.id); changed = true; }
    }
    if (!window.confirm(t("永久删除「{title}」及其所有后续节点（当前 {count} 个）？相关对话和话题记忆会删除；其他独立分支与修改版本、主题共享附件会保留。此操作无法撤销。", "Permanently delete “{title}” and all its descendant nodes (currently {count})? Related conversations and topic memories will be deleted. Other independent branches and revisions, and topic-shared attachments, will remain. This cannot be undone.", { title: target.title, count: descendants.size - 1 }))) return;
    try {
      // The server may know descendants created in another tab since our last refresh.
      const result = await api.deleteNode(id);
      const deletedIds = new Set(result.deleted);
      removeWorkspace({ treeId, nodeIds: [...deletedIds] });
      const remaining = treeNodes.filter(node => !deletedIds.has(node.id));
      if (treeRef.current === treeId) {
        ++mapRevision.current;
        setTreeNodes(current => current.filter(node => !deletedIds.has(node.id)));
        if (focusRef.current !== null && deletedIds.has(focusRef.current)) {
          const focus = remaining.find(node => node.id === target.parent_id)?.id ?? remaining[0]?.id;
          if (focus) await selectNode(focus);
          else { navigation.current.next(); focusRef.current = null; setActiveNodeId(null); setThread([]); }
        }
      }
      setNotice(t("分支已永久删除。", "Branch permanently deleted."));
      // Surviving revisions may have had their source references cleared by deletion.
      try {
        await refreshTree(treeId);
        const focus = focusRef.current;
        if (treeRef.current === treeId && focus !== null) await selectNode(focus);
      } catch { if (treeRef.current === treeId) setErr(t("分支已删除，刷新记录失败，请重新选择节点。", "The branch was deleted, but refreshing failed. Select the node again.")); }
    } catch (e) { setErr(String((e as Error).message ?? e)); }
  }

  async function exportTree() {
    if (treeRef.current == null) return;
    try {
      const data = await api.exportTree(treeRef.current);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url;
      link.download = `${t('学习树', 'LearningTree')}-${activeTree?.title ?? t('学习记录', 'Learning history')}-${new Date().toISOString().slice(0, 10)}.json`;
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice(t("学习记录已导出。", "Learning history exported."));
    } catch (e) { setErr(String((e as Error).message ?? e)); }
  }

  async function importTree(data: unknown) {
    try {
      const restored = await api.importTree(data);
      const list = await loadTrees(); await selectTree(restored.id, list);
      setNotice(t("学习记录已导入为新主题。", "Learning records imported as a new topic."));
    } catch (e) { setErr(String((e as Error).message ?? e)); }
  }

  async function addModel(m: ModelInput) {
    if (editingModel) await api.updateModel(editingModel.id, m);
    else await api.addModel(m);
    await loadModels();
  }
  async function addMock() {
    await api.addModel({
      label: "Mock",
      base_url: "https://mock/v1",
      llm_model: "mock",
      api_key: "mock",
      is_default: models.length === 0,
      protocol: 'openai', max_tokens: 4096, context_window: 32768, target_input_tokens: null,
    });
    await loadModels();
  }
  async function deleteModel(id: number) {
    await api.deleteModel(id);
    await loadModels();
  }
  async function setDefaultModel(id: number) {
    await api.setDefaultModel(id);
    await loadModels();
  }

  async function addMcp(m: McpInput) {
    await api.addMcp(m);
    await loadMcp();
  }
  async function deleteMcp(id: number) {
    await api.deleteMcp(id);
    await loadMcp();
  }
  async function updateMcp(id: number, m: McpInput) {
    await api.updateMcp(id, m);
    await loadMcp();
  }

  // Stored provenance can outlive its source: check it before leaving this view, and stay put if it is gone.
  async function openMemorySource(treeId: number, nodeId: number | null) {
    const ticket = navigation.current.peek();
    const sameTopic = treeId === treeRef.current;
    let found: "found" | "deleted";
    try { found = await locateSource({ treeId, nodeId }, id => sameTopic ? refreshTree(id) : api.getTree(id)); }
    catch (e) { if (navigation.current.current(ticket)) setErr(String((e as Error).message ?? e)); return; }
    // The learner went elsewhere meanwhile.
    if (!navigation.current.current(ticket)) return;
    if (found === "deleted") { setNotice(t("这条来源已删除。", "This source was deleted.")); return; }
    if (!sameTopic) await selectTree(treeId, undefined, nodeId ?? undefined);
    else if (nodeId !== null) await selectNode(nodeId);
  }

  function startResize(e: React.MouseEvent) {
    e.preventDefault();
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    let w = rightWidth;
    const onMove = (ev: MouseEvent) => {
      w = Math.min(640, Math.max(240, window.innerWidth - ev.clientX));
      setRightWidth(w);
    };
    const onUp = () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      localStorage.setItem("bl-right-w", String(w));
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  const activeTree = trees.find((t) => t.id === activeTreeId) ?? null;
  const focused = thread[thread.length - 1];
  const toggleMap = () => { setSidebarOpen(false); setRightOpen(o => { saveValue("bl-map-open", !o); return !o; }); };
  // Replaces the whole conversation pane, header included, so it keeps the header's map toggle:
  // on a phone the map starts closed and is the way to open another question.
  const conversationFallback = <main className="center chat-pane">
    <header className="chat-header">
      <div className="chat-heading"><span className="chat-heading-dot" /><h1>{activeNodeId != null ? activeTree?.title ?? "" : t("学习空间", "Learning space")}</h1></div>
      <div className="chat-header-actions"><MapToggle open={rightOpen} onToggle={toggleMap} /></div>
    </header>
    <div className="chat-error" role="alert">{t("这段对话暂时无法显示。可以从学习树打开其他问题，或重新载入页面。", "This conversation could not be displayed. Open another question from the learning tree, or reload the page.")} <button type="button" className="chat-error-reload" onClick={() => window.location.reload()}>{t("重新载入", "Reload")}</button></div>
  </main>;
  const topicIds = useMemo(() => new Set(trees.map(tree => tree.id)), [trees]);
  const mapNodeIds = useMemo(() => new Set(treeNodes.map(node => node.id)), [treeNodes]);

  return (
    <div className={`app${sidebarOpen ? ' sidebar-visible' : ''}`}>
      <button className="mobile-sidebar-toggle" onClick={() => { setSidebarOpen(v => !v); setRightOpen(false); }} aria-label={sidebarOpen ? t("关闭主题列表", "Close topic list") : t("打开主题列表", "Open topic list")} aria-expanded={sidebarOpen}>☰</button>
      {sidebarOpen && <button className="sidebar-scrim" onClick={() => setSidebarOpen(false)} aria-label={t("关闭主题列表", "Close topic list")} />}
      <input type="file" accept=".json,application/json" hidden ref={importRef} onChange={async e => {
        const file = e.currentTarget.files?.[0]; e.currentTarget.value = '';
        if (!file) return;
        if (file.size > 25 * 1024 * 1024) { setErr(t("备份文件不能超过 25 MB。", "The backup file must be 25 MB or smaller.")); return; }
        try { await importTree(JSON.parse(await file.text())); } catch { setErr(t("无法读取这个 JSON 备份。", "This JSON backup could not be read.")); }
      }} />
      <Sidebar
        trees={trees}
        activeTreeId={activeTreeId}
        onSelect={selectTree}
        onNew={() => setNewTreeOpen(true)}
        onDelete={deleteTree}
        onRename={renameTree}
        onArchive={archiveTree}
        onOpenSettings={() => setSettingsOpen(true)}
        onImport={() => importRef.current?.click()}
      />
      <div className="workspace-main">
      {pairingRequired && <div className="pairing-required" role="alert">{t("这台设备需要重新配对：请在电脑上打开「设置 → 手机访问」，用手机扫描二维码。", "This device needs to be paired again: open Settings → Phone access on the computer and scan the QR code with your phone.")}</div>}
      {notice && <div className="notice" role="status"><span>{localizeError(notice.message, locale)}</span><button onClick={() => setNotice(null)} aria-label={t("关闭提示", "Dismiss notification")}>×</button></div>}
      {busyNavigation && <div className="loading-line" aria-label={t("正在加载", "Loading")} />}
      {/* Stored answers are rendered from data: a value that breaks the conversation must not blank the app. */}
      <ErrorBoundary resetKey={`${activeTreeId}:${activeNodeId}`} fallback={conversationFallback}>
      <ChatPane
        thread={thread}
        activeTreeId={activeTreeId}
        activeNodeId={activeNodeId}
        onNavigate={selectNode}
        navigationAnchor={navigationAnchor}
        onExport={exportTree}
        onNew={() => setNewTreeOpen(true)}
        treeTitle={activeTree?.title ?? ""}
        hasNode={activeNodeId != null}
        focusedSeed={focused?.seed_text ?? null}
        live={live}
        liveReasoning={liveReasoning}
        liveSteps={liveSteps}
        liveDiagnostics={liveDiagnostics}
        liveSources={liveSources}
        onOpenSource={(treeId, nodeId) => void openMemorySource(treeId, nodeId)}
        topicIds={topicIds}
        mapNodeIds={mapNodeIds}
        contextStatus={contextStatus}
        reconnecting={reconnecting}
        pendingQuestion={pendingQuestion}
        sendingImages={sendingImages}
        sendingDocuments={sendingDocuments}
        streaming={streaming}
        loading={busyNavigation}
        showStream={streaming && activeTreeId === requestRef.current?.tree && (activeNodeId === streamFromId || activeNodeId === streamTargetRef.current)}
        err={err}
        models={models}
        activeModelId={activeModelId}
        onModelChange={id => { setActiveModelId(id); saveValue("bl-model", id); }}
        onOpenSettings={() => setSettingsOpen(true)}
        onAsk={onAsk}
        onStop={onStop}
        onStopNode={nodeId => void stopNode(nodeId)}
        onUnderstandingChange={changeUnderstanding}
        onBranch={onBranch}
        onDeleteNode={onDeleteNode}
        rightOpen={rightOpen}
        onToggleRight={toggleMap}
        onAddMock={addMock}
        mcpServers={mcpServers.filter((s) => s.enabled)}
        onTestMcp={(id) => api.testMcp(id)}
        learningPresets={learningPresets}
        learningPresetsFailed={learningPresetsFailed}
        topicPreset={activeTree?.learning_preset ?? null}
        onLearningPresetChange={activeTreeId == null ? undefined : id => changeLearningPreset(activeTreeId, id)}
        onReloadPresets={() => void loadLearningPresets()}
        sendingPreset={sendingPreset}
      />
      </ErrorBoundary>
      </div>
      {compactLayout && rightOpen && <button className="map-scrim" onClick={() => setRightOpen(false)} aria-label={t("关闭学习树面板", "Close learning tree panel")} />}
      {rightOpen && <div className="splitter" onMouseDown={startResize} />}
      {rightOpen && (
        <LearningMap
          nodes={treeNodes}
          activeId={activeNodeId}
          onSelect={(id, anchor) => { if (compactLayout) setRightOpen(false); void selectNode(id, anchor); }}
          onDelete={onDeleteNode}
          deleteDisabled={streaming}
          onCollapse={() => { setRightOpen(false); saveValue("bl-map-open", false); }}
          width={rightWidth}
          treeKey={activeTreeId ?? "empty"}
        />
      )}
      {settingsOpen && (
        <SettingsModal
          models={models}
          mcpServers={mcpServers}
          trees={trees}
          activeTreeId={activeTreeId}
          onOpenTree={selectTree}
          onDeleteTree={deleteTree}
          onRenameTree={renameTree}
          onArchiveTree={archiveTree}
          onClose={() => setSettingsOpen(false)}
          onDelete={deleteModel}
          onTest={(id) => api.testModel(id)}
          onAddMock={addMock}
          onOpenAddModel={() => { setEditingModel(null); setAddModelOpen(true); }}
          onOpenEditModel={model => { setEditingModel(model); setAddModelOpen(true); }}
          onSetDefault={setDefaultModel}
          onOpenMcpConfig={(server) => {
            setMcpConfigServer(server);
            setMcpConfigOpen(true);
          }}
          onOpenMemorySource={(treeId, nodeId) => void openMemorySource(treeId, nodeId)}
          theme={theme}
          onThemeChange={setTheme}
        />
      )}
      {addModelOpen && (
        <AddModelModal model={editingModel} isFirst={models.length === 0} onClose={() => setAddModelOpen(false)} onAdd={addModel} />
      )}
      {mcpConfigOpen && (
        <McpConfigModal
          server={mcpConfigServer}
          onClose={() => setMcpConfigOpen(false)}
          onAdd={addMcp}
          onUpdate={updateMcp}
          onDelete={deleteMcp}
          onTest={(id) => api.testMcp(id)}
        />
      )}
      {newTreeOpen && (
        <NewTreeModal
          onClose={() => setNewTreeOpen(false)}
          onCreate={(title) => {
            setNewTreeOpen(false);
            createTree(title);
          }}
        />
      )}
    </div>
  );
}
