/**
 * The six factor families, as `baskfy_core.factor_registry.FactorFamily` enumerates them and
 * docs/01 §3 groups them.
 *
 * **This is a second copy, and it is deliberate.** `GET /meta/factors` publishes every factor with
 * its family, and the landing page could count them at build time — but that would make the
 * marketing page unrenderable when the API is down, for a table that changes when the *code*
 * changes rather than when the *data* does. `src/lib/__tests__/factor-families.test.ts` fetches
 * nothing and instead asserts the counts against `packages/core`'s registry through the committed
 * JSON Schema, the same way `operands.ts` is pinned to `CUSTOM_FILTER_OPERANDS` by
 * `packages/core/tests/test_operand_parity.py`.
 *
 * The prose is ours; the counts and the family keys are not.
 */

export interface FactorFamilySummary {
  key: string;
  label: string;
  description: string;
  count: number;
}

export const FACTOR_FAMILIES: readonly FactorFamilySummary[] = [
  {
    key: "absolute_return",
    label: "Absolute return",
    description:
      "Point-to-point price change on adjusted closes — five windows, plus eleven averages of two to five of them.",
    count: 16,
  },
  {
    key: "sharpe_return",
    label: "Sharpe return",
    description:
      "Return divided by the realised volatility of daily log returns over the same window — five windows, plus twelve averages.",
    count: 17,
  },
  {
    key: "rsi",
    label: "RSI",
    description:
      "Wilder's relative strength index over the window's trading days, reported as a level rather than as a signal.",
    count: 16,
  },
  {
    key: "risk_adjusted",
    label: "Risk-adjusted",
    description:
      "Absolute and Sharpe return divided by beta against the index, for the one-year window and three averages.",
    count: 5,
  },
  {
    key: "skip_month",
    label: "Skip-month momentum",
    description:
      "Twelve-month return excluding the most recent one or two months — the academic 12−1 construction.",
    count: 2,
  },
  {
    key: "non_momentum",
    label: "Non-momentum sort keys",
    description:
      "Volatility, beta, P/E, market capitalisation, both close prices and the two away-from-high measures.",
    count: 8,
  },
] as const;

/** docs/01 §3 is headed "62 ranking factors" and enumerates 64. We implement every named key. */
export const FACTOR_COUNT = FACTOR_FAMILIES.reduce((total, family) => total + family.count, 0);
