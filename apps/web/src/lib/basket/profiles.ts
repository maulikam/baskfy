/**
 * Holding profiles and the cash share, mirrored from `baskfy_core.basket_sizing` (SB1).
 *
 * Two separate questions, kept separate on purpose:
 *
 * - **How many names** is a concentration preference — a fact about the investor. That is what a
 *   profile carries, and fewer names is the *more aggressive* choice.
 * - **How much stays in cash** is a fact about today, and the desk already decides it weekly as
 *   an equity exposure cap (R1 100%, R2 70%, R3 40%). `cashPctForTier` reads that cap.
 *
 * Folding the two together would mean an investor could not ask for a concentrated basket without
 * also claiming the market was defensive.
 *
 * This file is a duplicate of a Python table, for the same reason `lib/screens/operands.ts` and
 * `lib/market/universes.ts` are: no `/meta/` endpoint publishes it, and a network round trip per
 * keystroke would be a worse trade than a parity test. The link is
 * `packages/core/tests/test_holding_profile_parity.py`, which fails if the two drift.
 */

export const HOLDING_PROFILES = ["CONSERVATIVE", "BALANCED", "AGGRESSIVE"] as const;

export type HoldingProfile = (typeof HOLDING_PROFILES)[number];

/** Mirrors Python `SUGGESTED_HOLDINGS`. Fewer names = more concentrated = more aggressive. */
export const SUGGESTED_HOLDINGS: Record<HoldingProfile, number> = {
  CONSERVATIVE: 25,
  BALANCED: 20,
  AGGRESSIVE: 12,
};

/** Mirrors Python `DEFAULT_PROFILE`. 20 is what a screen has always materialized at. */
export const DEFAULT_PROFILE: HoldingProfile = "BALANCED";

/** Mirrors Python `MIN_HOLDINGS` / `MAX_HOLDINGS`. */
export const MIN_HOLDINGS = 2;
export const MAX_HOLDINGS = 50;

/** Mirrors Python `MIN_CASH_BUFFER_PCT` / `MAX_CASH_PCT` / `ZERO_CASH_PCT`. */
export const MIN_CASH_BUFFER_PCT = 5;
export const MAX_CASH_PCT = 95;
/** An explicit opt-out of the cash sleeve (SB7). Not a silent default. */
export const ZERO_CASH_PCT = 0;

/** Mirrors Python `baskfy_core.sleeves.cap_for_tier`'s default table. */
export const TIER_EQUITY_CAP_PCT: Record<string, number> = {
  R1: 100,
  R2: 70,
  R3: 40,
  R4: 0,
};

export const PROFILE_LABELS: Record<HoldingProfile, string> = {
  CONSERVATIVE: "Spread out",
  BALANCED: "Balanced",
  AGGRESSIVE: "Concentrated",
};

export const PROFILE_BLURBS: Record<HoldingProfile, string> = {
  CONSERVATIVE: "More names, so no single one moves the basket much.",
  BALANCED: "The middle: diversified, still recognisable as a momentum basket.",
  AGGRESSIVE: "Fewer names, larger positions, a bumpier ride.",
};

export function suggestedHoldings(profile: HoldingProfile = DEFAULT_PROFILE): number {
  return SUGGESTED_HOLDINGS[profile];
}

/**
 * How much of the amount the current exposure tier says to leave in cash.
 *
 * When no tier is given, the suggestion is `MIN_CASH_BUFFER_PCT` (5%). The investor can override
 * with any whole percent from `ZERO_CASH_PCT` through `MAX_CASH_PCT` (SB7).
 */
export function suggestedCashPct(tier: string | null | undefined): number {
  return cashPctForTier(tier);
}

/**
 * Resolved cash share: the investor's number when they gave one, else the tier/default suggestion.
 */
export function resolveCashPct({
  exposureTier,
  requested,
}: {
  exposureTier?: string | null | undefined;
  /* `| undefined` explicitly, not just `?`: under `exactOptionalPropertyTypes` an optional
     property and one that may hold `undefined` are different types, and every caller passes the
     key unconditionally with a value that may be absent. */
  requested?: number | null | undefined;
}): number {
  const suggested = suggestedCashPct(exposureTier);
  if (requested === null || requested === undefined || !Number.isFinite(requested)) {
    return suggested;
  }
  return Math.min(MAX_CASH_PCT, Math.max(ZERO_CASH_PCT, Math.trunc(requested)));
}

/**
 * How much of the amount the current exposure tier says to leave in cash.
 *
 * The complement of the desk's equity cap, floored at `MIN_CASH_BUFFER_PCT` when a tier applies
 * and capped at `MAX_CASH_PCT`. With no tier the suggestion is the buffer default.
 */
export function cashPctForTier(tier: string | null | undefined): number {
  if (!tier) return MIN_CASH_BUFFER_PCT;
  const cap = TIER_EQUITY_CAP_PCT[tier.toUpperCase()];
  if (cap === undefined) return MIN_CASH_BUFFER_PCT;
  return Math.min(MAX_CASH_PCT, Math.max(MIN_CASH_BUFFER_PCT, 100 - cap));
}

/**
 * How many names to hold: the investor's number when they gave one, else the profile's.
 *
 * A *suggestion* is trimmed to what the screen found; an explicit request is clamped only to the
 * permitted range. The server refuses an explicit count it cannot fill — this is the preview, so
 * it shows the closest thing it can rather than an error mid-keystroke.
 */
export function resolveHoldings({
  available,
  profile = DEFAULT_PROFILE,
  requested,
}: {
  available: number;
  profile?: HoldingProfile;
  requested?: number | null;
}): number {
  const ceiling = Math.min(MAX_HOLDINGS, Math.max(MIN_HOLDINGS, available));
  if (requested === null || requested === undefined || !Number.isFinite(requested)) {
    return Math.min(suggestedHoldings(profile), ceiling);
  }
  return Math.min(Math.max(Math.trunc(requested), MIN_HOLDINGS), ceiling);
}
