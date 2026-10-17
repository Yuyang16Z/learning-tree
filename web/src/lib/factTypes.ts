/** Display labels for structured topic facts; unknown or missing types read as conclusions. */
const FACT_TYPE_LABELS: Record<string, [string, string]> = {
  definition: ["定义", "Definition"],
  condition: ["适用条件", "Condition"],
  misconception: ["纠正误解", "Correction"],
  open_question: ["待解决疑问", "Open question"],
  learner_state: ["学习状态", "Learner state"],
  conclusion: ["结论", "Conclusion"],
};

export function factTypeLabel(type: string | null | undefined): [string, string] {
  return FACT_TYPE_LABELS[type ?? ""] ?? FACT_TYPE_LABELS.conclusion;
}
