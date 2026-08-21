import type { CellOut } from "@baskfy/api-client";

import { EMPTY_CELL, formatCrore, formatFraction, formatNumber, formatPercent } from "@/lib/format";

/**
 * How each factsheet cell is rendered — one place, so the same key never prints two ways.
 *
 * The API sends values already rounded to their storage precision (CLAUDE.md house rule 8), so
 * nothing here re-rounds. What it decides is *units and shape*: a return carries a sign and a `%`,
 * volatility is a stored fraction that becomes a percentage (docs/13 §2 finding 4 — "the UI
 * multiplies by 100"; `docs/06a` §10 records that the API deliberately does not), marketcap is
 * integer ₹ crore, and anything the API sent as `null` renders as an em dash and never as `0`.
 *
 * That last rule is Prompt 10's third acceptance criterion. A young listing has no 1-year return;
 * printing `0.00%` would state a fact about it that is false.
 */

export type CellValue = string | number | null | undefined;

export type CellKind =
  | "price"
  | "percent"
  | "share"
  | "fraction"
  | "crore"
  | "count"
  | "ratio"
  | "text";

/** The unit family a key belongs to, from its name. The registry's keys are systematic. */
export function kindOf(key: string): CellKind {
  if (key.startsWith("ret_") || key.startsWith("sharpe_") || key.startsWith("away_high_")) {
    return "percent";
  }
  // A share of days, not a change: `+65.59%` would read as "up 65.59%".
  if (key.startsWith("pos_days_")) return "share";
  if (key.startsWith("vol_")) return "fraction";
  if (key.startsWith("rsi_")) return "ratio";
  if (key.startsWith("circuits_")) return "count";
  if (key === "marketcap_cr") return "crore";
  if (key === "median_vol_12m") return "price";
  if (key === "series" || key === "listed_on") return "text";
  if (key === "pe" || key === "beta_12m") return "ratio";
  return "price";
}

const PRICE_DECIMALS = 2;
const RATIO_DECIMALS = 2;

export function formatCell(key: string, value: CellValue): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  switch (kindOf(key)) {
    case "percent":
      return formatPercent(value);
    case "share":
      return formatNumber(value, { decimals: PRICE_DECIMALS, suffix: "%" });
    case "fraction":
      return formatFraction(value);
    case "crore":
      return formatCrore(value);
    case "count":
      return formatNumber(value, { decimals: 0 });
    case "ratio":
      return formatNumber(value, { decimals: RATIO_DECIMALS });
    case "text":
      return String(value);
    case "price":
      return formatNumber(value, { decimals: PRICE_DECIMALS });
  }
}

export function display(cell: CellOut): string {
  return formatCell(cell.key, cell.value);
}

/** `pos_days_*` arrives label-first from `market_quality`, with no key to dispatch on. */
export function formatPositiveDays(value: CellValue): string {
  return formatCell("pos_days_12m", value);
}

/**
 * The percentile as the grid wants it: `0`–`1`, or undefined when there is nothing to rank.
 *
 * `exactOptionalPropertyTypes` is on, so an absent percentile has to be `undefined` rather than
 * `null` — which is also the honest encoding: the bar is not drawn at all.
 */
export function percentileOf(cell: CellOut): number | undefined {
  return cell.percentile === null || cell.percentile === undefined ? undefined : cell.percentile;
}

/** Corporate actions that move the adjusted series — docs/08: "flag rows that materially affect
 * adjusted history". A dividend does too, but only slightly; a split or a bonus rebases it. */
const MATERIAL_ACTIONS = new Set(["split", "bonus", "rights", "demerger"]);

export function isMaterial(actionType: string): boolean {
  return MATERIAL_ACTIONS.has(actionType);
}

/** "4:1" for a bonus, "10:1" for a split, "₹12.50" for a dividend, "—" when neither is known. */
export function actionValue(action: {
  action_type: string;
  ratio_from?: string | number | null;
  ratio_to?: string | number | null;
  amount?: string | number | null;
}): string {
  const { ratio_from: from, ratio_to: to, amount } = action;
  if (from !== null && from !== undefined && to !== null && to !== undefined) {
    return `${formatNumber(from, { decimals: 0 })}:${formatNumber(to, { decimals: 0 })}`;
  }
  if (amount !== null && amount !== undefined) {
    return `₹${formatNumber(amount, { decimals: PRICE_DECIMALS })}`;
  }
  return EMPTY_CELL;
}
