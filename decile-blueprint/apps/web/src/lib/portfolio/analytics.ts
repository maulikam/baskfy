import {
  addDecimalStrings,
  compareDecimalStrings,
  parseDecimal,
  toDecimalString,
} from "@/lib/portfolios/decimal";

import type { PortfolioRow } from "@/lib/portfolio/overview";
import type { Unallocated } from "@/lib/portfolio/organize";

/**
 * The arithmetic behind `/portfolio/portfolios` — weights, concentration and today's movers.
 *
 * Maulik, 11 Sep 2026: *"this one needs more analytical stats and dashboard like feel where one
 * can analyse and view and understand the metrics ... more numbers and fun looking"*. The numbers
 * are here; the page is thin over them, which is the same division `allocation_ledger` uses on the
 * server and for the same reason — every figure below is checkable against a fixture rather than
 * against a screenshot.
 *
 * WHY THIS IS NOT THE OVERVIEW PAGE'S JOB
 * ---------------------------------------
 * `/portfolio` already draws the hero, the NAV chart and the per-portfolio table: what each
 * portfolio is worth and what it did. What it does not answer is the *shape* of the book — how
 * much of your net worth sits in one place, whether four portfolios are four real bets or one bet
 * wearing four names, and which of them actually moved the number today. Those are comparisons
 * between portfolios rather than facts about any one of them, and they are what this module
 * computes.
 *
 * MONEY IS NEVER A FLOAT (house rule 9)
 * -------------------------------------
 * Every figure is a decimal string in and a decimal string out, with scaled-integer arithmetic in
 * between. A weight is a ratio of two rupee amounts and rounding it through a float would put a
 * paisa of drift into a column whose whole purpose is to add to 100%.
 *
 * ABSENT IS NOT ZERO
 * ------------------
 * A portfolio the market data cannot price has no value, and `null` says so. Treating it as zero
 * would shrink the denominator, inflate every other portfolio's weight, and still add to 100% —
 * the failure that looks most like success. Weights are computed over the priced rows only, and
 * the caller is told how many were left out.
 */

/** Two decimals: a weight is read, not calculated with, and 12.34% is as fine as anyone needs. */
const PERCENT_SCALE = 2;

/** Where "one big bet" ends and "a real spread" begins, as a share of net worth. */
export const CONCENTRATED_ABOVE_PCT = 50;
export const BALANCED_ABOVE_PCT = 25;

export interface AllocationSlice {
  portfolioId: number;
  name: string;
  /** Market value, or `null` when the row could not be priced. */
  value: string | null;
  /** Share of the priced total, two decimals. `null` when there is no total to share. */
  weightPct: string | null;
  /** Today's move in rupees, `null` when unknown. */
  todaysPnl: string | null;
  holdingsCount: number;
  cash: string;
  /** The labelled headline return, carried through so the table can print its label (§5.2). */
  returnLabel: string | null;
  /** The headline return as a PERCENTAGE, two decimals — not the fraction the API stores. */
  returnPct: string | null;
}

export type Spread = "concentrated" | "balanced" | "spread";

export interface AllocationAnalytics {
  slices: readonly AllocationSlice[];
  /** Sum of the priced slices. `null` when nothing could be priced. */
  allocatedValue: string | null;
  unallocatedValue: string | null;
  /** Allocated + unallocated — what the weights below are shares OF. */
  totalValue: string | null;
  /** Unallocated as a share of the total: §6.6's progress bar, in one number. */
  unallocatedPct: string | null;
  /** The largest single portfolio's share, and the top three together. */
  topShare: string | null;
  topThreeShare: string | null;
  /** A word for `topShare`, so the page does not have to re-derive the thresholds. */
  spread: Spread | null;
  /** Portfolios that moved most in each direction today, priced ones only. */
  bestToday: AllocationSlice | null;
  worstToday: AllocationSlice | null;
  /** Today's move across every capital portfolio, added exactly. */
  todaysTotal: string | null;
  portfolioCount: number;
  holdingsCount: number;
  /** Rows the market data could not price. They are excluded from every figure above. */
  unpricedCount: number;
}

/**
 * `part / whole * 100`, exact, to two decimals — or `null` when the question has no answer.
 *
 * Scaled-integer throughout: both sides are brought to a common scale as bigints, multiplied by
 * 100 and by the rounding factor, then divided once. A float here would be invisible on one row
 * and visible in a column that has to total 100.
 */
export function percentOf(part: string | null, whole: string | null): string | null {
  if (part === null || whole === null) return null;
  const numerator = parseDecimal(part);
  const denominator = parseDecimal(whole);
  if (numerator === null || denominator === null || denominator.units === 0n) return null;

  const scale = Math.max(numerator.scale, denominator.scale);
  const top = numerator.units * 10n ** BigInt(scale - numerator.scale);
  const bottom = denominator.units * 10n ** BigInt(scale - denominator.scale);
  if (bottom === 0n) return null;

  const factor = 10n ** BigInt(PERCENT_SCALE);
  // Half-up on the last digit kept, done on integers so it cannot drift.
  const doubled = top * 100n * factor * 2n;
  const rounded = (doubled / bottom + (doubled < 0n ? -1n : 1n)) / 2n;
  return toDecimalString({ units: rounded, scale: PERCENT_SCALE });
}

/** The word for a share of net worth. Thresholds live here so the page cannot disagree. */
export function spreadFor(topSharePct: string | null): Spread | null {
  if (topSharePct === null) return null;
  if (compareDecimalStrings(topSharePct, String(CONCENTRATED_ABOVE_PCT)) > 0) return "concentrated";
  if (compareDecimalStrings(topSharePct, String(BALANCED_ABOVE_PCT)) > 0) return "balanced";
  return "spread";
}

/**
 * Everything `/portfolio/portfolios` draws, from the overview payload it already fetches.
 *
 * `rows` are the CAPITAL portfolios — monitoring views are excluded by the caller, because §4.1
 * says a lens enters no total and a weight is a share of a total. Broker piles are already absent
 * from that list (0038): their shares are Unallocated, and they appear here as exactly that.
 */
export function allocationAnalytics(
  rows: readonly PortfolioRow[],
  unallocated: Unallocated | null,
): AllocationAnalytics {
  const slices: AllocationSlice[] = rows.map((row) => ({
    portfolioId: row.portfolio_id,
    name: row.name,
    value: row.value ?? null,
    weightPct: null,
    todaysPnl: row.todays_pnl?.amount ?? null,
    holdingsCount: row.holdings_count ?? 0,
    cash: row.cash ?? "0",
    returnLabel: row.headline_return?.label ?? null,
    /* A PERCENTAGE, like every other `*Pct` on this slice — `headline_return.value` arrives as a
       stored FRACTION (`0.125000` for 12.5%) and it used to be carried through unconverted. The
       comparison table then printed it verbatim with a `%` appended, so every portfolio appeared
       to have returned a tenth of what it did. One field on this interface meaning something
       different from its three neighbours is how that survived review. `percentOf(x, "1")` is the
       decimal-safe conversion: `0.0199 * 100` is `1.9900000000000002` in a float. */
    returnPct: percentOf(row.headline_return?.value ?? null, "1"),
  }));

  const priced = slices.filter((slice) => slice.value !== null);
  const allocatedValue = priced.length > 0 ? addDecimalStrings(priced.map((s) => s.value)) : null;
  // `holdings_value`, not a total that includes cash: a weight is a share of what the SHARES are
  // worth, and folding idle rupees into the denominator would shrink every portfolio's weight for
  // a reason that has nothing to do with the portfolios.
  const unallocatedValue = unallocated?.holdings_value ?? null;
  const totalValue =
    allocatedValue === null && unallocatedValue === null
      ? null
      : addDecimalStrings([allocatedValue, unallocatedValue]);

  for (const slice of slices) slice.weightPct = percentOf(slice.value, totalValue);

  // Biggest first, and the unpriced last rather than interleaved: a row with no figure has no
  // place in an ordering by figure, and putting it at the top would read as "the largest".
  const ordered = [...slices].sort((left, right) => {
    if (left.value === null && right.value === null) return left.name.localeCompare(right.name);
    if (left.value === null) return 1;
    if (right.value === null) return -1;
    const byValue = compareDecimalStrings(right.value, left.value);
    return byValue !== 0 ? byValue : left.name.localeCompare(right.name);
  });

  const topShare = ordered[0]?.weightPct ?? null;
  const topThree = ordered.slice(0, 3).filter((slice) => slice.value !== null);
  const topThreeShare =
    topThree.length > 0 ? percentOf(addDecimalStrings(topThree.map((s) => s.value)), totalValue) : null;

  const movers = ordered.filter((slice) => slice.todaysPnl !== null);
  const byMove = [...movers].sort((left, right) =>
    compareDecimalStrings(right.todaysPnl ?? "0", left.todaysPnl ?? "0"),
  );

  return {
    slices: ordered,
    allocatedValue,
    unallocatedValue,
    totalValue,
    unallocatedPct: percentOf(unallocatedValue, totalValue),
    topShare,
    topThreeShare,
    spread: spreadFor(topShare),
    // Only when they are different portfolios and at least one actually moved: naming the same
    // row as both the best and the worst is a statement about a book of one, not about a day.
    bestToday: byMove.length > 1 ? (byMove[0] ?? null) : null,
    worstToday: byMove.length > 1 ? (byMove[byMove.length - 1] ?? null) : null,
    todaysTotal: movers.length > 0 ? addDecimalStrings(movers.map((s) => s.todaysPnl)) : null,
    portfolioCount: slices.length,
    holdingsCount: slices.reduce((sum, slice) => sum + slice.holdingsCount, 0),
    unpricedCount: slices.length - priced.length,
  };
}
