import { useId, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { localizeError } from "../i18n/workspace";
import type { McpInput, McpServer } from "../types";
import { parseMcpArgs } from "../lib/mcpArgs";

interface Props {
  server: McpServer | null; // null = add a new server
  onClose: () => void;
  onAdd: (m: McpInput) => Promise<void>;
  onUpdate: (id: number, m: McpInput) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
  onTest: (id: number) => Promise<{ ok: boolean; detail: string; tools: string[] }>;
}

export function McpConfigModal({ server, onClose, onAdd, onUpdate, onDelete, onTest }: Props) {
  const { locale, t } = useI18n();
  const fieldId = useId();
  const [form, setForm] = useState({
    label: server?.label ?? "",
    command: server?.command ?? "",
    args: JSON.stringify(server?.args ?? [], null, 2),
    enabled: server?.enabled ?? true,
  });
  const [busy, setBusy] = useState<"save" | "test" | "delete" | null>(null);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string; tools: string[] } | null>(null);
  const dirty = !!server && (
    form.label !== server.label || form.command !== server.command ||
    form.args !== JSON.stringify(server.args, null, 2) || form.enabled !== server.enabled
  );

  const payload = (): McpInput => ({
    label: form.label.trim(),
    command: form.command.trim(),
    args: parseMcpArgs(form.args),
    enabled: form.enabled,
  });

  function close() {
    if (!busyRef.current) onClose();
  }

  function update(values: Partial<typeof form>) {
    setForm((current) => ({ ...current, ...values }));
    setError(null);
    setTestResult(null);
  }

  async function save() {
    if (busyRef.current) return;
    setError(null);
    if (!form.label.trim() || !form.command.trim()) {
      setError(t("请填写显示名和启动命令。", "Enter a display name and launch command."));
      return;
    }
    busyRef.current = true;
    setBusy("save");
    try {
      if (server) await onUpdate(server.id, payload());
      else await onAdd(payload());
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("保存失败，请重试。", "Could not save. Please try again."));
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  }

  async function test() {
    if (!server || busyRef.current || dirty) return;
    busyRef.current = true;
    setBusy("test");
    setError(null);
    setTestResult(null);
    try {
      setTestResult(await onTest(server.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("连接测试失败，请重试。", "Connection test failed. Please try again."));
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  }

  async function remove() {
    if (!server || busyRef.current) return;
    busyRef.current = true;
    setBusy("delete");
    setError(null);
    try {
      await onDelete(server.id);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("删除失败，请重试。", "Could not delete. Please try again."));
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  }

  return (
    <div className="overlay overlay-top" onClick={close}>
      <div className="modal modal-form" role="dialog" aria-modal="true" aria-labelledby={`${fieldId}-title`} aria-busy={!!busy} onClick={(e) => e.stopPropagation()}>
        <h3 id={`${fieldId}-title`}>{server ? t("MCP 配置", "MCP configuration") : t("添加 MCP", "Add MCP")}</h3>

        <div className="field">
          <label htmlFor={`${fieldId}-label`}>{t("显示名", "Display name")}</label>
          <input id={`${fieldId}-label`} placeholder={t("如 网页读取", "e.g. Web reader")} value={form.label} disabled={!!busy} onChange={(e) => update({ label: e.target.value })} />
        </div>
        <div className="field">
          <label htmlFor={`${fieldId}-command`}>{t("启动命令", "Launch command")}</label>
          <input id={`${fieldId}-command`} placeholder="uvx / npx / python" value={form.command} disabled={!!busy} spellCheck={false} onChange={(e) => update({ command: e.target.value })} />
        </div>
        <div className="field">
          <label htmlFor={`${fieldId}-args`}>{t("启动参数", "Arguments")}</label>
          <textarea id={`${fieldId}-args`} className="mono" rows={Math.min(8, Math.max(3, form.args.split("\n").length))} style={{ width: "100%", resize: "vertical" }} placeholder={'["mcp-server-fetch"]'} value={form.args} disabled={!!busy} spellCheck={false} aria-describedby={`${fieldId}-args-hint`} onChange={(e) => update({ args: e.target.value })} />
          <div id={`${fieldId}-args-hint`} className="hint">{t("JSON 数组；每个参数单独加引号，路径中的空格会保留。", "JSON array. Quote each argument; spaces in paths are preserved.")}</div>
        </div>
        <label className="hint">
          <input type="checkbox" checked={form.enabled} disabled={!!busy} onChange={(e) => update({ enabled: e.target.checked })} style={{ marginRight: 6 }} />
          {t("启用", "Enabled")}
        </label>

        {server && (
          <div style={{ marginTop: 12 }}>
            <button className="btn" onClick={test} disabled={!!busy || dirty}>{busy === "test" ? t("测试中…", "Testing…") : t("测试连接", "Test connection")}</button>
            {dirty && <span className="hint" style={{ marginLeft: 8 }}>{t("保存后可测试更改", "Save changes before testing")}</span>}
            {testResult && (
              <div className={testResult.ok ? "test-ok" : "test-bad"} role="status" style={{ marginTop: 6, overflowWrap: "anywhere" }}>
                {localizeError(testResult.detail, locale)}
                {testResult.tools.length > 0 && <details><summary>{t("{count} 个工具", "{count} tools", { count: testResult.tools.length })}</summary>{testResult.tools.join(" · ")}</details>}
              </div>
            )}
          </div>
        )}

        {error && <div className="test-bad" role="alert" style={{ marginTop: 10, overflowWrap: "anywhere" }}>{localizeError(error, locale)}</div>}

        <div className="modal-foot">
          <div>{server && <button className="btn btn-danger" onClick={remove} disabled={!!busy}>{busy === "delete" ? t("删除中…", "Deleting…") : t("删除", "Delete")}</button>}</div>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn" onClick={close} disabled={!!busy}>{t("取消", "Cancel")}</button>
            <button className="btn btn-primary" onClick={save} disabled={!!busy}>
              {busy === "save" ? t("保存中…", "Saving…") : server ? t("保存", "Save") : t("添加", "Add")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
