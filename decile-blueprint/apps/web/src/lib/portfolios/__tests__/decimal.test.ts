import { describe, expect, it } from "vitest";

import {
  NO_FIGURE,
  addDecimalStrings,
  compareDecimalStrings,
  decimalStringsEqual,
  formatQuantity,
  formatRupees,
  groupIndian,
  isPositiveDecimal,
  parseDecimal,
  roundDecimalString,
  toDecimalString,
} from "@/lib/portfolios/decimal";

/**
 * CLAUDE.md house rule 9: money is `numeric`, never `float`.
 *
 * These assert the *spec* — that the sums are exact and that an unparseable figure stays missing
 * rather than becoming a zero — not the implementation. Each case is one a `Number()` round trip
 * gets wrong, which is why the module exists.
 */

describe("exact decimal addition", () => {
  it("adds the classic binary-fraction case without drift", () => {
    // 0.1 + 0.2 === 0.30000000000000004 as doubles.
    expect(addDecimalStrings(["0.1", "0.2"])).toBe("0.3");
  });

  it("keeps every digit past 2^53, where a double silently rounds", () => {
    // 9007199254740993 is the first integer a double cannot represent.
    expect(addDecimalStrings(["9007199254740992", "1"])).toBe("9007199254740993");
  });

  it("lines up different scales rather than truncating to the shortest", () => {
    expect(addDecimalStrings(["1.5", "2.25", "0.001"])).toBe("3.751");
  });

  it("answers null for nothing summable, which is not the same as zero", () => {
    expect(addDecimalStrings([])).toBeNull();
    expect(addDecimalStrings([null, undefined, ""])).toBeNull();
  });

  it("refuses a value that is not a decimal string instead of coercing it", () => {
    expect(addDecimalStrings(["abc"])).toBeNull();
    expect(addDecimalStrings(["1e6"])).toBeNull();
    expect(addDecimalStrings(["12", "oops"])).toBe("12");
  });

  it("handles negatives on both sides of zero", () => {
    expect(addDecimalStrings(["-1.50", "1.50"])).toBe("0.00");
    expect(addDecimalStrings(["-10", "-2.5"])).toBe("-12.5");
  });
});

describe("parsing and comparing", () => {
  it("round-trips a decimal string through its scaled integer", () => {
    const parsed = parseDecimal("-1234.5600");
    expect(parsed?.units).toBe(-12345600n);
    expect(parsed?.scale).toBe(4);
    expect(parsed === null ? null : toDecimalString(parsed)).toBe("-1234.5600");
  });

  it("treats trailing zeros as numerically equal, which string equality does not", () => {
    const padded: string = "1.50";
    expect(padded === "1.5").toBe(false);
    expect(decimalStringsEqual("1.50", "1.5")).toBe(true);
    expect(compareDecimalStrings("1.50", "1.51")).toBe(-1);
  });

  it("reports a positive figure without going through a float", () => {
    expect(isPositiveDecimal("0.0000001")).toBe(true);
    expect(isPositiveDecimal("0.00")).toBe(false);
    expect(isPositiveDecimal(null)).toBe(false);
  });
});

describe("rounding for display", () => {
  it("rounds half away from zero on the digits", () => {
    expect(roundDecimalString("2.675", 2)).toBe("2.68");
    expect(roundDecimalString("-2.675", 2)).toBe("-2.68");
    // The float answer: (2.675).toFixed(2) === "2.67", because 2.675 is not representable.
    expect((2.675).toFixed(2)).toBe("2.67");
  });

  it("pads rather than rounds when asked for more places than it has", () => {
    expect(roundDecimalString("7", 2)).toBe("7.00");
  });
});

describe("rendering rupees", () => {
  it("groups the Indian way", () => {
    expect(groupIndian("12345678")).toBe("1,23,45,678");
    expect(formatRupees("3600000", { decimals: 0 })).toBe("₹36,00,000");
    expect(formatRupees("1234.5", { decimals: 2 })).toBe("₹1,234.50");
  });

  it("renders a missing or unparseable figure as a dash, never as zero", () => {
    expect(formatRupees(null)).toBe(NO_FIGURE);
    expect(formatRupees("")).toBe(NO_FIGURE);
    expect(formatRupees("not a number")).toBe(NO_FIGURE);
  });

  it("keeps a negative sign outside the rupee mark", () => {
    expect(formatRupees("-500", { decimals: 0 })).toBe("-₹500");
  });
});

describe("rendering a quantity", () => {
  it("drops meaningless trailing zeros but keeps a real fraction", () => {
    expect(formatQuantity("1000.0000")).toBe("1,000");
    expect(formatQuantity("10.5000")).toBe("10.5");
  });

  it("renders no quantity as a dash", () => {
    expect(formatQuantity(null)).toBe(NO_FIGURE);
    expect(formatQuantity("")).toBe(NO_FIGURE);
  });
});
