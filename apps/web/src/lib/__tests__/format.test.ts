import { describe, expect, it } from "vitest";

import {
  describeChange,
  direction,
  directionGlyph,
  EMPTY_CELL,
  formatCrore,
  formatFraction,
  formatNumber,
  formatPercent,
  formatTradeDate,
} from "@/lib/format";

describe("signed numbers", () => {
  it("carries the sign as well as the colour", () => {
    // docs/11 §Accessibility: "Colour is never the sole carrier of meaning (pair positive/negative
    // colour with sign and, where dense, an arrow glyph)."
    expect(formatPercent(753)).toBe("+753.00%");
    expect(formatPercent(-12.5)).toBe("-12.50%");
  });

  it("supplies an arrow for dense surfaces", () => {
    expect(directionGlyph(1)).toBe("▲");
    expect(directionGlyph(-1)).toBe("▼");
    expect(directionGlyph(0)).toBe("—");
    expect(directionGlyph(null)).toBe("—");
  });

  it("describes the change in words for a screen reader", () => {
    expect(describeChange(12.5, "%")).toBe("up 12.5%");
    expect(describeChange(-3, "%")).toBe("down 3%");
    expect(describeChange(0)).toBe("unchanged at 0");
    expect(describeChange(null)).toBe("no value");
  });

  it("treats a missing value as flat rather than as zero", () => {
    expect(direction(undefined)).toBe("flat");
    expect(direction(Number.NaN)).toBe("flat");
  });
});

describe("volatility", () => {
  it("multiplies the stored fraction by 100 for display", () => {
    // docs/13 §2 finding 4: stored as a decimal fraction, "the UI multiplies by 100". docs/06a §10
    // and docs/07a §13 record that the API deliberately does not, so this is the only place it
    // happens — which is what stops two meanings of `vol_12m` existing at once.
    expect(formatFraction(0.5793)).toBe("57.93%");
    expect(formatFraction(0.179)).toBe("17.90%");
  });
});

describe("missing values", () => {
  it.each([null, undefined, ""])("renders %s as an em dash", (value) => {
    expect(formatNumber(value)).toBe(EMPTY_CELL);
    expect(formatPercent(value)).toBe(EMPTY_CELL);
    expect(formatFraction(value)).toBe(EMPTY_CELL);
    expect(formatCrore(value)).toBe(EMPTY_CELL);
  });
});

describe("marketcap", () => {
  it("is an integer in crore, grouped the Indian way", () => {
    // docs/13 §2 finding 7: "marketcap is in ₹ crore, integer".
    expect(formatCrore(971984)).toBe("9,71,984 cr");
  });
});

describe("trade dates", () => {
  it("renders the freshness pill's wording", () => {
    // docs/08 §"App shell": "data-freshness pill (`Data: 19 Aug 2026`)".
    expect(formatTradeDate("2026-08-19")).toBe("19 Aug 2026");
  });

  it("does not shift the date across a timezone", () => {
    // A trade date is a calendar date, not an instant. Parsing it as local midnight and formatting
    // in another zone is how "2026-08-18" becomes "17 Aug 2026" for half the world.
    expect(formatTradeDate("2026-01-01")).toBe("1 Jan 2026");
  });

  it("renders an unknown date as an em dash", () => {
    expect(formatTradeDate(null)).toBe(EMPTY_CELL);
    expect(formatTradeDate("not-a-date")).toBe(EMPTY_CELL);
  });
});
