import type { ScreenDefinition } from "@baskfy/api-client";
import { describe, expect, it } from "vitest";

import { defaultDefinition } from "@/lib/screens/defaults";
import {
  decodeState,
  definitionsEqual,
  diffDefinition,
  encodeState,
  parseDefinition,
} from "@/lib/screens/url-state";

/**
 * Prompt 9's third acceptance criterion, at the unit level: "encode a full filter state, reload,
 * and the form matches."
 *
 * `e2e/url-state.spec.ts` drives the same property through a real browser and a real reload. This
 * asserts it exhaustively and instantly — including the cases a browser test would never think to
 * try, like a truncated URL or a value the schema rejects.
 */
function fullyPopulated(): ScreenDefinition {
  return {
    ...defaultDefinition(),
    index: "nifty-microcap-250",
    sort_by: "sharpe_div_beta_12m",
    sort_direction: "asc",
    apply_filters_on: "decile_3",
    min_return_1y: "12.5",
    median_volume_1y: 20_000_000,
    moving_average: {
      enabled: true,
      above_200: true,
      above_100: false,
      above_50: true,
      above_20: false,
      below_200: false,
      below_100: true,
      below_50: false,
      below_20: false,
    },
    away_from_high: { ath: 30, one_year: 15 },
    positive_days: { m12: 55, m9: 0, m6: 52, m3: 0, m1: 60 },
    circuits: { m12: 12, m9: 999, m6: 6, m3: 999, m1: 1 },
    marketcap: { from: "2500", to: "900000" },
    pe: { enabled: true, from: "5", to: "45" },
    series: ["EQ", "BE"],
    ignore_top_beta: { enabled: true, count: 5 },
    ignore_top_volatility: { enabled: true, count: 10 },
    ignore_above_beta: 2,
    price: { from: "50", to: "5000" },
    factor_two: { enabled: true, sort_by: "vol_12m", sort_direction: "asc" },
    factor_three: { enabled: true, sort_by: "beta_12m", sort_direction: "asc" },
    historical_date: "2026-08-17",
    custom_filters: [
      { enabled: true, left: "ma_50", op: ">=", right: "ma_200" },
      { enabled: false, left: "vol_avg_1w", op: "<=", right: "vol_avg_12m" },
    ],
  };
}

describe("round-tripping through the URL", () => {
  it("restores a fully populated state exactly", () => {
    const base = defaultDefinition();
    const wanted = fullyPopulated();
    const encoded = encodeState(base, wanted);
    expect(encoded).not.toBeNull();
    expect(decodeState(base, encoded)).toEqual(wanted);
  });

  it("restores it against the saved screen it was shared from", () => {
    // The diff is relative to the *screen's* definition, not to the defaults.
    const saved: ScreenDefinition = { ...defaultDefinition(), index: "nifty-50", sort_by: "ret_12m" };
    const wanted = { ...fullyPopulated(), index: "nifty-50" as const };
    expect(decodeState(saved, encodeState(saved, wanted))).toEqual(wanted);
  });

  it("writes nothing when nothing has changed", () => {
    const base = defaultDefinition();
    expect(encodeState(base, { ...base })).toBeNull();
    expect(decodeState(base, null)).toEqual(base);
  });

  it("writes only what changed", () => {
    const base = defaultDefinition();
    const encoded = encodeState(base, { ...base, index: "nifty-50" });
    expect(encoded).toBe('{"index":"nifty-50"}');
  });

  it("diffs a nested group field by field, not whole", () => {
    const base = defaultDefinition();
    const encoded = encodeState(base, {
      ...base,
      positive_days: { ...base.positive_days, m6: 55 },
    });
    // Not the whole five-window object — a URL that carried four unchanged zeroes for one edit
    // would be four times the length for no information.
    expect(encoded).toBe('{"positive_days":{"m6":55}}');
  });
});

describe("a hostile or stale URL", () => {
  const base = defaultDefinition();

  it.each(["", "not json", "{", "[1,2,3]", '"a string"', "null"])(
    "falls back to the saved screen for %j",
    (raw) => {
      expect(decodeState(base, raw)).toEqual(base);
    },
  );

  it("rejects a patch the schema would not accept", () => {
    // A hand-edited URL should open the saved screen, not an error page — and the API would
    // refuse this definition a moment later anyway (docs/07: 400 invalid-screen-definition).
    expect(decodeState(base, '{"index":"atlantis"}')).toEqual(base);
    expect(decodeState(base, '{"sort_by":"DROP TABLE"}')).toEqual(base);
    expect(decodeState(base, '{"away_from_high":{"ath":-5}}')).toEqual(base);
  });

  it("ignores an unknown key rather than carrying it through", () => {
    // `ScreenDefinition` is `strictObject`, so an extra key fails validation and the base wins.
    expect(decodeState(base, '{"not_a_field":1}')).toEqual(base);
  });
});

describe("equality", () => {
  it("sees two spellings of the same state as equal", () => {
    const a = defaultDefinition();
    const b = { ...defaultDefinition() };
    expect(definitionsEqual(a, b)).toBe(true);
  });

  it("notices a change in a nested field", () => {
    const a = defaultDefinition();
    const b = { ...a, circuits: { ...a.circuits, m3: 4 } };
    expect(definitionsEqual(a, b)).toBe(false);
  });

  it("notices a removal as well as an addition", () => {
    const a = { ...defaultDefinition(), custom_filters: [] };
    const b = {
      ...a,
      custom_filters: [{ enabled: true, left: "ma_50", op: ">=" as const, right: "ma_200" }],
    };
    expect(definitionsEqual(a, b)).toBe(false);
    expect(definitionsEqual(b, a)).toBe(false);
  });
});

describe("diffDefinition", () => {
  it("returns an empty object for identical inputs", () => {
    expect(diffDefinition({ a: 1, b: { c: 2 } }, { a: 1, b: { c: 2 } })).toEqual({});
  });

  it("compares arrays whole", () => {
    // A positional diff of an ordered list is a bug generator; both arrays here are short.
    expect(diffDefinition({ series: ["EQ"] }, { series: ["EQ", "BE"] })).toEqual({
      series: ["EQ", "BE"],
    });
  });
});

describe("parseDefinition", () => {
  it("accepts what the API returns", () => {
    const parsed = parseDefinition({ index: "nifty-500", sort_by: "ret_12m" });
    expect(parsed.index).toBe("nifty-500");
    expect(parsed.series).toEqual(["EQ"]);
  });

  it("falls back to the defaults for a payload it cannot read", () => {
    expect(parseDefinition(null).index).toBe(defaultDefinition().index);
    expect(parseDefinition({ index: "nope" }).index).toBe(defaultDefinition().index);
  });
});
