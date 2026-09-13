import {
  MAX_FACTOR_RANGES,
  MAX_RANKING_TERMS,
  ScreenDefinitionSchema,
  type FactorOut,
  type ScreenDefinition,
} from "@baskfy/api-client";

/**
 * Pure helpers behind the editor's Ranking, Factor Ranges and Regime sections
 * (docs/ranking/PLAN.md C3; gates/ranking-2.H-web.md G1–G2).
 *
 * **Every edit is validated before it is patched.** The editor mirrors its state into the URL, and
 * `decodeState` discards a definition the schema refuses — so a patch that breaks a C3 rule
 * (single mode with two terms, a target range with no bound, `sort_by` out of step with the first
 * term) would vanish without a word. `checkPatch` runs the same Zod schema first and hands the
 * section a sentence to show instead.
 */

/**
 * `FactorOut` plus the registry fields C6 adds to `GET /meta/factors`.
 *
 * Optional because the generated client does not carry them yet — the API regeneration is leaf
 * 2.G's. Once it does, `FactorOut` has them and this intersection collapses into it.
 */
export type RankingFactorMeta = FactorOut & {
  preference?: string | undefined;
  rankable?: boolean | undefined;
  weight_family?: string | undefined;
  validation_status?: string | undefined;
  definition?: string | undefined;
};

export type RankingMode = ScreenDefinition["ranking_mode"];
export type RankingScope = ScreenDefinition["ranking_scope"];
export type RankingTerm = ScreenDefinition["ranking_terms"][number];
export type TermPreference = RankingTerm["preference"];
export type MissingDataPolicy = ScreenDefinition["missing_data"];
export type FactorRange = ScreenDefinition["factor_ranges"][number];
export type RegimeLabel = NonNullable<ScreenDefinition["regime_in"]>[number];
export type WeightFamily = keyof NonNullable<ScreenDefinition["family_weights"]>;

/** The editor's new-screen scope (decision 2D.1). The schema default stays `filtered_results`. */
export const NEW_SCREEN_RANKING_SCOPE: RankingScope = "fixed_universe";

export const WEIGHT_FAMILIES: readonly { key: WeightFamily; label: string }[] = [
  { key: "momentum", label: "Momentum" },
  { key: "path_quality", label: "Path quality" },
  { key: "trend_structure", label: "Trend structure" },
  { key: "participation", label: "Participation" },
  { key: "risk_execution", label: "Risk and execution" },
];

export function weightFamilyLabel(key: string | undefined): string | null {
  if (key === undefined) return null;
  return WEIGHT_FAMILIES.find((family) => family.key === key)?.label ?? key;
}

/**
 * Whether a factor may be a ranking term. `rankable` from the registry when the API sends it;
 * until then an `eligibility` preference is the registry's own marker for a filter-only factor
 * (`excess_ret_*`, `atr_14`, `mom_pctile`, `nse_mr*`). The server refuses the rest either way.
 */
export function isRankable(factor: RankingFactorMeta): boolean {
  if (factor.rankable !== undefined) return factor.rankable;
  return factor.preference !== "eligibility";
}

export function rankableFactors(factors: readonly RankingFactorMeta[]): RankingFactorMeta[] {
  return factors.filter(isRankable);
}

const TERM_PREFERENCES: readonly TermPreference[] = ["higher", "lower", "target_range"];

/** The registry's preference when it is a term preference, else `higher_is_better`. */
export function defaultPreference(factor: RankingFactorMeta | undefined): TermPreference {
  const stated = factor?.preference;
  if (stated !== undefined && (TERM_PREFERENCES as readonly string[]).includes(stated)) {
    return stated as TermPreference;
  }
  return factor === undefined || factor.higher_is_better ? "higher" : "lower";
}

function findFactor(
  factors: readonly RankingFactorMeta[],
  key: string,
): RankingFactorMeta | undefined {
  return factors.find((factor) => factor.key === key);
}

/**
 * A new term on `key`. A target-range factor gets `higher` until the user supplies a band,
 * because a target range with no bound is not a valid term.
 */
export function newTerm(factors: readonly RankingFactorMeta[], key: string): RankingTerm {
  const preference = defaultPreference(findFactor(factors, key));
  return {
    factor: key,
    preference: preference === "target_range" ? "higher" : preference,
    weight: 1,
    target_min: null,
    target_max: null,
  };
}

/** The fields that must follow the terms list so the C3 rules keep holding. */
function followTerms(
  definition: ScreenDefinition,
  terms: RankingTerm[],
): Partial<ScreenDefinition> {
  const first = terms[0];
  if (first === undefined) {
    return {
      ranking_terms: [],
      family_weights: null,
      missing_data: "penalize",
      ranking_scope:
        definition.ranking_scope === "within_sector"
          ? NEW_SCREEN_RANKING_SCOPE
          : definition.ranking_scope,
    };
  }
  return {
    ranking_terms: terms,
    sort_by: first.factor,
    // Terms replace factor two and three (C3).
    factor_two: { ...definition.factor_two, enabled: false },
    factor_three: { ...definition.factor_three, enabled: false },
  };
}

export function withTermAdded(
  definition: ScreenDefinition,
  factors: readonly RankingFactorMeta[],
): Partial<ScreenDefinition> {
  const terms = definition.ranking_terms;
  const used = new Set(terms.map((term) => term.factor));
  const candidates = rankableFactors(factors).filter((factor) => !used.has(factor.key));
  // The first term starts from Sort By, so adding one does not change what the screen ranks on.
  const sortByFactor = findFactor(factors, definition.sort_by);
  const preferred =
    terms.length === 0 &&
    !used.has(definition.sort_by) &&
    (sortByFactor === undefined || isRankable(sortByFactor))
      ? definition.sort_by
      : candidates[0]?.key;
  if (preferred === undefined) return {};
  return followTerms(definition, [...terms, newTerm(factors, preferred)]);
}

export function withTermRemoved(
  definition: ScreenDefinition,
  index: number,
): Partial<ScreenDefinition> {
  return followTerms(
    definition,
    definition.ranking_terms.filter((_, i) => i !== index),
  );
}

export function withTermChanged(
  definition: ScreenDefinition,
  index: number,
  next: Partial<RankingTerm>,
): Partial<ScreenDefinition> {
  return followTerms(
    definition,
    definition.ranking_terms.map((term, i) => (i === index ? { ...term, ...next } : term)),
  );
}

/** A new factor on an existing term: the preference follows the factor, the band is dropped. */
export function withTermFactor(
  definition: ScreenDefinition,
  factors: readonly RankingFactorMeta[],
  index: number,
  key: string,
): Partial<ScreenDefinition> {
  const current = definition.ranking_terms[index];
  if (current === undefined) return {};
  const fresh = newTerm(factors, key);
  return withTermChanged(definition, index, { ...fresh, weight: current.weight });
}

/**
 * Sort By changed from outside the Ranking section (the chip, the rail). With explicit terms,
 * `sort_by` must equal the first term, so the first term follows it.
 */
export function withSortBy(
  definition: ScreenDefinition,
  factors: readonly RankingFactorMeta[],
  key: string,
): Partial<ScreenDefinition> {
  if (definition.ranking_terms.length === 0) return { sort_by: key };
  return { sort_by: key, ...withTermFactor(definition, factors, 0, key) };
}

/** Switching mode, carrying along whatever the new mode's rules require (C3). */
export function withRankingMode(
  definition: ScreenDefinition,
  mode: RankingMode,
): Partial<ScreenDefinition> {
  const partial: Partial<ScreenDefinition> = { ranking_mode: mode };
  let terms = definition.ranking_terms;
  if (mode === "single") {
    partial.factor_two = { ...definition.factor_two, enabled: false };
    partial.factor_three = { ...definition.factor_three, enabled: false };
    terms = terms.slice(0, 1);
  }
  if (mode !== "composite") {
    terms = terms.map((term) => ({ ...term, weight: 1 }));
    partial.family_weights = null;
    if (definition.ranking_scope === "within_sector") {
      partial.ranking_scope = NEW_SCREEN_RANKING_SCOPE;
    }
  }
  if (terms !== definition.ranking_terms) partial.ranking_terms = terms;
  return partial;
}

export function canAddTerm(definition: ScreenDefinition): boolean {
  if (definition.ranking_mode === "single") return definition.ranking_terms.length === 0;
  return definition.ranking_terms.length < MAX_RANKING_TERMS;
}

/** `within_sector` only changes a composite score over explicit terms (C3, 2D.4). */
export function withinSectorAllowed(definition: ScreenDefinition): boolean {
  return definition.ranking_mode === "composite" && definition.ranking_terms.length > 0;
}

// --- factor ranges ----------------------------------------------------------

export const EXCESS_RETURN_WINDOWS = [
  { key: "excess_ret_3m", label: "3 months" },
  { key: "excess_ret_6m", label: "6 months" },
  { key: "excess_ret_12m", label: "1 year" },
] as const;

export type ExcessReturnKey = (typeof EXCESS_RETURN_WINDOWS)[number]["key"];

const EXCESS_KEYS: readonly string[] = EXCESS_RETURN_WINDOWS.map((window) => window.key);

/** The range the "beat NIFTY 500" control owns: an excess-return factor with only a minimum. */
export function beatBenchmarkIndex(ranges: readonly FactorRange[]): number {
  return ranges.findIndex(
    (range) => EXCESS_KEYS.includes(range.factor) && range.min !== null && range.max === null,
  );
}

/**
 * Set, move or clear the "beat NIFTY 500 by at least `points` pp" range. `excess_ret_*` is stored
 * in percentage points (return % minus NIFTY 500's return %), so the minimum is the number typed.
 */
export function withBeatBenchmark(
  definition: ScreenDefinition,
  key: ExcessReturnKey,
  points: number | null,
): Partial<ScreenDefinition> {
  const ranges = definition.factor_ranges;
  const index = beatBenchmarkIndex(ranges);
  if (points === null) {
    return index === -1 ? {} : { factor_ranges: ranges.filter((_, i) => i !== index) };
  }
  const range: FactorRange = { enabled: true, factor: key, min: points, max: null };
  if (index === -1) return { factor_ranges: [...ranges, range] };
  return { factor_ranges: ranges.map((existing, i) => (i === index ? range : existing)) };
}

export function canAddRange(definition: ScreenDefinition): boolean {
  return definition.factor_ranges.length < MAX_FACTOR_RANGES;
}

export function withRegimeToggled(
  definition: ScreenDefinition,
  label: RegimeLabel,
  on: boolean,
): Partial<ScreenDefinition> {
  const current = definition.regime_in ?? [];
  const next = on
    ? [...current.filter((entry) => entry !== label), label]
    : current.filter((entry) => entry !== label);
  return { regime_in: next.length === 0 ? null : next };
}

// --- validation -------------------------------------------------------------

/**
 * `null` when `definition + partial` is a valid screen, else the first reason it is not.
 */
export function checkPatch(
  definition: ScreenDefinition,
  partial: Partial<ScreenDefinition>,
): string | null {
  const parsed = ScreenDefinitionSchema.safeParse({ ...definition, ...partial });
  if (parsed.success) return null;
  const message = parsed.error.issues[0]?.message ?? "";
  return (
    PLAIN_MESSAGES.find(([needle]) => message.includes(needle))?.[1] ??
    (message || "this change would make the screen invalid.")
  );
}

/** The schema's messages name fields; the editor names controls. First match wins. */
const PLAIN_MESSAGES: readonly (readonly [string, string])[] = [
  ["name a factor more than once", "that factor is already a term. Raise its weight instead."],
  ["within_sector", "Within sector needs Composite mode and at least one ranking term."],
  ["single' ranks by exactly one term", "Single mode ranks by exactly one term."],
  ["factor_ranges[", "a factor range needs a minimum, a maximum or both, with the minimum not above the maximum."],
  ["regime_in", "pick at least one regime, or clear them all to turn the filter off."],
];

/** How a factor's stored number reads, so a range bound is typed in the right unit. */
export function unitHint(unit: string | undefined): string {
  switch (unit) {
    case "percent":
      return "In percent: 5 means 5%.";
    case "fraction":
      return "As a fraction: 0.25 means 25%.";
    case "crore":
      return "In ₹ crore.";
    case "rupees":
      return "In rupees.";
    case "price":
      return "In rupees per share.";
    case "count":
      return "A count.";
    default:
      return "In the factor's own units.";
  }
}
