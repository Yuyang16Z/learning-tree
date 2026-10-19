import { describe, expect, it } from "vitest";
import type { ModelDiscovery } from "../types";
import { detectedWindow, detectionRequest, sameDetection } from "./modelDiscovery";

const result = (context_window: number | null): ModelDiscovery => ({ context_window, max_output_tokens: null, source: "provider", note: "已从服务商获取该模型的上限。" });

describe("context window detection", () => {
  it("fills in only windows the form accepts", () => {
    expect(detectedWindow(result(1048576))).toEqual({ fill: 1048576, rejected: null });
    expect(detectedWindow(result(8192))).toEqual({ fill: 8192, rejected: null });
    expect(detectedWindow(result(2097152))).toEqual({ fill: 2097152, rejected: null });
  });

  it("reports a detected window outside the accepted range instead of filling it in", () => {
    expect(detectedWindow(result(4096))).toEqual({ fill: null, rejected: 4096 });
    expect(detectedWindow(result(4194304))).toEqual({ fill: null, rejected: 4194304 });
    expect(detectedWindow(result(131072.5))).toEqual({ fill: null, rejected: 131072.5 });
    expect(detectedWindow(result(null))).toEqual({ fill: null, rejected: null });
  });

  it("applies a result only while the form still describes the model it asked about", () => {
    const form = { protocol: "openai" as const, base_url: " https://api.example/v1 ", llm_model: "model-a", api_key: "" };
    const asked = detectionRequest(form);
    expect(sameDetection(asked, detectionRequest({ ...form, base_url: "https://api.example/v1" }))).toBe(true);
    expect(sameDetection(asked, detectionRequest({ ...form, llm_model: "model-b" }))).toBe(false);
    expect(sameDetection(asked, detectionRequest({ ...form, protocol: "anthropic" }))).toBe(false);
    expect(sameDetection(asked, detectionRequest({ ...form, base_url: "https://other.example/v1" }))).toBe(false);
    expect(sameDetection(asked, detectionRequest({ ...form, api_key: "sk-new" }))).toBe(false);
  });
});
