/**
 * The custom-filter operand list — docs/01 §2.14, in the document's order.
 *
 *     "Both operands are drawn from the same list: Absolute return 1 year · Volatility
 *      1y/9m/6m/3m/1m · Beta · Close · Close raw · Away from high all time · Away from high 1 year
 *      · Ma 200 · Ma 100 · Ma 50 · Ma 20 · Volume day · Volume 1y avg · Volume 9m avg · Volume 6m
 *      avg · Volume 3m avg · Volume 1m avg · Volume week average."
 *
 * It mirrors `baskfy_core.factor_registry.CUSTOM_FILTER_OPERANDS`, which is what the API validates
 * against — a key not on that list is a `400 invalid-screen-definition` before any SQL is built
 * (docs/06 §"The factor registry"). Duplicated here rather than fetched because there is no
 * `/meta/custom-filter-operands` endpoint; `coverage.test.ts` asserts the two lists agree in
 * length and content against the committed OpenAPI document.
 *
 * Labels for the volume columns are supplied here because they are not factors and so are not in
 * `/meta/columns` either.
 */
export const CUSTOM_FILTER_OPERAND_KEYS = [
  "ret_12m",
  "vol_12m",
  "vol_9m",
  "vol_6m",
  "vol_3m",
  "vol_1m",
  "beta_12m",
  "close",
  "close_raw",
  "away_high_ath",
  "away_high_1y",
  "ma_200",
  "ma_100",
  "ma_50",
  "ma_20",
  "vol_day_val",
  "vol_avg_12m",
  "vol_avg_9m",
  "vol_avg_6m",
  "vol_avg_3m",
  "vol_avg_1m",
  "vol_avg_1w",
] as const;

export type CustomFilterOperand = (typeof CUSTOM_FILTER_OPERAND_KEYS)[number];

/** Fallback labels for the operands `/meta/columns` and `/meta/factors` do not name. */
export const OPERAND_LABELS: Partial<Record<CustomFilterOperand, string>> = {
  vol_9m: "VOLATILITY 9 MONTHS",
  vol_6m: "VOLATILITY 6 MONTHS",
  vol_3m: "VOLATILITY 3 MONTHS",
  vol_1m: "VOLATILITY 1 MONTH",
  vol_day_val: "VOLUME DAY",
  vol_avg_12m: "VOLUME 1 YEAR AVERAGE",
  vol_avg_9m: "VOLUME 9 MONTHS AVERAGE",
  vol_avg_6m: "VOLUME 6 MONTHS AVERAGE",
  vol_avg_3m: "VOLUME 3 MONTHS AVERAGE",
  vol_avg_1m: "VOLUME 1 MONTH AVERAGE",
  vol_avg_1w: "VOLUME WEEK AVERAGE",
};
