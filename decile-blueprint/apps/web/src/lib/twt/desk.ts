import type { TwtOpenPosition } from "@/lib/twt/fetch";
import { REASONS } from "@/lib/twt/copy";
import {
  distanceToTriggerPct,
  figure,
  noFigure,
  percent,
  quantity as formatQuantity,
  rupees,
  type Figure,
} from "@/lib/twt/numbers";
import { positionView, type PositionView } from "@/lib/twt/view";

/**
 * The operator view of one session's plan — `docs/twt/05` §2.
 *
 * WHAT THIS MODULE IS, AND WHAT IT IS NOT
 * ---------------------------------------
 * It is the **shape** of the desk page: which sections appear, in which order, what each line
 * says, when the confirm control exists and when it must not. It is pure — a plan and a clock in,
 * a rendered description out — so every rule below is testable without a browser and without a
 * broker.
 *
 * It is **not a way to place an order, and the tree it lives in cannot become one.** `05` §2
 * says the desk console is the only place a TWT line becomes an order, and the product's first
 * non-negotiable says an order fires only from a confirmed plan through the gateway. Nothing here
 * calls anything: the confirm control is a slot the host supplies, and the web app never supplies
 * one. `src/app/(app)/twt/__tests__/read-only.test.tsx` asserts that over this file too.
 *
 * THE TWO RULES OF THE PAGE THAT ARE ACTUALLY RULES
 * ------------------------------------------------
 * **Exits come first.** `05` §2 puts them above the entries for the reason the swing desk does: a
 * morning that runs out of attention should have armed the stops, not taken a new position.
 *
 * **An expired plan's controls are absent, not disabled.** A greyed-out Confirm reads as "try
 * again" — it invites a reload, and a reload of an expired plan is a person hunting for a way to
 * send it anyway. A section that simply is not there, under a banner saying the plan has expired
 * and must be rebuilt, reads as the only true thing: that line cannot be sent.
 */

/** `03` §7's plan-line kinds. `SELL_AT_OPEN` exists in the schema and no TWT rule emits one. */
export type TwtLineKind = "ARM_GTT" | "RAISE_GTT_STOP" | "BUY_AT_OPEN" | "SELL_AT_OPEN";

export type TwtLineState =
  | "PROPOSED"
  | "CONFIRMED"
  | "SENT"
  | "FILLED"
  | "REJECTED"
  | "EXPIRED"
  | "SKIPPED";

export interface TwtPlanLine {
  id: number;
  kind: TwtLineKind;
  state: TwtLineState;
  instrument_id: number;
  symbol: string;
  name: string;
  quantity: string | null;
  value_inr: string | null;
  /** The trigger this line would set, or the stop the fill will be given. */
  stop_price: string | null;
  /** For a ratchet: what is resting now, and the high the new trigger was derived from. */
  old_trigger: string | null;
  high_since: string | null;
  last_price: string | null;
  /** Which cap bound the size, in words. Null when nothing bound it. */
  cap_note: string | null;
  /** What a same-session tie was broken by, as a rupee turnover. */
  rank_key: string | null;
}

/** `04` §10.1's skip reasons, check-constrained in `03` §7. */
export type TwtSkipReason =
  | "GATE_SHUT"
  | "ALREADY_HELD"
  | "SESSION_CAP"
  | "SLOTS_FULL"
  | "EXPOSURE_FULL"
  | "BELOW_MIN_TRADE_VALUE"
  | "TURNOVER_CAP"
  | "LOCKED_UPPER_CIRCUIT"
  | "NO_SLEEVE_CAPITAL"
  | "NO_BAR"
  | "BELOW_LIQUIDITY_FLOOR"
  | "STOP_NOT_BELOW_ENTRY";

export interface TwtPlanSkip {
  instrument_id: number;
  symbol: string;
  reason: TwtSkipReason;
}

/** `03` §8 — the counts that say whether the session's one new mechanism actually ran. */
export interface TwtSessionCounts {
  date: string;
  states: number;
  signals: number;
  orders_placed: number;
  confirms: number;
  fills: number;
  /** Its own count on purpose: a week of zeroes with the market up is a defect, not a quiet spell. */
  ratchets: number;
  exits: number;
  naked_at_1515: number;
}

export interface TwtDeskPlan {
  plan_id: string;
  built_at: string;
  /** `03` §7: `built_at + 30 min`, and the desk enforces it. This page only tells the truth about it. */
  expires_at: string;
  source: "EVENING" | "MORNING" | "MANUAL";
  gate: "OPEN" | "SHUT";
  /** The desk's own mode. `05` §2: a badge, never a footnote. */
  mode: "DRY_RUN" | "LIVE";
  execution_enabled: boolean;
  session: TwtSessionCounts;
  lines: readonly TwtPlanLine[];
  skips: readonly TwtPlanSkip[];
  positions: readonly TwtOpenPosition[];
  /** `05` §2's 15:15 strip: when the sweep ran, and what it found with nothing resting. */
  sweep: { at: string | null; naked: readonly { position_id: number; symbol: string }[] } | null;
}

/** What each skip reason means to the operator, in words rather than in its stored spelling. */
const SKIP_WORDS: Record<TwtSkipReason, string> = {
  GATE_SHUT: "the gate was shut for this session",
  ALREADY_HELD: "this name is already open",
  SESSION_CAP: "the most entries one session may take was already reached",
  SLOTS_FULL: "every position slot is taken",
  EXPOSURE_FULL: "the money set aside for this strategy is fully committed",
  BELOW_MIN_TRADE_VALUE: "the size that fits would be too small a trade to be worth the costs",
  TURNOVER_CAP: "the size that fits is more than a sensible share of a normal day's trading",
  LOCKED_UPPER_CIRCUIT: "it closed locked at its upper circuit, so there was no price to buy at",
  NO_SLEEVE_CAPITAL: "no money has been set aside for this strategy yet",
  NO_BAR: "this name had no price for the session",
  BELOW_LIQUIDITY_FLOOR: "not enough trades through it on an average day",
  STOP_NOT_BELOW_ENTRY: "the stop would not have sat below the entry, so the trade was refused",
};

export function skipWords(reason: TwtSkipReason): string {
  return SKIP_WORDS[reason];
}

/** What each line kind is called on the page — the action, not the stored tag. */
const KIND_WORDS: Record<TwtLineKind, string> = {
  ARM_GTT: "Place the stop",
  RAISE_GTT_STOP: "Raise the stop",
  BUY_AT_OPEN: "Buy at the open",
  SELL_AT_OPEN: "Sell at the open, asked for by hand",
};

export interface DeskLineView {
  readonly id: number;
  readonly kind: TwtLineKind;
  readonly action: string;
  readonly symbol: string;
  readonly name: string;
  readonly state: TwtLineState;
  readonly quantity: Figure;
  readonly value: Figure;
  readonly stop: Figure;
  readonly oldTrigger: Figure;
  readonly highSince: Figure;
  readonly lastPrice: Figure;
  readonly distanceToTrigger: Figure;
  readonly capNote: string | null;
  readonly rankKey: Figure;
  /**
   * Whether this line may be confirmed at all.
   *
   * False on an expired plan and false on a line the desk has already sent, and in both cases the
   * control is not rendered — see the class comment. A line that has been sent already carries
   * its own state as words, which is a different sentence from "this plan has expired".
   */
  readonly confirmable: boolean;
}

function lineView(line: TwtPlanLine, expired: boolean): DeskLineView {
  const distance = distanceToTriggerPct(line.last_price, line.stop_price);
  return {
    id: line.id,
    kind: line.kind,
    action: KIND_WORDS[line.kind],
    symbol: line.symbol,
    name: line.name,
    state: line.state,
    quantity: figure(
      line.quantity === null ? null : formatQuantity(line.quantity),
      REASONS.notComputed,
    ),
    value: figure(line.value_inr === null ? null : rupees(line.value_inr, 0), REASONS.notComputed),
    stop: figure(line.stop_price === null ? null : rupees(line.stop_price), REASONS.notComputed),
    oldTrigger:
      line.old_trigger === null
        ? noFigure(REASONS.noRestingStop)
        : figure(rupees(line.old_trigger), REASONS.noRestingStop),
    highSince: figure(
      line.high_since === null ? null : rupees(line.high_since),
      REASONS.notComputed,
    ),
    lastPrice: figure(line.last_price === null ? null : rupees(line.last_price), REASONS.noQuote),
    distanceToTrigger: figure(
      distance === null ? null : percent(distance, 1),
      line.stop_price === null ? REASONS.noRestingStop : REASONS.noQuote,
    ),
    capNote: line.cap_note,
    rankKey: figure(line.rank_key === null ? null : rupees(line.rank_key, 0), REASONS.notComputed),
    confirmable: !expired && line.state === "PROPOSED",
  };
}

export interface DeskSection {
  readonly id: "exits" | "entries";
  readonly heading: string;
  readonly lines: readonly DeskLineView[];
  /** Said when the section is empty, so an empty section is never read as a missing one. */
  readonly emptyReason: string;
}

export interface DeskSkipView {
  readonly symbol: string;
  readonly reason: string;
}

export interface DeskView {
  readonly planReference: string;
  readonly mode: "DRY_RUN" | "LIVE";
  readonly gate: "OPEN" | "SHUT";
  readonly session: TwtSessionCounts;
  readonly executionEnabled: boolean;
  /** True once the plan is older than its expiry. Every confirm control is then absent. */
  readonly expired: boolean;
  /** Whole minutes left, floored, and never negative. Zero means "this minute". */
  readonly minutesLeft: number;
  readonly sections: readonly DeskSection[];
  readonly positions: readonly PositionView[];
  readonly skips: readonly DeskSkipView[];
  /** `05` §2's red band: every open line with nothing resting at 15:15. */
  readonly sweepNaked: readonly string[];
  readonly sweepAt: string | null;
}

const MINUTE_MS = 60_000;

/**
 * `05` §2's page, in order: the strip, the exits, the entries, the open positions, the confirms.
 *
 * `now` is a parameter rather than a call to `Date.now()` inside so that expiry is a fact the
 * caller states and a test can state differently. A clock read inside a render is a rule nobody
 * can write a failing test for, and expiry is the rule on this page most worth a failing test.
 */
export function deskView(plan: TwtDeskPlan, now: Date): DeskView {
  const expiresAt = Date.parse(plan.expires_at);
  /* An unparseable expiry is treated as expired. The alternative — treating it as live — makes a
     malformed timestamp the one way to get a confirm control onto a plan nobody vouched for. */
  const expired = Number.isNaN(expiresAt) || now.getTime() >= expiresAt;
  const minutesLeft = expired
    ? 0
    : Math.floor((expiresAt - now.getTime()) / MINUTE_MS);

  const of = (kind: TwtLineKind): DeskLineView[] =>
    plan.lines.filter((line) => line.kind === kind).map((line) => lineView(line, expired));

  /* Arming a stop that is missing outranks raising one that already exists: a line with nothing
     resting is unprotected right now, a line whose stop is merely low is protected at yesterday's
     level. Both are exits, and both come before any buying. */
  const exits = [...of("ARM_GTT"), ...of("RAISE_GTT_STOP")];
  const entries = [...of("BUY_AT_OPEN"), ...of("SELL_AT_OPEN")];

  return {
    planReference: plan.plan_id.slice(0, 8),
    mode: plan.mode,
    gate: plan.gate,
    session: plan.session,
    executionEnabled: plan.execution_enabled,
    expired,
    minutesLeft,
    sections: [
      {
        id: "exits",
        heading: "Stops first",
        lines: exits,
        emptyReason:
          "Every open position already has the right stop resting at the exchange for this session.",
      },
      {
        id: "entries",
        heading: "Then entries",
        lines: entries,
        emptyReason:
          plan.gate === "SHUT"
            ? "The gate is shut for this session, so no entry was planned."
            : "No name signalled an entry for this session.",
      },
    ],
    positions: plan.positions.map(positionView),
    skips: plan.skips.map((skip) => ({ symbol: skip.symbol, reason: skipWords(skip.reason) })),
    sweepNaked: (plan.sweep?.naked ?? []).map((row) => row.symbol),
    sweepAt: plan.sweep?.at ?? null,
  };
}
