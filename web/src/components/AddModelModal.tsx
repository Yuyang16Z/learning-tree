import { useId, useState } from "react";
import { useI18n } from "../i18n";
import { localizeError } from "../i18n/workspace";
import type { ModelCfg, ModelInput, ModelProtocol } from "../types";

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
    is_default: model?.is_default ?? isFirst,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const close = () => { if (!busy) onClose(); };
  const set = (key: "label" | "base_url" | "llm_model" | "api_key") =>
    (event: React.ChangeEvent<HTMLInputElement>) => setForm(current => ({ ...current, [key]: event.target.value }));

  function changeProtocol(protocol: ModelProtocol) {
    setForm(current => ({
      ...current, protocol,
      base_url: Object.values(defaultUrls).includes(current.base_url) ? defaultUrls[protocol] : current.base_url,
      llm_model: current.llm_model === "deepseek-chat" && protocol === "anthropic" ? "" : current.llm_model,
    }));
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
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
    if (!Number.isInteger(payload.context_window) || payload.context_window < 8192 || payload.context_window > 2097152) {
      setError(t("上下文窗口应为 8192–2097152 之间的整数。", "Context window must be an integer between 8192 and 2097152.")); return;
    }
    if (payload.context_window - payload.max_tokens - Math.max(512, Math.floor(payload.context_window / 20)) < 1024) {
      setError(t("扣除回答预留和预算余量后，须至少保留 1024 的输入空间。", "Context window must exceed the answer reserve plus headroom by at least 1024.")); return;
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
          <input id={`${id}-context`} type="number" min={8192} max={2097152} step={1} value={form.context_window || ""} onChange={event => setForm(current => ({ ...current, context_window: Number(event.target.value) }))} disabled={busy} />
          <div className="hint">{t("按服务商提供的模型上下文容量填写。", "Use the model context capacity specified by your provider.")}</div>
        </div>
        <div className="field">
          <label htmlFor={`${id}-tokens`}>{form.protocol === "anthropic" ? t("最大输出长度（tokens）", "Max output tokens") : t("回答预留（tokens）", "Answer reserve (tokens)")}</label>
          <input id={`${id}-tokens`} type="number" min={1} max={131072} step={1} value={form.max_tokens || ""} onChange={event => setForm(current => ({ ...current, max_tokens: Number(event.target.value) }))} disabled={busy} />
          {form.protocol === "openai" && <div className="hint">{t("用于本地输入预算；实际输出长度由服务商控制。", "Used for the local input budget; actual output length is controlled by the provider.")}</div>}
        </div>
      </details>
      {error && <div className="test-bad" role="alert">{localizeError(error, locale)}</div>}
      <div className="modal-foot"><label className="hint"><input type="checkbox" checked={form.is_default} onChange={event => setForm(current => ({ ...current, is_default: event.target.checked }))} disabled={busy} /> {t("设为默认", "Set as default")}</label><div className="model-form-actions"><button type="button" className="btn" onClick={close} disabled={busy}>{t("取消", "Cancel")}</button><button type="submit" className="btn btn-primary" disabled={busy}>{busy ? t("保存中…", "Saving…") : model ? t("保存", "Save") : t("添加", "Add")}</button></div></div>
    </form>
  </div>;
}
