import { describe, expect, it } from "vitest";

import { EMPTY_CELL } from "@/lib/format";
import type { ExploreBasketCard, ExploreMetrics } from "@/lib/explore/fetch";
import {
  UNCOMPUTED_METRIC_KEYS,
  capitalMetrics,
  cardMetrics,
  formatReturn,
  formatRupeesCompact,
  formatVolatility,
  isAvailable,
  returnMetrics,
  riskMetrics,
  uncomputedMetrics,
  volatilityBasisNote,
  volatilityBucketLabel,
} from "@/lib/discover/metrics";

function metrics(over: Partial<ExploreMetrics> = {}): ExploreMetrics {
  return {
    as_of_date: "2026-08-22",
    min_amount: "684000.00",
    volatility_bucket: "MEDIUM",
    volatility_value: "0.1820000000",
    ret_1m: null,
    ret_6m: null,
    ret_1y: "53.30",
    cagr_3y: null,
    cagr_5y: null,
    since_inception_pct: null,
    headline_label: "1Y returns",
    headline_pct: "53.30",
    return_convention: "PRICE_RETURN",
    dividends_included: false,
    return_convention_note: "Price return; dividends are not included.",
    ...over,
  };
}

function card(over: Partial<ExploreBasketCard> = {}): ExploreBasketCard {
  return {
    slug: "liquid-momentum",
    name: "Liquid Momentum",
    access: "FREE",
    visibility: "PUBLISHED",
    type: "STOCK",
    categories: ["momentum", "liquidity"],
    rebalance_frequency: "QUARTERLY",
    source: "SCAN",
    description_md: null,
    launched_at: null,
    manager: { slug: "baskfy-engine", name: "Baskfy Engine", kind: "ENGINE" },
    metrics: metrics(),
    ...over,
  };
}

describe("returns carry their unit", () => {
  it("formats the string the API actually sends, not only the number a test would pass", () => {
    // The defect this pins: the card appended `%` under `typeof value === "number"`, and every
    // real payload is a string because the field is a Pydantic Decimal.
    expect(formatReturn("53.30")).toBe("+53.30%");
    expect(formatReturn(53.3)).toBe("+53.30%");
  });

  it("signs a negative return without needing colour to carry it", () => {
    expect(formatReturn("-12.40")).toBe("-12.40%");
  });

  it("renders an em dash rather than a bare zero when there is no value", () => {
    expect(formatReturn(null)).toBe(EMPTY_CELL);
    expect(formatReturn("")).toBe(EMPTY_CELL);
  });

  it("puts the unit on the headline metric that reaches the card", () => {
    const [headline] = returnMetrics(metrics());
    expect(headline?.value).toBe("+53.30%");
    expect(headline?.label).toBe("1Y returns");
  });
});

describe("volatility", () => {
  it("multiplies the stored fraction into a percentage exactly once", () => {
    expect(formatVolatility("0.1820000000")).toBe("18.2%");
  });

  it("is never signed — a plus in front of volatility would read as a gain", () => {
    expect(formatVolatility("0.1820000000")).not.toContain("+");
  });

  it("names the bucket in words rather than as an opaque token", () => {
    expect(volatilityBucketLabel("MEDIUM")).toBe("Medium");
    expect(volatilityBucketLabel("MED")).toBe("Medium");
    expect(volatilityBucketLabel(null)).toBeNull();
    expect(volatilityBucketLabel("SOMETHING_NEW")).toBeNull();
  });

  it("discloses when the figure is blended from holdings rather than measured", () => {
    const note = volatilityBasisNote("CONSTITUENT_WEIGHTED");
    expect(note).toMatch(/too little history/i);
    expect(note).toMatch(/move together/i);
    expect(volatilityBasisNote("BASKET")).toMatch(/own history/i);
  });

  it("labels the measure, and the label is not the word Swing", () => {
    const [vol] = riskMetrics(metrics());
    expect(vol?.label).toBe("Annualised volatility");
    expect(vol?.value).toBe("18.2% · Medium");
  });

  it("says there is no volatility rather than showing zero", () => {
    const [vol] = riskMetrics(metrics({ volatility_value: null }));
    expect(vol?.value).toBe(EMPTY_CELL);
    expect(isAvailable(vol!)).toBe(false);
    expect(vol?.absence?.kind).toBe("no-data");
  });
});

describe("capital", () => {
  it("renders lakh and crore, because this is an India-equities product", () => {
    // Two decimals always, including the trailing zero: a column of ₹6.84L / ₹2.50Cr aligns on
    // the point, and "keep financial numbers aligned and easy to scan" is worth one extra glyph.
    expect(formatRupeesCompact("684000.00")).toBe("₹6.84L");
    expect(formatRupeesCompact(25_000_000)).toBe("₹2.50Cr");
    expect(formatRupeesCompact(4_500)).toBe("₹4.5K");
    expect(formatRupeesCompact(820)).toBe("₹820");
    expect(formatRupeesCompact(null)).toBe(EMPTY_CELL);
  });

  it("carries the reason a minimum is large, which is what makes it comprehensible", () => {
    const [min] = capitalMetrics(card());
    expect(min?.value).toBe("₹6.84L");
    expect(min?.explain).toMatch(/at least one share of every holding/i);
  });
});

describe("absent is not zero", () => {
  it("returns a row for every metric nobody computes, so a comparison shows the blank", () => {
    const rows = uncomputedMetrics();
    expect(rows.map((row) => row.key).sort()).toEqual([...UNCOMPUTED_METRIC_KEYS].sort());
    for (const row of rows) {
      expect(row.value).toBe(EMPTY_CELL);
      expect(isAvailable(row)).toBe(false);
      expect(row.absence?.kind).toBe("not-computed");
      expect(row.explain.length).toBeGreaterThan(20);
    }
  });

  it("gives every uncomputed metric a plain-language explanation, not a formula", () => {
    for (const row of uncomputedMetrics()) {
      expect(row.explain, `${row.key} explains itself`).not.toMatch(/[=∑σ]/);
    }
  });

  it("marks a return absent as too-young rather than as no data", () => {
    const [headline] = returnMetrics(metrics({ headline_pct: null }));
    expect(headline?.absence?.kind).toBe("too-young");
  });
});

describe("card ordering", () => {
  it("puts risk before return before capital", () => {
    expect(cardMetrics(card()).map((row) => row.tone)).toEqual(["risk", "return", "capital"]);
  });

  it("every metric explains itself, so 'explain this metric' has something to show", () => {
    for (const row of cardMetrics(card())) {
      expect(row.explain.length, `${row.key} has an explanation`).toBeGreaterThan(20);
    }
  });
});
