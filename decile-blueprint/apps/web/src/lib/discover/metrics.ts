import { EMPTY_CELL, formatFraction, formatNumber, formatPercent } from "@/lib/format";
import type { ExploreBasketCard, ExploreMetrics } from "@/lib/explore/fetch";

/**
 * One place where a basket's numbers become text, and the only place.
 *
 * Two defects made this module necessary rather than nice.
 *
 * **The missing percent sign.** The card appended `%` only when the value was a `number`
 * (`components/basket/basket-card.tsx`, before this change). Every real value is a `string`:
 * the API types the field `string | number | null` because it is a Pydantic `Decimal`, and a
 * `Decimal` serialises to JSON as a string. The branch that added the unit could therefore never
 * run in production and always ran in tests, which is the worst arrangement of the two. Formatting
 * that lives next to the type that describes the payload cannot drift from it that way again.
 *
 * **Absent is not zero.** Most of what a reader would want — maximum drawdown, Sharpe, turnover,
 * benchmark difference — is not computed anywhere in this product (see `docs/DISCOVER-METRICS-GAP.md`).
 * A comparison table that silently omits those rows implies the baskets are equal on them. So a
 * metric here is a *record with an availability*, and "not computed yet" is a value it can take.
 */

/** Why a figure is not on the screen. Never rendered as a number, always as a sentence. */
export type Absence =
  | { kind: "not-computed"; note: string }
  | { kind: "too-young"; note: string }
  | { kind: "no-data"; note: string };

export type MetricTone = "return" | "risk" | "capital" | "operations" | "evidence";

export interface DisplayMetric {
  key: string;
  /** The name of the measure, never an abbreviation a reader has to decode. */
  label: string;
  /** Formatted with its unit, or the em dash when absent. */
  value: string;
  tone: MetricTone;
  /** Present exactly when the figure could not be shown. */
  absence?: Absence;
  /**
   * One sentence explaining what the measure is. Powers the brief's "explain this metric"
   * interaction, and lives with the metric so the two cannot disagree.
   */
  explain: string;
}

export function isAvailable(metric: DisplayMetric): boolean {
  return metric.absence === undefined;
}

/** A return, signed and with its unit. Accepts the string the API actually sends. */
export function formatReturn(value: string | number | null | undefined): string {
  return formatPercent(value, 2);
}

/**
 * Volatility, stored as a fraction and displayed as a percentage.
 *
 * Unsigned: a "+18.20%" volatility would read as a gain. `lib/format.formatFraction` owns the
 * ×100, which docs/06a §10 and docs/07a §13 record as deliberately absent from the API so that
 * exactly one place in the system does it.
 */
export function formatVolatility(value: string | number | null | undefined): string {
  return formatFraction(value, 1);
}

/** ₹, grouped Indian-style, no paise — a minimum investment is never quoted to the rupee. */
export function formatRupees(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  const numeric = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(numeric)) return EMPTY_CELL;
  return `₹${formatNumber(numeric, { decimals: 0 })}`;
}

/**
 * "₹6.84L" — the compact form the brief's card uses, for places where the column is narrow.
 * Lakh and crore, because this is an India-equities product and ₹684,000 is harder to read here.
 */
export function formatRupeesCompact(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  const numeric = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(numeric)) return EMPTY_CELL;
  const abs = Math.abs(numeric);
  if (abs >= 10_000_000) return `₹${formatNumber(numeric / 10_000_000, { decimals: 2 })}Cr`;
  if (abs >= 100_000) return `₹${formatNumber(numeric / 100_000, { decimals: 2 })}L`;
  if (abs >= 1_000) return `₹${formatNumber(numeric / 1_000, { decimals: 1 })}K`;
  return `₹${formatNumber(numeric, { decimals: 0 })}`;
}

/**
 * What the volatility bucket actually means, in the reader's terms.
 *
 * The card used to label this "Swing", which names nothing: it does not say what is swinging,
 * over what period, or measured how. These are the three buckets the API sends.
 */
const VOLATILITY_BUCKET_COPY: Record<string, string> = {
  LOW: "Low",
  MEDIUM: "Medium",
  MED: "Medium",
  HIGH: "High",
};

export function volatilityBucketLabel(bucket: string | null | undefined): string | null {
  if (!bucket) return null;
  return VOLATILITY_BUCKET_COPY[bucket.toUpperCase()] ?? null;
}

/**
 * Whether the volatility figure was measured on the basket itself or inferred from its holdings.
 *
 * `CONSTITUENT_WEIGHTED` means the basket has too little history of its own, so the number is a
 * weighted blend of its stocks' volatilities and ignores how they move together — it will read
 * high, because real diversification is exactly the effect it cannot see. A reader comparing two
 * baskets on this number deserves to know which of the two it is.
 */
export function volatilityBasisNote(basis: string | null | undefined): string | null {
  if (!basis) return null;
  if (basis.toUpperCase() === "CONSTITUENT_WEIGHTED") {
    return "Blended from the holdings' own volatility — the basket has too little history to measure directly, and this ignores how the holdings move together.";
  }
  if (basis.toUpperCase() === "BASKET") {
    return "Measured on the basket's own history.";
  }
  return null;
}

const NOT_COMPUTED_NOTE =
  "Not computed yet — this product does not store it. See docs/DISCOVER-METRICS-GAP.md.";

function notComputed(key: string, label: string, tone: MetricTone, explain: string): DisplayMetric {
  return {
    key,
    label,
    value: EMPTY_CELL,
    tone,
    absence: { kind: "not-computed", note: NOT_COMPUTED_NOTE },
    explain,
  };
}

/**
 * The figures the brief's card and comparison table ask for that nothing in this product computes.
 *
 * They are returned as real rows so a comparison shows a blank where a blank is the truth, rather
 * than dropping the row and letting a reader assume the baskets are alike on it. `docs/DISCOVER-METRICS-GAP.md`
 * is the register; this is the same list in the type system, and a test holds the two together.
 */
export const UNCOMPUTED_METRIC_KEYS = [
  "max_drawdown",
  "recovery_days",
  "sharpe",
  "sortino",
  "rolling_1y_positive_pct",
  "turnover_pct",
  "benchmark_delta_pct",
  "top10_concentration_pct",
] as const;

export type UncomputedMetricKey = (typeof UNCOMPUTED_METRIC_KEYS)[number];

export function uncomputedMetrics(): DisplayMetric[] {
  return [
    notComputed(
      "max_drawdown",
      "Maximum drawdown",
      "risk",
      "The deepest fall from a previous high, peak to trough. It is the loss a holder would actually have had to sit through.",
    ),
    notComputed(
      "recovery_days",
      "Recovery time",
      "risk",
      "How long the basket took to get back to its previous high after its worst fall.",
    ),
    notComputed(
      "sharpe",
      "Sharpe ratio",
      "risk",
      "Return above the risk-free rate per unit of volatility. Higher means more of the return came from skill than from taking risk.",
    ),
    notComputed(
      "sortino",
      "Sortino ratio",
      "risk",
      "Like Sharpe, but counting only downward moves as risk — upside volatility is not punished.",
    ),
    notComputed(
      "rolling_1y_positive_pct",
      "Positive rolling years",
      "evidence",
      "Of every one-year window in the history, the share that ended up. It answers 'how often did patience pay?' better than one headline number.",
    ),
    notComputed(
      "turnover_pct",
      "Annual turnover",
      "operations",
      "How much of the basket is bought and sold in a year. High turnover means more brokerage, more tax, and more slippage.",
    ),
    notComputed(
      "benchmark_delta_pct",
      "vs benchmark",
      "return",
      "The difference between this basket's return and its benchmark's over the same period.",
    ),
    notComputed(
      "top10_concentration_pct",
      "Top-10 weight",
      "risk",
      "The share of the basket held in its ten largest positions. High concentration means single stocks drive the result.",
    ),
  ];
}

/**
 * Risk first, then return, then capital — the order the brief asks for and the order that stops a
 * reader forming an opinion from a number before they know what it cost to earn.
 */
export function riskMetrics(metrics: ExploreMetrics | null): DisplayMetric[] {
  const bucket = volatilityBucketLabel(metrics?.volatility_bucket);
  const value = metrics?.volatility_value ?? null;
  const rows: DisplayMetric[] = [];

  rows.push(
    value === null || value === ""
      ? {
          key: "volatility",
          label: "Annualised volatility",
          value: EMPTY_CELL,
          tone: "risk",
          absence: {
            kind: "no-data",
            note: "No volatility has been computed for this basket yet.",
          },
          explain:
            "How much the basket's value moves about in a year, up or down. A higher number means bigger swings in both directions, not a worse basket.",
        }
      : {
          key: "volatility",
          label: "Annualised volatility",
          value: bucket
            ? `${formatVolatility(value)} · ${bucket}`
            : formatVolatility(value),
          tone: "risk",
          explain:
            "How much the basket's value moves about in a year, up or down. A higher number means bigger swings in both directions, not a worse basket.",
        },
  );

  return rows;
}

export function returnMetrics(metrics: ExploreMetrics | null): DisplayMetric[] {
  if (!metrics || metrics.headline_pct === null || metrics.headline_pct === undefined) {
    return [
      {
        key: "headline",
        label: metrics?.headline_label ?? "Return",
        value: EMPTY_CELL,
        tone: "return",
        absence: {
          kind: "too-young",
          note: "Too little history to state a return over any standard window.",
        },
        explain:
          "The longest return window this basket has enough history for. A younger basket gets a shorter window, never a longer one filled in.",
      },
    ];
  }
  return [
    {
      key: "headline",
      label: metrics.headline_label ?? "Return",
      value: formatReturn(metrics.headline_pct),
      tone: "return",
      explain:
        "The longest return window this basket has enough history for. A younger basket gets a shorter window, never a longer one filled in.",
    },
  ];
}

export function capitalMetrics(basket: ExploreBasketCard): DisplayMetric[] {
  const min = basket.metrics?.min_amount ?? null;
  return [
    min === null || min === ""
      ? {
          key: "min_amount",
          label: "Minimum investment",
          value: EMPTY_CELL,
          tone: "capital",
          absence: {
            kind: "no-data",
            note: "No minimum has been computed for this basket yet.",
          },
          explain: MIN_AMOUNT_EXPLAIN,
        }
      : {
          key: "min_amount",
          label: "Minimum investment",
          value: formatRupeesCompact(min),
          tone: "capital",
          explain: MIN_AMOUNT_EXPLAIN,
        },
  ];
}

/**
 * The sentence that makes a six-lakh minimum comprehensible.
 *
 * It already existed, on the wrong surface: `components/basket/sizing-controls.tsx` says it on the
 * detail page, where the reader has already decided to look. The card is where the number first
 * causes alarm, so the reason belongs there too.
 */
export const MIN_AMOUNT_EXPLAIN =
  "The least you can put in and still buy at least one share of every holding. Baskets holding expensive shares need more, which is a fact about share prices rather than about the strategy.";

/** Risk before return before capital, as one list. */
export function cardMetrics(basket: ExploreBasketCard): DisplayMetric[] {
  return [
    ...riskMetrics(basket.metrics),
    ...returnMetrics(basket.metrics),
    ...capitalMetrics(basket),
  ];
}
