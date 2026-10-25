import { useId, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { localizeError } from "../i18n/workspace";
import type { McpConfirmPolicy, McpInput, McpServer, McpTransport } from "../types";
import { parseMcpArgs } from "../lib/mcpArgs";
import { headerPayload, headerRows, headersChanged, remoteUrl, type McpHeaderRow } from "../lib/mcpRemote";

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
    transport: (server?.transport ?? "stdio") as McpTransport,
    command: server?.command ?? "",
    args: JSON.stringify(server?.args ?? [], null, 2),
    url: server?.url ?? "",
    headers: headerRows(server?.header_names),
    allowPrivate: server?.allow_private ?? false,
    enabled: server?.enabled ?? true,
    confirmTools: (server?.confirm_tools ?? "writes") as McpConfirmPolicy,
  });
  const [busy, setBusy] = useState<"save" | "test" | "delete" | null>(null);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string; tools: string[] } | null>(null);
  const remote = form.transport === "http";
  const dirty = !!server && (
    form.label !== server.label || form.transport !== (server.transport ?? "stdio") ||
    form.command !== server.command || form.args !== JSON.stringify(server.args ?? [], null, 2) ||
    form.url !== (server.url ?? "") || form.allowPrivate !== (server.allow_private ?? false) ||
    headersChanged(form.headers, server.header_names) || form.enabled !== server.enabled ||
    form.confirmTools !== (server.confirm_tools ?? "writes")
  );

  const payload = (): McpInput => remote
    ? {
      label: form.label.trim(), transport: "http", command: "", args: [], url: remoteUrl(form.url),
      headers: headerPayload(form.headers), allow_private: form.allowPrivate, enabled: form.enabled,
      confirm_tools: form.confirmTools,
    }
    : { label: form.label.trim(), transport: "stdio", command: form.command.trim(), args: parseMcpArgs(form.args), enabled: form.enabled, confirm_tools: form.confirmTools };

  function close() {
    if (!busyRef.current) onClose();
  }

  function update(values: Partial<typeof form>) {
    setForm((current) => ({ ...current, ...values }));
    setError(null);
    setTestResult(null);
  }

  function updateHeader(index: number, values: Partial<McpHeaderRow>) {
    update({ headers: form.headers.map((row, i) => (i === index ? { ...row, ...values } : row)) });
  }

  async function save() {
    if (busyRef.current) return;
    setError(null);
    if (!form.label.trim() || (remote ? !form.url.trim() : !form.command.trim())) {
      setError(remote
        ? t("请填写显示名和服务地址。", "Enter a display name and server URL.")
        : t("请填写显示名和启动命令。", "Enter a display name and launch command."));
      return;
    }
    let body: McpInput;
    try {
      body = payload();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("保存失败，请重试。", "Could not save. Please try again."));
      return;
    }
    busyRef.current = true;
    setBusy("save");
    try {
      if (server) await onUpdate(server.id, body);
      else await onAdd(body);
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
      <div className="modal modal-form mcp-config-form" role="dialog" aria-modal="true" aria-labelledby={`${fieldId}-title`} aria-busy={!!busy} onClick={(e) => e.stopPropagation()}>
        <h3 id={`${fieldId}-title`}>{server ? t("MCP 配置", "MCP configuration") : t("添加 MCP", "Add MCP")}</h3>

        <div className="field">
          <label htmlFor={`${fieldId}-label`}>{t("显示名", "Display name")}</label>
          <input id={`${fieldId}-label`} placeholder={t("如 网页读取", "e.g. Web reader")} value={form.label} disabled={!!busy} onChange={(e) => update({ label: e.target.value })} />
        </div>
        <fieldset className="mcp-transport" disabled={!!busy}>
          <legend>{t("连接方式", "Connection")}</legend>
          <label><input type="radio" name={`${fieldId}-transport`} checked={!remote} onChange={() => update({ transport: "stdio" })} />{t("本机命令", "Local command")}</label>
          <label><input type="radio" name={`${fieldId}-transport`} checked={remote} onChange={() => update({ transport: "http" })} />{t("远程 HTTP", "Remote HTTP")}</label>
        </fieldset>
        {!remote && <>
          <div className="field">
            <label htmlFor={`${fieldId}-command`}>{t("启动命令", "Launch command")}</label>
            <input id={`${fieldId}-command`} placeholder="uvx / npx / python" value={form.command} disabled={!!busy} spellCheck={false} onChange={(e) => update({ command: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor={`${fieldId}-args`}>{t("启动参数", "Arguments")}</label>
            <textarea id={`${fieldId}-args`} className="mono" rows={Math.min(8, Math.max(3, form.args.split("\n").length))} style={{ width: "100%", resize: "vertical" }} placeholder={'["mcp-server-fetch"]'} value={form.args} disabled={!!busy} spellCheck={false} aria-describedby={`${fieldId}-args-hint`} onChange={(e) => update({ args: e.target.value })} />
            <div id={`${fieldId}-args-hint`} className="hint">{t("JSON 数组；每个参数单独加引号，路径中的空格会保留。", "JSON array. Quote each argument; spaces in paths are preserved.")}</div>
          </div>
        </>}
        {remote && <>
          <div className="field">
            <label htmlFor={`${fieldId}-url`}>{t("服务地址", "Server URL")}</label>
            <input id={`${fieldId}-url`} type="url" inputMode="url" placeholder="https://example.com/mcp" value={form.url} disabled={!!busy} spellCheck={false} autoComplete="off" onChange={(e) => update({ url: e.target.value })} />
          </div>
          <div className="field" role="group" aria-labelledby={`${fieldId}-headers`}>
            <div id={`${fieldId}-headers`} className="mcp-field-title">{t("请求头", "Headers")}</div>
            {form.headers.map((row, index) => (
              <div className="mcp-header-row" key={index}>
                <input aria-label={t("请求头名称", "Header name")} placeholder="Authorization" value={row.name} disabled={!!busy} spellCheck={false} autoComplete="off" onChange={(e) => updateHeader(index, { name: e.target.value })} />
                <input type="password" aria-label={t("请求头的值", "Header value")} placeholder={row.original !== undefined ? t("留空则保持原值", "Leave blank to keep") : t("值", "Value")} value={row.value} disabled={!!busy} spellCheck={false} autoComplete="new-password" onChange={(e) => updateHeader(index, { value: e.target.value })} />
                <button type="button" className="btn" aria-label={t("删除请求头", "Remove header")} title={t("删除请求头", "Remove header")} disabled={!!busy} onClick={() => update({ headers: form.headers.filter((_, i) => i !== index) })}>×</button>
              </div>
            ))}
            <button type="button" className="btn" disabled={!!busy || form.headers.length >= 20} onClick={() => update({ headers: [...form.headers, { name: "", value: "" }] })}>{t("＋ 添加请求头", "＋ Add header")}</button>
            <div className="hint">{t("例如 Authorization: Bearer …。值只保存在这台电脑的数据库里，保存后不再显示。", "For example Authorization: Bearer …. Values stay in this computer's database and are not shown again after saving.")}</div>
          </div>
          <label className="hint mcp-private">
            <input type="checkbox" checked={form.allowPrivate} disabled={!!busy} onChange={(e) => update({ allowPrivate: e.target.checked })} />
            {t("允许内网地址", "Allow private addresses")}
          </label>
          {form.allowPrivate && <div className="hint mcp-warning" role="note">{t("仅用于你信任的本机开发服务：开启后可连接本机、局域网和 Tailscale 地址，学习树会把问题相关内容发送给它。", "Only for a development server you trust on this computer: this allows loopback, local-network and Tailscale addresses, and LearningTree sends it question-related content.")}</div>}
        </>}
        <div className="field">
          <label htmlFor={`${fieldId}-confirm`}>{t("运行工具前确认", "Ask before running tools")}</label>
          <select id={`${fieldId}-confirm`} value={form.confirmTools} disabled={!!busy} onChange={(e) => update({ confirmTools: e.target.value as McpConfirmPolicy })}>
            <option value="writes">{t("服务未标为只读的工具先问我", "Ask for tools the server does not mark read-only")}</option>
            <option value="always">{t("每次都先问我", "Always ask")}</option>
            <option value="never">{t("直接运行，不问我", "Never ask")}</option>
          </select>
          <div className="hint">{t("需要确认时，回答会暂停并显示要运行的工具和参数；不允许或超时（默认 2 分钟）没有回应时，这次调用不会执行。", "When approval is needed, the answer pauses and shows the tool and its arguments; if you decline, or do not answer in time (2 minutes by default), that call does not run.")}</div>
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
