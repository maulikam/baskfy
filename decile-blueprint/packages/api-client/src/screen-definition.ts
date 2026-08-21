/**
 * ScreenDefinition — the TypeScript mirror of
 * packages/core/src/decile_core/screen_definition.py.
 *
 * Shape: docs/04-data-model.md §Screens. Semantics: docs/06-screener-semantics.md §"Step 4".
 *
 * This file is hand-kept in sync with the Pydantic model, and test/parity.test.ts fails the
 * build if it drifts — structurally (against the generated JSON Schema) and behaviourally
 * (against the shared accept/reject corpus).
 */

import { z } from "zod";

/** Sentinels: the reference product encodes "filter off" inside the value (docs/01 §2.4–§2.10). */
export const AWAY_FROM_HIGH_IGNORE = 100;
export const POSITIVE_DAYS_IGNORE = 0;
export const CIRCUITS_IGNORE_ABOVE = 250;
export const IGNORE_ABOVE_BETA_IGNORE = 100;

/** docs/01 §2.14 — the screener exposes exactly three custom-filter slots. */
export const MAX_CUSTOM_FILTERS = 3;

/** docs/01 §2.9 */
export const SERIES_VALUES = ["EQ", "BE"] as const;

/** docs/01 §2.1 — the 14 selectable universes. Mirrors decile_core.universes.UNIVERSE_SLUGS. */
export const UNIVERSE_SLUGS = [
  "nifty-50",
  "nifty-next-50",
  "nifty-100",
  "nifty-200",
  "nifty-500",
  "nifty-total-market",
  "nifty-large-mid-250",
  "nifty-midcap-150",
  "nifty-smallcap-250",
  "nifty-microcap-250",
  "nifty-mid-small-400",
  "nifty-allcap",
  "nifty-fno",
  "etf",
] as const;

/**
 * Pydantic accepts a Decimal as a JSON number or a numeric string; mirror that exactly, or the
 * two validators disagree on payloads that arrive from a form as strings.
 */
const decimalLike = z.union([
  z.number(),
  z.string().regex(/^(?!^[-+.]*$)[+-]?0*\d*\.?\d*$/),
]);

/** A factor / column key. The 62-factor whitelist arrives with the registry in Prompt 5. */
const factorKey = z
  .string()
  .min(1)
  .max(64)
  .regex(/^[a-z][a-z0-9_]*$/);

export const SortDirectionSchema = z.enum(["asc", "desc"]);
export const ApplyFiltersOnSchema = z.enum([
  "all",
  "decile_1",
  "decile_2",
  "decile_3",
  "decile_4",
  "decile_5",
  "top_50",
  "top_100",
]);
export const CustomFilterOpSchema = z.enum([">=", "<=", "="]);

const MA_WINDOWS = [200, 100, 50, 20] as const;

/** Eight independent switches, AND-combined (docs/01 §2.3). */
export const MovingAverageFilterSchema = z
  .strictObject({
    enabled: z.boolean().default(false),
    above_200: z.boolean().default(false),
    above_100: z.boolean().default(false),
    above_50: z.boolean().default(false),
    above_20: z.boolean().default(false),
    below_200: z.boolean().default(false),
    below_100: z.boolean().default(false),
    below_50: z.boolean().default(false),
    below_20: z.boolean().default(false),
  })
  .superRefine((value, ctx) => {
    if (!value.enabled) return;
    const both = MA_WINDOWS.filter(
      (k) => value[`above_${k}` as const] && value[`below_${k}` as const],
    );
    if (both.length > 0) {
      ctx.addIssue({
        code: "custom",
        message:
          `moving_average: above and below cannot both be set for MA ${both.join(", ")}; ` +
          "no stock can satisfy both.",
      });
    }
  });

/** "Within X% of high". 100 = ignore (docs/01 §2.4). */
export const AwayFromHighFilterSchema = z.strictObject({
  ath: z.int().min(0).max(AWAY_FROM_HIGH_IGNORE).default(AWAY_FROM_HIGH_IGNORE),
  one_year: z.int().min(0).max(AWAY_FROM_HIGH_IGNORE).default(AWAY_FROM_HIGH_IGNORE),
});

/** Minimum % of trading days that closed up. 0 = ignore (docs/01 §2.5). */
export const PositiveDaysFilterSchema = z.strictObject({
  m12: z.int().min(0).max(100).default(POSITIVE_DAYS_IGNORE),
  m9: z.int().min(0).max(100).default(POSITIVE_DAYS_IGNORE),
  m6: z.int().min(0).max(100).default(POSITIVE_DAYS_IGNORE),
  m3: z.int().min(0).max(100).default(POSITIVE_DAYS_IGNORE),
  m1: z.int().min(0).max(100).default(POSITIVE_DAYS_IGNORE),
});

/** Maximum circuit-hit days in the window. > 250 = ignore (docs/01 §2.6). */
export const CircuitsFilterSchema = z.strictObject({
  m12: z.int().min(0).default(999),
  m9: z.int().min(0).default(999),
  m6: z.int().min(0).default(999),
  m3: z.int().min(0).default(999),
  m1: z.int().min(0).default(999),
});

const rangeShape = {
  from: decimalLike.nullable().default(null),
  to: decimalLike.nullable().default(null),
};

const rangeIsOrdered = (value: { from: unknown; to: unknown }): boolean => {
  if (value.from === null || value.to === null) return true;
  return Number(value.from) <= Number(value.to);
};

const RANGE_INVERTED = "range is inverted: from must not exceed to";

/** An inclusive [from, to] band; both null means ignore (docs/06 step 4). */
export const RangeFilterSchema = z
  .strictObject(rangeShape)
  .refine(rangeIsOrdered, { message: RANGE_INVERTED });

/** P/E band behind its own switch (docs/01 §2.8). */
export const PeFilterSchema = z
  .strictObject({ ...rangeShape, enabled: z.boolean().default(false) })
  .refine(rangeIsOrdered, { message: RANGE_INVERTED });

/** "Ignore Top Beta / Top Volatility" — a precomputed per-universe flag (docs/06 step 4). */
export const TopRiskFilterSchema = z.strictObject({
  enabled: z.boolean().default(false),
  count: z.int().min(0).default(0),
});

/** Factor two / factor three of the combined ranking (docs/01 §2.12). */
export const ExtraFactorSchema = z
  .strictObject({
    enabled: z.boolean().default(false),
    sort_by: factorKey.nullable().default(null),
    sort_direction: SortDirectionSchema.default("desc"),
  })
  .refine((value) => !value.enabled || value.sort_by !== null, {
    message: "sort_by is required when the extra factor is enabled",
  });

/** A field-to-field comparison, e.g. ma_50 >= ma_200 (docs/01 §2.14). */
export const CustomFilterSchema = z.strictObject({
  enabled: z.boolean().default(true),
  left: factorKey,
  op: CustomFilterOpSchema,
  right: factorKey,
});

export const ScreenDefinitionSchema = z
  .strictObject({
    index: z.enum(UNIVERSE_SLUGS),
    sort_by: factorKey,
    sort_direction: SortDirectionSchema.default("desc"),
    apply_filters_on: ApplyFiltersOnSchema.default("all"),

    min_return_1y: decimalLike.nullable().default(null),
    /** Minimum median daily traded value over 1 year, in rupees (docs/13 §2 finding 6). */
    median_volume_1y: z.int().nullable().default(null),

    moving_average: MovingAverageFilterSchema.prefault({}),
    away_from_high: AwayFromHighFilterSchema.prefault({}),
    positive_days: PositiveDaysFilterSchema.prefault({}),
    circuits: CircuitsFilterSchema.prefault({}),
    marketcap: RangeFilterSchema.prefault({}),
    pe: PeFilterSchema.prefault({}),
    series: z.array(z.enum(SERIES_VALUES)).default(["EQ"]),
    ignore_top_beta: TopRiskFilterSchema.prefault({}),
    ignore_top_volatility: TopRiskFilterSchema.prefault({}),
    ignore_above_beta: z.int().min(0).default(IGNORE_ABOVE_BETA_IGNORE),
    price: RangeFilterSchema.prefault({}),
    factor_two: ExtraFactorSchema.prefault({}),
    factor_three: ExtraFactorSchema.prefault({}),
    historical_date: z.iso.date().nullable().default(null),
    custom_filters: z.array(CustomFilterSchema).max(MAX_CUSTOM_FILTERS).default([]),
  })
  .superRefine((value, ctx) => {
    if (new Set(value.series).size !== value.series.length) {
      ctx.addIssue({ code: "custom", message: `series contains duplicates: ${value.series}` });
    }
    // docs/01 §2.12: factor three is revealed by, and ranks after, factor two.
    if (value.factor_three.enabled && !value.factor_two.enabled) {
      ctx.addIssue({
        code: "custom",
        message: "factor_three cannot be enabled while factor_two is disabled",
      });
    }
  });

export type ScreenDefinition = z.infer<typeof ScreenDefinitionSchema>;
