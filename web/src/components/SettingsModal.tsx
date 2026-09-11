import { useId, useState } from "react";
import { useI18n } from "../i18n";
import { localizeError, mcpDisplayName } from "../i18n/workspace";
import type { McpServer, Memory, ModelCfg } from "../types";

interface Props {
  models: ModelCfg[];
  mcpServers: McpServer[];
  onClose: () => void;
  onDelete: (id: number) => Promise<void>;
  onTest: (id: number) => Promise<{ ok: boolean; detail: string }>;
  onAddMock: () => Promise<void>;
  onOpenAddModel: () => void;
  onOpenEditModel: (model: ModelCfg) => void;
  onSetDefault: (id: number) => Promise<void>;
  onOpenMcpConfig: (server: McpServer | null) => void;
  memories: Memory[];
  onDeleteMemory: (id: number) => Promise<void>;
  onClearMemories: () => Promise<void>;
  theme: "light" | "dark" | "system";
  onThemeChange: (t: "light" | "dark" | "system") => void;
}

type Tab = "models" | "mcp" | "memory" | "appearance" | "language";

export function SettingsModal(props: Props) {
  const { locale, setLocale, t } = useI18n();
  const titleId = useId();
  const { models, mcpServers, onClose, onDelete, onTest, onAddMock } = props;
  const { onOpenAddModel, onSetDefault, onOpenMcpConfig, theme, onThemeChange } = props;
  const { memories, onDeleteMemory, onClearMemories } = props;

  const [tab, setTab] = useState<Tab>("models");
  const [tests, setTests] = useState<Record<number, { ok: boolean; detail: string }>>({});
  const [testing, setTesting] = useState<number | null>(null);

  async function test(id: number) {
    setTesting(id);
    try { const r = await onTest(id); setTests(previous => ({ ...previous, [id]: r })); }
    catch (cause) { setTests(previous => ({ ...previous, [id]: { ok: false, detail: cause instanceof Error ? cause.message : t("连接测试失败。", "Connection test failed.") } })); }
    finally { setTesting(null); }
  }

  return (
    <div className="overlay" onClick={onClose}>
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
            <button className={tab === "memory" ? "on" : ""} onClick={() => setTab("memory")}>
              {t("记忆", "Memory")}
            </button>
            <button className={tab === "appearance" ? "on" : ""} onClick={() => setTab("appearance")}>
              {t("外观", "Appearance")}
            </button>
            <button className={tab === "language" ? "on" : ""} onClick={() => setTab("language")}>
              语言 / Language
            </button>
          </div>

          <div className="settings-content">
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

            {tab === "memory" && (
              <>
                <div className="sub">
                  {t("AI 会记住你的偏好和话题结论，供相关提问使用。", "AI saves preferences and topic facts for relevant future questions.")}
                </div>
                {memories.length === 0 && (
                  <div className="hint" style={{ marginBottom: 6 }}>{t("还没有记忆，多聊几轮就有了。", "No memories yet. They will appear as you chat.")}</div>
                )}
                {memories.some((m) => m.kind === "preference") && (
                  <div className="section-title" style={{ fontSize: 13 }}>{t("关于你（偏好 / 习惯）", "About you · Preferences")}</div>
                )}
                {memories
                  .filter((m) => m.kind === "preference")
                  .map((m) => (
                    <div className="model-row" key={m.id}>
                      <div className="grow">{m.content}</div>
                      <button className="btn" onClick={() => onDeleteMemory(m.id)}>{t("删除", "Delete")}</button>
                    </div>
                  ))}
                {memories.some((m) => m.kind === "fact") && (
                  <div className="section-title" style={{ fontSize: 13 }}>{t("话题事实", "Topic facts")}</div>
                )}
                {memories
                  .filter((m) => m.kind === "fact")
                  .map((m) => (
                    <div className="model-row" key={m.id}>
                      <div className="grow">{m.content}</div>
                      <button className="btn" onClick={() => onDeleteMemory(m.id)}>{t("删除", "Delete")}</button>
                    </div>
                  ))}
                {memories.length > 0 && (
                  <div className="row-between" style={{ marginTop: 10 }}>
                    <span />
                    <button className="btn btn-danger" onClick={onClearMemories}>{t("全部清空", "Clear all")}</button>
                  </div>
                )}
              </>
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
          <button className="btn" onClick={onClose}>{t("关闭", "Close")}</button>
        </div>
      </div>
    </div>
  );
}
