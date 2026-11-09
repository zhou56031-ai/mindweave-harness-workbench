export interface AnswerContract {
  hasConclusion: boolean
  hasPoints: boolean
  hasSources: boolean
  ok: boolean
}

/** 检查回答是否满足“结论 + 要点/依据 + 来源”的结构契约。 */
export function checkAnswerContract(text: string): AnswerContract {
  const hasConclusion = /结论/.test(text)
  const hasPoints = /要点|依据|步骤/.test(text)
  const hasSources = /参考|来源|\[\d+\]/.test(text)
  return { hasConclusion, hasPoints, hasSources, ok: hasConclusion }
}
