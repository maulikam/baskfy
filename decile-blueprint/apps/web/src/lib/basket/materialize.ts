/**
 * Turn a screen run into an investable basket projection (Tree 6 §5).
 *
 * Equal-weight by default, 5% cash buffer. `topN` defaults to 20. No I/O — pure DataFrames-in
 * spirit for the web layer.
 */

export const DEFAULT_TOP_N = 20;
export const DEFAULT_CASH_BUFFER = 0.05;
export const DEFAULT_NOTIONAL = 100_000;

export interface MaterializeRowIn {
  symbol: string;
  name?: string;
  rank: number;
  /** Optional factor weight signal; when present and `mode === "factor"`, used for weights. */
  sorting_factor?: number | null;
  close_raw?: number | null;
  close?: number | null;
}

export type WeightMode = "equal" | "factor";

export interface MaterializedHolding {
  rank: number;
  symbol: string;
  name: string;
  weight: number;
  price: number | null;
  amount: number;
}

export interface MaterializedBasket {
  name: string;
  thesis: string;
  asOf: string | null;
  cashPct: number;
  notional: number;
  minInvestment: number;
  holdings: MaterializedHolding[];
  source: `screen:${string}` | "preview";
}

function priceOf(row: MaterializeRowIn): number | null {
  const raw = row.close_raw ?? row.close;
  if (raw === null || raw === undefined) return null;
  const n = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function factorOf(row: MaterializeRowIn): number {
  const v = row.sorting_factor;
  if (v === null || v === undefined) return 0;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/**
 * Ranked screen rows → weighted holdings + cash.
 */
export function materializeBasket(args: {
  rows: readonly MaterializeRowIn[];
  name: string;
  thesis?: string;
  asOf?: string | null;
  topN?: number;
  cashBuffer?: number;
  notional?: number;
  mode?: WeightMode;
  source?: MaterializedBasket["source"];
}): MaterializedBasket {
  const topN = args.topN ?? DEFAULT_TOP_N;
  const cash = Math.min(0.5, Math.max(0, args.cashBuffer ?? DEFAULT_CASH_BUFFER));
  const notional = args.notional ?? DEFAULT_NOTIONAL;
  const mode = args.mode ?? "equal";
  const slice = [...args.rows]
    .sort((a, b) => a.rank - b.rank)
    .slice(0, Math.max(1, topN));

  const investable = 1 - cash;
  let rawWeights: number[];

  if (mode === "factor") {
    const scores = slice.map(factorOf);
    const sum = scores.reduce((a, b) => a + b, 0);
    rawWeights =
      sum > 0 ? scores.map((s) => s / sum) : slice.map(() => 1 / slice.length);
  } else {
    rawWeights = slice.map(() => 1 / slice.length);
  }

  const holdings: MaterializedHolding[] = slice.map((row, i) => {
    const weight = rawWeights[i]! * investable;
    const price = priceOf(row);
    return {
      rank: row.rank,
      symbol: row.symbol,
      name: row.name?.trim() || row.symbol,
      weight,
      price,
      amount: Math.round(notional * weight),
    };
  });

  const minLot = holdings.reduce((min, h) => {
    if (h.price === null) return min;
    const need = h.price / Math.max(h.weight, 1e-9);
    return Math.max(min, need);
  }, 0);

  return {
    name: args.name,
    thesis: args.thesis ?? "Equal-weight basket from your screen rules.",
    asOf: args.asOf ?? null,
    cashPct: cash * 100,
    notional,
    minInvestment: Math.ceil(minLot / 100) * 100 || Math.ceil(notional * 0.05),
    holdings,
    source: args.source ?? "preview",
  };
}
