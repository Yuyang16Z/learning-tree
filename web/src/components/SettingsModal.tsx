import { useEffect, useId, useState } from "react";
import { api } from "../api";
import { useI18n } from "../i18n";
import { localizeError, mcpDisplayName } from "../i18n/workspace";
import type { McpServer, MemoryRetrievalStatus, ModelCfg, Tree } from "../types";
import { MemoryPanel } from "./MemoryPanel";
import { TopicList } from "./TopicList";

interface Props {
  models: ModelCfg[];
  mcpServers: McpServer[];
  trees: Tree[];
  activeTreeId: number | null;
  onOpenTree: (id: number) => void;
  onDeleteTree: (id: number) => void;
  onRenameTree: (id: number, title: string) => Promise<void>;
  onArchiveTree: (id: number, archived: boolean) => Promise<void>;
  onClose: () => void;
  onDelete: (id: number) => Promise<void>;
  onTest: (id: number) => Promise<{ ok: boolean; detail: string }>;
  onAddMock: () => Promise<void>;
  onOpenAddModel: () => void;
  onOpenEditModel: (model: ModelCfg) => void;
  onSetDefault: (id: number) => Promise<void>;
  onOpenMcpConfig: (server: McpServer | null) => void;
  onOpenMemorySource: (treeId: number, nodeId: number | null) => void;
  theme: "light" | "dark" | "system";
  onThemeChange: (t: "light" | "dark" | "system") => void;
}

type Tab = "models" | "mcp" | "memory" | "archived" | "appearance" | "language";

function MemoryRetrievalPanel() {
  const { t } = useI18n();
  const [status, setStatus] = useState<MemoryRetrievalStatus | null>(null);
  const [action, setAction] = useState({ kind: "status", revision: 0 });
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [pollingPaused, setPollingPaused] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let polls = 0;
    setLoading(true);
    setFailed(false);
    setPollingPaused(false);

    async function load(first = false) {
      try {
        const next = first && action.kind === "prepare"
          ? await api.prepareMemoryRetrieval(controller.signal)
          : await api.memoryRetrievalStatus(controller.signal);
        if (controller.signal.aborted) return;
        setStatus(next);
        setFailed(false);
        setLoading(false);
        // Preparation may be slow on the first download. Limit background
        // polling; closing or switching tabs also cancels in-flight requests.
        if (next.state === "preparing") {
          if (polls++ < 12) timer = setTimeout(() => void load(), 2500);
          else setPollingPaused(true);
        }
      } catch {
        if (controller.signal.aborted) return;
        setFailed(true);
        setLoading(false);
      }
    }

    void load(true);
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [action]);

  const summary = failed
    ? t("暂时无法读取检索状态。", "Retrieval status is temporarily unavailable.")
    : !status
      ? t("正在读取检索状态…", "Checking retrieval status…")
      : status.state === "ready"
        ? t("本地语义检索已就绪", "Local semantic retrieval is ready")
        : status.state === "preparing"
          ? t("正在准备本地检索模型…", "Preparing local retrieval models…")
          : status.state === "disabled"
            ? t("语义检索已停用，当前使用关键词检索", "Semantic retrieval is disabled; keyword retrieval is active")
            : status.state === "not_installed"
              ? t("当前使用轻量关键词检索", "Lightweight keyword retrieval is active")
              : status.embedding_ready
              ? t("语义检索已启用，精排暂不可用", "Semantic retrieval is active; reranking is temporarily unavailable")
              : t("语义检索暂不可用，当前使用关键词检索", "Semantic retrieval is temporarily unavailable; keyword retrieval is active");

  return (
    <div className="model-row model-config-row" data-memory-retrieval-state={failed ? "error" : status?.state ?? "loading"}>
      <div className="grow">
        <div>{t("记忆检索", "Memory retrieval")}</div>
        <div className="k" role="status" aria-live="polite">{summary}</div>
        <div className="k">{t("按当前问题查找相关记忆，话题内容仅来自当前学习路径。", "Finds memories relevant to your question, with topic content limited to the current learning path.")}</div>
        {status?.state === "not_installed" && (
          <div className="k">
            {t("语义检索为可选增强。在项目目录运行以下命令安装，然后重启应用：", "Semantic retrieval is optional. Run this in the project directory, then restart the app:")}
            <div style={{ overflowWrap: "anywhere" }}><code>uv run python scripts/manage.py retrieval</code></div>
            <a href="https://github.com/Yuyang16Z/learning-tree/blob/main/docs/operations.md" target="_blank" rel="noopener noreferrer">{t("安装说明与资源需求", "Installation and resource requirements")}</a>
          </div>
        )}
        {status?.state === "degraded" && (
          <div className="k">{t("首次准备需要联网下载模型；检索模型在本机运行，无需额外 API key。", "First-time preparation downloads models. Retrieval runs locally without an extra API key.")}</div>
        )}
        {pollingPaused && !failed && (
          <div className="k">{t("仍在后台准备，可稍后刷新查看。", "Preparation continues in the background. Refresh again shortly.")}</div>
        )}
      </div>
      <div className="model-row-actions">
        {status?.state === "degraded" && !failed && (
          <button className="btn" disabled={loading} onClick={() => setAction(previous => ({ kind: "prepare", revision: previous.revision + 1 }))}>
            {loading ? t("准备中…", "Preparing…") : t("准备 / 重试", "Prepare / retry")}
          </button>
        )}
        <button className="btn" disabled={loading} onClick={() => setAction(previous => ({ kind: "status", revision: previous.revision + 1 }))}>
          {t("刷新状态", "Refresh status")}
        </button>
      </div>
    </div>
  );
}

export function SettingsModal(props: Props) {
  const { locale, setLocale, t } = useI18n();
  const titleId = useId();
  const { models, mcpServers, onClose, onDelete, onTest, onAddMock } = props;
  const { onOpenAddModel, onSetDefault, onOpenMcpConfig, theme, onThemeChange } = props;

  const [tab, setTab] = useState<Tab>("models");
  const [memoryVisited, setMemoryVisited] = useState(false);
  const [memoryDirty, setMemoryDirty] = useState(false);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [tests, setTests] = useState<Record<number, { ok: boolean; detail: string }>>({});
  const [testing, setTesting] = useState<number | null>(null);

  useEffect(() => {
    if (!memoryDirty && !memoryBusy) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [memoryDirty, memoryBusy]);

  function canClose() {
    if (memoryBusy) return false;
    return !memoryDirty || window.confirm(t("记忆还有未保存的修改，放弃修改并关闭设置吗？", "You have unsaved memory changes. Discard them and close settings?"));
  }

  function close() { if (canClose()) onClose(); }

  async function test(id: number) {
    setTesting(id);
    try { const r = await onTest(id); setTests(previous => ({ ...previous, [id]: r })); }
    catch (cause) { setTests(previous => ({ ...previous, [id]: { ok: false, detail: cause instanceof Error ? cause.message : t("连接测试失败。", "Connection test failed.") } })); }
    finally { setTesting(null); }
  }

  return (
    <div className="overlay" onClick={close}>
      <div className="modal modal-settings" role="dialog" aria-modal="true" aria-labelledby={titleId} onClick={(e) => e.stopPropagation()}>
        <h3 id={titleId}>{t("设置", "Settings")}</h3>

        <div className="settings-body">
          <div className="settings-nav">
            <button className={tab === "models" ? "on" : ""} onClick={() => setTab("models")}>
              {t("模型与 API key", "Models & API keys")}
            </button>
            <button className={tab === "mcp" ? "on" : ""} onClick={() => setTab("mcp")}>
              {t("MCP 工具", "MCP tools")}
            </button>
            <button className={tab === "memory" ? "on" : ""} onClick={() => { setMemoryVisited(true); setTab("memory"); }}>
              {t("记忆", "Memory")}
            </button>
            <button className={tab === "archived" ? "on" : ""} onClick={() => setTab("archived")}>
              {t("已归档", "Archived")}
            </button>
            <button className={tab === "appearance" ? "on" : ""} onClick={() => setTab("appearance")}>
              {t("外观", "Appearance")}
            </button>
            <button className={tab === "language" ? "on" : ""} onClick={() => setTab("language")}>
              语言 / Language
            </button>
          </div>

          <div className="settings-content">
            {tab === "archived" && (
              <TopicList
                trees={props.trees.filter(tree => tree.archived)}
                activeTreeId={props.activeTreeId}
                ariaLabel={t("已归档主题", "Archived topics")}
                emptyLabel={t("还没有归档的主题。", "No archived topics yet.")}
                onSelect={id => { if (canClose()) { onClose(); props.onOpenTree(id); } }}
                onDelete={props.onDeleteTree}
                onRename={props.onRenameTree}
                onArchive={props.onArchiveTree}
              />
            )}
            {tab === "models" && (
              <>
                <div className="sub">{t("支持 OpenAI 兼容与 Anthropic 原生格式。", "Supports OpenAI-compatible and native Anthropic APIs.")}</div>

                {models.length === 0 && (
                  <div className="hint" style={{ marginBottom: 10 }}>
                    {t("还没有模型。", "No models yet.")} <button className="btn" onClick={onAddMock}>{t("试用演示模型", "Try a demo model")}</button>
                  </div>
                )}

                {models.map((m) => (
                  <div className="model-row model-config-row" key={m.id}>
                    <div className="grow">
                      <div>
                        {m.label} {m.is_default && <span className="tag">{t("默认", "Default")}</span>}
                      </div>
                      <div className="k">
                        {m.protocol === 'anthropic' ? 'Anthropic' : t('OpenAI 兼容', 'OpenAI compatible')} · {m.llm_model}
                      </div>
                      {tests[m.id] && (
                        <div className={tests[m.id].ok ? "test-ok" : "test-bad"}>{localizeError(tests[m.id].detail, locale)}</div>
                      )}
                    </div>
                    <div className="model-row-actions">{!m.is_default && (
                      <button className="btn" onClick={() => onSetDefault(m.id)}>{t("设为默认", "Set as default")}</button>
                    )}
                    <button className="btn" onClick={() => props.onOpenEditModel(m)}>{t("编辑", "Edit")}</button>
                    <button className="btn" onClick={() => void test(m.id)} disabled={testing != null}>{testing === m.id ? t("测试中…", "Testing…") : t("测试", "Test")}</button>
                    <button className="btn" onClick={() => onDelete(m.id)}>{t("删除", "Delete")}</button>
                    </div>
                  </div>
                ))}

                <div className="row-between">
                  <span />
                  <button className="btn" onClick={onOpenAddModel}>{t("＋ 添加模型", "＋ Add model")}</button>
                </div>
              </>
            )}

            {tab === "mcp" && (
              <>
                <div className="sub">{t("启用后可在输入框的工具菜单中选用。", "Enable a server, then select it in the message tool menu.")}</div>

                {mcpServers.length === 0 && <div className="hint" style={{ marginBottom: 6 }}>{t("还没有 MCP。", "No MCP servers yet.")}</div>}

                {mcpServers.map((s) => (
                  <div className="config-row" key={s.id} role="button" tabIndex={0} aria-label={t("{name}，{status}，编辑配置", "{name}, {status}, edit configuration", { name: mcpDisplayName(s, locale), status: s.enabled ? t("已启用", "enabled") : t("已停用", "disabled") })} onClick={() => onOpenMcpConfig(s)} onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onOpenMcpConfig(s);
                    }
                  }}>
                    <span className={"dot " + (s.enabled ? "dot-on" : "dot-off")} />
                    <span className="grow">{mcpDisplayName(s, locale)}</span>
                    <span className="k mono">{s.command.split(/[\\/]/).pop()}</span>
                    <span className="chevron">›</span>
                  </div>
                ))}

                <div className="row-between">
                  <span />
                  <button className="btn" onClick={() => onOpenMcpConfig(null)}>{t("＋ 添加 MCP", "＋ Add MCP")}</button>
                </div>
              </>
            )}

            {memoryVisited && (
              <div hidden={tab !== "memory"}>
                <MemoryPanel onDirtyChange={setMemoryDirty} onBusyChange={setMemoryBusy} onOpenSource={(treeId, nodeId) => {
                  if (canClose()) { onClose(); props.onOpenMemorySource(treeId, nodeId); }
                }} />
                {tab === "memory" && <div className="memory-retrieval-footer"><MemoryRetrievalPanel /></div>}
              </div>
            )}

            {tab === "language" && (
              <>
                <div className="sub">{t("选择界面语言。", "Choose your interface language.")}</div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }} role="group" aria-label={t("界面语言", "Interface language")}>
                  <button className={"chip" + (locale === "zh-CN" ? " on" : "")} lang="zh-CN" aria-pressed={locale === "zh-CN"} onClick={() => setLocale("zh-CN")}>中文</button>
                  <button className={"chip" + (locale === "en" ? " on" : "")} lang="en" aria-pressed={locale === "en"} onClick={() => setLocale("en")}>English</button>
                </div>
              </>
            )}

            {tab === "appearance" && (
              <>
                <div className="sub">{t("选择界面主题。", "Choose your theme.")}</div>
                <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                  {(["light", "dark", "system"] as const).map((themeOption) => (
                    <button
                      key={themeOption}
                      className={"chip" + (theme === themeOption ? " on" : "")}
                      aria-pressed={theme === themeOption}
                      onClick={() => onThemeChange(themeOption)}
                    >
                      {themeOption === "light" ? t("浅色", "Light") : themeOption === "dark" ? t("深色", "Dark") : t("跟随系统", "System")}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>

        <div className="row-between" style={{ marginTop: 14 }}>
          <span />
          <button className="btn" disabled={memoryBusy} onClick={close}>{t("关闭", "Close")}</button>
        </div>
      </div>
    </div>
  );
}
