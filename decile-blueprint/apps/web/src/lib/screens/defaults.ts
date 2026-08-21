import {
  AWAY_FROM_HIGH_IGNORE,
  CIRCUITS_IGNORE_ABOVE,
  IGNORE_ABOVE_BETA_IGNORE,
  POSITIVE_DAYS_IGNORE,
  type ScreenDefinition,
} from "@decile/api-client";

/**
 * The screen definition's defaults, and the predicates that decide whether a field is *doing*
 * anything — docs/01 §2.4–§2.10 and `decile_core.screen_definition`.
 *
 * The sentinels are the reason this file exists. The reference product encodes "this filter is
 * off" inside the value itself, so a naive reader applies a filter the user meant to disable, and
 * a naive *writer* shows an accordion group with no active-count badge while it is quietly
 * removing two thousand rows. Every "is this on" question in the UI is answered here, by the same
 * predicates the Python model uses, so the badge and the query can never disagree.
 *
 * Note the asymmetry that catches people out: away-from-high and beta are `=== sentinel`, positive
 * days is `!== 0`, and **circuits is a threshold** — `> 250` means ignore, so 250 itself is a live
 * cap (docs/01 §2.6).
 */
export const WINDOW_KEYS = ["m12", "m9", "m6", "m3", "m1"] as const;
export type WindowKey = (typeof WINDOW_KEYS)[number];

/** docs/01 §3 renders the 12-month window as "1 Year", not "12 Months". */
export const WINDOW_LABELS: Record<WindowKey, string> = {
  m12: "1 Year",
  m9: "9 Months",
  m6: "6 Months",
  m3: "3 Months",
  m1: "1 Month",
};

export const MA_WINDOWS = [200, 100, 50, 20] as const;
export type MaWindow = (typeof MA_WINDOWS)[number];

/** The circuits sentinel is a threshold; this is the value the UI writes to mean "off". */
export const CIRCUITS_OFF_VALUE = 999;

export function defaultDefinition(): ScreenDefinition {
  return {
    index: "nifty-500",
    sort_by: "avg_sharpe_12_6_3_1",
    sort_direction: "desc",
    apply_filters_on: "all",
    min_return_1y: null,
    median_volume_1y: null,
    moving_average: {
      enabled: false,
      above_200: false,
      above_100: false,
      above_50: false,
      above_20: false,
      below_200: false,
      below_100: false,
      below_50: false,
      below_20: false,
    },
    away_from_high: { ath: AWAY_FROM_HIGH_IGNORE, one_year: AWAY_FROM_HIGH_IGNORE },
    positive_days: {
      m12: POSITIVE_DAYS_IGNORE,
      m9: POSITIVE_DAYS_IGNORE,
      m6: POSITIVE_DAYS_IGNORE,
      m3: POSITIVE_DAYS_IGNORE,
      m1: POSITIVE_DAYS_IGNORE,
    },
    circuits: {
      m12: CIRCUITS_OFF_VALUE,
      m9: CIRCUITS_OFF_VALUE,
      m6: CIRCUITS_OFF_VALUE,
      m3: CIRCUITS_OFF_VALUE,
      m1: CIRCUITS_OFF_VALUE,
    },
    marketcap: { from: null, to: null },
    pe: { enabled: false, from: null, to: null },
    series: ["EQ"],
    ignore_top_beta: { enabled: false, count: 0 },
    ignore_top_volatility: { enabled: false, count: 0 },
    ignore_above_beta: IGNORE_ABOVE_BETA_IGNORE,
    price: { from: null, to: null },
    factor_two: { enabled: false, sort_by: null, sort_direction: "desc" },
    factor_three: { enabled: false, sort_by: null, sort_direction: "desc" },
    historical_date: null,
    custom_filters: [],
  };
}

// --- "is this filter doing anything?" ---------------------------------------

export function awayFromHighActive(value: number): boolean {
  return value !== AWAY_FROM_HIGH_IGNORE;
}

export function positiveDaysActive(value: number): boolean {
  return value !== POSITIVE_DAYS_IGNORE;
}

/** docs/01 §2.6: "> 250 = ignore", so 250 is a live cap and 251 is not. */
export function circuitsActive(value: number): boolean {
  return value <= CIRCUITS_IGNORE_ABOVE;
}

export function ignoreAboveBetaActive(value: number): boolean {
  return value !== IGNORE_ABOVE_BETA_IGNORE;
}

export function rangeActive(range: { from: unknown; to: unknown }): boolean {
  return range.from !== null || range.to !== null;
}

export function movingAverageActive(ma: ScreenDefinition["moving_average"]): boolean {
  if (!ma.enabled) return false;
  return MA_WINDOWS.some((w) => ma[`above_${w}` as const] || ma[`below_${w}` as const]);
}

/** How many switches inside the moving-average group are on, for the badge. */
export function movingAverageCount(ma: ScreenDefinition["moving_average"]): number {
  if (!ma.enabled) return 0;
  return MA_WINDOWS.reduce(
    (total, w) =>
      total + (ma[`above_${w}` as const] ? 1 : 0) + (ma[`below_${w}` as const] ? 1 : 0),
    0,
  );
}
