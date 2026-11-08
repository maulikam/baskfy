/**
 * How the deployed money is split — mirrored from `baskfy_core.basket_sizing.WeightMethod` (SB4).
 *
 * Equal / rank / inverse-vol are the backtest's schemes. Score is the screen's ranking factor
 * (the algo path). Custom is the investor's own numbers, applied to the screen's names.
 *
 * `packages/core/tests/test_weight_method_parity.py` fails if this file drifts.
 */

import { equalWeights, normalizeWeights } from "@/lib/create/weights";

export const WEIGHT_METHODS = ["EQUAL", "RANK", "SCORE", "INV_VOL", "CUSTOM"] as const;

export type WeightMethod = (typeof WEIGHT_METHODS)[number];

export const DEFAULT_METHOD: WeightMethod = "EQUAL";

export const METHOD_LABELS: Record<WeightMethod, string> = {
  EQUAL: "Equal",
  RANK: "By rank",
  SCORE: "By the screen’s score",
  INV_VOL: "Smoother ride",
  CUSTOM: "Your numbers",
};

export const METHOD_BLURBS: Record<WeightMethod, string> = {
  EQUAL: "The same rupees in every name. The default, and what existing baskets use.",
  RANK: "More in the names the screen put at the top. Best gets the most, last gets the least.",
  SCORE: "Split in proportion to the number the screen ranked on. The algo path.",
  INV_VOL: "More in the calmer names, less in the jumpy ones. Needs 1-year volatility.",
  CUSTOM: "You type the weights. They have to cover every name the screen selected — nothing extra.",
};

export const METHOD_THESES: Record<WeightMethod, string> = {
  EQUAL: "Equal-weight basket from your screen rules.",
  RANK: "Rank-weighted from your screen rules — more in the names at the top.",
  SCORE: "Weighted by the number the screen ranked on.",
  INV_VOL: "Inverse-vol weighted from your screen rules — more in the calmer names.",
  CUSTOM: "Your own weights on the names this screen selected.",
};

export interface MethodRow {
  symbol: string;
  rank: number;
  score?: number | null | undefined;
  vol?: number | null | undefined;
}

export function schemeWeights(
  rows: readonly MethodRow[],
  method: WeightMethod,
  custom?: Readonly<Record<string, number>> | null,
): number[] {
  if (rows.length === 0) return [];

  if (method === "CUSTOM") {
    const raw = rows.map((row) => {
      const value = custom?.[row.symbol] ?? custom?.[row.symbol.toUpperCase()];
      return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
    });
    if (raw.every((value) => value <= 0)) return equalWeights(rows.length);
    return normalizeWeights(raw);
  }

  if (method === "EQUAL") return equalWeights(rows.length);

  if (method === "RANK") {
    const count = rows.length;
    return normalizeWeights(rows.map((_, index) => count - index));
  }

  if (method === "SCORE") {
    const raw = rows.map((row) =>
      typeof row.score === "number" && Number.isFinite(row.score) && row.score > 0 ? row.score : 0,
    );
    if (raw.every((value) => value <= 0)) return equalWeights(rows.length);
    return normalizeWeights(raw);
  }

  const raw = rows.map((row) => {
    if (typeof row.vol === "number" && Number.isFinite(row.vol) && row.vol > 0) {
      return 1 / row.vol;
    }
    return 0;
  });
  if (raw.every((value) => value <= 0)) return equalWeights(rows.length);
  return normalizeWeights(raw);
}

/** Percents that sum to 100, for the custom-weight inputs. */
export function seedEqualPercents(symbols: readonly string[]): Record<string, number> {
  const weights = equalWeights(symbols.length);
  return Object.fromEntries(
    symbols.map((symbol, index) => [symbol, Math.round((weights[index] ?? 0) * 10_000) / 100]),
  );
}

/**
 * Keep the investor's numbers for names they already typed; seed equal percents for new ones
 * (a count change) without wiping a mid-keystroke zero.
 */
export function fillMissingCustomWeights(
  symbols: readonly string[],
  custom: Readonly<Record<string, number>>,
): Record<string, number> {
  const seeded = seedEqualPercents(symbols);
  const next: Record<string, number> = {};
  for (const symbol of symbols) {
    next[symbol] = Object.prototype.hasOwnProperty.call(custom, symbol)
      ? (custom[symbol] as number)
      : (seeded[symbol] as number);
  }
  return next;
}

/** Preview-only: the server still refuses; this just says why the table looks equal. */
export function methodPreviewNote(
  method: WeightMethod,
  rows: readonly MethodRow[],
): string | null {
  if (method === "SCORE") {
    const scored = rows.some((row) => typeof row.score === "number" && row.score > 0);
    if (!scored) {
      return "This screen’s rows have no ranking score, so the preview is showing equal weight. Save will refuse score weighting until the screen ranks on a number.";
    }
  }
  if (method === "INV_VOL") {
    const missing = rows
      .filter((row) => !(typeof row.vol === "number" && row.vol > 0))
      .map((row) => row.symbol);
    if (missing.length === rows.length) {
      return "None of these rows have 1-year volatility, so the preview is showing equal weight. Save will refuse inverse-vol until the screen projects vol_12m.";
    }
    if (missing.length > 0) {
      const names = missing.join(", ");
      const verb = missing.length === 1 ? "has" : "have";
      return `${names} ${verb} no 1-year volatility. Save will refuse inverse-vol until every chosen name has vol_12m.`;
    }
  }
  return null;
}

/** What save posts when the method is CUSTOM. Undefined for every other method. */
export function customWeightsForSave(
  method: WeightMethod,
  symbols: readonly string[],
  custom: Readonly<Record<string, number>>,
): { symbol: string; weight: number }[] | undefined {
  if (method !== "CUSTOM") return undefined;
  const filled = fillMissingCustomWeights(symbols, custom);
  return symbols.map((symbol) => ({ symbol, weight: filled[symbol] ?? 0 }));
}
