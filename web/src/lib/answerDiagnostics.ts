type Translate = (zh: string, en: string) => string;

/** Why an answer's tool rounds ended early, or null when no agent budget stopped them. */
export function budgetStopLabel(stop: unknown, t: Translate): string | null {
  if (stop === "time") return t("工具时间已用完", "Tool time limit reached");
  if (stop === "tokens") return t("工具结果已达上限", "Tool result budget reached");
  return null;
}
