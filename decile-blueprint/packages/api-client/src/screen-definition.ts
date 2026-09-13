/**
 * ScreenDefinition — the TypeScript mirror of
 * packages/core/src/baskfy_core/screen_definition.py.
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

/** docs/ranking/PLAN.md C3 — term, range and weight limits. Mirrors screen_definition.py. */
export const MAX_RANKING_TERMS = 8;
export const MAX_FACTOR_RANGES = 10;
export const MAX_TERM_WEIGHT = 100;

/** The Wasserstein regime labels `factor_daily.regime` carries. */
export const REGIME_VALUES = ["BULL", "NEUTRAL", "BEAR"] as const;

/** docs/01 §2.9 */
// Mirrors `SERIES_VALUES` in packages/core/src/baskfy_core/screen_definition.py.
// EQ/BE are the main board; SM/ST/SZ the NSE Emerge (SME) platform (M59).
export const SERIES_VALUES = ["EQ", "BE", "SM", "ST", "SZ"] as const;

/**
 * docs/ranking/PLAN.md correction 1 — registry factors with `rankable=False`. A ranking term
 * refuses them; they belong in `factor_ranges`. Mirrors `NON_RANKABLE_FACTORS` in
 * packages/core/src/baskfy_core/screen_definition.py (registry order), and
 * packages/core/tests/test_screen_definition_parity.py fails if the two drift.
 */
export const NON_RANKABLE_FACTORS = [
  "atr_14",
  "excess_ret_3m",
  "excess_ret_6m",
  "excess_ret_12m",
  "mom_pctile",
  "nse_mr6",
  "nse_mr12",
] as const;

const NON_RANKABLE: ReadonlySet<string> = new Set(NON_RANKABLE_FACTORS);

/** docs/01 §2.1 — the 15 selectable universes. Mirrors baskfy_core.universes.UNIVERSE_SLUGS. */
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
  "nse-sme-emerge",
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
export const RankingModeSchema = z.enum(["single", "sequential", "composite"]);
export const RankingScopeSchema = z.enum([
  "filtered_results",
  "fixed_universe",
  "within_sector",
]);
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
export const TermPreferenceSchema = z.enum(["higher", "lower", "target_range"]);
export const MissingDataPolicySchema = z.enum(["penalize", "neutral", "exclude"]);
export const RegimeLabelSchema = z.enum(REGIME_VALUES);

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

/**
 * One explicit ranking term (docs/ranking/PLAN.md C3). Whether the factor exists is enforced
 * server-side against GET /meta/factors, exactly like sort_by; a filter-only factor
 * (`NON_RANKABLE_FACTORS`) is refused here too, as the server refuses it.
 */
export const RankingTermSchema = z
  .strictObject({
    factor: factorKey,
    preference: TermPreferenceSchema,
    weight: z.number().gt(0).max(MAX_TERM_WEIGHT).default(1),
    target_min: z.number().nullable().default(null),
    target_max: z.number().nullable().default(null),
  })
  .superRefine((value, ctx) => {
    if (NON_RANKABLE.has(value.factor)) {
      ctx.addIssue({
        code: "custom",
        message:
          `ranking_terms.factor: '${value.factor}' cannot rank a list; it is a filter-only factor. ` +
          "Use it in factor_ranges as an eligibility filter instead",
      });
    }
    const hasBound = value.target_min !== null || value.target_max !== null;
    if (value.preference === "target_range") {
      if (!hasBound) {
        ctx.addIssue({
          code: "custom",
          message: `ranking_terms[${value.factor}]: target_range needs target_min, target_max or both`,
        });
      } else if (
        value.target_min !== null &&
        value.target_max !== null &&
        value.target_min > value.target_max
      ) {
        ctx.addIssue({
          code: "custom",
          message: `ranking_terms[${value.factor}]: target range is inverted`,
        });
      }
    } else if (hasBound) {
      ctx.addIssue({
        code: "custom",
        message: `ranking_terms[${value.factor}]: target_min/target_max only apply to preference='target_range'`,
      });
    }
  });

/** An inclusive [min, max] eligibility filter on one stored factor (C3). */
export const FactorRangeSchema = z
  .strictObject({
    enabled: z.boolean().default(true),
    factor: factorKey,
    min: z.number().nullable().default(null),
    max: z.number().nullable().default(null),
  })
  .superRefine((value, ctx) => {
    if (value.min === null && value.max === null) {
      ctx.addIssue({
        code: "custom",
        message: `factor_ranges[${value.factor}]: set min, max or both`,
      });
    } else if (value.min !== null && value.max !== null && value.min > value.max) {
      ctx.addIssue({
        code: "custom",
        message: `factor_ranges[${value.factor}]: range is inverted`,
      });
    }
  });

const familyWeight = z.number().min(0).nullable().default(null);

/** Relative weight per weight family, composite only (C3/C4). null = no opinion. */
export const FamilyWeightsSchema = z.strictObject({
  momentum: familyWeight,
  path_quality: familyWeight,
  trend_structure: familyWeight,
  participation: familyWeight,
  risk_execution: familyWeight,
});

const extraIsActive = (extra: { enabled: boolean; sort_by: string | null }): boolean =>
  extra.enabled && extra.sort_by !== null;

export const ScreenDefinitionSchema = z
  .strictObject({
    index: z.enum(UNIVERSE_SLUGS),
    sort_by: factorKey,
    sort_direction: SortDirectionSchema.default("desc"),
    apply_filters_on: ApplyFiltersOnSchema.default("all"),
    ranking_mode: RankingModeSchema.default("composite"),
    ranking_scope: RankingScopeSchema.default("filtered_results"),

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

    // Phase 2 (docs/ranking/PLAN.md C3). canonical_json omits each at its default.
    ranking_terms: z.array(RankingTermSchema).max(MAX_RANKING_TERMS).default([]),
    family_weights: FamilyWeightsSchema.nullable().default(null),
    missing_data: MissingDataPolicySchema.default("penalize"),
    factor_ranges: z.array(FactorRangeSchema).max(MAX_FACTOR_RANGES).default([]),
    regime_in: z.array(RegimeLabelSchema).nullable().default(null),
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
    const extrasActive = extraIsActive(value.factor_two) || extraIsActive(value.factor_three);
    if (value.ranking_mode === "single" && extrasActive) {
      ctx.addIssue({
        code: "custom",
        message:
          "ranking_mode='single' cannot combine factor_two or factor_three; use sequential or composite",
      });
    }
    // desk_score is the book's SCORE (C2); it never sums row numbers with another factor.
    if (value.sort_by === "desk_score" && extrasActive) {
      ctx.addIssue({
        code: "custom",
        message:
          "desk_score cannot combine with factor_two or factor_three; it is a single computed SCORE",
      });
    }
    if (value.regime_in !== null) {
      if (value.regime_in.length === 0) {
        ctx.addIssue({ code: "custom", message: "regime_in cannot be empty; use null for no filter" });
      } else if (new Set(value.regime_in).size !== value.regime_in.length) {
        ctx.addIssue({ code: "custom", message: "regime_in contains duplicates" });
      }
    }
    const factors = value.ranking_terms.map((term) => term.factor);
    if (new Set(factors).size !== factors.length) {
      ctx.addIssue({
        code: "custom",
        message: "ranking_terms name a factor more than once; use one term with a larger weight",
      });
    }
    const first = value.ranking_terms[0];
    if (first === undefined) {
      if (value.ranking_scope === "within_sector") {
        ctx.addIssue({
          code: "custom",
          message: "ranking_scope='within_sector' needs explicit ranking_terms",
        });
      }
      if (value.family_weights !== null) {
        ctx.addIssue({
          code: "custom",
          message: "family_weights needs ranking_terms in composite mode",
        });
      }
      if (value.missing_data !== "penalize") {
        ctx.addIssue({ code: "custom", message: "missing_data applies to ranking_terms" });
      }
      return;
    }
    if (value.sort_by !== first.factor) {
      ctx.addIssue({
        code: "custom",
        message: `sort_by must equal ranking_terms[0].factor (${first.factor})`,
      });
    }
    if (extrasActive) {
      ctx.addIssue({
        code: "custom",
        message: "ranking_terms replace factor_two and factor_three; disable them",
      });
    }
    if (value.ranking_mode === "single" && value.ranking_terms.length !== 1) {
      ctx.addIssue({
        code: "custom",
        message: "ranking_mode='single' ranks by exactly one term",
      });
    }
    if (value.ranking_mode !== "composite") {
      if (value.ranking_terms.some((term) => term.weight !== 1)) {
        ctx.addIssue({
          code: "custom",
          message: "term weights only apply to ranking_mode='composite'",
        });
      }
      if (value.family_weights !== null) {
        ctx.addIssue({
          code: "custom",
          message: "family_weights only apply to ranking_mode='composite'",
        });
      }
      if (value.ranking_scope === "within_sector") {
        ctx.addIssue({
          code: "custom",
          message: "ranking_scope='within_sector' only changes a composite score",
        });
      }
    }
  });

export type ScreenDefinition = z.infer<typeof ScreenDefinitionSchema>;
