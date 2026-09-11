import type {
  ExitReason,
  RebalanceNameOut,
  RebalanceOut,
  TargetWeightOut,
} from "@baskfy/api-client";

import { percentOf } from "@/lib/portfolio/analytics";
import { metric, type Metric } from "@/lib/portfolio/command-center";
import type { DetailHolding, PortfolioDetail } from "@/lib/portfolio/overview";
import {
  addDecimalStrings,
  compareDecimalStrings,
  parseDecimal,
  toDecimalString,
} from "@/lib/portfolios/decimal";

/**
 * The rebalance preview — everything "Review rebalance" shows, and everything it refuses to show.
 *
 * The brief (11 Sep 2026) asks this drawer for current versus target weights, quantity changes,
 * buy and sell values, cash required or released, turnover, estimated brokerage and taxes,
 * tax-lot considerations, exposure and risk before and after, orders grouped by broker, and
 * liquidity warnings. Roughly half of that list has no source anywhere in Baskfy.
 *
 * WHAT `RebalanceOut` ACTUALLY CARRIES
 * -----------------------------------
 * Four lists of names — entries, exits, inside-window, holds — plus `target_weights`, which
 * `baskfy_core.rank_buffer._target_weights` computes as an equal weight over the post-rebalance
 * portfolio, summing to exactly 1. And that is all. There is no quantity in the response, no cash
 * figure, no turnover and no cost. `RebalanceOrderOut.planned_qty` exists in the schema but it
 * belongs to the *desk's* weekly plan, a different object served by a different route.
 *
 * WHY NOTHING HERE DERIVES ONE
 * ----------------------------
 * `weight x portfolio value / last price` is four lines of arithmetic and it would be wrong in a
 * way that does not look wrong. It assumes the last close is the fill, assumes the whole book is
 * available to redeploy, assumes no charges and no lot constraints — and then prints "SELL 214"
 * beside a symbol. A quantity on a screen is read as an instruction, and this product places real
 * orders with real money on the live box. So every figure that needs a quantity is named here as
 * something the execution step produces, with the reason, and is never computed.
 *
 * `docs/PORTFOLIO-COMMAND-CENTER.md` §6.3 is the decision; §2.2 is the wider survey.
 *
 * WHAT IS REAL, AND IS THEREFORE SHOWN IN FULL
 * --------------------------------------------
 * Weights are real on both sides: `DetailHoldingOut.weight` is a holding's value over the
 * portfolio's, and `TargetWeightOut.weight` is the screen's equal weight. Both are fractions
 * (`baskfy_api.portfolio_overview` quantizes to `RETURN_PRECISION`; `rank_buffer` to
 * `WEIGHT_STEP`), so the comparison is a comparison of like with like and needs no price at all.
 * Concentration follows from weights, so the before-and-after the brief wants IS computable for
 * the part of "risk" that is arithmetic over weights — and is not, for the part that needs a
 * covariance matrix nobody has built.
 *
 * MONEY IS NEVER A FLOAT, AND A WEIGHT IS MONEY'S SHADOW
 * ------------------------------------------------------
 * House rule 9. Every figure below is a decimal string in and a decimal string out, with
 * scaled-integer arithmetic in between, exactly as `lib/portfolio/analytics` does it. A column of
 * weights that has to add to 100% cannot be summed through `Number`.
 */

/** The one place a fraction becomes a percentage, at two decimals. */
function toPercent(fraction: string | null | undefined): string | null {
  if (fraction === null || fraction === undefined) return null;
  return percentOf(fraction, "1");
}

function negate(value: string): string | null {
  const parsed = parseDecimal(value);
  if (parsed === null) return null;
  return toDecimalString({ units: -parsed.units, scale: parsed.scale });
}

/** `left - right`, exactly, or `null` when either side has no figure. */
function subtract(left: string | null, right: string | null): string | null {
  if (left === null || right === null) return null;
  const flipped = negate(right);
  if (flipped === null) return null;
  return addDecimalStrings([left, flipped]);
}

function descending(weights: readonly string[]): string[] {
  return [...weights].sort((a, b) => compareDecimalStrings(b, a));
}

function sumOfTop(weights: readonly string[], count: number): string | null {
  const top = descending(weights).slice(0, count);
  return top.length === 0 ? null : addDecimalStrings(top);
}

/* ------------------------------------------------------------------ the inputs */

/** A screen the portfolio can be diffed against. `useScreens()` already returns this shape. */
export interface ScreenRef {
  readonly public_id: string;
  readonly name: string;
}

export interface PreviewInput {
  readonly portfolioName: string;
  /** The computed proposal, or `null` before the first run. */
  readonly rebalance: RebalanceOut | null;
  /** `GET /portfolio/{id}` — holdings with weights, and the summary's freshness fields. */
  readonly detail: PortfolioDetail | null;
  /** Every screen the account owns. None means the diff has nothing to diff against. */
  readonly screens: readonly ScreenRef[];
  /** Instrument ids the reader has taken out of the plan. Reversible; see {@link ComparisonRow}. */
  readonly excluded: ReadonlySet<number>;
}

/* ------------------------------------------------------------------ the shapes */

export type PreviewSide = "entry" | "exit" | "inside-window" | "hold";

export interface ComparisonRow {
  readonly instrumentId: number;
  readonly symbol: string;
  readonly name: string;
  readonly side: PreviewSide;
  readonly rank: number | null;
  /** Set on exits only, exactly as `rank_buffer.ExitReason` defines it. */
  readonly exitReason: ExitReason | null;
  /** Weight of the portfolio today. A percentage, or the reason there is none. */
  readonly current: Metric;
  /** Weight of the portfolio the screen is aiming at. */
  readonly target: Metric;
  /** Target minus current, in points of the portfolio. */
  readonly delta: Metric;
  /** Why the screen put this name on this list — the rule, restated, never a recommendation. */
  readonly ruleLine: string;
  /** Broker accounts that hold it today. Empty for a name not held. */
  readonly brokers: readonly string[];
  /**
   * Only an entry or an exit can be excluded: they are the two lists that propose an action.
   * A hold proposes nothing, so there is nothing to take out of the plan.
   */
  readonly excludable: boolean;
  readonly excluded: boolean;
  /** The instrument has no price today, so it has no weight and cannot be compared. */
  readonly unpriced: boolean;
  readonly pendingReconciliation: boolean;
}

export interface NameList {
  readonly key: PreviewSide;
  readonly title: string;
  /** The rank-buffer rule for this list, in the vocabulary the tracker already uses. */
  readonly rule: string;
  readonly count: number;
  readonly rows: readonly ComparisonRow[];
  readonly emptyMessage: string;
}

export type WarningLevel = "critical" | "review";

export interface PreviewWarning {
  readonly id: string;
  readonly level: WarningLevel;
  /** Names its subjects. Never "1 issue found". */
  readonly headline: string;
  readonly detail: string;
  /** The symbols, dates or accounts the headline is about. */
  readonly subjects: readonly string[];
}

export type ConcentrationMove = "less-concentrated" | "more-concentrated" | "unchanged" | "unknown";

export interface Impact {
  /** `null` when the holdings payload was never loaded — 0 would be a fabricated count. */
  readonly namesNow: number | null;
  readonly namesAfter: number;
  readonly entering: number;
  readonly exiting: number;
  readonly staying: number;
  readonly largestNow: Metric;
  readonly largestAfter: Metric;
  readonly topFiveNow: Metric;
  readonly topFiveAfter: Metric;
  /** How much of the book the kept target weights cover. 100% until something is excluded. */
  readonly coverage: Metric;
  /** What an exclusion leaves unassigned. Deliberately not re-spread — see {@link buildImpact}. */
  readonly leftUnassigned: Metric;
  readonly concentrationMove: ConcentrationMove;
  /** Current weights are shares of the PRICED book; say so when something is unpriced. */
  readonly pricedOnly: boolean;
}

/** A figure the brief asks for that this preview will not produce, and where it comes from. */
export interface NotProducedHere {
  readonly id: string;
  readonly name: string;
  /** Why this preview cannot honestly produce it. */
  readonly reason: string;
  /** Where the figure does come from — never "coming soon". */
  readonly insteadFrom: string;
}

export type PreviewStatus = "no-screen" | "not-analysed" | "ready";

export interface Blocker {
  readonly headline: string;
  readonly detail: string;
  /** Exactly one next action. A blocked drawer that offers three is a drawer that offers none. */
  readonly action: { readonly label: string; readonly href: string };
}

export interface RebalancePreview {
  readonly status: PreviewStatus;
  readonly portfolioName: string;
  /** Non-null only when `status` is `"no-screen"`. */
  readonly blocker: Blocker | null;
  readonly screenName: string | null;
  readonly topN: number | null;
  readonly holdBuffer: number | null;
  /** `top_n + hold_buffer` — the worst rank a held name may have and still be kept. */
  readonly bandLimit: number | null;
  readonly asOf: string | null;
  readonly requestedAsOf: string | null;
  readonly computedAt: string | null;
  readonly lists: readonly NameList[];
  readonly rows: readonly ComparisonRow[];
  readonly impact: Impact;
  readonly warnings: readonly PreviewWarning[];
  readonly excludedRows: readonly ComparisonRow[];
}

/* ------------------------------------------------------- what is not produced here */

/**
 * The brief's list, minus what exists. Kept as data so the drawer can say it on its own surface.
 *
 * Every entry names where the figure genuinely comes from. "Coming soon" is not an answer a
 * person can act on; "your broker's order window shows it before you commit" is.
 */
export const NOT_PRODUCED_HERE: readonly NotProducedHere[] = [
  {
    id: "quantity",
    name: "Quantity to buy or sell",
    reason:
      "The rebalance answers with names and target weights. A quantity needs a fill price, a lot size and the cash you actually mean to deploy, none of which is in this response — and a number printed beside a symbol reads as an instruction.",
    insteadFrom:
      "Your broker's own order window, where you enter each line and see the quantity priced before you commit.",
  },
  {
    id: "cash",
    name: "Cash required or released, and the value of each buy and sell",
    reason:
      "Every one of them is a quantity multiplied by a price. No quantity, no value — and an exit's proceeds do not arrive until settlement, so even the timing is not this preview's to state.",
    insteadFrom: "Your broker's funds screen, and its contract note once the exits have settled.",
  },
  {
    id: "turnover",
    name: "Turnover",
    reason:
      "Turnover is a share of value changing hands, not a share of names. Counting names would be a different number wearing the same label.",
    insteadFrom:
      "The value side of the plan, which needs quantities. The name-level churn IS shown above: how many enter, exit and stay.",
  },
  {
    id: "costs",
    name: "Brokerage, STT and charges",
    reason:
      "packages/core carries a cost model for the desk's weekly rebalancer. It has never been wired to a portfolio rebalance, and a charge estimated from the wrong model is worse than no estimate.",
    insteadFrom:
      "Your broker's contract note, and the charges preview in its order window. Wiring the existing cost model through this path is the change that would put it here.",
  },
  {
    id: "tax-lots",
    name: "Tax lots and holding period",
    reason:
      "first_bought_on is null for broker-synced holdings until a contract-note import runs, so for most positions Baskfy does not know when the shares were bought.",
    insteadFrom:
      "Your broker's own capital-gains statement. A CAS import would give Baskfy the purchase dates it is missing.",
  },
  {
    id: "exposure-after",
    name: "Equity exposure and regime headroom, after",
    reason:
      "Exposure after depends on the cash a rebalance releases or requires, which depends on quantities and on what actually fills. The regime tier itself is a reading of the market, not something a rebalance moves — what changes is how much of its cap you are using.",
    insteadFrom:
      "The regime panel, once the trades have filled and the holdings have synced; it reads the live cap rather than projecting one.",
  },
  {
    id: "risk-stats",
    name: "Beta, volatility and Value at Risk, before and after",
    reason:
      "No return-series statistics are computed anywhere in Baskfy, and a Value at Risk without a stated model is worse than none.",
    insteadFrom:
      "Nothing yet. A statistics job over the daily NAV series and a benchmark would produce them.",
  },
  {
    id: "sector",
    name: "Sector and market-cap exposure, before and after",
    reason: "Instruments carry no sector or market-cap map, so there is nothing to group by.",
    insteadFrom: "Nothing yet. A sector map on the instrument table, sourced and kept current.",
  },
  {
    id: "liquidity",
    name: "Liquidity and days to liquidate",
    reason: "No traded-volume history is stored per instrument, so there is no average daily volume to measure a position against.",
    insteadFrom: "Nothing yet. A liquidity column on the daily bars.",
  },
  {
    id: "routing",
    name: "Which account an entry belongs in",
    reason:
      "Baskfy knows which account holds a name today, and shows it. It does not choose an account for a name you do not hold yet — that is your decision about where your cash sits.",
    insteadFrom: "You, at the broker you choose.",
  },
];

/* ------------------------------------------------------------------ the workflow */

export type StepKey = "analyse" | "adjust" | "impact" | "confirm" | "plan";

export interface WorkflowStep {
  readonly key: StepKey;
  readonly title: string;
  /** One line saying what this step is for, shown under the step's heading. */
  readonly purpose: string;
}

/**
 * The brief's five steps, in its own order: *"analyse differences → edit or exclude proposed
 * changes → review impact → confirm broker quantities → generate an execution plan."*
 *
 * The fourth is renamed for what it can honestly be. There are no broker quantities to confirm
 * here, because there are no quantities; what the step does is show where each name is held
 * today, and take the reader's acknowledgement that the quantities are theirs to enter. The
 * fifth produces a plan a person carries to their broker. **Neither sends anything anywhere.**
 */
export const WORKFLOW: readonly WorkflowStep[] = [
  {
    key: "analyse",
    title: "Analyse",
    purpose: "Diff what you hold against a screen, with a hold buffer so a near miss does not churn.",
  },
  {
    key: "adjust",
    title: "Adjust",
    purpose: "Take names out of the plan, put them back, and note anything you want to carry with them.",
  },
  {
    key: "impact",
    title: "Impact",
    purpose: "What changes about the shape of this portfolio — and which of the brief's figures Baskfy does not have.",
  },
  {
    key: "confirm",
    title: "Confirm",
    purpose: "Where each name is held today, and whose job the quantities are.",
  },
  {
    key: "plan",
    title: "Plan",
    purpose: "A plan to take to your broker. Baskfy sends nothing.",
  },
];

/**
 * Why a step cannot be opened yet, or `null` when it can.
 *
 * The two real gates: nothing can be reviewed before the diff has run, and the plan does not
 * assemble until the reader has said, in as many words, that the quantities are theirs to enter.
 */
export function stepBlockedReason(
  step: StepKey,
  preview: RebalancePreview,
  acknowledged: boolean,
): string | null {
  if (step === "analyse") return null;
  if (preview.status !== "ready") {
    return "Run the diff first — there is nothing to review until a screen has been compared with what you hold.";
  }
  if (step === "plan" && !acknowledged) {
    return "Acknowledge on the Confirm step that the quantities are yours to enter at your broker.";
  }
  return null;
}

/* ------------------------------------------------------------------ the holdings */

interface HeldName {
  readonly instrumentId: number;
  readonly symbol: string;
  readonly name: string;
  /** Fraction of the portfolio, summed across this instrument's broker lines. */
  readonly weight: string | null;
  readonly brokers: readonly string[];
  readonly unpriced: boolean;
  readonly pendingReconciliation: boolean;
}

/**
 * One row per instrument, from the per-broker lines the detail payload actually sends.
 *
 * `DetailHoldingOut` is a `(instrument, broker account)` position, so a name held at two brokers
 * arrives twice. Their weights add; their broker labels are collected, because "where is this
 * held" is the one part of the brief's "orders grouped by broker" that Baskfy genuinely knows.
 *
 * **A line with no weight makes the whole name unweighted, not zero.** The server leaves `weight`
 * null when the instrument has no price, and it also leaves that position out of the portfolio
 * value — so summing the priced half would report a share of a book that excludes it.
 */
export function heldNames(detail: PortfolioDetail | null): readonly HeldName[] {
  const byInstrument = new Map<number, { lines: DetailHolding[] }>();
  for (const line of detail?.holdings ?? []) {
    const existing = byInstrument.get(line.instrument.instrument_id);
    if (existing) existing.lines.push(line);
    else byInstrument.set(line.instrument.instrument_id, { lines: [line] });
  }

  return [...byInstrument.entries()].map(([instrumentId, { lines }]) => {
    const unpriced = lines.some((line) => line.weight === null || line.weight === undefined);
    const first = lines[0];
    return {
      instrumentId,
      symbol: first?.instrument.symbol ?? String(instrumentId),
      name: first?.instrument.name ?? "",
      weight: unpriced ? null : addDecimalStrings(lines.map((line) => line.weight)),
      brokers: [...new Set(lines.map((line) => line.broker.label))].sort((a, b) => a.localeCompare(b)),
      unpriced,
      pendingReconciliation: lines.some((line) => line.pending_reconciliation),
    };
  });
}

/* ------------------------------------------------------------------ the rows */

const ZERO_WEIGHT = "0";
const NO_HOLDINGS_LOADED = "Holdings not loaded, so there is no weight to compare.";
const UNPRICED = "No price today, so it has no weight.";

const CURRENT_DEFINITION =
  "What this name is worth as a share of the portfolio right now, from the broker's holdings and the latest price we have. A name you do not hold is 0.00% — a measured zero, not a missing figure.";
const TARGET_DEFINITION =
  "The weight this name carries in the portfolio the screen is aiming at — an equal split across the names it keeps. A name the screen exits carries 0.00% of that target. It is a share of the portfolio, never a rupee amount.";
const DELTA_DEFINITION =
  "Target weight minus current weight, in points of the portfolio. It is a change in shape. It is not a quantity, and a quantity cannot be read off it without a price and a cash figure this preview does not have.";

function ruleLineFor(
  side: PreviewSide,
  row: RebalanceNameOut,
  topN: number,
  bandLimit: number,
): string {
  if (side === "entry") return `Enters at rank ${row.rank ?? topN}.`;
  if (side === "exit") {
    if (row.reason === "delisted") return "Delisted — the position cannot be held any longer.";
    if (row.reason === "not_in_screen") return "The screen no longer returns it at all.";
    return `Rank ${row.rank ?? bandLimit + 1}, past the hold band's ${bandLimit}.`;
  }
  if (side === "inside-window") {
    return `Rank ${row.rank ?? topN + 1}, inside the hold band of ${bandLimit}.`;
  }
  return `Rank ${row.rank ?? topN}, inside the screen's top ${topN}.`;
}

/**
 * One row of the comparison.
 *
 * The two zeros here are deliberate and they are not the house rule's forbidden zero. "Absent is
 * not zero" guards against printing an unknown as `0`; a name the screen returned as an *entry*
 * is a name the server has just established you do not hold, and a name absent from an equal
 * weighting that sums to exactly 1 carries exactly none of it. Those are measured zeros. An
 * unpriced holding, by contrast, has an unknown weight and renders its reason.
 */
function buildRow(
  side: PreviewSide,
  source: RebalanceNameOut,
  targets: ReadonlyMap<number, TargetWeightOut>,
  held: ReadonlyMap<number, HeldName>,
  excluded: ReadonlySet<number>,
  topN: number,
  bandLimit: number,
  holdingsLoaded: boolean,
): ComparisonRow {
  const holding = held.get(source.instrument_id);
  const target = targets.get(source.instrument_id);

  /* Both sides stay fractions until the last moment. Rounding each to two decimals first and then
     subtracting puts a hundredth of a point into a column whose whole purpose is to be compared,
     and rounding before summing is what makes a coverage figure read 100.02%. */
  const currentWeight = side === "entry" ? ZERO_WEIGHT : (holding?.weight ?? null);
  /* An exit leaves the book, so its share of the target is zero — the target set is an equal
     weight over the names that stay, and this is not one of them. */
  const targetWeight = side === "exit" ? ZERO_WEIGHT : (target?.weight ?? null);

  const currentReason = !holdingsLoaded ? NO_HOLDINGS_LOADED : UNPRICED;
  const targetReason =
    side === "exit" ? "The screen exits it." : "The screen returned no target weight for it.";

  const current = metric(
    "Current weight",
    CURRENT_DEFINITION,
    toPercent(currentWeight),
    currentReason,
  );
  const targetMetric = metric("Target weight", TARGET_DEFINITION, toPercent(targetWeight), targetReason);
  const delta = metric(
    "Change",
    DELTA_DEFINITION,
    toPercent(subtract(targetWeight, currentWeight)),
    holding?.unpriced === true
      ? "No price today, so the change cannot be measured."
      : "One side of the comparison has no weight.",
  );

  return {
    instrumentId: source.instrument_id,
    symbol: source.symbol,
    name: source.name,
    side,
    rank: source.rank ?? null,
    exitReason: source.reason ?? null,
    current,
    target: targetMetric,
    delta,
    ruleLine: ruleLineFor(side, source, topN, bandLimit),
    brokers: holding?.brokers ?? [],
    excludable: side === "entry" || side === "exit",
    excluded: excluded.has(source.instrument_id),
    unpriced: holding?.unpriced ?? false,
    pendingReconciliation: holding?.pendingReconciliation ?? false,
  };
}

/* ------------------------------------------------------------------ the impact */

const NO_WEIGHTS_YET = "No weighted holding to measure.";

/**
 * Concentration before and after, and what an exclusion does to the target.
 *
 * **An exclusion is never re-spread.** Taking an entry out of the plan leaves its slice
 * unassigned; the remaining names keep the weights the server computed. Re-normalising the rest
 * to add back to 100% would be this preview inventing an allocation the screen never proposed,
 * which is the same class of mistake as inventing a quantity. The honest presentation is a
 * coverage figure and the unassigned remainder, and the honest fix is to re-run the diff with a
 * smaller top N.
 *
 * **Keeping a name the screen exits makes the after-weights indeterminate**, and says so. The
 * target set does not contain that name, so the book after would hold it *plus* a set that
 * already adds to 100%. There is no non-invented answer to "what is the largest weight then",
 * and a plausible one would be a fabricated risk figure in a product that places live orders.
 */
function buildImpact(
  rows: readonly ComparisonRow[],
  held: readonly HeldName[],
  targets: readonly TargetWeightOut[],
  excluded: ReadonlySet<number>,
  holdingsLoaded: boolean,
): Impact {
  /* A book nobody fetched has an unknown number of names, not zero — and every "now" figure below
     is unknown with it. Reporting 0 alongside a target of 6 would read as "you hold nothing". */
  const noCurrentSide = holdingsLoaded ? NO_WEIGHTS_YET : NO_HOLDINGS_LOADED;
  /* Fractions throughout; the conversion to a percentage happens once, at the end. Summing
     two-decimal percentages instead would make six equal weights of 16.6666 add to 100.02. */
  const currentWeights = held
    .map((name) => name.weight)
    .filter((weight): weight is string => weight !== null);

  const keptTargets = targets.filter((target) => !excluded.has(target.instrument_id));
  const keptTargetWeights = keptTargets.map((target) => target.weight);

  const keptExits = rows.filter((row) => row.side === "exit" && row.excluded);
  const indeterminate = keptExits.length > 0;
  const keptExitReason = `${keptExits.map((row) => row.symbol).join(", ")} ${
    keptExits.length === 1 ? "is" : "are"
  } kept but not in the screen's target, so the weights after would no longer add to 100%.`;

  const largestNowValue = currentWeights.length > 0 ? (descending(currentWeights)[0] ?? null) : null;
  const largestAfterValue = indeterminate
    ? null
    : keptTargetWeights.length > 0
      ? (descending(keptTargetWeights)[0] ?? null)
      : null;

  const largestNow = metric(
    "Largest name now",
    "The biggest single holding as a share of the portfolio today.",
    toPercent(largestNowValue),
    noCurrentSide,
  );
  const largestAfter = metric(
    "Largest name after",
    "The biggest weight in the target the screen proposes, after your exclusions.",
    toPercent(largestAfterValue),
    indeterminate ? keptExitReason : "The screen's target is empty.",
  );

  const coverageValue = keptTargetWeights.length > 0 ? addDecimalStrings(keptTargetWeights) : null;
  const unassignedValue = coverageValue === null ? null : subtract("1", coverageValue);

  const move: ConcentrationMove =
    largestNowValue === null || largestAfterValue === null
      ? "unknown"
      : compareDecimalStrings(largestAfterValue, largestNowValue) < 0
        ? "less-concentrated"
        : compareDecimalStrings(largestAfterValue, largestNowValue) > 0
          ? "more-concentrated"
          : "unchanged";

  return {
    namesNow: holdingsLoaded ? held.length : null,
    namesAfter: keptTargets.length + keptExits.length,
    entering: rows.filter((row) => row.side === "entry" && !row.excluded).length,
    exiting: rows.filter((row) => row.side === "exit" && !row.excluded).length,
    staying: rows.filter((row) => row.side === "hold" || row.side === "inside-window").length,
    largestNow,
    largestAfter,
    topFiveNow: metric(
      "Top five now",
      "What the five biggest holdings are worth together, as a share of the portfolio — or all of them, when it holds fewer than five names.",
      toPercent(sumOfTop(currentWeights, 5)),
      noCurrentSide,
    ),
    topFiveAfter: metric(
      "Top five after",
      "The five biggest target weights together, or all of them when the target holds fewer than five.",
      indeterminate ? null : toPercent(sumOfTop(keptTargetWeights, 5)),
      indeterminate ? keptExitReason : "The screen's target is empty.",
    ),
    coverage: metric(
      "Target coverage",
      "How much of the portfolio the target weights you kept account for. It is 100% until you exclude something.",
      toPercent(coverageValue),
      "The screen's target is empty.",
    ),
    leftUnassigned: metric(
      "Left unassigned",
      "The slice your exclusions take out of the target. Baskfy does not spread it over the other names — that would be inventing an allocation the screen never proposed.",
      toPercent(unassignedValue),
      "Nothing is excluded.",
    ),
    concentrationMove: move,
    pricedOnly: held.some((name) => name.unpriced),
  };
}

/* ------------------------------------------------------------------ the warnings */

function buildWarnings(
  rebalance: RebalanceOut,
  detail: PortfolioDetail | null,
  held: readonly HeldName[],
  rows: readonly ComparisonRow[],
): PreviewWarning[] {
  const warnings: PreviewWarning[] = [];

  const delisted = rows.filter((row) => row.exitReason === "delisted");
  if (delisted.length > 0 || rebalance.delisted_count > 0) {
    const subjects = delisted.map((row) => row.symbol);
    warnings.push({
      id: "delisted",
      level: "critical",
      headline:
        subjects.length > 0
          ? `${subjects.join(", ")} ${subjects.length === 1 ? "is" : "are"} delisted`
          : `${rebalance.delisted_count} held instrument${rebalance.delisted_count === 1 ? " is" : "s are"} delisted`,
      detail:
        "A delisted instrument cannot be sold on the exchange. Whatever happens to it happens through the registrar or the exchange's own process, not through an order.",
      subjects,
    });
  }

  const unpriced = held.filter((name) => name.unpriced);
  if (unpriced.length > 0) {
    warnings.push({
      id: "unpriced",
      level: "review",
      headline: `${unpriced.map((name) => name.symbol).join(", ")} ${unpriced.length === 1 ? "has" : "have"} no price today`,
      detail:
        "An unpriced holding has no weight, so it is missing from the current side of every comparison here and from the concentration figures. Its rows show the reason rather than a number.",
      subjects: unpriced.map((name) => name.symbol),
    });
  }

  const unreconciled = held.filter((name) => name.pendingReconciliation);
  const portfolioUnreconciled = detail?.summary.pending_reconciliation === true;
  if (unreconciled.length > 0 || portfolioUnreconciled) {
    const subjects = unreconciled.map((name) => name.symbol);
    warnings.push({
      id: "reconciliation",
      level: "critical",
      headline:
        subjects.length > 0
          ? `${subjects.join(", ")} ${subjects.length === 1 ? "does" : "do"} not reconcile with the broker`
          : `${detail?.summary.name ?? "This portfolio"} does not reconcile with the broker`,
      detail:
        "Baskfy and the broker disagree about what is held. The diff above was computed against Baskfy's copy, so a name may be on the wrong list until the difference is resolved.",
      subjects,
    });
  }

  const pricesAsOf = detail?.summary.prices_as_of ?? null;
  if (pricesAsOf !== null && rebalance.as_of < pricesAsOf) {
    warnings.push({
      id: "stale-as-of",
      level: "review",
      headline: `The screen ran on ${rebalance.as_of}; your holdings are marked at ${pricesAsOf}`,
      detail:
        "The ranking and the weights on the current side come from different sessions. The comparison still holds, but a name that moved between the two will look further from its target than it is.",
      subjects: [rebalance.as_of, pricesAsOf],
    });
  }

  const requested = rebalance.requested_as_of ?? null;
  if (requested !== null && requested !== rebalance.as_of) {
    warnings.push({
      id: "requested-as-of",
      level: "review",
      headline: `You asked for ${requested}; the screen's latest completed session is ${rebalance.as_of}`,
      detail:
        "A daily bar exists only once its day has closed, so the diff used the last session that had one.",
      subjects: [requested, rebalance.as_of],
    });
  }

  if (rebalance.screen_result_count < rebalance.top_n) {
    warnings.push({
      id: "screen-short",
      level: "review",
      headline: `The screen returned ${rebalance.screen_result_count} names, fewer than the top ${rebalance.top_n} you asked for`,
      detail:
        "The target is therefore spread across fewer names than you intended, and each one carries a larger weight than a full list would have given it.",
      subjects: [String(rebalance.screen_result_count), String(rebalance.top_n)],
    });
  }

  if (detail !== null && held.length > 0 && rebalance.holdings_count !== held.length) {
    warnings.push({
      id: "holdings-drift",
      level: "critical",
      headline: `The diff ran against ${rebalance.holdings_count} holdings; this portfolio now shows ${held.length}`,
      detail:
        "The proposal and the portfolio on this screen are not the same set of holdings. Run the diff again before you act on it.",
      subjects: [String(rebalance.holdings_count), String(held.length)],
    });
  }

  return warnings;
}

/* ------------------------------------------------------------------ the preview */

const EMPTY_IMPACT: Impact = {
  namesNow: null,
  namesAfter: 0,
  entering: 0,
  exiting: 0,
  staying: 0,
  largestNow: metric("Largest name now", "The biggest single holding as a share of the portfolio today.", null, NO_WEIGHTS_YET),
  largestAfter: metric("Largest name after", "The biggest weight in the target the screen proposes.", null, "No diff has been run yet."),
  topFiveNow: metric("Top five now", "What the five biggest holdings are worth together, or all of them when the portfolio holds fewer than five names.", null, NO_WEIGHTS_YET),
  topFiveAfter: metric("Top five after", "The five biggest target weights together, or all of them when the target holds fewer than five.", null, "No diff has been run yet."),
  coverage: metric("Target coverage", "How much of the portfolio the target weights you kept account for.", null, "No diff has been run yet."),
  leftUnassigned: metric("Left unassigned", "The slice your exclusions take out of the target.", null, "Nothing is excluded."),
  concentrationMove: "unknown",
  pricedOnly: false,
};

const NO_SCREEN_BLOCKER: Blocker = {
  headline: "There is no screen to rebalance against",
  detail:
    "A rebalance is a diff: what you hold, against a ranked list. Baskfy has no target allocation stored for a portfolio, so without a screen there is nothing to compare with and nothing this drawer could honestly show.",
  action: { label: "Build a screen", href: "/build" },
};

/** Build the whole preview. Pure: same payload and same exclusions in, same drawer out. */
export function rebalancePreview(input: PreviewInput): RebalancePreview {
  const held = heldNames(input.detail);
  const heldById = new Map(held.map((name) => [name.instrumentId, name]));

  if (input.screens.length === 0) {
    return {
      status: "no-screen",
      portfolioName: input.portfolioName,
      blocker: NO_SCREEN_BLOCKER,
      screenName: null,
      topN: null,
      holdBuffer: null,
      bandLimit: null,
      asOf: null,
      requestedAsOf: null,
      computedAt: null,
      lists: [],
      rows: [],
      impact: EMPTY_IMPACT,
      warnings: [],
      excludedRows: [],
    };
  }

  const rebalance = input.rebalance;
  if (rebalance === null) {
    return {
      status: "not-analysed",
      portfolioName: input.portfolioName,
      blocker: null,
      screenName: null,
      topN: null,
      holdBuffer: null,
      bandLimit: null,
      asOf: null,
      requestedAsOf: null,
      computedAt: null,
      lists: [],
      rows: [],
      impact: EMPTY_IMPACT,
      warnings: [],
      excludedRows: [],
    };
  }

  const topN = rebalance.top_n;
  const bandLimit = rebalance.top_n + rebalance.hold_buffer;
  const targets = new Map(rebalance.target_weights.map((target) => [target.instrument_id, target]));
  const holdingsLoaded = input.detail !== null;

  const make = (side: PreviewSide, source: RebalanceNameOut): ComparisonRow =>
    buildRow(side, source, targets, heldById, input.excluded, topN, bandLimit, holdingsLoaded);

  const exits = rebalance.exits.map((row) => make("exit", row));
  const entries = rebalance.entries.map((row) => make("entry", row));
  const insideWindow = rebalance.inside_wrh.map((row) => make("inside-window", row));
  const holds = rebalance.holds.map((row) => make("hold", row));
  const rows = [...exits, ...entries, ...insideWindow, ...holds];

  /* The tracker's own column order and its own wording — docs/07's three lists, plus the fourth
     `rank_buffer` returns so a reader is not shown a diff that omits half their portfolio. */
  const lists: NameList[] = [
    {
      key: "exit",
      title: "Exits",
      rule: `Held, and ranked worse than ${bandLimit} — or gone from the screen.`,
      count: exits.length,
      rows: exits,
      emptyMessage: "Nothing to sell. Every holding is still inside the buffer.",
    },
    {
      key: "entry",
      title: "Entries",
      rule: `In the screen's top ${topN}, and not currently held.`,
      count: entries.length,
      rows: entries,
      emptyMessage: "Nothing to buy — you already hold the whole top N.",
    },
    {
      key: "inside-window",
      title: "Inside the hold band",
      rule: `Held, ranked ${topN + 1}–${bandLimit}. Inside the band, so they stay.`,
      count: insideWindow.length,
      rows: insideWindow,
      emptyMessage: "No holding is in the buffer band right now.",
    },
    {
      key: "hold",
      title: "Core holds",
      rule: `Held, and still inside the screen's top ${topN}.`,
      count: holds.length,
      rows: holds,
      emptyMessage: "No holding is inside the top N.",
    },
  ];

  return {
    status: "ready",
    portfolioName: input.portfolioName,
    blocker: null,
    screenName: rebalance.screen.name,
    topN,
    holdBuffer: rebalance.hold_buffer,
    bandLimit,
    asOf: rebalance.as_of,
    requestedAsOf: rebalance.requested_as_of ?? null,
    computedAt: rebalance.created_at,
    lists,
    rows,
    impact: buildImpact(rows, held, rebalance.target_weights, input.excluded, holdingsLoaded),
    warnings: buildWarnings(rebalance, input.detail, held, rows),
    excludedRows: rows.filter((row) => row.excluded),
  };
}

/* ------------------------------------------------------------------ the plan */

export interface PlanTextInput {
  readonly preview: RebalancePreview;
  /** The reader's own words against a name, carried into the plan. Never generated. */
  readonly notes: ReadonlyMap<number, string>;
}

const PLAN_BANNER = "BASKFY REBALANCE PLAN — A PLAN, NOT AN ORDER";

/**
 * The artefact step five produces: text a person reads, copies and takes to their broker.
 *
 * There is no send button anywhere near it, and there is nothing for one to call. Baskfy's web
 * app has no path to a broker order at all — the desk's gateway is a separate service behind the
 * desk's own non-negotiables, and this drawer does not speak to it.
 *
 * Pure, and clockless: every date in the output comes from the payload. A "generated at" stamp
 * would need `Date.now()` and would make this untestable for the sake of a line nobody reads.
 */
export function planText({ preview, notes }: PlanTextInput): string {
  if (preview.status !== "ready") {
    return `${PLAN_BANNER}\n\nNo diff has been run, so there is no plan to write.`;
  }

  const lines: string[] = [PLAN_BANNER, ""];
  lines.push(`Portfolio:      ${preview.portfolioName}`);
  lines.push(`Screen:         ${preview.screenName ?? "unnamed"} · top ${preview.topN} · hold buffer ${preview.holdBuffer}`);
  lines.push(`Screen data:    ${preview.asOf}`);
  lines.push(`Diff computed:  ${preview.computedAt}`);
  lines.push("");
  lines.push("Baskfy has not sent any of this to a broker and has no way to. There are no");
  lines.push("quantities below: enter each line yourself in your broker's own order window,");
  lines.push("where the quantity, the price and the charges are shown before you commit.");
  lines.push("");

  const active = (side: PreviewSide): ComparisonRow[] =>
    preview.rows.filter((row) => row.side === side && !row.excluded);

  const section = (title: string, rows: readonly ComparisonRow[], suffix: string) => {
    lines.push(`${title} (${rows.length})${suffix}`);
    if (rows.length === 0) {
      lines.push("  none");
    } else {
      for (const row of rows) {
        const note = notes.get(row.instrumentId)?.trim();
        const where = row.brokers.length > 0 ? ` [${row.brokers.join(", ")}]` : "";
        lines.push(`  ${row.symbol.padEnd(14)}${row.name}${where}`);
        lines.push(`  ${" ".repeat(14)}${row.ruleLine}`);
        if (row.target.value !== null) {
          lines.push(`  ${" ".repeat(14)}target weight ${row.target.value}%`);
        }
        if (note) lines.push(`  ${" ".repeat(14)}your note: ${note}`);
      }
    }
    lines.push("");
  };

  section("EXIT — the whole position, quantity as your broker shows it", active("exit"), "");
  section("ENTER — to the target weight below", active("entry"), "");
  section("HOLD — no action, inside the band", [...active("inside-window"), ...active("hold")], "");

  if (preview.excludedRows.length > 0) {
    lines.push(`EXCLUDED BY YOU (${preview.excludedRows.length}) — deliberately not in this plan`);
    for (const row of preview.excludedRows) {
      const note = notes.get(row.instrumentId)?.trim();
      lines.push(`  ${row.symbol.padEnd(14)}${row.ruleLine}${note ? ` — your note: ${note}` : ""}`);
    }
    lines.push("");
  }

  lines.push("NOT IN THIS PLAN, AND WHERE EACH FIGURE COMES FROM INSTEAD");
  for (const item of NOT_PRODUCED_HERE) {
    lines.push(`  ${item.name}`);
    lines.push(`    ${item.reason}`);
    lines.push(`    Instead: ${item.insteadFrom}`);
  }
  lines.push("");
  lines.push("Baskfy is not a registered investment adviser. The lines above restate a screen's");
  lines.push("own ranking rule against what you hold. They are not advice to buy or to sell.");

  return lines.join("\n");
}
