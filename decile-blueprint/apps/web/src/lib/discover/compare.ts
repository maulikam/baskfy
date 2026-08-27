import type { ExploreBasketCard, ExploreMetrics } from "@/lib/explore/fetch";
import { EMPTY_CELL } from "@/lib/format";
import {
  type Absence,
  formatReturn,
  formatRupeesCompact,
  formatVolatility,
  uncomputedMetrics,
  volatilityBucketLabel,
} from "@/lib/discover/metrics";

/**
 * A comparison of two or three baskets, built so the columns are actually comparable.
 *
 * **The window has to be the same, or the table lies.** Each card picks its own headline window —
 * five-year CAGR if the basket is old enough, one-year return if it is not (`curated_metrics.py`
 * `headline_return`). That is right on a card and catastrophic in a comparison: putting a young
 * basket's one-year return beside an old basket's five-year CAGR in a row labelled "Return" makes
 * the young one look extraordinary, because one year of a good run is not an annualised five-year
 * figure. So the table finds the longest window **every** selected basket has, states it in the
 * row label, and says which baskets were too young when it had to shorten.
 *
 * **A metric nobody computes is a visible blank, not a missing row.** Dropping the drawdown row
 * because no basket has one lets a reader conclude the baskets are alike on drawdown. The row
 * stays, empty, with the reason. `docs/DISCOVER-METRICS-GAP.md` is the register of what those are.
 */

export type CompareSection =
  | "Performance"
  | "Downside"
  | "Consistency"
  | "Portfolio"
  | "Operations"
  | "Construction"
  | "Capital"
  | "Evidence";

export interface CompareCell {
  slug: string;
  value: string;
  /** Present exactly when this cell has no value. */
  absence?: Absence;
}

export interface CompareRow {
  key: string;
  section: CompareSection;
  label: string;
  explain: string;
  cells: CompareCell[];
  /** True when at least one column has a value. */
  anyAvailable: boolean;
}

/** The return windows, longest first. The first one every basket has is the one the table uses. */
const WINDOWS: readonly { key: keyof ExploreMetrics; label: string }[] = [
  { key: "cagr_5y", label: "5Y CAGR" },
  { key: "cagr_3y", label: "3Y CAGR" },
  { key: "ret_1y", label: "1Y return" },
  { key: "ret_6m", label: "6M return" },
  { key: "ret_1m", label: "1M return" },
];

export interface CommonWindow {
  key: keyof ExploreMetrics | null;
  label: string;
  /** Baskets that have no value at this window — named, so the shortening is not silent. */
  missing: string[];
  /** The longest window any single basket had, when it is longer than the common one. */
  shortenedFrom: string | null;
}

function hasValue(metrics: ExploreMetrics | null, key: keyof ExploreMetrics): boolean {
  const value = metrics?.[key];
  return value !== null && value !== undefined && value !== "";
}

/**
 * The longest return window every selected basket can support.
 *
 * When no window is shared, `key` is null and the caller renders the row empty with the reason
 * rather than quietly falling back to whatever each basket happened to have.
 */
export function commonWindow(baskets: readonly ExploreBasketCard[]): CommonWindow {
  if (baskets.length === 0) {
    return { key: null, label: "Return", missing: [], shortenedFrom: null };
  }
  const longestAnywhere = WINDOWS.find((window) =>
    baskets.some((basket) => hasValue(basket.metrics, window.key)),
  );
  const shared = WINDOWS.find((window) =>
    baskets.every((basket) => hasValue(basket.metrics, window.key)),
  );
  if (!shared) {
    return {
      key: null,
      label: "Return",
      missing: baskets.map((basket) => basket.name),
      shortenedFrom: longestAnywhere?.label ?? null,
    };
  }
  return {
    key: shared.key,
    label: shared.label,
    missing: [],
    shortenedFrom:
      longestAnywhere && longestAnywhere.key !== shared.key ? longestAnywhere.label : null,
  };
}

/** The sentence above the table explaining what window it settled on and why. */
export function windowNote(window: CommonWindow, baskets: readonly ExploreBasketCard[]): string {
  if (window.key === null) {
    return "These baskets share no return window — at least one of them has too little history for any standard period, so no like-for-like return can be shown.";
  }
  if (window.shortenedFrom) {
    const youngest = baskets
      .filter((basket) => !hasValue(basket.metrics, WINDOWS[0]!.key))
      .map((basket) => basket.name);
    const who = youngest.length > 0 ? ` ${youngest.join(" and ")} has less history.` : "";
    return `Every return below is measured over ${window.label}, the longest window all of these baskets share. One of them has a ${window.shortenedFrom} figure, which is not shown because comparing it with a shorter one would flatter it.${who}`;
  }
  return `Every return below is measured over ${window.label}, the same window for all of these baskets.`;
}

function cell(slug: string, value: string | null, absence?: Absence): CompareCell {
  if (value === null || value === EMPTY_CELL) {
    return {
      slug,
      value: EMPTY_CELL,
      absence: absence ?? { kind: "no-data", note: "Not available for this basket." },
    };
  }
  return { slug, value };
}

function row(
  key: string,
  section: CompareSection,
  label: string,
  explain: string,
  cells: CompareCell[],
): CompareRow {
  return {
    key,
    section,
    label,
    explain,
    cells,
    anyAvailable: cells.some((c) => c.absence === undefined),
  };
}

/**
 * Build the comparison.
 *
 * The section order is the brief's, and it is deliberate: Performance is first because that is
 * what a reader came for, and Downside is second so they cannot leave with the first number and
 * not the second.
 */
export function buildComparison(baskets: readonly ExploreBasketCard[]): {
  window: CommonWindow;
  rows: CompareRow[];
} {
  const window = commonWindow(baskets);
  const rows: CompareRow[] = [];

  rows.push(
    row(
      "return",
      "Performance",
      window.key ? `Return (${window.label})` : "Return",
      "The same period for every basket here. A longer window for one basket and a shorter one for another is not a comparison.",
      baskets.map((basket) =>
        window.key
          ? cell(basket.slug, formatReturn(basket.metrics?.[window.key] as string | number | null))
          : cell(basket.slug, null, {
              kind: "too-young",
              note: "No window is shared by all the selected baskets.",
            }),
      ),
    ),
  );

  rows.push(
    row(
      "return_convention",
      "Performance",
      "What the return counts",
      "A price return leaves out dividends. Two baskets computed on different conventions are not comparable, so the convention is stated rather than assumed.",
      baskets.map((basket) =>
        cell(
          basket.slug,
          basket.metrics
            ? basket.metrics.dividends_included
              ? "Price and dividends"
              : "Price only — dividends not included"
            : null,
        ),
      ),
    ),
  );

  rows.push(
    row(
      "volatility",
      "Downside",
      "Annualised volatility",
      "How much the basket's value moves about in a year, up or down — bigger swings in both directions, not a worse basket. Where a basket has too little history of its own, this figure is blended from its holdings, which ignores how they move together and reads high. `cb_metrics.volatility_basis` records which it is and the API does not expose it yet (docs/DISCOVER-METRICS-GAP.md).",
      baskets.map((basket) => {
        const value = basket.metrics?.volatility_value;
        if (value === null || value === undefined || value === "") return cell(basket.slug, null);
        const bucket = volatilityBucketLabel(basket.metrics?.volatility_bucket);
        return cell(
          basket.slug,
          bucket ? `${formatVolatility(value)} · ${bucket}` : formatVolatility(value),
        );
      }),
    ),
  );

  // Everything the brief asks for that nothing in this product computes. Rendered as blanks with
  // reasons rather than omitted, so absence cannot be mistaken for equality.
  const gapSections: Record<string, CompareSection> = {
    max_drawdown: "Downside",
    recovery_days: "Downside",
    sharpe: "Downside",
    sortino: "Downside",
    rolling_1y_positive_pct: "Consistency",
    turnover_pct: "Operations",
    benchmark_delta_pct: "Performance",
    top10_concentration_pct: "Portfolio",
  };
  for (const metric of uncomputedMetrics()) {
    rows.push(
      row(
        metric.key,
        gapSections[metric.key] ?? "Evidence",
        metric.label,
        metric.explain,
        baskets.map((basket) => cell(basket.slug, null, metric.absence)),
      ),
    );
  }

  rows.push(
    row(
      "rebalance",
      "Operations",
      "Rebalance schedule",
      "How often the holdings are recomputed. More often means more trading, and more trading costs money whether or not it helps.",
      baskets.map((basket) => cell(basket.slug, basket.rebalance_frequency.toLowerCase())),
    ),
  );

  rows.push(
    row(
      "strategy",
      "Construction",
      "Strategy tags",
      "What the strategy says it does. Two baskets with the same tags are likely to hold similar things.",
      baskets.map((basket) =>
        cell(basket.slug, basket.categories.length > 0 ? basket.categories.join(", ") : null),
      ),
    ),
  );

  rows.push(
    row(
      "manager",
      "Construction",
      "Run by",
      "Who publishes and rebalances it. Baskets from one publisher can share assumptions that are not visible in the numbers.",
      baskets.map((basket) => cell(basket.slug, basket.manager.name)),
    ),
  );

  rows.push(
    row(
      "min_amount",
      "Capital",
      "Minimum investment",
      "The least you can put in and still buy at least one share of every holding.",
      baskets.map((basket) => {
        const min = basket.metrics?.min_amount;
        return min === null || min === undefined || min === ""
          ? cell(basket.slug, null)
          : cell(basket.slug, formatRupeesCompact(min));
      }),
    ),
  );

  rows.push(
    row(
      "access",
      "Capital",
      "Access",
      "Whether the basket is free to view and follow, or behind a plan.",
      baskets.map((basket) => cell(basket.slug, basket.access.toLowerCase())),
    ),
  );

  rows.push(
    row(
      "evidence",
      "Evidence",
      "Live since",
      "When the basket started publishing. Everything before that date is simulated, and simulated results are not results.",
      baskets.map((basket) => cell(basket.slug, basket.launched_at?.slice(0, 10) ?? null)),
    ),
  );

  rows.push(
    row(
      "as_of",
      "Evidence",
      "Figures as of",
      "The date these numbers were computed. Two baskets priced on different days are not quite comparable.",
      baskets.map((basket) => cell(basket.slug, basket.metrics?.as_of_date ?? null)),
    ),
  );

  return { window, rows };
}

export const SECTION_ORDER: readonly CompareSection[] = [
  "Performance",
  "Downside",
  "Consistency",
  "Portfolio",
  "Operations",
  "Construction",
  "Capital",
  "Evidence",
];

export function rowsBySection(rows: readonly CompareRow[]): [CompareSection, CompareRow[]][] {
  const grouped: [CompareSection, CompareRow[]][] = SECTION_ORDER.map((section) => [
    section,
    rows.filter((entry) => entry.section === section),
  ]);
  return grouped.filter(([, sectionRows]) => sectionRows.length > 0);
}

/**
 * "What is different" — the differences a reader would otherwise have to spot themselves.
 *
 * Only rows where the columns genuinely disagree, and only rows that have values: two blanks are
 * not a difference, and reporting them as one would turn the metrics gap into false signal.
 */
export function differences(rows: readonly CompareRow[]): CompareRow[] {
  return rows.filter((row) => {
    if (!row.anyAvailable) return false;
    const values = row.cells.map((c) => c.value);
    return new Set(values).size > 1;
  });
}
