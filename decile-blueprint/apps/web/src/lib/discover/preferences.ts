import {
  DEFAULT_PREFERENCES,
  GOAL_COPY,
  HORIZON_COPY,
  type GoalKey,
  type HorizonKey,
  type Preferences,
  REBALANCE_COPY,
  RISK_COPY,
  type RebalanceKey,
  type RiskKey,
} from "@/lib/discover/match";

/**
 * Preferences in the URL, so a filtered Discover page is a link.
 *
 * The composer is a client component and the results are rendered on the server, so the state has
 * to cross that boundary somehow. The URL is the only carrier that also makes the result
 * shareable, bookmarkable, back-button-correct and reproducible in a bug report — which
 * `useState` in a provider is not.
 *
 * Every value is validated on the way back in. A hand-edited `?risk=extreme` falls back to the
 * default rather than reaching the matcher, because a preference the matcher does not understand
 * would silently stop being checked and quietly change what "3 of 4" means.
 */

const PARAM = {
  goal: "goal",
  horizon: "horizon",
  risk: "risk",
  amount: "amount",
  rebalance: "rebalance",
} as const;

function one(
  params: URLSearchParams | Record<string, string | string[] | undefined>,
  key: string,
): string | undefined {
  if (params instanceof URLSearchParams) return params.get(key) ?? undefined;
  const raw = params[key];
  if (Array.isArray(raw)) return raw[0];
  return raw;
}

function validated<T extends string>(
  value: string | undefined,
  allowed: Record<T, unknown>,
  fallback: T,
): T {
  return value !== undefined && Object.prototype.hasOwnProperty.call(allowed, value)
    ? (value as T)
    : fallback;
}

/** The smallest and largest amounts the composer will accept, in rupees. */
export const MIN_AMOUNT = 1_000;
export const MAX_AMOUNT = 100_000_000;

export function validAmount(raw: string | number | undefined): number {
  // Currency symbol, grouping separators and spaces only. Stripping *every* non-digit turned
  // "-5000" into 5000, which passed the range check as a positive amount the reader never typed.
  const numeric =
    typeof raw === "string" ? Number(raw.replace(/[₹,\s]/g, "").trim() || "x") : raw;
  if (numeric === undefined || !Number.isFinite(numeric)) return DEFAULT_PREFERENCES.amount;
  if (numeric < MIN_AMOUNT || numeric > MAX_AMOUNT) return DEFAULT_PREFERENCES.amount;
  return Math.round(numeric);
}

export function preferencesFromParams(
  params: URLSearchParams | Record<string, string | string[] | undefined>,
): Preferences {
  return {
    goal: validated<GoalKey>(one(params, PARAM.goal), GOAL_COPY, DEFAULT_PREFERENCES.goal),
    horizon: validated<HorizonKey>(
      one(params, PARAM.horizon),
      HORIZON_COPY,
      DEFAULT_PREFERENCES.horizon,
    ),
    risk: validated<RiskKey>(one(params, PARAM.risk), RISK_COPY, DEFAULT_PREFERENCES.risk),
    amount: validAmount(one(params, PARAM.amount)),
    rebalance: validated<RebalanceKey>(
      one(params, PARAM.rebalance),
      REBALANCE_COPY,
      DEFAULT_PREFERENCES.rebalance,
    ),
  };
}

export function preferencesToQuery(prefs: Preferences): string {
  const params = new URLSearchParams();
  params.set(PARAM.goal, prefs.goal);
  params.set(PARAM.horizon, prefs.horizon);
  params.set(PARAM.risk, prefs.risk);
  params.set(PARAM.amount, String(prefs.amount));
  params.set(PARAM.rebalance, prefs.rebalance);
  return params.toString();
}

/** True when the reader has actually stated preferences, rather than landing on the defaults. */
export function hasStatedPreferences(
  params: URLSearchParams | Record<string, string | string[] | undefined>,
): boolean {
  return Object.values(PARAM).some((key) => one(params, key) !== undefined);
}
