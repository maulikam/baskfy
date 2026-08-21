import type { FactsheetOut } from "@baskfy/api-client";

/**
 * Factsheet payloads for the render tests.
 *
 * `CUPID_FACTSHEET` carries the numbers from `tests/fixtures/reference-screen-export-2026-08-18.csv`
 * — the same row `services/api/tests/test_api_instruments.py` asserts the API produces. This file
 * is about *rendering* those numbers; that suite is about deriving them.
 *
 * `YOUNG_FACTSHEET` is a four-month-old listing: every window longer than 1M is `null`, which is
 * Prompt 10's third acceptance criterion — the page must show an em dash, never a zero.
 */

const WINDOWS = ["1Y", "9M", "6M", "3M", "1M"] as const;

function cells(
  prefix: string,
  values: readonly (string | null)[],
  percentiles: readonly (number | null)[] = [],
) {
  return WINDOWS.map((label, index) => ({
    key: `${prefix}${label === "1Y" ? "12m" : label.toLowerCase()}`,
    label,
    value: values[index] ?? null,
    percentile: percentiles[index] ?? null,
  }));
}

export const CUPID_FACTSHEET: FactsheetOut = {
  symbol: "CUPID",
  as_of: "2026-08-18",
  data_version: 42,
  header: {
    symbol: "CUPID",
    name: "CUPID LIMITED",
    exchange: "NSE",
    close_raw: "284.03",
    close: "284.03",
    isin: "INE509F01011",
  },
  key_stats: [
    { key: "pe", label: "P/E", value: null, percentile: null },
    { key: "marketcap_cr", label: "Marketcap (cr)", value: 38192, percentile: null },
    { key: "beta_12m", label: "Beta", value: "0.8412554591", percentile: null },
    { key: "series", label: "Series", value: "EQ", percentile: null },
    { key: "listed_on", label: "Listed On", value: null, percentile: null },
  ],
  pros: [
    "The close is above 200-day moving average.",
    "The close is above 100-day moving average.",
    "The close is above 50-day moving average.",
    "The close is above 20-day moving average.",
    "The close is within 25% of all time high.",
    "The beta is less than 1.25",
    "More than 55% of days in the last year closed positive.",
    "Median daily turnover is above ₹1 crore.",
  ],
  cons: [],
  undecided: [],
  metric_cards: [
    { key: "close", label: "Closing Price", value: "284.03", median: "112.40", observations: 765 },
    {
      key: "ret_12m",
      label: "Rolling 1-Yr Returns (%)",
      value: "726.63",
      median: "726.63",
      observations: 1,
    },
    { key: "pe", label: "Price to Earnings", value: null, median: null, observations: 0 },
    { key: "marketcap_cr", label: "Marketcap", value: 38192, median: 38192, observations: 1 },
    { key: "rsi_12m", label: "1-Year RSI", value: "67.2928", median: "67.2928", observations: 1 },
  ],
  price_and_mas: [
    { key: "face_value", label: "Face Value", value: null, percentile: null },
    { key: "high_1y", label: "1Y High", value: "299.00", percentile: null },
    { key: "away_high_1y", label: "Away from 1Y High", value: "-5.01", percentile: null },
    { key: "high_ath", label: "ATH", value: "299.00", percentile: null },
    { key: "away_high_ath", label: "Away from ATH", value: "-5.01", percentile: null },
    { key: "ma_200", label: "MA 200", value: "120.21", percentile: null },
    { key: "ma_100", label: "MA 100", value: "162.77", percentile: null },
    { key: "ma_50", label: "MA 50", value: "213.47", percentile: null },
    { key: "ma_20", label: "MA 20", value: "249.53", percentile: null },
  ],
  returns: [
    ...cells("ret_", ["726.63", "328.85", "242.87", "148.41", "37.03"], [0.99, 0.98, 0.97, 0.96, 0.9]),
    { key: "ret_12m_minus_1m", label: "12M-1M", value: "688.35", percentile: 0.99 },
    { key: "ret_12m_minus_2m", label: "12M-2M", value: "601.22", percentile: 0.98 },
  ],
  sharpe_returns: cells("sharpe_", ["12.54", "5.43", "4.40", "2.92", "0.67"], [0.99, 0.97, 0.96, 0.94, 0.7]),
  volatility: cells(
    "vol_",
    ["0.57931794", "0.60583091", "0.55192076", "0.50887624", "0.55115164"],
    [0.88, 0.9, 0.87, 0.85, 0.86],
  ),
  rsi: cells("rsi_", ["67.2928", "66.5956", "70.3243", "74.9039", "75.3903"], [0.9, 0.89, 0.92, 0.95, 0.96]),
  market_quality: {
    regime: "BULL",
    regime_distance_bull: 0.0031,
    regime_distance_bear: 0.0184,
    median_vol_12m: "1687913366",
    circuits: WINDOWS.map((label, index) => ({
      label,
      value: [1, 1, 0, 0, 0][index] ?? 0,
    })),
    positive_days: WINDOWS.map((label, index) => ({
      label,
      value: ["65.59", "68.65", "71.07", "79.69", "72.73"][index] ?? null,
    })),
  },
  corporate_actions: [
    { action_type: "bonus", ex_date: "2025-06-12", ratio_from: "4", ratio_to: "1", amount: null },
    { action_type: "split", ex_date: "2024-02-08", ratio_from: "10", ratio_to: "1", amount: null },
    { action_type: "dividend", ex_date: "2023-09-01", ratio_from: null, ratio_to: null, amount: "1.50" },
  ],
  index_memberships: [
    { slug: "nifty-total-market", name: "NIFTY TOTAL MARKET" },
    { slug: "nifty-microcap-250", name: "NIFTY MICROCAP 250" },
  ],
  percentile_universe: { slug: "nifty-microcap-250", name: "NIFTY MICROCAP 250" },
  notes: [],
};

export const YOUNG_FACTSHEET: FactsheetOut = {
  ...CUPID_FACTSHEET,
  symbol: "YOUNGCO",
  header: {
    symbol: "YOUNGCO",
    name: "YOUNGCO LIMITED",
    exchange: "NSE",
    close_raw: "101.00",
    close: "101.00",
    isin: null,
  },
  key_stats: [
    { key: "pe", label: "P/E", value: null, percentile: null },
    { key: "marketcap_cr", label: "Marketcap (cr)", value: null, percentile: null },
    { key: "beta_12m", label: "Beta", value: null, percentile: null },
    { key: "series", label: "Series", value: "EQ", percentile: null },
    { key: "listed_on", label: "Listed On", value: "2026-04-20", percentile: null },
  ],
  pros: ["The close is above 20-day moving average."],
  cons: [],
  undecided: [
    "close_above_ma_200",
    "close_above_ma_100",
    "close_above_ma_50",
    "within_25pct_of_ath",
    "beta_below_ceiling",
    "positive_days_above_floor",
    "liquid_enough",
  ],
  metric_cards: [
    { key: "close", label: "Closing Price", value: "101.00", median: "98.50", observations: 82 },
    {
      key: "ret_12m",
      label: "Rolling 1-Yr Returns (%)",
      value: null,
      median: null,
      observations: 0,
    },
    { key: "pe", label: "Price to Earnings", value: null, median: null, observations: 0 },
    { key: "marketcap_cr", label: "Marketcap", value: null, median: null, observations: 0 },
    { key: "rsi_12m", label: "1-Year RSI", value: null, median: null, observations: 0 },
  ],
  price_and_mas: [
    { key: "face_value", label: "Face Value", value: null, percentile: null },
    { key: "high_1y", label: "1Y High", value: null, percentile: null },
    { key: "away_high_1y", label: "Away from 1Y High", value: null, percentile: null },
    { key: "high_ath", label: "ATH", value: null, percentile: null },
    { key: "away_high_ath", label: "Away from ATH", value: null, percentile: null },
    { key: "ma_200", label: "MA 200", value: null, percentile: null },
    { key: "ma_100", label: "MA 100", value: null, percentile: null },
    { key: "ma_50", label: "MA 50", value: null, percentile: null },
    { key: "ma_20", label: "MA 20", value: "99.00", percentile: null },
  ],
  returns: [
    ...cells("ret_", [null, null, null, null, "4.20"], [null, null, null, null, 0.4]),
    { key: "ret_12m_minus_1m", label: "12M-1M", value: null, percentile: null },
    { key: "ret_12m_minus_2m", label: "12M-2M", value: null, percentile: null },
  ],
  sharpe_returns: cells("sharpe_", [null, null, null, null, null]),
  volatility: cells("vol_", [null, null, null, null, "0.31000000"], [null, null, null, null, 0.5]),
  rsi: cells("rsi_", [null, null, null, null, "55.0000"], [null, null, null, null, 0.5]),
  market_quality: {
    regime: null,
    regime_distance_bull: null,
    regime_distance_bear: null,
    median_vol_12m: null,
    circuits: WINDOWS.map((label, index) => ({ label, value: index === 4 ? 0 : null })),
    positive_days: WINDOWS.map((label, index) => ({
      label,
      value: index === 4 ? "52.00" : null,
    })),
  },
  corporate_actions: [],
  index_memberships: [{ slug: "nifty-total-market", name: "NIFTY TOTAL MARKET" }],
  percentile_universe: { slug: "nifty-total-market", name: "NIFTY TOTAL MARKET" },
  notes: [],
};
