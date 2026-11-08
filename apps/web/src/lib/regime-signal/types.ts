/** Per-stock market regime on a scan row (contract: docs/swing/REGIME-SIGNAL.md). */
export interface RegimeSignalIndex {
  name: string;
  via: "member" | "sector" | "size";
  ema10: number | string | null;
  ema20: number | string | null;
  up: boolean;
}

export interface RegimeSignal {
  signal: "GREEN" | "RED";
  fallback: boolean;
  indices: RegimeSignalIndex[];
  triggered: string[];
}

export type RegimeSort = "none" | "green" | "red";

export const FALLBACK_LINE = "No index mapping — Smallcap fallback";

/** GREEN 0, RED 1, absent 2 — absent always sorts last. */
export function regimeRank(signal: RegimeSignal | null | undefined, sort: RegimeSort): number {
  if (!signal) return 2;
  const green = signal.signal === "GREEN";
  if (sort === "red") return green ? 1 : 0;
  return green ? 0 : 1;
}

export function isGreen(signal: RegimeSignal | null | undefined): boolean {
  return signal?.signal === "GREEN";
}

/** Stable reorder/filter of rows by their regime signal. */
export function orderByRegime<T>(
  rows: readonly T[],
  signalOf: (row: T) => RegimeSignal | null | undefined,
  sort: RegimeSort,
  greenOnly: boolean,
): T[] {
  const kept = greenOnly ? rows.filter((row) => isGreen(signalOf(row))) : [...rows];
  if (sort === "none") return kept;
  return kept
    .map((row, index) => ({ row, index }))
    .sort(
      (a, b) =>
        regimeRank(signalOf(a.row), sort) - regimeRank(signalOf(b.row), sort) || a.index - b.index,
    )
    .map(({ row }) => row);
}

export function nextRegimeSort(sort: RegimeSort): RegimeSort {
  return sort === "none" ? "green" : sort === "green" ? "red" : "none";
}
