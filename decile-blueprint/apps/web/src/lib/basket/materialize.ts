/**
 * Turn a screen run into an investable basket projection (Tree 6 §5, extended by SB1 / SB4).
 *
 * The preview half of `baskfy_core.basket_sizing`. A screen says which stocks; this adds the two
 * things it does not — how much money, across how many of its names — so the investor can move both
 * and watch the table change without a round trip. `POST /cb/baskets/from-screen` recomputes all of
 * it server-side on save, from its own run of the same screen, and that result is the record.
 *
 * The name count comes from a holding profile (`lib/basket/profiles.ts`) unless the investor gives
 * their own; the cash share comes from the desk's exposure tier; the split across names comes from
 * a weight method (`lib/basket/methods.ts`). No I/O — pure DataFrames-in spirit for the web layer.
 *
 * Preview is allowed to fall back to equal weight mid-keystroke (a missing score, a cleared custom
 * cell). The save endpoint raises on the same inputs — the preview must stay usable while typing.
 */

import {
  DEFAULT_METHOD,
  METHOD_THESES,
  methodPreviewNote,
  schemeWeights,
  type WeightMethod,
} from "@/lib/basket/methods";
import {
  DEFAULT_PROFILE,
  MIN_CASH_BUFFER_PCT,
  resolveHoldings,
  resolveCashPct,
  type HoldingProfile,
} from "@/lib/basket/profiles";
import { numericField } from "@/lib/basket/health";
import { EMPTY_FACTS, factsFromScreenRow, type HoldingFacts } from "@/lib/basket/holding-facts";

export const DEFAULT_TOP_N = 20;
/** Percent, matching Python `MIN_CASH_BUFFER_PCT`. Never a fraction — see `profiles.ts`. */
export const DEFAULT_CASH_PCT = MIN_CASH_BUFFER_PCT;
export const DEFAULT_NOTIONAL = 100_000;
/** A minimum is rounded up to this, so it reads as a threshold rather than a computation. */
export const MINIMUM_STEP = 100;

export interface MaterializeRowIn {
  symbol: string;
  name?: string | undefined;
  rank: number;
  /** The screen's ranking factor; used by `SCORE`. */
  sorting_factor?: number | null | undefined;
  close_raw?: number | null | undefined;
  close?: number | null | undefined;
  /** 1-year volatility; used by `INV_VOL`. */
  vol?: number | null | undefined;
  facts?: HoldingFacts | undefined;
}

/** @deprecated Use `WeightMethod`. Kept so older call sites (`mode: "factor"`) still compile. */
export type WeightMode = "equal" | "factor";

export interface MaterializedHolding {
  rank: number;
  symbol: string;
  name: string;
  /** Share of the investor's whole amount, cash included. */
  weight: number;
  price: number | null;
  amount: number;
  facts: HoldingFacts;
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
  /** What the names add up to. */
  deployed: number;
  /** `notional - deployed`: the cash share plus the whole-rupee remainder. */
  cash: number;
  /** The profile whose suggestion set the count, or null when the count was chosen. */
  profile: HoldingProfile | null;
  /** True when the investor's amount cannot fill every name at its target weight. */
  underfunded: boolean;
  /** How the deployed money was split. */
  method: WeightMethod;
}

function priceOf(row: MaterializeRowIn): number | null {
  const raw = row.close_raw ?? row.close;
  if (raw === null || raw === undefined) return null;
  const n = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Map a screen-run row (additionalProperties) onto what sizing reads. */
export function rowsFromScreen(rows: readonly Record<string, unknown>[]): MaterializeRowIn[] {
  return rows.map((row) => ({
    symbol: typeof row.symbol === "string" ? row.symbol : "",
    name: typeof row.name === "string" ? row.name : undefined,
    rank: typeof row.rank === "number" ? row.rank : Number(row.rank) || 0,
    sorting_factor: numericField(row, "sorting_factor"),
    close_raw: numericField(row, "close_raw") ?? numericField(row, "close"),
    vol: numericField(row, "vol_12m"),
    facts: factsFromScreenRow(row),
  }));
}

/**
 * Ranked screen rows → weighted holdings + cash.
 *
 * `holdings` overrides the profile's suggestion. `topN` is the older name for the same thing and
 * still wins when given, so the featured basket and the saved-screen cards keep their behaviour.
 */
export function materializeBasket(args: {
  rows: readonly MaterializeRowIn[];
  name: string;
  thesis?: string | undefined;
  asOf?: string | null | undefined;
  topN?: number | undefined;
  holdings?: number | null | undefined;
  profile?: HoldingProfile | undefined;
  /** Percent of the amount to leave in cash. Overrides tier suggestion when set. */
  cashPct?: number | null | undefined;
  /** The desk's current exposure tier (R1–R4), which decides the cash share. */
  exposureTier?: string | null | undefined;
  notional?: number | undefined;
  method?: WeightMethod | undefined;
  /** Raw custom numbers keyed by symbol. Required by the server when `method` is CUSTOM. */
  customWeights?: Readonly<Record<string, number>> | null | undefined;
  /** @deprecated Prefer `method`. `"factor"` is `SCORE`. */
  mode?: WeightMode | undefined;
  source?: MaterializedBasket["source"] | undefined;
}): MaterializedBasket {
  const profile = args.profile ?? DEFAULT_PROFILE;
  const requested = args.topN ?? args.holdings ?? null;
  const count = resolveHoldings({
    available: args.rows.length,
    profile,
    requested,
  });
  const resolvedCashPct = resolveCashPct({
    exposureTier: args.exposureTier,
    requested: args.cashPct,
  });
  const cash = resolvedCashPct / 100;
  const notional = args.notional ?? DEFAULT_NOTIONAL;
  const method: WeightMethod =
    args.method ?? (args.mode === "factor" ? "SCORE" : DEFAULT_METHOD);
  const slice = [...args.rows].sort((a, b) => a.rank - b.rank).slice(0, count);
  const methodRows = slice.map((row) => ({
    symbol: row.symbol,
    rank: row.rank,
    score: row.sorting_factor,
    vol: row.vol,
  }));
  // Preview-only: if the chosen method cannot be applied (empty custom, no score,
  // no vol), size as equal so the table does not hand the last name a residual.
  // Save still raises. `method` on the result stays what the investor picked.
  const applied: WeightMethod =
    methodPreviewNote(method, methodRows) !== null ||
    (method === "CUSTOM" &&
      !Object.values(args.customWeights ?? {}).some((value) => value > 0))
      ? "EQUAL"
      : method;

  const investable = 1 - cash;
  const sleeveWeights = schemeWeights(methodRows, applied, args.customWeights);

  // Math.floor, not Math.round: the basket must never propose more than its budget. The
  // shortfall becomes cash, which is where Python puts it too.
  //
  // EQUAL DIVIDES THE BUDGET. THE OTHER METHODS MULTIPLY IT.
  // Same reason as Python: four-decimal residual on the last name would otherwise look like a
  // bigger position under equal weight.
  const budget = Math.floor(notional * investable);
  const holdings: MaterializedHolding[] = slice.map((row, i) => {
    const sleeve = sleeveWeights[i] ?? 0;
    const weight = sleeve * investable;
    return {
      rank: row.rank,
      symbol: row.symbol,
      name: row.name?.trim() || row.symbol,
      weight,
      price: priceOf(row),
      amount: applied === "EQUAL" ? Math.floor(budget / count) : Math.floor(budget * sleeve),
      facts: row.facts ?? EMPTY_FACTS,
    };
  });

  const deployed = holdings.reduce((sum, h) => sum + h.amount, 0);
  const minLot = holdings.reduce((min, h) => {
    if (h.price === null) return min;
    return Math.max(min, h.price / Math.max(h.weight, 1e-9));
  }, 0);
  const minInvestment =
    minLot > 0
      ? Math.ceil(minLot / MINIMUM_STEP) * MINIMUM_STEP
      : notional > 0
        ? Math.ceil(notional * 0.05)
        : MINIMUM_STEP;

  return {
    name: args.name,
    thesis: args.thesis ?? METHOD_THESES[method],
    asOf: args.asOf ?? null,
    cashPct: resolvedCashPct,
    notional,
    minInvestment,
    holdings,
    source: args.source ?? "preview",
    deployed,
    cash: notional - deployed,
    profile: requested === null ? profile : null,
    underfunded: notional < minInvestment,
    method,
  };
}
