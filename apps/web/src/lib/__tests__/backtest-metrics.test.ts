import { describe, expect, it } from "vitest";

import {
  METRIC_ROWS,
  UNKNOWN,
  formatMetric,
  formatMonthCell,
  metricsOf,
  rollingOf,
} from "@/lib/backtests/metrics";
import { parseFrame } from "@/lib/backtests/queries";

/**
 * docs/10 §Outputs lists the metrics the page owes the user. This file pins that list, because a
 * metric dropped from `METRIC_ROWS` disappears from the table silently — the API keeps computing
 * and storing it, and nothing else notices.
 */
describe("the metrics table covers docs/10 §Outputs", () => {
  const keys = new Set(METRIC_ROWS.map((row) => row.key));

  it.each([
    "cagr",
    "total_return",
    "annualised_volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_drawdown_peak",
    "max_drawdown_trough",
    "calmar",
    "hit_rate",
    "average_win",
    "average_loss",
    "annual_turnover",
    "total_costs",
    "exposure",
    "best_month",
    "worst_month",
    "alpha",
    "beta",
    "tracking_error",
    "information_ratio",
  ])("names %s", (key) => {
    expect(keys.has(key)).toBe(true);
  });

  it("gives every row an explanation, not just a label", () => {
    for (const row of METRIC_ROWS) {
      expect(row.hint.length, `${row.key} has no hint`).toBeGreaterThan(10);
    }
  });

  it("has no duplicate keys", () => {
    expect(keys.size).toBe(METRIC_ROWS.length);
  });
});

describe("formatting", () => {
  it("renders a fraction as a signed percentage", () => {
    expect(formatMetric(0.1234, "percent")).toBe("+12.34%");
    expect(formatMetric(-0.0756, "percent")).toBe("-7.56%");
  });

  it("renders a rupee string, because Decimal does not survive JSON as a number", () => {
    expect(formatMetric("128456.78", "money")).toContain("1,28,457");
  });

  it("renders a missing value as the em dash the rest of the app uses", () => {
    expect(formatMetric(null, "percent")).toBe(UNKNOWN);
    expect(formatMetric(undefined, "ratio")).toBe(UNKNOWN);
    expect(formatMetric(null, "date")).toBe(UNKNOWN);
  });

  it("renders a month cell as its month and its return", () => {
    expect(formatMonthCell({ month: "2019-03", return: 0.081 })).toBe("2019-03 · +8.10%");
    expect(formatMonthCell({ month: "2019-03" })).toBe(UNKNOWN);
    expect(formatMonthCell(null)).toBe(UNKNOWN);
  });
});

describe("narrowing the stored metric block", () => {
  it("treats a missing block as empty rather than throwing", () => {
    expect(metricsOf(undefined)).toEqual({});
  });

  it("refuses a rolling distribution with no windows in it", () => {
    expect(rollingOf({ rolling_12m: { count: 0 } })).toBeNull();
    expect(rollingOf({})).toBeNull();
  });

  it("reads a rolling distribution", () => {
    const rolling = rollingOf({
      rolling_12m: { count: 12, min: -0.2, p05: -0.1, median: 0.14, p95: 0.5, max: 0.6, negative_share: 0.25 },
    });
    expect(rolling?.count).toBe(12);
    expect(rolling?.negative_share).toBe(0.25);
  });
});

/**
 * The SSE wire format, parsed here rather than trusted. A keep-alive comment and a malformed
 * payload must both be ignored silently — the progress bar is not a place to surface a parse
 * error, because the poll is already behind it.
 */
describe("the progress stream", () => {
  it("reads a frame", () => {
    const frame = parseFrame(
      'event: progress\ndata: {"public_id":"abc","status":"running","stage":"simulating","completed":250,"total":3900,"percent":6,"as_of":"2015-02-02","detail":null}',
    );
    expect(frame?.percent).toBe(6);
    expect(frame?.stage).toBe("simulating");
  });

  it("ignores a keep-alive comment", () => {
    expect(parseFrame(": keep-alive")).toBeNull();
  });

  it("ignores an unparsable payload", () => {
    expect(parseFrame("event: progress\ndata: {not json")).toBeNull();
  });

  it("ignores a payload that is not a progress frame", () => {
    expect(parseFrame('data: {"hello":"world"}')).toBeNull();
  });
});
