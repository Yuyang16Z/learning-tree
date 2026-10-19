import type { ModelDiscovery, ModelProtocol } from "../types";

/** Context windows the model form accepts; the API validates the same range. */
export const CONTEXT_WINDOW_MIN = 8192;
export const CONTEXT_WINDOW_MAX = 2097152;

/** The model a detection asked about. */
export interface DetectionRequest { protocol: ModelProtocol; base_url: string; llm_model: string; api_key: string }

export function detectionRequest(form: DetectionRequest): DetectionRequest {
  return { protocol: form.protocol, base_url: form.base_url.trim(), llm_model: form.llm_model.trim(), api_key: form.api_key.trim() };
}

/** A late result applies only while the form still describes the model it was asked about. */
export function sameDetection(a: DetectionRequest, b: DetectionRequest): boolean {
  return a.protocol === b.protocol && a.base_url === b.base_url && a.llm_model === b.llm_model && a.api_key === b.api_key;
}

/**
 * The window a detection fills in, or the reported value it rejected: providers may report windows
 * the form cannot save (a 4,096-token vLLM max_model_len, or more than the form allows).
 */
export function detectedWindow(result: ModelDiscovery): { fill: number | null; rejected: number | null } {
  const value = result.context_window;
  if (typeof value !== "number") return { fill: null, rejected: null };
  return Number.isInteger(value) && value >= CONTEXT_WINDOW_MIN && value <= CONTEXT_WINDOW_MAX
    ? { fill: value, rejected: null }
    : { fill: null, rejected: value };
}
