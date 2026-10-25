import { useId, useRef, useState } from "react";
import { api } from "../api";
import { useI18n } from "../i18n";
import { localizeError } from "../i18n/workspace";
import { CONTEXT_WINDOW_MAX, CONTEXT_WINDOW_MIN, TARGET_INPUT_MIN, detectedWindow, detectionRequest, effectiveTarget, parseTargetInput, sameDetection, validTargetInput } from "../lib/modelDiscovery";
import type { ModelCfg, ModelDiscovery, ModelInput, ModelProtocol } from "../types";

interface Props {
  isFirst: boolean;
  model?: ModelCfg | null;
  onClose: () => void;
  onAdd: (m: ModelInput) => Promise<void>;
}

const defaultUrls = { openai: "https://api.deepseek.com/v1", anthropic: "https://api.anthropic.com" };

export function AddModelModal({ isFirst, model, onClose, onAdd }: Props) {
  const { locale, t } = useI18n();
  const id = useId();
  const [form, setForm] = useState<ModelInput>({
    label: model?.label ?? "",
    protocol: model?.protocol ?? "openai",
    base_url: model?.base_url ?? defaultUrls.openai,
    llm_model: model?.llm_model ?? "deepseek-chat",
    api_key: "",
    max_tokens: model?.max_tokens ?? 4096,
    context_window: model?.context_window ?? 32768,
    target_input_tokens: model?.target_input_tokens ?? null,
    is_default: model?.is_default ?? isFirst,
    use_for_summaries: model?.use_for_summaries ?? false,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [detected, setDetected] = useState<{ result: ModelDiscovery; rejected: number | null } | null>(null);
  const formRef = useRef(form);
  formRef.current = form;
  const close = () => { if (!busy) onClose(); };
  // Only on request: asks the provider (or the approximate catalog) and fills the window field.
  // The field and Save wait meanwhile; a result for a model the form no longer describes is dropped.
  async function detect() {
    if (detecting || busy) return;
    const asked = detectionRequest(form);
    setDetecting(true);
    setDetected(null);
    setError(null);
    try {
      const result = await api.discoverModel({ ...asked, config_id: model?.id });
      if (!sameDetection(asked, detectionRequest(formRef.current))) return;
      const { fill, rejected } = detectedWindow(result);
      if (fill !== null) setForm(current => ({ ...current, context_window: fill }));
      setDetected({ result, rejected });
    } catch (cause) {
      if (sameDetection(asked, detectionRequest(formRef.current))) setError(cause instanceof Error ? cause.message : t("连接测试失败，请重试。", "Connection test failed. Please try again."));
    }
    finally { setDetecting(false); }
  }
  const set = (key: "label" | "base_url" | "llm_model" | "api_key") =>
    (event: React.ChangeEvent<HTMLInputElement>) => {
      setForm(current => ({ ...current, [key]: event.target.value }));
      // A detection note only describes the model it asked about.
      if (key !== "label") setDetected(null);
    };

  function changeProtocol(protocol: ModelProtocol) {
    setDetected(null);
    setForm(current => ({
      ...current, protocol,
      base_url: Object.values(defaultUrls).includes(current.base_url) ? defaultUrls[protocol] : current.base_url,
      llm_model: current.llm_model === "deepseek-chat" && protocol === "anthropic" ? "" : current.llm_model,
    }));
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (busy || detecting) return;
    setError(null);
    const payload = { ...form, label: form.label.trim(), base_url: form.base_url.trim(), llm_model: form.llm_model.trim(), api_key: form.api_key.trim() };
    if (!payload.label || !payload.base_url || !payload.llm_model || (!model && !payload.api_key)) {
      setError(t("请填写显示名、模型 ID、API 地址和密钥。", "Enter a display name, model ID, API URL, and key.")); return;
    }
    try {
      const url = new URL(payload.base_url);
      if (!["http:", "https:"].includes(url.protocol)) throw new Error();
    } catch { setError(t("API 地址需要以 https:// 或 http:// 开头。", "The API URL must start with https:// or http://.")); return; }
    if (!Number.isInteger(payload.max_tokens) || payload.max_tokens < 1 || payload.max_tokens > 131072) {
      setError(payload.protocol === "anthropic"
        ? t("最大输出长度应为 1–131072 之间的整数。", "Max output tokens must be an integer between 1 and 131072.")
        : t("回答预留应为 1–131072 之间的整数。", "Answer reserve must be an integer between 1 and 131072.")); return;
    }
    if (!Number.isInteger(payload.context_window) || payload.context_window < CONTEXT_WINDOW_MIN || payload.context_window > CONTEXT_WINDOW_MAX) {
      setError(t("上下文窗口应为 8192–2097152 之间的整数。", "Context window must be an integer between 8192 and 2097152.")); return;
    }
    if (payload.context_window - payload.max_tokens - Math.max(512, Math.floor(payload.context_window / 20)) < 1024) {
      setError(t("扣除回答预留和预算余量后，须至少保留 1024 的输入空间。", "Context window must exceed the answer reserve plus headroom by at least 1024.")); return;
    }
    if (!validTargetInput(payload.target_input_tokens, payload.context_window)) {
      setError(t("单次请求目标应为 16384 到上下文窗口之间的整数；留空则自动。", "The request size target must be an integer between 16384 and the context window; leave it blank for automatic.")); return;
    }
    setBusy(true);
    try { await onAdd(payload); onClose(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : t("保存失败，请重试。", "Could not save. Please try again.")); }
    finally { setBusy(false); }
  }

  return <div className="overlay overlay-top" onClick={close}>
    <form className="modal modal-form model-config-form" role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} onSubmit={save} onClick={event => event.stopPropagation()} onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); close(); } }}>
      <h3 id={`${id}-title`}>{model ? t("编辑模型", "Edit model") : t("添加模型", "Add model")}</h3>
      <div className="field">
        <label htmlFor={`${id}-protocol`}>{t("API 格式", "API format")}</label>
        <select id={`${id}-protocol`} value={form.protocol} onChange={event => changeProtocol(event.target.value as ModelProtocol)} disabled={busy}>
          <option value="openai">{t("OpenAI 兼容 · Chat Completions", "OpenAI compatible · Chat Completions")}</option>
          <option value="anthropic">Anthropic · Messages</option>
        </select>
      </div>
      <div className="field"><label htmlFor={`${id}-label`}>{t("显示名", "Display name")}</label><input autoFocus id={`${id}-label`} placeholder={t("给这个模型起个名字", "Give this model a name")} value={form.label} onChange={set("label")} disabled={busy} required /></div>
      <div className="field"><label htmlFor={`${id}-model`}>{t("模型 ID", "Model ID")}</label><input id={`${id}-model`} placeholder={t("填写服务商提供的模型 ID", "Model ID from your provider")} value={form.llm_model} onChange={set("llm_model")} disabled={busy} required spellCheck={false} /></div>
      <div className="field"><label htmlFor={`${id}-url`}>{t("API 地址", "API URL")}</label><input id={`${id}-url`} type="url" placeholder={defaultUrls[form.protocol]} value={form.base_url} onChange={set("base_url")} disabled={busy} required spellCheck={false} /></div>
      <div className="field"><label htmlFor={`${id}-key`}>API Key</label><input id={`${id}-key`} type="password" autoComplete="new-password" placeholder={model ? t("留空保留当前密钥 {hint}", "Leave blank to keep the current key {hint}", { hint: model.key_hint }) : t("输入密钥", "Enter your API key")} value={form.api_key} onChange={set("api_key")} disabled={busy} required={!model} /></div>
      <details className="model-advanced">
        <summary>{t("高级设置", "Advanced")}</summary>
        <div className="field">
          <label htmlFor={`${id}-context`}>{t("上下文窗口（tokens）", "Context window (tokens)")}</label>
          <div className="model-detect-row"><input id={`${id}-context`} type="number" min={CONTEXT_WINDOW_MIN} max={CONTEXT_WINDOW_MAX} step={1} value={form.context_window || ""} onChange={event => setForm(current => ({ ...current, context_window: Number(event.target.value) }))} disabled={busy || detecting} /><button type="button" className="btn" onClick={() => void detect()} disabled={busy || detecting || !form.base_url.trim() || !form.llm_model.trim()} title={t("向服务商查询该模型的上下文窗口；查不到时使用内置近似目录", "Ask the provider for this model's context window; falls back to an approximate built-in catalog")}>{detecting ? t("检测中…", "Detecting…") : t("检测", "Detect")}</button></div>
          <div className="hint">{t("按服务商提供的模型上下文容量填写。", "Use the model context capacity specified by your provider.")}</div>
          {detected && <div className={detected.result.source === "unknown" || detected.rejected !== null ? "test-bad" : "test-ok"} role="status">{localizeError(detected.result.note, locale)}{detected.result.max_output_tokens ? ` ${t("服务商最大输出 {tokens} tokens。", "Provider max output {tokens} tokens.", { tokens: detected.result.max_output_tokens })}` : ""}{detected.rejected !== null ? ` ${t("检测到的上下文窗口 {tokens} 超出可填写范围（8192–2097152），未填入。", "The detected context window of {tokens} tokens is outside the accepted range (8192–2097152), so it was not filled in.", { tokens: detected.rejected })}` : ""}</div>}
        </div>
        <div className="field">
          <label htmlFor={`${id}-tokens`}>{form.protocol === "anthropic" ? t("最大输出长度（tokens）", "Max output tokens") : t("回答预留（tokens）", "Answer reserve (tokens)")}</label>
          <input id={`${id}-tokens`} type="number" min={1} max={131072} step={1} value={form.max_tokens || ""} onChange={event => setForm(current => ({ ...current, max_tokens: Number(event.target.value) }))} disabled={busy} />
          {form.protocol === "openai" && <div className="hint">{t("用于本地输入预算；实际输出长度由服务商控制。", "Used for the local input budget; actual output length is controlled by the provider.")}</div>}
        </div>
        <div className="field">
          <label htmlFor={`${id}-target`}>{t("单次请求目标（tokens）", "Request size target (tokens)")}</label>
          <input id={`${id}-target`} type="number" min={TARGET_INPUT_MIN} max={form.context_window || CONTEXT_WINDOW_MAX} step={1} placeholder={t("自动：{tokens}", "Automatic: {tokens}", { tokens: effectiveTarget(null, form.context_window || CONTEXT_WINDOW_MAX) })} value={form.target_input_tokens ?? ""} onChange={event => setForm(current => ({ ...current, target_input_tokens: parseTargetInput(event.target.value) }))} disabled={busy} />
          <div className="hint">{t("一次提问的输入超过这个大小时，先压缩较早的历史，窗口很大时也能控制每次的用量；工具结果和必须保留的内容仍可用到整个窗口。留空则自动使用 131072 与窗口中的较小值。", "Older history is compacted once a question's input would exceed this size, which keeps each request affordable on very large windows; tool results and required content may still use the whole window. Leave blank to use 131072 or the window, whichever is smaller.")}</div>
        </div>
        <label className="hint model-summary-flag"><input type="checkbox" checked={form.use_for_summaries} onChange={event => setForm(current => ({ ...current, use_for_summaries: event.target.checked }))} disabled={busy} />{t("用这个模型整理长对话摘要", "Use this model for context summaries")}</label>
        <div className="hint">{t("长对话较早的历史在回答结束后整理成摘要，之后的提问直接使用，可以选一个更便宜的模型。同一时间只有一个模型负责；都不选时由回答所用的模型整理。这些历史会发送给这个模型的服务商。", "Older history of long chats is summarized after an answer, ready for later questions, so a cheaper model can do it. One model at a time does this; with none chosen, the answering model does. That history is sent to this model's provider.")}</div>
      </details>
      {error && <div className="test-bad" role="alert">{localizeError(error, locale)}</div>}
      <div className="modal-foot"><label className="hint"><input type="checkbox" checked={form.is_default} onChange={event => setForm(current => ({ ...current, is_default: event.target.checked }))} disabled={busy} /> {t("设为默认", "Set as default")}</label><div className="model-form-actions"><button type="button" className="btn" onClick={close} disabled={busy}>{t("取消", "Cancel")}</button><button type="submit" className="btn btn-primary" disabled={busy || detecting}>{busy ? t("保存中…", "Saving…") : model ? t("保存", "Save") : t("添加", "Add")}</button></div></div>
    </form>
  </div>;
}
