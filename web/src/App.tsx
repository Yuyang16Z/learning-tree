import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useI18n } from "./i18n";
import { localizeError } from "./i18n/workspace";
import type {
  McpInput,
  McpServer,
  Memory,
  ModelCfg,
  ModelInput,
  ThreadNode,
  ToolStep,
  Tree,
  TreeNode,
  SourceAnchor,
  NavigationAnchor,
} from "./types";
import { Sidebar } from "./components/Sidebar";
import { ChatPane } from "./components/ChatPane";
import { LearningMap } from "./components/LearningMap";
import { NavigationGate, readSaved, saveValue } from "./lib/workspace";
import { SettingsModal } from "./components/SettingsModal";
import { NewTreeModal } from "./components/NewTreeModal";
import { AddModelModal } from "./components/AddModelModal";
import { McpConfigModal } from "./components/McpConfigModal";

export default function App() {
  const { locale, t } = useI18n();
  const [models, setModels] = useState<ModelCfg[]>([]);
  const [activeModelId, setActiveModelId] = useState<number | null>(null);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [trees, setTrees] = useState<Tree[]>([]);
  const [activeTreeId, setActiveTreeId] = useState<number | null>(null);
  const [treeNodes, setTreeNodes] = useState<TreeNode[]>([]);
  const [activeNodeId, setActiveNodeId] = useState<number | null>(null);
  const [thread, setThread] = useState<ThreadNode[]>([]);
  const [rightOpen, setRightOpen] = useState(() => window.innerWidth >= 760 && readSaved("bl-map-open", window.innerWidth >= 1100));
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
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const [sendingImages, setSendingImages] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [theme, setTheme] = useState<"light" | "dark" | "system">(
    () => (localStorage.getItem("bl-theme") as "light" | "dark" | "system") || "system",
  );
  const abortRef = useRef<AbortController | null>(null);
  const [streamFromId, setStreamFromId] = useState<number | null>(null);
  const focusRef = useRef<number | null>(null);
  const treeRef = useRef<number | null>(null);
  const navigation = useRef(new NavigationGate());
  const mapRevision = useRef(0);
  const [navigationAnchor, setNavigationAnchor] = useState<NavigationAnchor | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [busyNavigation, setBusyNavigation] = useState(false);
  const streamTargetRef = useRef<number | null>(null);
  const requestRef = useRef<{ from: number; origin: number; tree: number; requestId: string } | null>(null);
  const [recovery, setRecovery] = useState<Record<string, unknown> | null>(() => readSaved('bl-recovery', null));
  const [notice, setNotice] = useState<string | null>(null);
  const importRef = useRef<HTMLInputElement>(null);

  async function loadModels() {
    const m = await api.listModels();
    setModels(m);
    setActiveModelId((prev) =>
      prev && m.some((x) => x.id === prev) ? prev : m.find(x => x.id === readSaved("bl-model", 0))?.id ?? m.find((x) => x.is_default)?.id ?? m[0]?.id ?? null,
    );
  }

  async function loadTrees() {
    const t = await api.listTrees();
    setTrees(t);
    return t;
  }

  async function loadMcp() {
    setMcpServers(await api.listMcp());
  }

  async function loadMemories() {
    setMemories(await api.listMemories());
  }

  useEffect(() => {
    if (settingsOpen) loadMemories().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsOpen]);

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
    loadModels().catch((e) => setErr(String(e)));
    loadMcp().catch((e) => setErr(String(e)));
    loadTrees()
      .then((t) => {
        const saved = readSaved<number | null>("bl-tree", null);
        const initial = t.find(x => x.id === saved) ?? t[0];
        if (initial) selectTree(initial.id, t);
      })
      .catch((e) => setErr(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function selectTree(id: number, list?: Tree[]) {
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
      if (mapTicket === mapRevision.current) setTreeNodes(nodes);
      const tr = (list ?? trees).find(t => t.id === id);
      const saved = readSaved<number | null>(`bl-node:${id}`, null);
      const root = tr?.root_node_id ?? nodes.find(n => n.parent_id == null)?.id;
      const selected = nodes.find(n => n.id === saved)?.id ?? root ?? nodes[0]?.id;
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
    if (treeRef.current === id && ticket === mapRevision.current) setTreeNodes(nodes);
    return nodes;
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

  async function keepRecovery(treeId: number) {
    const backup = await api.exportTree(treeId);
    if (!saveValue('bl-recovery', backup)) throw new Error(t("浏览器空间不足，请先导出备份再删除。", "Browser storage is full. Export a backup before deleting."));
    setRecovery(backup);
  }

  async function deleteTree(id: number) {
    if (streaming) { setErr(t("请先停止当前回答，再删除。", "Stop the current response before deleting.")); return; }
    try {
      await keepRecovery(id);
      await api.deleteTree(id);
      const list = await loadTrees();
      setNotice(t("已删除，整棵树的备份已保留。", "Deleted. A backup of the entire tree is available."));
      if (treeRef.current === id) {
        if (list[0]) await selectTree(list[0].id, list);
        else {
          navigation.current.next(); treeRef.current = null; focusRef.current = null;
          setActiveTreeId(null); setTreeNodes([]); setActiveNodeId(null); setThread([]);
        }
      }
    } catch (e) { setErr(String((e as Error).message ?? e)); }
  }

  async function onAsk(payload: { question: string; images?: string[]; tools?: string[]; deep?: boolean; mode?: 'continue' | 'retry' | 'revise'; nodeId?: number; question_message_id?: number; onAccepted?: (nodeId: number) => void }): Promise<boolean> {
    const fromId = payload.nodeId ?? focusRef.current;
    const treeId = treeRef.current;
    if (!fromId || !treeId || !activeModelId || abortRef.current || busyNavigation) return false;
    const origin = focusRef.current ?? fromId;
    const requestId = crypto.randomUUID();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    requestRef.current = { from: fromId, origin, tree: treeId, requestId };
    streamTargetRef.current = null;
    setPendingQuestion(payload.question); setSendingImages(payload.images ?? []);
    setLive(''); setLiveReasoning(''); setLiveSteps([]);
    setStreaming(true); setStreamFromId(origin); setErr(null);
    let succeeded = false;
    let accepted = false;
    const acceptQuestion = (nodeId: number) => {
      if (accepted) return;
      accepted = true;
      payload.onAccepted?.(nodeId);
    };
    try {
      const meta = await api.ask(fromId, {
        question: payload.question, config_id: activeModelId,
        images: payload.images, tools: payload.tools, deep_think: payload.deep,
        mode: payload.mode, request_id: requestId, question_message_id: payload.question_message_id,
      }, {
        onStart: meta => { streamTargetRef.current = meta.node_id ?? fromId; acceptQuestion(streamTargetRef.current); void refreshTree(treeId).catch(() => {}); },
        onDelta: d => setLive(p => p + d),
        onReasoning: r => setLiveReasoning(p => p + r),
        onToolStart: name => setLiveSteps(s => [...s, { tool: name, result: null }]),
        onToolEnd: (name, result) => setLiveSteps(s => {
          const copy = [...s];
          const index = copy.map(x => x.result === null).lastIndexOf(true);
          if (index >= 0) copy[index] = { tool: name, result };
          return copy;
        }),
      }, ctrl.signal);
      streamTargetRef.current = meta.node_id ?? streamTargetRef.current ?? fromId;
      acceptQuestion(streamTargetRef.current);
      succeeded = true;
    } catch (e: any) {
      if (e?.meta?.node_id && e.meta.request_id === requestId) {
        streamTargetRef.current = e.meta.node_id;
        // A 409 may point to another request already running on this node.
        acceptQuestion(e.meta.node_id);
      }
      if (e?.name !== 'AbortError' && treeRef.current === treeId && focusRef.current === origin)
        setErr(String(e?.message ?? e));
    } finally {
      try {
        await refreshTree(treeId);
        const target = streamTargetRef.current;
        if (target && treeRef.current === treeId && (focusRef.current === origin || focusRef.current === target))
          await selectNode(target);
      } catch (e) {
        if (treeRef.current === treeId) setErr(t("回答已结束，刷新记录失败，请重新选择节点。", "The response ended, but the history could not be refreshed. Select the node again."));
      }
      abortRef.current = null; requestRef.current = null; streamTargetRef.current = null;
      setStreaming(false); setStreamFromId(null); setLive(''); setLiveReasoning('');
      setLiveSteps([]); setPendingQuestion(null); setSendingImages([]);
    }
    return succeeded;
  }

  async function onStop() {
    const req = requestRef.current;
    const ctrl = abortRef.current;
    if (!req || !ctrl) return;
    try { const stopped = await api.stop(streamTargetRef.current ?? req.from, req.requestId); if (stopped.node_id) streamTargetRef.current = stopped.node_id; }
    catch { setErr(t("停止请求未确认，连接已断开；刷新后可查看状态。", "The stop request was not confirmed. The connection is closed; refresh to check its status.")); }
    finally { ctrl.abort(); }
  }

  async function onBranch(seed: string, fromNodeId: number, anchor?: SourceAnchor): Promise<void> {
    const treeId = treeRef.current;
    if (!treeId) return;
    const ticket = navigation.current.next();
    try {
      const child = await api.branch(fromNodeId, seed, anchor);
      await refreshTree(treeId);
      if (treeRef.current === treeId && navigation.current.current(ticket)) await selectNode(child.id);
    } catch (e) { if (treeRef.current === treeId) setErr(String((e as Error).message ?? e)); throw e; }
  }

  async function onDeleteNode(id: number) {
    const treeId = treeRef.current;
    if (!treeId || streaming) return;
    try {
      const parent = treeNodes.find(n => n.id === id)?.parent_id;
      if (parent == null && treeNodes.filter(n => n.parent_id == null).length === 1) { await deleteTree(treeId); return; }
      await keepRecovery(treeId);
      await api.deleteNode(id);
      const nodes = await refreshTree(treeId);
      setNotice(t("已删除节点，整棵树的备份已保留。", "Node deleted. A backup of the entire tree is available."));
      if (treeRef.current !== treeId) return;
      const focus = nodes.find(n => n.id === parent)?.id ?? nodes[0]?.id;
      if (focus) await selectNode(focus);
      else { focusRef.current = null; setActiveNodeId(null); setThread([]); }
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
      setNotice(t("已恢复为一棵新树。", "Restored as a new tree."));
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
      protocol: 'openai', max_tokens: 4096,
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

  async function deleteMemory(id: number) {
    await api.deleteMemory(id);
    await loadMemories();
  }
  async function clearMemories() {
    await api.clearMemories();
    await loadMemories();
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

  return (
    <div className={`app${sidebarOpen ? ' sidebar-visible' : ''}`}>
      <button className="mobile-sidebar-toggle" onClick={() => setSidebarOpen(v => !v)} aria-label={sidebarOpen ? t("关闭主题列表", "Close topic list") : t("打开主题列表", "Open topic list")}>☰</button>
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
        onOpenSettings={() => setSettingsOpen(true)}
        onImport={() => importRef.current?.click()}
        recoveryAvailable={!!recovery}
        onRecover={() => recovery && importTree(recovery)}
      />
      <div className="workspace-main">
      {notice && <div className="notice" role="status"><span>{localizeError(notice, locale)}</span><button onClick={() => setNotice(null)} aria-label={t("关闭提示", "Dismiss notification")}>×</button></div>}
      {busyNavigation && <div className="loading-line" aria-label={t("正在加载", "Loading")} />}
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
        pendingQuestion={pendingQuestion}
        sendingImages={sendingImages}
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
        onBranch={onBranch}
        onDeleteNode={onDeleteNode}
        rightOpen={rightOpen}
        onToggleRight={() => setRightOpen(o => { saveValue("bl-map-open", !o); return !o; })}
        onAddMock={addMock}
        mcpServers={mcpServers.filter((s) => s.enabled)}
        onTestMcp={(id) => api.testMcp(id)}
      />
      </div>
      {rightOpen && <div className="splitter" onMouseDown={startResize} />}
      {rightOpen && (
        <LearningMap
          nodes={treeNodes}
          activeId={activeNodeId}
          onSelect={selectNode}
          onDelete={onDeleteNode}
          onCollapse={() => { setRightOpen(false); saveValue("bl-map-open", false); }}
          width={rightWidth}
          treeKey={activeTreeId ?? "empty"}
        />
      )}
      {settingsOpen && (
        <SettingsModal
          models={models}
          mcpServers={mcpServers}
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
          memories={memories}
          onDeleteMemory={deleteMemory}
          onClearMemories={clearMemories}
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
