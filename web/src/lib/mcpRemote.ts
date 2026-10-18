import { translate } from "../i18n";
import type { McpServer } from "../types";

/** One editable header. `original` names a header the server already stores; its value is never sent back. */
export interface McpHeaderRow { name: string; value: string; original?: string }

// Set by the transport itself; mirrors RESERVED_MCP_HEADERS in app/schemas.py.
const RESERVED = new Set([
  "accept", "connection", "content-length", "content-type", "host", "keep-alive", "last-event-id",
  "mcp-protocol-version", "mcp-session-id", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
]);
const TOKEN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,100}$/;

export function headerRows(names: readonly string[] | undefined): McpHeaderRow[] {
  return (names ?? []).map(name => ({ name, value: "", original: name }));
}

const blank = (row: McpHeaderRow) => row.original === undefined && !row.name.trim() && !row.value;

/** True when the rows no longer describe exactly the stored headers with their stored values. */
export function headersChanged(rows: readonly McpHeaderRow[], names: readonly string[] | undefined): boolean {
  const current = rows.filter(row => !blank(row));
  const stored = names ?? [];
  return current.length !== stored.length
    || current.some((row, index) => row.original !== stored[index] || row.name !== row.original || row.value !== "");
}

/**
 * Build the request's headers. A blank value keeps the stored value of the same header
 * (the server merges it); untouched empty rows are ignored. Throws a localized Error.
 */
export function headerPayload(rows: readonly McpHeaderRow[]): Record<string, string> {
  const result: Record<string, string> = {};
  const seen = new Set<string>();
  for (const row of rows) {
    if (blank(row)) continue;
    const name = row.name.trim();
    const key = name.toLowerCase();
    if (!TOKEN.test(name)) throw new Error(translate("请求头名称只能包含字母、数字和 - 等符号。", "Header names may contain only letters, digits and symbols such as -."));
    if (RESERVED.has(key)) throw new Error(translate("该请求头由连接自动设置，不能自定义。", "This header is set by the connection and cannot be customized."));
    if (seen.has(key)) throw new Error(translate("请求头名称重复。", "Duplicate header name."));
    if (/[\r\n\0]/.test(row.value)) throw new Error(translate("请求头的值不能包含换行。", "Header values cannot contain line breaks."));
    // Only an unrenamed stored header may keep its value; anything else needs one.
    if (!row.value && row.original?.toLowerCase() !== key) throw new Error(translate("新的请求头需要填写值。", "New headers need a value."));
    seen.add(key);
    result[name] = row.value;
  }
  return result;
}

/** Validate a remote server address the way the backend does; returns the trimmed URL. */
export function remoteUrl(text: string): string {
  const value = text.trim();
  let parsed: URL | null = null;
  try { parsed = new URL(value); } catch { /* Reported below. */ }
  if (!parsed || (parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname) {
    throw new Error(translate("请填写以 http:// 或 https:// 开头的服务地址。", "Enter a server URL starting with http:// or https://."));
  }
  if (parsed.username || parsed.password) throw new Error(translate("请把凭据放在请求头中，不要写在地址里。", "Put credentials in headers, not in the URL."));
  return value;
}

/** What Settings shows beside a server: the remote host, or the launch command's file name. */
export function mcpTarget(server: McpServer): string {
  if (server.transport === "http") {
    try { return new URL(server.url ?? "").host; } catch { return server.url ?? ""; }
  }
  return (server.command ?? "").split(/[\\/]/).pop() ?? "";
}
