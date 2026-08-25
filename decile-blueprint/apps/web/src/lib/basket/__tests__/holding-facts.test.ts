import { describe, expect, it } from "vitest";

import {
  EMPTY_FACTS,
  factsFromScreenRow,
  formatFact,
  formatLiquidity,
  visibleFactColumns,
  withBasketFactColumns,
} from "@/lib/basket/holding-facts";

describe("factsFromScreenRow", () => {
  it("reads the columns the basket table can show, and nothing it cannot", () => {
    const facts = factsFromScreenRow({
      marketcap_cr: 892.1,
      ret_12m: 24.5,
      vol_12m: 0.28,
      beta_12m: 0.9,
      sharpe_12m: 1.4,
      median_vol_12m: 16_879_133_66,
      pe: 18.2,
      sector: "Metals",
    });
    expect(facts.marketcapCr).toBe(892.1);
    expect(facts.ret12m).toBe(24.5);
    expect(facts.vol12m).toBe(0.28);
    expect(facts.beta12m).toBe(0.9);
    expect(facts.sharpe12m).toBe(1.4);
    expect(facts.pe).toBe(18.2);
    expect(facts).not.toHaveProperty("sector");
  });

  it("treats a missing column as null, not zero", () => {
    expect(factsFromScreenRow({ symbol: "AAA" })).toEqual(EMPTY_FACTS);
  });
});

describe("visibleFactColumns", () => {
  it("hides a fact that no row carries, so the table does not promise a number", () => {
    const keys = visibleFactColumns([
      { ...EMPTY_FACTS, marketcapCr: 100, ret12m: 12 },
      { ...EMPTY_FACTS, marketcapCr: 200, ret12m: 8 },
    ]).map((column) => column.key);
    expect(keys).toEqual(["marketcapCr", "ret12m"]);
    expect(keys).not.toContain("vol12m");
  });
});

describe("formatFact", () => {
  it("formats market cap as crore and vol as a percent", () => {
    expect(formatFact("marketcapCr", 892.1)).toMatch(/892/);
    expect(formatFact("vol12m", 0.28)).toBe("28.00%");
    expect(formatFact("ret12m", 12.5)).toBe("+12.50%");
    expect(formatFact("marketcapCr", null)).toBe("—");
  });

  it("formats liquidity in crore once the rupee volume is a crore or more", () => {
    expect(formatLiquidity(16_879_133_66)).toMatch(/cr/);
    expect(formatLiquidity(50_000)).toMatch(/₹/);
  });
});

describe("withBasketFactColumns", () => {
  it("asks the preview for the fact columns without dropping the screen's own", () => {
    const columns = withBasketFactColumns(["rsi_12m"]);
    expect(columns).toContain("marketcap_cr");
    expect(columns).toContain("median_vol_12m");
    expect(columns).toContain("rsi_12m");
  });
});
