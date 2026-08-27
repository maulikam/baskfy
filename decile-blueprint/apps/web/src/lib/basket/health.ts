/**
 * Facts about a sized basket that the create preview can compute without a new network call (SB3).
 *
 * A screen already answered *which names*. Sizing answered *how much and how many*. This module
 * reads those two answers and reports concentration and the median 1-year numbers the screen
 * rows already carry. It does not invent a risk score, a VaR, or a recommendation — those need
 * either a return matrix we do not have on this page, or language docs/11 forbids.
 *
 * The single-name cap is the desk's ``MAX_SINGLE_WEIGHT`` (15% of the stocks sleeve). Stating
 * that a name is over it is a fact about the arithmetic, not advice to change it.
 */

/** Desk `MAX_SINGLE_WEIGHT`: percent of the *equity sleeve*, not of the whole amount. */
export const SINGLE_NAME_CAP_PCT = 15;

/** Below this name count the panel notes that one name moves the basket. Not a rule. */
export const FEW_NAMES = 5;

export interface HealthRow {
  symbol: string;
  /** Share of the investor's whole amount, cash included (same unit as `MaterializedHolding.weight`). */
  weightOfAmount: number;
  ret12m?: number | null;
  vol12m?: number | null;
}

export type HealthNoteKind = "single-name" | "few-names";

export interface HealthNote {
  kind: HealthNoteKind;
  text: string;
}

export interface BasketHealth {
  names: number;
  /** Largest name as a percent of the *stocks sleeve* (deployed money). */
  largestSleevePct: number | null;
  /** Largest name as a percent of the whole amount, cash included. */
  largestAmountPct: number | null;
  medianRet12m: number | null;
  medianVol12m: number | null;
  notes: HealthNote[];
}

function finite(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function median(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  const odd = sorted.length % 2 === 1;
  if (odd) return sorted[mid] ?? null;
  const left = sorted[mid - 1];
  const right = sorted[mid];
  if (left === undefined || right === undefined) return null;
  return (left + right) / 2;
}

/**
 * Read a screen-row field that may be a number, a numeric string, or missing.
 *
 * Screen rows are `additionalProperties` — the column set is the user's — so a missing key is
 * expected, not an error.
 */
export function numericField(row: Record<string, unknown>, key: string): number | null {
  const value = row[key];
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function healthRowsFrom(
  holdings: readonly { symbol: string; weight: number }[],
  screenRows: readonly Record<string, unknown>[],
): HealthRow[] {
  const bySymbol = new Map(
    screenRows.map((row) => [String(row.symbol ?? "").toUpperCase(), row]),
  );
  return holdings.map((holding) => {
    const row = bySymbol.get(holding.symbol.toUpperCase()) ?? {};
    return {
      symbol: holding.symbol,
      weightOfAmount: holding.weight,
      ret12m: numericField(row, "ret_12m"),
      vol12m: numericField(row, "vol_12m"),
    };
  });
}

export function assessBasketHealth(rows: readonly HealthRow[]): BasketHealth {
  const names = rows.length;
  const sleeveTotal = rows.reduce((sum, row) => sum + row.weightOfAmount, 0);
  const largestAmount = rows.reduce(
    (max, row) => Math.max(max, row.weightOfAmount),
    0,
  );
  const largestSleeve =
    sleeveTotal > 0
      ? rows.reduce((max, row) => Math.max(max, row.weightOfAmount / sleeveTotal), 0)
      : null;

  const notes: HealthNote[] = [];
  const largestSleevePct = largestSleeve === null ? null : largestSleeve * 100;
  if (largestSleevePct !== null && largestSleevePct > SINGLE_NAME_CAP_PCT) {
    notes.push({
      kind: "single-name",
      text: `One name is ${largestSleevePct.toFixed(1)}% of the stocks allocation. The desk caps a single name at ${SINGLE_NAME_CAP_PCT}%.`,
    });
  }
  if (names > 0 && names < FEW_NAMES) {
    notes.push({
      kind: "few-names",
      text: `${names} name${names === 1 ? "" : "s"}. A move in one of them moves the basket.`,
    });
  }

  return {
    names,
    largestSleevePct,
    largestAmountPct: names === 0 ? null : largestAmount * 100,
    medianRet12m: median(rows.map((row) => row.ret12m).filter(finite)),
    medianVol12m: median(rows.map((row) => row.vol12m).filter(finite)),
    notes,
  };
}
