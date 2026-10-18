import type { ToolStep } from "../types";

/** A step's pointer to the complete result stored for its call. */
export type ToolReference = Pick<ToolStep, "call_index" | "truncated" | "request_id" | "node_id">;
export interface FullResultSource { nodeId: number; callIndex: number; requestId: string }

/** Read a tool_end event and its stream envelope; malformed fields are dropped. */
export function toolReference(
  event: { call_index?: unknown; truncated?: unknown },
  envelope: { request_id?: unknown; node_id?: unknown },
): ToolReference {
  const reference: ToolReference = {};
  if (typeof event.truncated === "boolean") reference.truncated = event.truncated;
  if (Number.isInteger(event.call_index) && (event.call_index as number) > 0) {
    reference.call_index = event.call_index as number;
    if (typeof envelope.request_id === "string" && envelope.request_id) reference.request_id = envelope.request_id;
    if (Number.isInteger(envelope.node_id)) reference.node_id = envelope.node_id as number;
  }
  return reference;
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
