import type { BacktestOut } from "@baskfy/api-client";

import { formatNumber, formatPercent } from "@/lib/format";

/**
 * How docs/10 §Outputs' metrics are read on the page.
 *
 *     "**Metrics:** CAGR, total return, annualised volatility, Sharpe (rf from a configurable
 *      T-bill series), Sortino, max drawdown + its dates, Calmar, hit rate, average win/loss,
 *      annual turnover, total costs paid, exposure %, best/worst month, rolling 12-month return
 *      distribution, alpha/beta vs benchmark, tracking error, information ratio."
 *
 * The API stores that block verbatim as `backtest.metrics` (a JSON object produced by
 * `baskfy_core.backtest_metrics.Metrics.as_dict`), so the page reads it by key rather than
 * through twenty-eight typed optional fields. Every key docs/10 names appears in `METRIC_ROWS`
 * below, which is what `metrics.test.ts` asserts — a metric quietly dropped from the table is a
 * metric the user was promised and does not get.
 *
 * Rupee amounts arrive as **strings**: `Decimal` does not survive JSON as a number, and
 * `float("1234567.89")` is not `1234567.89`. They are formatted from the string.
 */

export type MetricUnit = "percent" | "ratio" | "money" | "date" | "count" | "months";

export interface MetricRow {
  key: string;
  label: string;
  unit: MetricUnit;
  /** One line explaining what the number means. docs/08 §"Design principles" — never a bare number. */
  hint: string;
}

/** docs/10 §Outputs, in the document's own order. */
export const METRIC_ROWS: readonly MetricRow[] = [
  {
    key: "cagr",
    label: "CAGR",
    unit: "percent",
    hint: "Compound annual growth rate over the whole window.",
  },
  {
    key: "total_return",
    label: "Total return",
    unit: "percent",
    hint: "What ₹1 became, start to end, net of every cost.",
  },
  {
    key: "annualised_volatility",
    label: "Annualised volatility",
    unit: "percent",
    hint: "Standard deviation of daily returns, annualised on the observed trading-day count.",
  },
  {
    key: "sharpe",
    label: "Sharpe",
    unit: "ratio",
    hint: "Excess return per unit of volatility, at the configured risk-free rate.",
  },
  {
    key: "sortino",
    label: "Sortino",
    unit: "ratio",
    hint: "Excess return per unit of downside volatility only.",
  },
  {
    key: "max_drawdown",
    label: "Max drawdown",
    unit: "percent",
    hint: "The deepest peak-to-trough fall in equity.",
  },
  {
    key: "max_drawdown_peak",
    label: "Drawdown peak",
    unit: "date",
    hint: "The high-water mark the deepest drawdown fell from.",
  },
  {
    key: "max_drawdown_trough",
    label: "Drawdown trough",
    unit: "date",
    hint: "The day the deepest drawdown bottomed out.",
  },
  {
    key: "max_drawdown_recovered",
    label: "Recovered on",
    unit: "date",
    hint: "When equity next reached the old high. Blank means it never did.",
  },
  {
    key: "calmar",
    label: "Calmar",
    unit: "ratio",
    hint: "CAGR divided by the max drawdown — return per unit of worst-case pain.",
  },
  {
    key: "hit_rate",
    label: "Hit rate",
    unit: "percent",
    hint: "Share of completed round trips that made money.",
  },
  {
    key: "average_win",
    label: "Average win",
    unit: "money",
    hint: "Mean profit on a winning round trip, net of costs.",
  },
  {
    key: "average_loss",
    label: "Average loss",
    unit: "money",
    hint: "Mean loss on a losing round trip, net of costs.",
  },
  {
    key: "annual_turnover",
    label: "Annual turnover",
    unit: "ratio",
    hint: "One-way traded notional over average equity, per year. 1.0 means the book turned over once.",
  },
  {
    key: "total_costs",
    label: "Total costs paid",
    unit: "money",
    hint: "Brokerage, STT and slippage charged across every fill.",
  },
  {
    key: "exposure",
    label: "Exposure",
    unit: "percent",
    hint: "Average share of the book that was invested rather than in cash.",
  },
  {
    key: "best_month",
    label: "Best month",
    unit: "months",
    hint: "The strongest calendar month in the run.",
  },
  {
    key: "worst_month",
    label: "Worst month",
    unit: "months",
    hint: "The weakest calendar month in the run.",
  },
  {
    key: "alpha",
    label: "Alpha vs benchmark",
    unit: "percent",
    hint: "Annualised intercept of the portfolio's excess return regressed on the benchmark's.",
  },
  {
    key: "beta",
    label: "Beta vs benchmark",
    unit: "ratio",
    hint: "Sensitivity to the benchmark. 1.0 moves with it.",
  },
  {
    key: "tracking_error",
    label: "Tracking error",
    unit: "percent",
    hint: "Annualised standard deviation of the difference from the benchmark.",
  },
  {
    key: "information_ratio",
    label: "Information ratio",
    unit: "ratio",
    hint: "Active return per unit of tracking error.",
  },
  {
    key: "benchmark_total_return",
    label: "Benchmark total return",
    unit: "percent",
    hint: "What the benchmark did over the same window.",
  },
  {
    key: "trades",
    label: "Fills",
    unit: "count",
    hint: "Every buy and sell the simulation executed.",
  },
  {
    key: "round_trips",
    label: "Round trips",
    unit: "count",
    hint: "Positions opened and fully closed.",
  },
  {
    key: "delistings",
    label: "Delistings taken",
    unit: "count",
    hint: "Holdings liquidated because the instrument stopped trading. Never dropped silently.",
  },
];

export type MetricBag = Record<string, unknown>;

/** `backtest.metrics` is stored as an opaque JSON object; this is the only place it is narrowed. */
export function metricsOf(backtest: BacktestOut | undefined): MetricBag {
  const value: unknown = backtest?.metrics;
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as MetricBag)
    : {};
}

export function metricValue(bag: MetricBag, key: string): unknown {
  return bag[key];
}

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

/** The em dash is the app's "we do not know", used everywhere else too (`lib/format.ts`). */
export const UNKNOWN = "—";

export function formatMetric(value: unknown, unit: MetricUnit): string {
  if (value === null || value === undefined) return UNKNOWN;
  switch (unit) {
    case "percent":
      return typeof value === "number" ? formatPercent(value * 100, 2) : UNKNOWN;
    case "ratio":
      return typeof value === "number" ? formatNumber(value, { decimals: 2 }) : UNKNOWN;
    case "money": {
      const numeric = typeof value === "string" ? Number(value) : value;
      return typeof numeric === "number" && Number.isFinite(numeric)
        ? INR.format(numeric)
        : UNKNOWN;
    }
    case "count":
      return typeof value === "number" ? formatNumber(value, { decimals: 0 }) : UNKNOWN;
    case "date":
      return typeof value === "string" ? value : UNKNOWN;
    case "months":
      return formatMonthCell(value);
  }
}

/** `{"month": "2019-03", "return": 0.081}` → `2019-03 · +8.10%`. */
export function formatMonthCell(value: unknown): string {
  if (typeof value !== "object" || value === null) return UNKNOWN;
  const cell = value as { month?: unknown; return?: unknown };
  if (typeof cell.month !== "string" || typeof cell.return !== "number") return UNKNOWN;
  return `${cell.month} · ${formatPercent(cell.return * 100, 2)}`;
}

/** docs/10: "rolling 12-month return distribution". */
export interface RollingDistribution {
  count: number;
  min: number | null;
  p05: number | null;
  median: number | null;
  p95: number | null;
  max: number | null;
  negative_share: number | null;
}

export function rollingOf(bag: MetricBag): RollingDistribution | null {
  const value = bag.rolling_12m;
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.count !== "number" || record.count === 0) return null;
  return {
    count: record.count,
    min: numberOrNull(record.min),
    p05: numberOrNull(record.p05),
    median: numberOrNull(record.median),
    p95: numberOrNull(record.p95),
    max: numberOrNull(record.max),
    negative_share: numberOrNull(record.negative_share),
  };
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}
