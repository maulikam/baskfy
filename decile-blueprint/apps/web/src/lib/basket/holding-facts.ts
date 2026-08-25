/**
 * Extra facts on a sized holding — what the basket table and the custom-weight editor can
 * honestly show (SB6).
 *
 * Taken from the screen row's own columns. A missing key is expected: the column set is the
 * user's. Sector is not here — `instrument` has no sector in this tree (SB3.1).
 *
 * A column with no number on any row is not rendered. An em-dash column would be a promise
 * we cannot keep.
 */

import { numericField } from "@/lib/basket/health";
import { EMPTY_CELL, formatCrore, formatFraction, formatNumber, formatPercent } from "@/lib/format";

/** Columns the create preview asks for so these facts are on the row. */
export const BASKET_FACT_COLUMNS = [
  "marketcap_cr",
  "ret_12m",
  "vol_12m",
  "beta_12m",
  "sharpe_12m",
  "median_vol_12m",
  "pe",
] as const;

export interface HoldingFacts {
  marketcapCr: number | null;
  ret12m: number | null;
  vol12m: number | null;
  beta12m: number | null;
  sharpe12m: number | null;
  medianVol12m: number | null;
  pe: number | null;
}

export const EMPTY_FACTS: HoldingFacts = {
  marketcapCr: null,
  ret12m: null,
  vol12m: null,
  beta12m: null,
  sharpe12m: null,
  medianVol12m: null,
  pe: null,
};

export type FactColumnKey = keyof HoldingFacts;

export interface FactColumn {
  key: FactColumnKey;
  label: string;
  title: string;
}

/**
 * What the table shows, in this order. Labels match `COLUMN_DISPLAY` where that file
 * already named the column. "Bumpiness" is vol — the risk number we actually have.
 */
export const FACT_COLUMNS: readonly FactColumn[] = [
  { key: "marketcapCr", label: "Market cap", title: "Market capitalisation in ₹ crore" },
  { key: "ret12m", label: "1-yr return", title: "Absolute price return, 365 days" },
  { key: "vol12m", label: "Bumpiness", title: "Annualised volatility, 1 year" },
  { key: "beta12m", label: "Beta", title: "1-year beta versus the index" },
  { key: "sharpe12m", label: "Return vs risk", title: "1-year Sharpe ratio" },
  { key: "medianVol12m", label: "Liquidity", title: "Median 1-year traded value" },
  { key: "pe", label: "P/E", title: "Price / earnings" },
];

export function factsFromScreenRow(row: Record<string, unknown>): HoldingFacts {
  return {
    marketcapCr: numericField(row, "marketcap_cr"),
    ret12m: numericField(row, "ret_12m"),
    vol12m: numericField(row, "vol_12m"),
    beta12m: numericField(row, "beta_12m"),
    sharpe12m: numericField(row, "sharpe_12m"),
    medianVol12m: numericField(row, "median_vol_12m"),
    pe: numericField(row, "pe"),
  };
}

function finite(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Columns that have a number on at least one row. The rest stay off the table. */
export function visibleFactColumns(rows: readonly HoldingFacts[]): FactColumn[] {
  return FACT_COLUMNS.filter((column) => rows.some((row) => finite(row[column.key])));
}

/** Median 1-year volume is stored in rupees; crore is the readable unit above a crore. */
export function formatLiquidity(value: number | null | undefined): string {
  if (!finite(value)) return EMPTY_CELL;
  if (Math.abs(value) >= 10_000_000) return formatCrore(value / 10_000_000);
  return `₹${formatNumber(value, { decimals: 0 })}`;
}

export function formatFact(key: FactColumnKey, value: number | null | undefined): string {
  if (!finite(value)) return EMPTY_CELL;
  switch (key) {
    case "marketcapCr":
      return formatCrore(value);
    case "ret12m":
      return formatPercent(value);
    case "vol12m":
      return formatFraction(value);
    case "beta12m":
    case "sharpe12m":
      return formatNumber(value, { decimals: 2 });
    case "medianVol12m":
      return formatLiquidity(value);
    case "pe":
      return formatNumber(value, { decimals: 1 });
  }
}

export function withBasketFactColumns(saved: readonly string[] = []): string[] {
  return [...new Set(["symbol", "name", "sorting_factor", "close_raw", ...BASKET_FACT_COLUMNS, ...saved])];
}
