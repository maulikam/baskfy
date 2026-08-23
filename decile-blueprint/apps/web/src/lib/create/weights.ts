/**
 * Client-side weight helpers for `/create` (SC8).
 *
 * Mirrors `baskfy_core.curated_baskets.assert_weights_sum_to_one` + scan equal-weight
 * residual-on-last: four decimal places, sum to 1.0000.
 */

export const WEIGHT_QUANTIZE = 4;
export const WEIGHT_SUM_TARGET = 1;
/** Matches Python WEIGHT_SUM_TOLERANCE = 0.00005 */
export const WEIGHT_SUM_TOLERANCE = 0.00005;

export function quantizeWeight(value: number): number {
  const factor = 10 ** WEIGHT_QUANTIZE;
  return Math.round(value * factor) / factor;
}

/** n equal weights that sum to 1.0000 (residual on the last name). */
export function equalWeights(n: number): number[] {
  if (n < 1) throw new Error("need at least one constituent");
  const base = quantizeWeight(1 / n);
  const weights = Array.from({ length: n }, () => base);
  const residual = quantizeWeight(WEIGHT_SUM_TARGET - weights.reduce((a, b) => a + b, 0));
  weights[n - 1] = quantizeWeight(weights[n - 1]! + residual);
  assertWeightsSumToOne(weights);
  return weights;
}

/** Scale positive custom weights so they sum to 1.0000 within tolerance. */
export function normalizeWeights(raw: readonly number[]): number[] {
  if (raw.length === 0) throw new Error("constituent weights cannot be empty");
  const positive = raw.map((w) => (Number.isFinite(w) && w > 0 ? w : 0));
  const total = positive.reduce((a, b) => a + b, 0);
  if (total <= 0) throw new Error("weights must include at least one positive value");
  const scaled = positive.map((w) => quantizeWeight(w / total));
  const residual = quantizeWeight(WEIGHT_SUM_TARGET - scaled.reduce((a, b) => a + b, 0));
  scaled[scaled.length - 1] = quantizeWeight(scaled[scaled.length - 1]! + residual);
  assertWeightsSumToOne(scaled);
  return scaled;
}

export function assertWeightsSumToOne(weights: readonly number[]): void {
  if (weights.length === 0) throw new Error("constituent weights cannot be empty");
  const total = weights.map(quantizeWeight).reduce((a, b) => a + b, 0);
  const delta = Math.abs(total - WEIGHT_SUM_TARGET);
  if (delta > WEIGHT_SUM_TOLERANCE) {
    throw new Error(
      `constituent weights must sum to ${WEIGHT_SUM_TARGET}, got ${total} (delta ${delta})`,
    );
  }
}
