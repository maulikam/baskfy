export type SplitMode = "equal" | "risk" | "reward" | "score";

export const SPLIT_LABEL: Record<SplitMode, string> = {
  equal: "Equal",
  risk: "Equal risk",
  reward: "Reward (R:R)",
  score: "Score",
};

export const SPLIT_HINT: Record<SplitMode, string> = {
  equal: "Same rupees in every stock.",
  risk: "More money where the stop is closer, so each stock loses about the same rupees if its stop hits.",
  reward: "Weighted by reward-to-risk: a 3:1 setup gets three times a 1:1 setup.",
  score: "Weighted by the scanner's score, capped so one name takes at most 40%.",
};

export interface SplitInput {
  symbol: string;
  price: number;
  stop: number | null;
  target: number | null;
  riskReward: number;
  score: number;
  /** Multiplies the weight before normalising — 0.5 halves a flagged stock's share. */
  scale?: number;
}

export interface SplitLine {
  symbol: string;
  weight: number;
  amount: number;
  quantity: number;
  riskInr: number | null;
  rewardInr: number | null;
}

const MAX_SCORE_WEIGHT = 0.4;

function rawWeight(mode: SplitMode, r: SplitInput): number {
  if (mode === "equal") return 1;
  if (mode === "risk") {
    const dist = r.stop !== null && r.stop > 0 && r.stop < r.price ? (r.price - r.stop) / r.price : 0;
    return dist > 0 ? 1 / dist : 0;
  }
  if (mode === "reward") return r.riskReward > 0 ? r.riskReward : 0;
  return r.score > 0 ? r.score : 0;
}

function cap(weights: number[], max: number): number[] {
  if (weights.length * max < 1) return weights;
  let w = weights.slice();
  for (let i = 0; i < weights.length; i += 1) {
    const over = w.map((x) => x > max);
    if (!over.some(Boolean)) break;
    const excess = w.reduce((s, x) => s + Math.max(0, x - max), 0);
    const freeSum = w.reduce((s, x, j) => (over[j] ? s : s + x), 0);
    w = w.map((x, j) => (over[j] ? max : freeSum > 0 ? x + (excess * x) / freeSum : x));
  }
  return w;
}

/** Splits `total` rupees across rows; quantity rounds down at each row's price. */
export function splitAmount(total: number, rows: readonly SplitInput[], mode: SplitMode): SplitLine[] {
  const raw = rows.map((r) => rawWeight(mode, r) * (r.scale ?? 1));
  const sum = raw.reduce((s, x) => s + x, 0);
  let weights = sum > 0 ? raw.map((x) => x / sum) : rows.map(() => 0);
  if (mode === "score") weights = cap(weights, MAX_SCORE_WEIGHT);
  return rows.map((r, i) => {
    const weight = weights[i] ?? 0;
    const amount = total > 0 ? Math.floor(total * weight) : 0;
    const quantity = r.price > 0 ? Math.floor(amount / r.price) : 0;
    const riskInr = r.stop !== null && r.stop > 0 && r.stop < r.price ? quantity * (r.price - r.stop) : null;
    const rewardInr = r.target !== null && r.target > r.price ? quantity * (r.target - r.price) : null;
    return { symbol: r.symbol, weight, amount, quantity, riskInr, rewardInr };
  });
}
