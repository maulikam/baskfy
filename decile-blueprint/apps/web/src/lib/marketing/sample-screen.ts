import "server-only";

import type { ScreenRunResponse, ScreenRunRowOut } from "@baskfy/api-client";

import { apiOrigin } from "@/lib/api/config";
import { PUBLISHED_DATA_TAG } from "@/lib/market/fetch";

/**
 * The landing page's "live sample screen preview" (Prompt 18 deliverable 1).
 *
 * **It is a real screen run, not a screenshot.** The definition below is the one docs/13 captured —
 * NIFTY TOTAL MARKET, ranked by the 12/6/3/1-month average Sharpe return, with a ₹1 crore median
 * traded-value floor — which is also seeded as the first example screen (`baskfy_core.seed_data`,
 * `exmpl0000001`). Someone who signs up sees the same numbers under the same name, which is the
 * point of showing them at all.
 *
 * `POST /screens/preview` rather than `POST /screens/{id}/run`: a preview persists nothing, and a
 * marketing page that wrote a `screen_run` audit row every time ISR revalidated would be filling
 * an audit table with traffic.
 *
 * ## Why a null return rather than a fallback table
 *
 * If the API cannot be reached at build time this returns `null` and the page says so. The
 * alternative — shipping the committed reference export as canned rows — would put a table of
 * real-looking, months-stale numbers on a marketing page with nothing on screen to say they were
 * not live. docs/14 §Tone: the product's credibility "comes from showing its work". An empty slot
 * that admits it is empty costs a visitor nothing; a stale one costs the claim.
 */
const SAMPLE_ROWS = 8;

/** Revalidated hourly, and invalidated outright by `POST /api/revalidate` at the nightly publish. */
const REVALIDATE_SECONDS = 3600;

export const SAMPLE_SCREEN_NAME = "Investing 001";
export const SAMPLE_SCREEN_UNIVERSE = "NIFTY TOTAL MARKET";
export const SAMPLE_SCREEN_FACTOR = "Average Sharpe return, 12/6/3/1 months";

const SAMPLE_COLUMNS = ["close_raw", "marketcap_cr", "ret_12m", "sharpe_12m", "vol_12m"] as const;

const SAMPLE_DEFINITION = {
  index: "nifty-total-market",
  sort_by: "avg_sharpe_12_6_3_1",
  sort_direction: "desc",
  median_volume_1y: 10_000_000,
  series: ["EQ", "BE"],
} as const;

export interface SampleCell {
  key: string;
  label: string;
  /** Already formatted for display; the API rounds at write time (CLAUDE.md house rule 8). */
  value: string;
}

export interface SampleRow {
  rank: number;
  symbol: string;
  name: string;
  cells: SampleCell[];
  /**
   * The same columns as `cells`, unformatted.
   *
   * The table wants "+34.2%"; a plot wants 34.2. Keeping both off one fetch means the figure and
   * the table on the landing page can never disagree about a row — they are the same row. `null`
   * where the API sent no value, so a plot can leave a gap rather than draw a zero.
   */
  values: Record<string, number | null>;
}

export interface SampleScreen {
  asOf: string;
  resultCount: number;
  rows: SampleRow[];
}

export const SAMPLE_COLUMN_LABELS: Record<string, string> = {
  close_raw: "Close",
  marketcap_cr: "M-cap (₹ cr)",
  ret_12m: "1Y return",
  sharpe_12m: "Sharpe 1Y",
  vol_12m: "Volatility 1Y",
};

function render(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  /* `ScreenRunRowOut` is `additionalProperties: unknown` — the column set is the user's, not the
     schema's — so a cell arrives typed as `unknown` and is narrowed here. The API rounds at write
     time and serves `numeric` columns as strings, hence the string branch. */
  if (typeof value !== "number" && typeof value !== "string") return "—";
  const numeric = Number(value);
  if (Number.isNaN(numeric)) return typeof value === "string" ? value : "—";
  /* docs/06a §10: volatility is stored and served as a decimal fraction; the ×100 belongs on the
     display side and nowhere else. `lib/format.ts` owns that rule for the app; this repeats the
     *decision*, not the arithmetic, because the marketing table formats five columns and pulling
     in the table's cell renderer would pull in TanStack Table with it. */
  if (key === "vol_12m") return `${(numeric * 100).toFixed(1)}%`;
  if (key === "ret_12m") return `${numeric > 0 ? "+" : ""}${numeric.toFixed(1)}%`;
  if (key === "sharpe_12m") return numeric.toFixed(2);
  if (key === "marketcap_cr") return numeric.toLocaleString("en-IN");
  return numeric.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** The raw number behind a cell, or `null` when the API sent nothing usable. */
function numeric(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const parsed = Number(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function toRow(row: ScreenRunRowOut): SampleRow {
  const source = row as Record<string, unknown>;
  return {
    rank: row.rank,
    symbol: row.symbol,
    name: row.name,
    values: Object.fromEntries(
      SAMPLE_COLUMNS.map((key) => [key, numeric(source[key])]),
    ) as Record<string, number | null>,
    cells: SAMPLE_COLUMNS.map((key) => ({
      key,
      label: SAMPLE_COLUMN_LABELS[key] ?? key,
      value: render(key, (row as Record<string, unknown>)[key]),
    })),
  };
}

export async function fetchSampleScreen(): Promise<SampleScreen | null> {
  try {
    const response = await fetch(`${apiOrigin()}/api/v1/screens/preview`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ definition: SAMPLE_DEFINITION, columns: [...SAMPLE_COLUMNS] }),
      next: { tags: [PUBLISHED_DATA_TAG], revalidate: REVALIDATE_SECONDS },
    });
    if (!response.ok) return null;
    const payload = (await response.json()) as ScreenRunResponse;
    return {
      asOf: payload.as_of,
      resultCount: payload.result_count,
      rows: payload.rows.slice(0, SAMPLE_ROWS).map(toRow),
    };
  } catch {
    return null;
  }
}
