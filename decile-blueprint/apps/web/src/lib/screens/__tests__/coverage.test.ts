import { describe, expect, it } from "vitest";

import { ScreenDefinitionSchema } from "@baskfy/api-client";

import { COVERED_FIELDS, FILTER_GROUPS, ALWAYS_VISIBLE_FIELDS } from "@/lib/screens/groups";
import { CUSTOM_FILTER_OPERAND_KEYS } from "@/lib/screens/operands";

/**
 * Prompt 9's fourth acceptance criterion:
 *
 *     "No filter in docs/01 §2 is missing from the UI (write a test that reads the
 *      ScreenDefinition schema and asserts a form control exists for every field)."
 *
 * The schema is read from the Zod mirror in `@baskfy/api-client`, which
 * `packages/api-client/test/parity.test.ts` already pins against the generated JSON Schema — so
 * "the schema" here is the same object the API validates against, not a second copy of it. A field
 * added to `ScreenDefinition` and forgotten in the form fails this test at once.
 *
 * Coverage is *by field*, not by control: `moving_average` is one field and eight switches,
 * `custom_filters` is one field and three slots. What the test guarantees is that no field is
 * unreachable, which is the property docs/01 §2 is a list of.
 */
const SCHEMA_FIELDS = Object.keys(ScreenDefinitionSchema.shape).sort();

describe("form coverage of ScreenDefinition", () => {
  it("reads a schema with fields in it", () => {
    // A coverage test against an empty schema passes vacuously.
    expect(SCHEMA_FIELDS.length).toBeGreaterThan(15);
  });

  it("claims every field of the schema", () => {
    const missing = SCHEMA_FIELDS.filter((field) => !COVERED_FIELDS.includes(field as never));
    expect(missing, `not editable anywhere in the form: ${missing.join(", ")}`).toEqual([]);
  });

  it("claims nothing the schema does not have", () => {
    const extra = COVERED_FIELDS.filter((field) => !SCHEMA_FIELDS.includes(field));
    expect(extra, `claimed by a group but not in the schema: ${extra.join(", ")}`).toEqual([]);
  });

  it("claims each field exactly once", () => {
    // Two groups owning one field means two controls writing to it, which is how a form ends up
    // with a value that depends on which accordion you touched last.
    const seen = new Map<string, number>();
    for (const field of COVERED_FIELDS) seen.set(field, (seen.get(field) ?? 0) + 1);
    const duplicated = [...seen.entries()].filter(([, count]) => count > 1).map(([key]) => key);
    expect(duplicated).toEqual([]);
  });

  it("covers every filter docs/01 §2 names", () => {
    // Spelled out rather than derived, so a rename in the schema cannot quietly drop a filter the
    // document requires.
    const required = [
      "index", // §2.1
      "sort_by", // §2.1
      "sort_direction", // §2.1
      "apply_filters_on", // §2.2
      "min_return_1y", // §2.2
      "median_volume_1y", // §2.2
      "moving_average", // §2.3
      "away_from_high", // §2.4
      "positive_days", // §2.5
      "circuits", // §2.6
      "marketcap", // §2.7
      "pe", // §2.8
      "series", // §2.9
      "ignore_top_beta", // §2.10
      "ignore_top_volatility", // §2.10
      "ignore_above_beta", // §2.10
      "price", // §2.11
      "factor_two", // §2.12
      "factor_three", // §2.12
      "historical_date", // §2.13
      "custom_filters", // §2.14
    ];
    for (const field of required) {
      expect(COVERED_FIELDS, `docs/01 §2 requires ${field}`).toContain(field);
    }
  });
});

describe("the accordion groups", () => {
  it("are in the order docs/08 lists them", () => {
    expect(FILTER_GROUPS.map((group) => group.title)).toEqual([
      "General Filters",
      "Moving Average Filters",
      "Away from High Filters",
      "Percentage of Positive Days Filters",
      "Circuit Filters",
      "Marketcap Range",
      "Price to Earnings Range",
      "Series",
      "Ignore Top Beta / Volatility",
      "Price (CMP) Range",
      "Multi-Factor Combined Ranking",
      "Historical Ranks",
      "Custom Filters",
    ]);
  });

  it("keeps the three always-visible controls out of the accordion", () => {
    // docs/08: "1. Always visible: Index Universe · Sort By (Factor) · Sort Direction".
    expect([...ALWAYS_VISIBLE_FIELDS]).toEqual(["index", "sort_by", "sort_direction"]);
    for (const group of FILTER_GROUPS) {
      for (const field of ALWAYS_VISIBLE_FIELDS) {
        expect(group.fields).not.toContain(field);
      }
    }
  });

  it("has a unique id per group", () => {
    const ids = FILTER_GROUPS.map((group) => group.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("the custom-filter operand list", () => {
  it("matches docs/01 §2.14's twenty-two entries", () => {
    // "Absolute return 1 year · Volatility 1y/9m/6m/3m/1m · Beta · Close · Close raw · Away from
    // high all time · Away from high 1 year · Ma 200/100/50/20 · Volume day · Volume 1y/9m/6m/3m/1m
    // avg · Volume week average" — 1 + 5 + 1 + 2 + 2 + 4 + 1 + 5 + 1.
    expect(CUSTOM_FILTER_OPERAND_KEYS).toHaveLength(22);
    expect(new Set(CUSTOM_FILTER_OPERAND_KEYS).size).toBe(22);
  });

  it("covers each family docs/01 §2.14 lists", () => {
    /*
     * The authoritative comparison is on the Python side:
     * `packages/core/tests/test_operand_parity.py` reads this very file and asserts it equals
     * `baskfy_core.factor_registry.CUSTOM_FILTER_OPERANDS`, which is what the API validates
     * against (docs/06 §"The factor registry"). There is no `/meta/` endpoint publishing that
     * list — docs/07 §Metadata enumerates five and this is not one of them — so the parity test
     * is the link, and this checks the shape docs/01 §2.14 describes.
     */
    const keys = [...CUSTOM_FILTER_OPERAND_KEYS];
    expect(keys.filter((key) => /^vol_\d+m$/.test(key))).toHaveLength(5);
    expect(keys.filter((key) => /^ma_\d+$/.test(key))).toHaveLength(4);
    expect(keys.filter((key) => key.startsWith("vol_avg_"))).toHaveLength(6);
    expect(keys).toContain("ret_12m");
    expect(keys).toContain("beta_12m");
    expect(keys).toContain("close");
    expect(keys).toContain("close_raw");
    expect(keys).toContain("away_high_ath");
    expect(keys).toContain("away_high_1y");
    expect(keys).toContain("vol_day_val");
  });
});
