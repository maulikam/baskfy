import type { ExploreBasketCard, ExploreListParams } from "@/lib/explore/fetch";
import { formatReturn, formatRupeesCompact, formatVolatility } from "@/lib/discover/metrics";

/**
 * Turning what a reader says they want into a filter, and explaining what the filter did.
 *
 * **This module is filtering, and its language has to keep saying so.** D3 — the regulatory
 * posture — is written but unreviewed (root `CLAUDE.md`), and this product is not a registered
 * adviser. So nothing here may produce "best for you", "recommended" or "suitable": a preference
 * either matched a stated, visible property of a basket or it did not, and the reader is told
 * which. `matchSummary` is deliberately built from the words "matches n of m preferences" and
 * from the names of the preferences that matched.
 *
 * **A preference that cannot be checked is not counted as a match.** Three of the five things the
 * brief's composer asks for can be checked exactly against data this product holds; the other two
 * can only be checked partially, and one of those — horizon — is checked as *evidence available*
 * rather than as suitability, because "this basket has fourteen months of history" is a fact and
 * "this basket suits a five-year horizon" is an opinion. `checked` counts only what was really
 * examined, so "4 of 5" never quietly includes a coin flip.
 */

export type GoalKey = "steady-compounding" | "long-term-growth" | "high-growth";
export type HorizonKey = "1-3" | "3-5" | "5-plus";
export type RiskKey = "lower" | "moderate" | "higher";
export type RebalanceKey = "any" | "WEEKLY" | "MONTHLY" | "QUARTERLY";

export interface Preferences {
  goal: GoalKey;
  horizon: HorizonKey;
  risk: RiskKey;
  /** Rupees. The reader types a number; the composer never guesses one. */
  amount: number;
  rebalance: RebalanceKey;
}

export const DEFAULT_PREFERENCES: Preferences = {
  goal: "long-term-growth",
  horizon: "5-plus",
  risk: "moderate",
  amount: 500_000,
  rebalance: "any",
};

export const GOAL_COPY: Record<GoalKey, string> = {
  "steady-compounding": "steady compounding",
  "long-term-growth": "long-term growth",
  "high-growth": "high growth",
};

export const HORIZON_COPY: Record<HorizonKey, string> = {
  "1-3": "1–3 years",
  "3-5": "3–5 years",
  "5-plus": "5+ years",
};

export const RISK_COPY: Record<RiskKey, string> = {
  lower: "lower volatility",
  moderate: "moderate volatility",
  higher: "higher volatility",
};

export const REBALANCE_COPY: Record<RebalanceKey, string> = {
  any: "any rebalance schedule",
  WEEKLY: "weekly rebalancing",
  MONTHLY: "monthly rebalancing",
  QUARTERLY: "quarterly rebalancing",
};

/** Months of history a horizon would want before its evidence means anything. */
const HORIZON_MONTHS: Record<HorizonKey, number> = {
  "1-3": 12,
  "3-5": 36,
  "5-plus": 60,
};

/**
 * The category tags each goal filters for.
 *
 * These are the tags `cb_basket.categories` actually carries. The mapping is an editorial
 * shortcut and is described as one wherever it appears: a basket "carries the momentum tag,
 * which this goal filters for" is a statement about tags, not about the reader's finances.
 */
const GOAL_CATEGORIES: Record<GoalKey, readonly string[]> = {
  "steady-compounding": ["broad-market", "low-volatility", "risk-trimmed", "quality"],
  "long-term-growth": ["momentum", "quality", "risk-trimmed"],
  "high-growth": ["momentum", "trend", "short-window"],
};

const RISK_BUCKETS: Record<RiskKey, readonly string[]> = {
  lower: ["LOW"],
  moderate: ["LOW", "MEDIUM", "MED"],
  higher: ["MEDIUM", "MED", "HIGH"],
};

export interface PreferenceCheck {
  key: keyof Preferences;
  /** How the preference reads in the summary sentence: "a 5+ year horizon". */
  label: string;
  matched: boolean;
  /** Why it matched, or why it did not. Always a fact about the basket. */
  reason: string;
  /**
   * False when this product does not hold what the check would need. An unexaminable preference
   * is excluded from both the numerator and the denominator, never counted as a pass.
   */
  examinable: boolean;
}

export interface MatchResult {
  slug: string;
  checks: PreferenceCheck[];
  /** Preferences that matched. */
  matched: number;
  /** Preferences that could be examined at all. */
  examined: number;
  /** "Matches 3 of 4 preferences you set: …" — filter language, never advice. */
  summary: string;
}

function monthsAvailable(basket: ExploreBasketCard): number | null {
  const raw = basket.metrics?.as_of_date;
  const launched = basket.launched_at;
  if (!raw || !launched) return null;
  const start = new Date(`${launched.slice(0, 10)}T00:00:00Z`);
  const end = new Date(`${raw.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null;
  const months =
    (end.getUTCFullYear() - start.getUTCFullYear()) * 12 +
    (end.getUTCMonth() - start.getUTCMonth());
  return Math.max(months, 0);
}

function checkAmount(basket: ExploreBasketCard, prefs: Preferences): PreferenceCheck {
  const min = basket.metrics?.min_amount;
  if (min === null || min === undefined || min === "") {
    return {
      key: "amount",
      label: `around ${formatRupeesCompact(prefs.amount)}`,
      matched: false,
      reason: "No minimum investment has been computed for this basket yet.",
      examinable: false,
    };
  }
  const minimum = typeof min === "string" ? Number(min) : min;
  const matched = !Number.isNaN(minimum) && minimum <= prefs.amount;
  return {
    key: "amount",
    label: `around ${formatRupeesCompact(prefs.amount)}`,
    matched,
    reason: matched
      ? `Its minimum is ${formatRupeesCompact(minimum)}, within the ${formatRupeesCompact(prefs.amount)} you entered.`
      : `Its minimum is ${formatRupeesCompact(minimum)}, above the ${formatRupeesCompact(prefs.amount)} you entered.`,
    examinable: true,
  };
}

function checkRisk(basket: ExploreBasketCard, prefs: Preferences): PreferenceCheck {
  const bucket = basket.metrics?.volatility_bucket;
  const label = RISK_COPY[prefs.risk];
  if (!bucket) {
    return {
      key: "risk",
      label,
      matched: false,
      reason: "No volatility has been computed for this basket yet.",
      examinable: false,
    };
  }
  const allowed = RISK_BUCKETS[prefs.risk];
  const matched = allowed.includes(bucket.toUpperCase());
  const measured = basket.metrics?.volatility_value
    ? ` (${formatVolatility(basket.metrics.volatility_value)} annualised)`
    : "";
  return {
    key: "risk",
    label,
    matched,
    reason: matched
      ? `Its volatility bucket is ${bucket.toLowerCase()}${measured}, inside the range you asked for.`
      : `Its volatility bucket is ${bucket.toLowerCase()}${measured}, outside the range you asked for.`,
    examinable: true,
  };
}

function checkRebalance(basket: ExploreBasketCard, prefs: Preferences): PreferenceCheck {
  const label = REBALANCE_COPY[prefs.rebalance];
  if (prefs.rebalance === "any") {
    return {
      key: "rebalance",
      label,
      matched: false,
      reason: "You did not restrict the rebalance schedule, so this was not checked.",
      examinable: false,
    };
  }
  const matched = basket.rebalance_frequency.toUpperCase() === prefs.rebalance;
  return {
    key: "rebalance",
    label,
    matched,
    reason: `It rebalances ${basket.rebalance_frequency.toLowerCase()}.`,
    examinable: true,
  };
}

function checkGoal(basket: ExploreBasketCard, prefs: Preferences): PreferenceCheck {
  const wanted = GOAL_CATEGORIES[prefs.goal];
  const label = GOAL_COPY[prefs.goal];
  const carried = basket.categories.filter((category) => wanted.includes(category));
  if (basket.categories.length === 0) {
    return {
      key: "goal",
      label,
      matched: false,
      reason: "This basket carries no category tags, so there was nothing to match against.",
      examinable: false,
    };
  }
  const matched = carried.length > 0;
  return {
    key: "goal",
    label,
    matched,
    reason: matched
      ? `It carries the ${carried.map((tag) => `“${tag}”`).join(" and ")} tag${carried.length > 1 ? "s" : ""}, which this goal filters for.`
      : `It carries ${basket.categories.map((tag) => `“${tag}”`).join(", ")}, none of which this goal filters for.`,
    examinable: true,
  };
}

/**
 * Horizon is checked as *evidence available*, not as suitability.
 *
 * "This basket has fourteen months of history, so there is no five-year record to judge it on" is
 * a fact. "This basket suits a five-year horizon" is an opinion about the reader's finances, and
 * this product does not get to have one.
 */
function checkHorizon(basket: ExploreBasketCard, prefs: Preferences): PreferenceCheck {
  const label = `a ${HORIZON_COPY[prefs.horizon]} horizon`;
  const months = monthsAvailable(basket);
  if (months === null) {
    return {
      key: "horizon",
      label,
      matched: false,
      reason: "This basket has no launch date recorded, so its history cannot be measured.",
      examinable: false,
    };
  }
  const wanted = HORIZON_MONTHS[prefs.horizon];
  const matched = months >= wanted;
  return {
    key: "horizon",
    label,
    matched,
    reason: matched
      ? `It has ${months} months of history, enough to have been observed over ${HORIZON_COPY[prefs.horizon]}.`
      : `It has ${months} months of history, so there is no ${HORIZON_COPY[prefs.horizon]} record to judge it on.`,
    examinable: true,
  };
}

export function evaluateMatch(basket: ExploreBasketCard, prefs: Preferences): MatchResult {
  const checks: PreferenceCheck[] = [
    checkGoal(basket, prefs),
    checkHorizon(basket, prefs),
    checkRisk(basket, prefs),
    checkAmount(basket, prefs),
    checkRebalance(basket, prefs),
  ];
  const examinable = checks.filter((check) => check.examinable);
  const matched = examinable.filter((check) => check.matched);
  return {
    slug: basket.slug,
    checks,
    matched: matched.length,
    examined: examinable.length,
    summary: matchSummary(matched.length, examinable.length, matched.map((c) => c.label)),
  };
}

/**
 * The sentence under a starting choice.
 *
 * It names the preferences that matched, so a reader can disagree with the filter instead of
 * having to trust it, and it says how many were examined rather than how many were set — a
 * preference this product cannot check does not get to inflate the numerator or the denominator.
 */
export function matchSummary(
  matched: number,
  examined: number,
  labels: readonly string[],
): string {
  if (examined === 0) {
    return "None of your preferences could be checked against the data this basket has.";
  }
  if (matched === 0) {
    return `Matches none of the ${examined} preferences that could be checked.`;
  }
  return `Matches ${matched} of ${examined} preferences that could be checked: ${labels.join(", ")}.`;
}

/** The filter parameters "Show matching baskets" actually sends. */
export function preferencesToParams(prefs: Preferences): ExploreListParams {
  const params: ExploreListParams = {
    max_min_amount: String(prefs.amount),
    sort: "min_amount",
    order: "asc",
  };
  if (prefs.risk === "lower") params.volatility = "LOW";
  if (prefs.risk === "higher") params.volatility = "HIGH";
  if (prefs.rebalance !== "any") params.rebalance_frequency = prefs.rebalance;
  return params;
}

export type StartingChoiceKind = "closest" | "lower-swing" | "higher-growth";

export interface StartingChoice {
  kind: StartingChoiceKind;
  /** What this column is, in the reader's words. */
  title: string;
  /** Why this basket is in this column — a property of the basket, never a judgement. */
  rationale: string;
  basket: ExploreBasketCard;
  match: MatchResult;
}

function volatilityOf(basket: ExploreBasketCard): number {
  const raw = basket.metrics?.volatility_value;
  if (raw === null || raw === undefined || raw === "") return Number.POSITIVE_INFINITY;
  const value = typeof raw === "string" ? Number(raw) : raw;
  return Number.isNaN(value) ? Number.POSITIVE_INFINITY : value;
}

function headlineOf(basket: ExploreBasketCard): number {
  const raw = basket.metrics?.headline_pct;
  if (raw === null || raw === undefined || raw === "") return Number.NEGATIVE_INFINITY;
  const value = typeof raw === "string" ? Number(raw) : raw;
  return Number.isNaN(value) ? Number.NEGATIVE_INFINITY : value;
}

/**
 * Three starting choices, and never the same basket twice.
 *
 * The brief asks for a closest match, a lower-swing option and a higher-growth option. Two things
 * make that harder than picking three maxima.
 *
 * **The catalogue can collapse onto one basket.** With six published baskets, all momentum, the
 * naive version showed the same card three times — exactly the failure the collection shelves
 * had. So each column takes the best *remaining* candidate, and a column with nothing left is
 * absent rather than a repeat.
 *
 * **The lead column must not steal an extreme.** Tie-breaking "closest" by lowest volatility
 * looked sensible and was wrong: with three baskets matching equally it took the calmest, and the
 * next column then claimed "lowest volatility" about a basket that was not the lowest. Ties are
 * broken toward the *least extreme* basket instead — the median by volatility — which leaves both
 * ends for the columns whose whole job is to be an end.
 *
 * Even so, a superlative is only written when it is checked against the three actually shown.
 */
export function startingChoices(
  baskets: readonly ExploreBasketCard[],
  prefs: Preferences,
): StartingChoice[] {
  if (baskets.length === 0) return [];
  const matches = new Map(baskets.map((basket) => [basket.slug, evaluateMatch(basket, prefs)]));
  const taken = new Set<string>();
  const remaining = (): ExploreBasketCard[] => baskets.filter((b) => !taken.has(b.slug));

  const byMatchCount = [...baskets].sort(
    (a, b) => (matches.get(b.slug)?.matched ?? 0) - (matches.get(a.slug)?.matched ?? 0),
  );
  const bestCount = matches.get(byMatchCount[0]!.slug)?.matched ?? 0;
  const tied = byMatchCount
    .filter((basket) => (matches.get(basket.slug)?.matched ?? 0) === bestCount)
    .sort((a, b) => volatilityOf(a) - volatilityOf(b));

  // The lead column does not take a basket that one of the other two columns exists to show.
  // Otherwise "Moves around less" ends up describing the second-calmest basket while the calmest
  // sits in the column beside it, and the superlative under it is simply false. When every tied
  // basket is an extreme — a catalogue of one or two — the lead column takes one anyway, and the
  // superlative check below is what stops the wording overreaching.
  const calmestOverall = Math.min(...baskets.map(volatilityOf));
  const strongestOverall = Math.max(...baskets.map(headlineOf));
  const unclaimed = tied.filter(
    (basket) => volatilityOf(basket) !== calmestOverall && headlineOf(basket) !== strongestOverall,
  );
  const pool = unclaimed.length > 0 ? unclaimed : tied;
  const closest = pool[Math.floor((pool.length - 1) / 2)];

  const picks: { kind: StartingChoiceKind; basket: ExploreBasketCard }[] = [];
  if (closest) {
    taken.add(closest.slug);
    picks.push({ kind: "closest", basket: closest });
  }

  const calmest = [...remaining()].sort((a, b) => volatilityOf(a) - volatilityOf(b))[0];
  if (calmest && volatilityOf(calmest) !== Number.POSITIVE_INFINITY) {
    taken.add(calmest.slug);
    picks.push({ kind: "lower-swing", basket: calmest });
  }

  const strongest = [...remaining()].sort((a, b) => headlineOf(b) - headlineOf(a))[0];
  if (strongest && headlineOf(strongest) !== Number.NEGATIVE_INFINITY) {
    taken.add(strongest.slug);
    picks.push({ kind: "higher-growth", basket: strongest });
  }

  const shown = picks.map((pick) => pick.basket);
  const lowestShown = Math.min(...shown.map(volatilityOf));
  const highestShown = Math.max(...shown.map(headlineOf));

  return picks.map(({ kind, basket }) => {
    const match = matches.get(basket.slug)!;
    if (kind === "closest") {
      return {
        kind,
        title: "Closest to your preferences",
        rationale: match.summary,
        basket,
        match,
      };
    }
    if (kind === "lower-swing") {
      const superlative =
        volatilityOf(basket) === lowestShown ? "the least of the three shown here" : "lower";
      return {
        kind,
        title: "Moves around less",
        rationale: `Annualised volatility of ${formatVolatility(
          basket.metrics?.volatility_value ?? null,
        )} — ${superlative}. Lower volatility has meant smaller swings, not higher returns.`,
        basket,
        match,
      };
    }
    const superlative =
      headlineOf(basket) === highestShown ? "the highest of the three shown here" : "higher";
    return {
      kind,
      title: "Has returned more, with more risk",
      rationale: `${
        basket.metrics?.headline_label ?? "Return"
      } of ${formatReturn(basket.metrics?.headline_pct ?? null)} — ${superlative}. A higher past return is not a forecast, and it came with this basket's own volatility of ${formatVolatility(
        basket.metrics?.volatility_value ?? null,
      )}.`,
      basket,
      match,
    };
  });
}
