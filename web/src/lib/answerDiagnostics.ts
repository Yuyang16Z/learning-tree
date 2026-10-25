import type { MessageParams } from "../i18n/locale";
import type { AnswerDiagnostics } from "../types";

type Translate = (zh: string, en: string, params?: MessageParams) => string;
type Count = (value: number | null | undefined) => string;
interface InputFigures { estimated_input_tokens?: unknown; actual_input_tokens?: unknown; cached_input_tokens?: unknown }

/** Why an answer's tool rounds ended early, or null when no agent budget stopped them. */
export function budgetStopLabel(stop: unknown, t: Translate): string | null {
  if (stop === "time") return t("工具时间已用完", "Tool time limit reached");
  if (stop === "tokens") return t("工具结果已达上限", "Tool result budget reached");
  return null;
}

const finite = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;
const figures = (value: unknown): InputFigures | null => value && typeof value === "object" && !Array.isArray(value) ? value as InputFigures : null;

function tokens(source: InputFigures, t: Translate, n: Count): string {
  const est = n(finite(source.estimated_input_tokens));
  const actual = finite(source.actual_input_tokens);
  const cached = finite(source.cached_input_tokens);
  const text = actual != null
    ? t("估算 {est} · 实际 {actual} tokens", "est. {est} · actual {actual} tokens", { est, actual: n(actual) })
    : t("估算 {est} tokens", "est. {est} tokens", { est });
  return cached ? text + t("（缓存 {cached}）", " (cached {cached})", { cached: n(cached) }) : text;
}

/**
 * The collapsed summary of an answer's diagnostics, and the line for its summed input.
 * "Context" is what one provider request carried: the largest request of the answer.
 * An answer with tool rounds made several requests, and their summed input, which can
 * exceed the model window, is shown apart from it and never labelled as context.
 */
export function diagnosticsText(data: AnswerDiagnostics, t: Translate, n: Count): { summary: string; total: string | null } {
  const rounds = finite(data.tool_rounds) ?? 0;
  const requests = finite(data.requests);
  // Diagnostics stored before requests were counted: tool rounds imply several requests.
  const several = requests != null ? requests > 1 : rounds > 0;
  const sum = !several ? null : requests != null
    ? t("输入合计（{n} 次请求）：{figures}", "Input across {n} requests: {figures}", { n: requests, figures: tokens(data, t, n) })
    : t("输入合计：{figures}", "Total input: {figures}", { figures: tokens(data, t, n) });
  const single = figures(data.largest_request) ?? (several ? null : data);
  const context = single ? t("上下文：{figures}", "Context: {figures}", { figures: tokens(single, t, n) }) : null;
  const toolRounds = rounds === 1 ? t("工具 1 轮", "1 tool round") : rounds ? t("工具 {rounds} 轮", "{rounds} tool rounds", { rounds }) : null;
  const seconds = `${((finite(data.timings_ms?.total) ?? 0) / 1000).toFixed(1)} s`;
  const summary = [context ?? sum, toolRounds, budgetStopLabel(data.tool_budget_stop, t), seconds].filter(Boolean).join(" · ");
  return { summary, total: context ? sum : null };
}

/**
 * One line per provider request, in order, for an answer that made several (tool rounds and
 * the final answer): its input, output, whether it had to be trimmed to fit the budget, and
 * the tool calls it led to with their running time. Older answers have no rounds.
 */
export function roundLines(data: AnswerDiagnostics, t: Translate, n: Count): string[] {
  const rounds = Array.isArray(data.rounds) ? data.rounds : [];
  if (rounds.length < 2) return [];
  return rounds.flatMap((round, index) => {
    const source = figures(round);
    if (!source) return [];
    const parts = [t("第 {n} 轮：{figures}", "Round {n}: {figures}", { n: index + 1, figures: tokens(source, t, n) })];
    const output = finite((round as { output_tokens?: unknown }).output_tokens);
    if (output != null) parts.push(t("输出 {tokens}", "output {tokens}", { tokens: n(output) }));
    if ((round as { trimmed?: unknown }).trimmed === true) parts.push(t("已裁剪", "trimmed to fit"));
    const calls = finite((round as { tool_calls?: unknown }).tool_calls) ?? 0;
    const seconds = `${((finite((round as { tool_ms?: unknown }).tool_ms) ?? 0) / 1000).toFixed(2)} s`;
    if (calls === 1) parts.push(t("工具 1 次，{seconds}", "1 tool call, {seconds}", { seconds }));
    else if (calls > 1) parts.push(t("工具 {calls} 次，{seconds}", "{calls} tool calls, {seconds}", { calls, seconds }));
    return [parts.join(" · ")];
  });
}
