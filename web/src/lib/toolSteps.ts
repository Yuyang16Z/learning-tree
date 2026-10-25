import type { McpServer, ToolConfirmation, ToolConfirmationOutcome, ToolStep } from "../types";
import { managedMcpPreset } from "./chatTools";

type Translate = (zh: string, en: string) => string;

/** Interface names of the app's own tools as [zh, en] pairs. */
const BUILT_IN_TOOLS = new Map<string, [string, string]>([
  ["fetch", ["读取网页", "Read webpage"]],
  ["web_search", ["联网搜索", "Web search"]],
  ["search_available_tools", ["查找可用工具", "Find available tools"]],
  ["read_learning_source", ["回查原文", "Read source passages"]],
  ["search_learning_history", ["搜索学习记录", "Search learning history"]],
  ["read_document_source", ["查阅文档", "Read document"]],
  ["read_tool_result", ["回看工具结果", "Reread tool result"]],
]);

/** A tool step's name: built-in tools and the managed web-research MCP's tools are translated. */
export function toolStepLabel(tool: string, t: Translate, servers: readonly McpServer[] = []): string {
  const builtIn = BUILT_IN_TOOLS.get(tool);
  if (builtIn) return t(...builtIn);
  for (const server of servers) {
    if (managedMcpPreset(server) !== "web-research") continue;
    if (tool === `mcp_${server.id}_web_search`) return t("联网搜索", "Web search");
    if (tool === `mcp_${server.id}_read_webpage`) return t("读取网页", "Read webpage");
  }
  return tool;
}

/** A step's pointer to the complete result stored for its call. */
export type ToolReference = Pick<ToolStep, "call_index" | "truncated" | "request_id" | "node_id" | "confirmation">;
const OUTCOMES = new Set<ToolConfirmationOutcome>(["allowed", "declined", "expired"]);
export interface FullResultSource { nodeId: number; callIndex: number; requestId: string }

/** Read a tool_end event and its stream envelope; malformed fields are dropped. */
export function toolReference(
  event: { call_index?: unknown; truncated?: unknown; confirmation?: unknown },
  envelope: { request_id?: unknown; node_id?: unknown },
): ToolReference {
  const reference: ToolReference = {};
  if (typeof event.truncated === "boolean") reference.truncated = event.truncated;
  if (OUTCOMES.has(event.confirmation as ToolConfirmationOutcome)) reference.confirmation = event.confirmation as ToolConfirmationOutcome;
  if (Number.isInteger(event.call_index) && (event.call_index as number) > 0) {
    reference.call_index = event.call_index as number;
    if (typeof envelope.request_id === "string" && envelope.request_id) reference.request_id = envelope.request_id;
    if (Number.isInteger(envelope.node_id)) reference.node_id = envelope.node_id as number;
  }
  return reference;
}

/** A tool_confirm event, or null when it is malformed. */
export function confirmationRequest(value: unknown): ToolConfirmation | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const { index, name, args } = value as { index?: unknown; name?: unknown; args?: unknown };
  if (!Number.isInteger(index) || (index as number) < 1 || typeof name !== "string" || !name) return null;
  return { index: index as number, name, args: args ?? null };
}

export function startToolStep(steps: readonly ToolStep[], tool: string): ToolStep[] {
  return [...steps, { tool, result: null }];
}

/** Fill the latest running step; calls run one at a time, so it is this call's step. */
export function finishToolStep(steps: readonly ToolStep[], tool: string, result: string, reference: ToolReference = {}): ToolStep[] {
  const next = [...steps];
  const index = next.map(step => step.result === null).lastIndexOf(true);
  if (index >= 0) next[index] = { tool, result, ...reference };
  return next;
}

/** Where a truncated step's complete text can be read, or null when only its preview exists. */
export function fullResultSource(step: ToolStep, nodeId?: number | null): FullResultSource | null {
  const node = step.node_id ?? nodeId;
  if (!step.truncated || !step.call_index || !step.request_id || node == null) return null;
  return { nodeId: node, callIndex: step.call_index, requestId: step.request_id };
}
