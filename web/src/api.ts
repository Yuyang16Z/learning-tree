import { readSSE } from "./lib/sse";
import { getLocale, translate } from "./i18n";
import { localizeError } from "./i18n/workspace";
import type {
  McpInput,
  McpServer,
  Memory,
  ModelCfg,
  ModelInput,
  NodeDetail,
  ThreadNode,
  Tree,
  TreeNode,
  SourceAnchor,
} from "./types";

const API = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";

async function requestError(res: Response): Promise<Error> {
  let detail: any;
  try { const data = await res.json(); detail = data.detail ?? data; } catch { detail = null; }
  const message = typeof detail === 'string' ? detail : detail?.message;
  return Object.assign(new Error(message ? localizeError(message) : translate("请求失败（{status}），请重试。", "Request failed ({status}). Try again.", { status: res.status })), { meta: typeof detail === 'object' ? detail : undefined });
}
async function j<T>(res: Response): Promise<T> {
  if (!res.ok) throw await requestError(res);
  return (await res.json()) as T;
}

const jsonPost = (url: string, body: unknown) =>
  fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

export interface AskMeta {
  node_id?: number;
  message_id?: number;
  answered_by?: string;
  error?: string;
  status?: string;
  request_id?: string;
}

export interface AskBody {
  question: string;
  config_id: number | null;
  images?: string[];
  tools?: string[];
  deep_think?: boolean;
  mode?: "continue" | "retry" | "revise";
  question_message_id?: number;
  request_id?: string;
}

export interface AskHandlers {
  onStart?: (meta: AskMeta) => void;
  onDelta: (s: string) => void;
  onReasoning?: (s: string) => void;
  onToolStart?: (name: string, args: unknown) => void;
  onToolEnd?: (name: string, result: string) => void;
}

export const api = {
  base: API,

  listModels: () => fetch(`${API}/models`).then(j<ModelCfg[]>),
  addModel: (body: ModelInput) => jsonPost(`${API}/models`, body).then(j<ModelCfg>),
  updateModel: (id: number, body: ModelInput) => fetch(`${API}/models/${id}`, {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }).then(j<ModelCfg>),
  deleteModel: (id: number) => fetch(`${API}/models/${id}`, { method: "DELETE" }).then(j<unknown>),
  setDefaultModel: (id: number) => jsonPost(`${API}/models/${id}/default`, {}).then(j<ModelCfg>),
  testModel: (id: number) =>
    jsonPost(`${API}/models/${id}/test`, {}).then(j<{ ok: boolean; detail: string }>),

  listMcp: () => fetch(`${API}/mcp`).then(j<McpServer[]>),
  addMcp: (body: McpInput) => jsonPost(`${API}/mcp`, body).then(j<McpServer>),
  updateMcp: (id: number, body: McpInput) =>
    fetch(`${API}/mcp/${id}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(j<McpServer>),
  deleteMcp: (id: number) => fetch(`${API}/mcp/${id}`, { method: "DELETE" }).then(j<unknown>),
  toggleMcp: (id: number, enabled: boolean) =>
    fetch(`${API}/mcp/${id}?enabled=${enabled}`, { method: "PATCH" }).then(j<McpServer>),
  testMcp: (id: number) =>
    jsonPost(`${API}/mcp/${id}/test`, {}).then(j<{ ok: boolean; detail: string; tools: string[] }>),

  listMemories: () => fetch(`${API}/memories`).then(j<Memory[]>),
  deleteMemory: (id: number) => fetch(`${API}/memories/${id}`, { method: "DELETE" }).then(j<unknown>),
  clearMemories: () => fetch(`${API}/memories`, { method: "DELETE" }).then(j<unknown>),

  listTrees: () => fetch(`${API}/trees`).then(j<Tree[]>),
  createTree: (title: string, root_question?: string) =>
    jsonPost(`${API}/trees`, { title, root_question: root_question ?? null }).then(j<Tree>),
  getTree: (id: number) => fetch(`${API}/trees/${id}`).then(j<TreeNode[]>),
  deleteTree: (id: number) => fetch(`${API}/trees/${id}`, { method: "DELETE" }).then(j<unknown>),

  getNode: (id: number) => fetch(`${API}/nodes/${id}`).then(j<NodeDetail>),
  getThread: (id: number) =>
    fetch(`${API}/nodes/${id}/thread`).then(j<{ nodes: ThreadNode[] }>).then((r) => r.nodes),
  // Explicit deletion only. Editing creates a separate version through ask(mode=revise).
  deleteNode: (id: number) => fetch(`${API}/nodes/${id}`, { method: "DELETE" }).then(j<unknown>),
  branch: (nodeId: number, seed_text: string, anchor?: SourceAnchor) =>
    jsonPost(`${API}/nodes/${nodeId}/branch`, { seed_text, ...anchor }).then(
      j<{ id: number; parent_id: number; title: string; seed_text: string }>,
    ),

  exportTree: (id: number) => fetch(`${API}/trees/${id}/export`).then(j<Record<string, unknown>>),
  importTree: (body: unknown) => jsonPost(`${API}/trees/import`, body).then(j<Tree>),
  stop: (id: number, request_id?: string) => jsonPost(`${API}/nodes/${id}/stop`, { request_id }).then(j<{node_id: number; status: string}>),
  explain: (id: number, text: string, config_id: number | null, source_message_id?: number | null) =>
    jsonPost(`${API}/nodes/${id}/explain`, { text, config_id, source_message_id, locale: getLocale() }).then(j<{ explanation: string }>),
  saveNote: (id: number, learning_note: string) => fetch(`${API}/nodes/${id}`, {
    method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ learning_note }),
  }).then(j<unknown>),

  // 流式提问：逐段回调（文本 / 思考 / 工具步骤），返回结束时的元信息。signal 可中止。
  async ask(nodeId: number, body: AskBody, on: AskHandlers, signal?: AbortSignal): Promise<AskMeta> {
    const res = await fetch(`${API}/nodes/${nodeId}/ask`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok) throw await requestError(res);
    if (!res.body) throw new Error(localizeError("没有收到回答数据，请重试。"));

    let meta: AskMeta = {};
    let completed = false;
    let failure: string | null = null;
    await readSSE(res.body, obj => {
      if (obj.started) { meta = { ...meta, ...obj }; on.onStart?.(meta); }
      if (typeof obj.delta === 'string') on.onDelta(obj.delta);
      if (typeof obj.reasoning === 'string') on.onReasoning?.(obj.reasoning);
      if (obj.tool_start) on.onToolStart?.(obj.tool_start.name, obj.tool_start.args);
      if (obj.tool_end) on.onToolEnd?.(obj.tool_end.name, obj.tool_end.result);
      if (obj.error) { failure = obj.error; meta = { ...meta, ...obj }; }
      if (obj.done) { completed = true; meta = { ...meta, ...obj }; }
    });
    if (failure) throw Object.assign(new Error(localizeError(failure)), { meta });
    if (!completed) throw Object.assign(new Error(localizeError('连接中断，已保留问题，可重试。')), { meta });
    return meta;
  },
};
