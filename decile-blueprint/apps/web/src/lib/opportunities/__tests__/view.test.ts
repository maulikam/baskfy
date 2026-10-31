import { describe, expect, it } from "vitest";

import {
  actionabilityLabel,
  formatInr,
  formatInrCrore,
  formatIstDateTime,
  formatIstTime,
  formatMaxProfit,
  formatSessionDate,
  isActionable,
  legsSummary,
  optionsHref,
  progressPercent,
} from "@/lib/opportunities/view";

describe("INR formatting", () => {
  it("uses the rupee sign and Indian digit grouping", () => {
    expect(formatInr(1234567.5)).toBe("₹12,34,567.50");
    expect(formatInr(5000, 0)).toBe("₹5,000");
  });

  it("puts the sign before the rupee symbol for losses", () => {
    expect(formatInr(-5000)).toBe("-₹5,000.00");
  });

  it("renders missing values as an em dash", () => {
    expect(formatInr(null)).toBe("—");
    expect(formatInrCrore(undefined)).toBe("—");
  });

  it("formats crore turnover with one decimal", () => {
    expect(formatInrCrore(1234.56)).toBe("₹1,234.6 cr");
  });

  it("says Unlimited when max profit is null", () => {
    expect(formatMaxProfit(null)).toBe("Unlimited");
    expect(formatMaxProfit(7500)).toBe("₹7,500.00");
  });
});

describe("IST timestamps", () => {
  it("converts a UTC instant to Asia/Kolkata and labels it IST", () => {
    expect(formatIstDateTime("2026-10-30T09:44:05Z")).toBe("30 Oct 2026, 15:14:05 IST");
    expect(formatIstTime("2026-10-30T03:45:00Z")).toBe("09:15:00 IST");
  });

  it("crosses midnight correctly", () => {
    expect(formatIstDateTime("2026-10-30T19:00:00Z")).toBe("31 Oct 2026, 00:30:00 IST");
  });

  it("shows session dates as dates, without shifting the day", () => {
    expect(formatSessionDate("2026-10-29")).toBe("29 Oct 2026");
  });

  it("refuses an invalid stamp rather than printing Invalid Date", () => {
    expect(formatIstDateTime("nope")).toBe("—");
    expect(formatIstDateTime(null)).toBe("—");
  });
});

describe("actionable badge logic", () => {
  it("is actionable only when the row says so and data is FRESH", () => {
    expect(isActionable({ actionable: true }, "FRESH")).toBe(true);
    expect(isActionable({ actionable: false }, "FRESH")).toBe(false);
  });

  it("never shows actionable on STALE or UNAVAILABLE data, whatever the row says", () => {
    for (const status of ["STALE", "UNAVAILABLE"] as const) {
      expect(isActionable({ actionable: true }, status)).toBe(false);
      expect(actionabilityLabel({ actionable: true }, status)).toEqual({
        actionable: false,
        label: "Not actionable — data stale/incomplete",
      });
    }
  });
});

describe("small view helpers", () => {
  it("summarises legs", () => {
    const leg = {
      tradingsymbol: "X",
      strike: 24500,
      option_type: "CE" as const,
      side: "BUY" as const,
      bid: 1,
      ask: 1,
      exec_price: 1,
      quote_age_s: 1,
      spread_pct: 1,
      volume: 1,
      oi: 1,
      iv: null,
      delta: null,
      greeks_source: null,
    };
    expect(legsSummary([leg, { ...leg, strike: 24700, side: "SELL" }])).toBe("BUY 24,500 CE / SELL 24,700 CE");
  });

  it("clamps progress and handles an unknown total", () => {
    expect(progressPercent({ stage: "s", done: 5, total: 10 })).toBe(50);
    expect(progressPercent({ stage: "s", done: 12, total: 10 })).toBe(100);
    expect(progressPercent({ stage: "s", done: 0, total: 0 })).toBe(0);
  });

  it("builds a prefilled options link", () => {
    expect(optionsHref("M&M")).toBe("/opportunities/options?symbol=M%26M&direction=BULLISH");
  });
});
