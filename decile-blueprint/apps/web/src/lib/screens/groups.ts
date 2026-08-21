import type { ScreenDefinition } from "@decile/api-client";

import {
  MA_WINDOWS,
  WINDOW_KEYS,
  awayFromHighActive,
  circuitsActive,
  ignoreAboveBetaActive,
  movingAverageCount,
  positiveDaysActive,
  rangeActive,
} from "@/lib/screens/defaults";

/**
 * The accordion groups, in the reference product's order — docs/08 §"Screen editor":
 *
 *     "1. Always visible: Index Universe · Sort By (Factor) · Sort Direction · `Show More Filters`
 *      2. General Filters · Moving Average Filters · Away from High · Percentage of Positive Days ·
 *         Circuit Filters · Marketcap Range · P/E Range · Series · Ignore Top Beta/Volatility ·
 *         Price (CMP) Range · Multi-Factor Combined Ranking · Historical Ranks · Custom Filters"
 *
 * Each group declares **which fields of `ScreenDefinition` it owns**. That list is not decoration:
 * `src/lib/screens/__tests__/coverage.test.ts` reads the Zod schema and asserts every field is
 * claimed by exactly one group, which is Prompt 9's fourth acceptance criterion — "No filter in
 * docs/01 §2 is missing from the UI".
 *
 * `activeCount` answers docs/08's "count badge of active filters inside it, so a collapsed group
 * never hides state", using the sentinel predicates from `defaults.ts`.
 */
export type DefinitionField = keyof ScreenDefinition;

export interface FilterGroup {
  id: string;
  /** The heading, in the reference product's own wording (docs/01 §2). */
  title: string;
  /** The `ScreenDefinition` fields this group edits. */
  fields: readonly DefinitionField[];
  activeCount: (definition: ScreenDefinition) => number;
}

/**
 * `index`, `sort_by` and `sort_direction` are docs/08's "always visible" row rather than an
 * accordion group, so they are claimed here separately and the coverage test counts them too.
 */
export const ALWAYS_VISIBLE_FIELDS: readonly DefinitionField[] = [
  "index",
  "sort_by",
  "sort_direction",
];

export const FILTER_GROUPS: readonly FilterGroup[] = [
  {
    id: "general",
    title: "General Filters",
    fields: ["apply_filters_on", "min_return_1y", "median_volume_1y"],
    activeCount: (d) =>
      (d.apply_filters_on === "all" ? 0 : 1) +
      (d.min_return_1y === null ? 0 : 1) +
      (d.median_volume_1y === null ? 0 : 1),
  },
  {
    id: "moving-average",
    title: "Moving Average Filters",
    fields: ["moving_average"],
    activeCount: (d) => movingAverageCount(d.moving_average),
  },
  {
    id: "away-from-high",
    title: "Away from High Filters",
    fields: ["away_from_high"],
    activeCount: (d) =>
      (awayFromHighActive(d.away_from_high.ath) ? 1 : 0) +
      (awayFromHighActive(d.away_from_high.one_year) ? 1 : 0),
  },
  {
    id: "positive-days",
    title: "Percentage of Positive Days Filters",
    fields: ["positive_days"],
    activeCount: (d) => WINDOW_KEYS.filter((k) => positiveDaysActive(d.positive_days[k])).length,
  },
  {
    id: "circuits",
    title: "Circuit Filters",
    fields: ["circuits"],
    activeCount: (d) => WINDOW_KEYS.filter((k) => circuitsActive(d.circuits[k])).length,
  },
  {
    id: "marketcap",
    title: "Marketcap Range",
    fields: ["marketcap"],
    activeCount: (d) => (rangeActive(d.marketcap) ? 1 : 0),
  },
  {
    id: "pe",
    title: "Price to Earnings Range",
    fields: ["pe"],
    activeCount: (d) => (d.pe.enabled ? 1 : 0),
  },
  {
    id: "series",
    title: "Series",
    fields: ["series"],
    // Both series selected is the widest setting, so it filters nothing.
    activeCount: (d) => (d.series.length === 2 ? 0 : 1),
  },
  {
    id: "risk",
    title: "Ignore Top Beta / Volatility",
    fields: ["ignore_top_beta", "ignore_top_volatility", "ignore_above_beta"],
    activeCount: (d) =>
      (d.ignore_top_beta.enabled ? 1 : 0) +
      (d.ignore_top_volatility.enabled ? 1 : 0) +
      (ignoreAboveBetaActive(d.ignore_above_beta) ? 1 : 0),
  },
  {
    id: "price",
    title: "Price (CMP) Range",
    fields: ["price"],
    activeCount: (d) => (rangeActive(d.price) ? 1 : 0),
  },
  {
    id: "multi-factor",
    title: "Multi-Factor Combined Ranking",
    fields: ["factor_two", "factor_three"],
    activeCount: (d) => (d.factor_two.enabled ? 1 : 0) + (d.factor_three.enabled ? 1 : 0),
  },
  {
    id: "historical",
    title: "Historical Ranks",
    fields: ["historical_date"],
    activeCount: (d) => (d.historical_date === null ? 0 : 1),
  },
  {
    id: "custom",
    title: "Custom Filters",
    fields: ["custom_filters"],
    activeCount: (d) => d.custom_filters.filter((slot) => slot.enabled).length,
  },
];

/** Every field the form claims, across the always-visible row and the groups. */
export const COVERED_FIELDS: readonly DefinitionField[] = [
  ...ALWAYS_VISIBLE_FIELDS,
  ...FILTER_GROUPS.flatMap((group) => group.fields),
];

export function totalActiveFilters(definition: ScreenDefinition): number {
  return FILTER_GROUPS.reduce((total, group) => total + group.activeCount(definition), 0);
}

export const MA_WINDOW_LIST = MA_WINDOWS;
