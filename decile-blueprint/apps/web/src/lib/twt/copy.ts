import type { TwtGate, TwtHalfSize } from "@/lib/twt/fetch";

import { count, percent } from "@/lib/twt/numbers";

/**
 * Every sentence `/twt` says, in one file — `docs/twt/05` §1's `copy.ts`.
 *
 * It sits in `lib` rather than beside the page because `05` §1's hub and `05` §2's operator view
 * say versions of the same thing about the same gate, and a copy module only one of the two
 * surfaces can import is exactly the drift a copy module exists to prevent. DECISIONS-TW TW8.5.
 *
 * WHAT MAY NOT APPEAR IN ANY STRING BELOW
 * ---------------------------------------
 * A route, a column, a file, a host, an alert name, or any other word that belongs to the inside
 * of the system. On 11 Sep 2026 a screenshot of a customer-facing page carried a live loopback
 * address and three column names, and six tests were pinning that defect. The engineering detail
 * is not deleted — it moves into the comment next to the string, where the person who can act on
 * it is actually reading. `src/components/twt/__tests__/no-internals.test.tsx` reads the rendered
 * page as text and fails on anything that got through.
 */

/**
 * `05` §1.2, verbatim, and the single most likely support question about this strategy.
 *
 * Chartink's backtester evaluates weekly candles as *completed* candles, so its own export of
 * this screen knows Friday's close on Monday (`01` §2). A live 15:30 run cannot, and therefore
 * names fewer stocks on some days. Without this sentence on the page the difference reads as a
 * bug in the screen rather than as the only honest reading of a half-finished week.
 */
export function pointInTimeLine(session: string): string {
  return (
    `Computed point-in-time from the close of ${session}. Chartink's own backtest export uses ` +
    "the week's final close on every day of that week, so it names some stocks this screen does " +
    "not — see the method note."
  );
}

/**
 * What the gate means for what happens next.
 *
 * The SHUT sentence matters more than the OPEN one, and `05` §1.1 says so: a reader must never
 * have to infer that the exits keep running. "SHUT" must not read as "sell everything".
 */
export function gateMeaning(gate: "OPEN" | "SHUT"): string {
  if (gate === "OPEN")
    return "new entries may be planned, and everything already open is managed as always";
  return (
    "no new entries. Everything already open is managed exactly as always — the stops stay " +
    "where they are and the exits still fire"
  );
}

/** "62.4% of 1,412 names are above their 200-day average · the gate opens above 40%". */
export function breadthLine(gate: TwtGate): string {
  if (gate.pct_above_dma === null || gate.measured_count === null) {
    return `the gate opens above ${percent(gate.threshold_pct, 0)} of the market`;
  }
  return (
    `${percent(gate.pct_above_dma, 1)} of ${count(gate.measured_count)} names are above their ` +
    `200-day average · the gate opens above ${percent(gate.threshold_pct, 0)}`
  );
}

/**
 * `05` §1.1's funnel, as four steps a reader can follow: universe → with a bar → with a 200-day
 * average → above it. It lives inside a disclosure, not on the page, because an explanation that
 * is open by default is how an empty account came to render 4,661 pixels on a phone.
 */
export interface FunnelStep {
  readonly label: string;
  readonly value: string;
}

export function funnelSteps(gate: TwtGate): readonly FunnelStep[] {
  const steps: FunnelStep[] = [];
  const add = (label: string, value: number | null): void => {
    if (value !== null) steps.push({ label, value: count(value) });
  };
  add("Names in the screened market", gate.universe_count);
  add("With a price for this session", gate.with_bar_count);
  add("With a full 200-day average", gate.measured_count);
  add("Trading above that average", gate.above_count);
  return steps;
}

/** Said when the funnel has no steps at all, so an empty list is never mistaken for a quiet day. */
export const NO_FUNNEL =
  "No reading has been taken for this session, so the counts below are not a statement about " +
  "the market.";

/** `05` §1.1: a session dropped from the rolling calendar is recorded, and the gate is shut. */
export const THIN_SESSION =
  "Too little of the market traded on this session for the reading to mean anything, so the gate " +
  "is shut for it and the percentages are not published.";

/**
 * `05` §1.4 — `02` §3.6's discipline, on the page rather than behind a settings screen.
 *
 * When execution is off the line says so instead of counting down, because a counter that ticks
 * while nothing can trade is a number that describes an imaginary event.
 */
export function halfSizeLine(halfSize: TwtHalfSize): string {
  if (!halfSize.execution_enabled) {
    return (
      `Trading is switched off for this strategy, so none of the first ${count(halfSize.entries_total)} ` +
      "entries has been taken. Each of them will be planned at half the usual size when it is switched on."
    );
  }
  return (
    `${count(halfSize.entries_left)} of ${count(halfSize.entries_total)} first live entries ` +
    "remaining at half size."
  );
}

/** The two words a row's entry badge may say, and what they mean to a reader rather than to a job. */
export const ENTRY_TODAY = "Entry today";
export const WATCH_ONLY = "Watch only";

/**
 * Why a name that met every rule is not an entry.
 *
 * `03` §3 stores this as a rejected event with its failed filter; the reader needs the *reason*,
 * not the tag. A screen that hides what it passed over cannot be audited by the person whose
 * money it is (`05` §1.2), and a tag nobody outside the code can read hides it just as well.
 */
export function watchOnlyReason(failedFilters: readonly string[]): string {
  if (failedFilters.includes("TURNOVER"))
    return "not enough trades through it on an average day to buy and sell without moving the price";
  return "it did not clear one of the entry rules";
}

/** The reasons a figure on this page can be missing, in a reader's words. Never a dash. */
export const REASONS = {
  /* The live quote, not a stored bar: `05` §1.3 marks the open positions live, and a quote is
     simply absent for a name that has not traded. Absent is not zero, and a zero here would be a
     number somebody could act on. */
  noQuote: "No live price for this name yet",
  /* `03` §5: `gtt_id` null while shares are open. The distance has nothing to measure against,
     and a plausible zero would read as "the stop is right at the price". */
  noRestingStop: "No stop resting at the exchange",
  /* Before the nightly job has written a session there is no row at all — which is not the same
     fact as a row whose value is null, and the two need opposite responses. */
  notComputed: "Not computed for this session",
  /* `03` §9: the page shows the latest FINISHED run per source. A run that never finished, or one
     nobody has started, has no statistics — and inventing one would be inventing a result. */
  noFinishedRun: "No completed run yet",
} as const;
