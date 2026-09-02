import type { SwingLadder } from "@/lib/swing/fetch";

/**
 * The sentences and number formats of `/swing/journal`, kept out of the page so they can be
 * tested as functions of a payload rather than by reading a rendered tree.
 *
 * `05` §2 asks the page for "the current loss streak and what it means for the ladder". The
 * ladder (`04` §8.4) is four rules in precedence order, and a person reading the card wants the
 * one that applies to *him tonight*, said as what the next close will do — not the rule book.
 * `ladderSentence` is that one sentence.
 */

/**
 * `04` §8.4's ladder, mirrored from `baskfy_core.swing.config.MarketConfig` the way the market
 * page mirrors §8.3's two breadth thresholds: the API ships the rung and the closes it read, not
 * the rule, and the page has to say what the rule does next. If `MarketConfig` changes, the
 * `test_swing_docs_parity.py` suite fails on the Python side and these three lines follow.
 */
export const RUNGS = 4; // MarketConfig.tiers has four entries
export const LOOKBACK_TRADES = 5; // MarketConfig.lookback_trades
export const STEP_DOWN_LOSS_STREAK = 3; // MarketConfig.step_down_loss_streak

/** `GATE_UNKNOWN` in `baskfy_api.swing_journal`: no market row has been written yet. */
export const GATE_UNKNOWN = "UNKNOWN";

/** `+2.50R`, `-1.00R`, `0.00R` — R with its sign, because the sign is the whole point of R. */
export function signedR(value: number): string {
  const fixed = value.toFixed(2);
  if (value > 0) return `+${fixed}R`;
  // `(-0.001).toFixed(2)` is "-0.00"; a zero has no sign.
  return `${fixed === "-0.00" ? "0.00" : fixed}R`;
}

/** Rupees, whole, Indian grouping and a sign: `+₹12,500`, `-₹4,000`. */
export function signedInr(value: number): string {
  const magnitude = Math.round(Math.abs(value)).toLocaleString("en-IN");
  if (value > 0) return `+₹${magnitude}`;
  if (value < 0) return `-₹${magnitude}`;
  return `₹${magnitude}`;
}

/** A price as the positions page shows it: two places, or a dash. */
export function money(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

/** How many losses in a row the ladder read, counted from the newest close backwards. */
export function trailingLosses(lastR: readonly number[]): number {
  let streak = 0;
  for (const value of [...lastR].reverse()) {
    if (value < 0) streak += 1;
    else break;
  }
  return streak;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * What the next close does to the ladder, in one sentence, from the rung, the gate and the
 * closes the ladder read — `04` §8.4's four rules, in their precedence order, reduced to the one
 * that applies tonight.
 *
 * Rung numbers are said as people say them: `level` 0 is "rung 1 of 4". The streak is counted
 * from `last_r` — the closes the ladder actually read — rather than from a card's statistics,
 * so the sentence and the number beside it cannot describe different records.
 */
export function ladderSentence(ladder: SwingLadder): string {
  const rung = ladder.level + 1;
  const of = `of ${RUNGS}`;
  const streak = trailingLosses(ladder.last_r);
  const closes = ladder.last_r.length;
  // Summed at the two places the API stores, so a float residue cannot turn a net zero into a
  // climb the Decimal arithmetic on the server would not make.
  const net = Math.round(ladder.last_r.reduce((sum, value) => sum + value, 0) * 100) / 100;

  if (ladder.gate === GATE_UNKNOWN) {
    return (
      "The gate has not been measured yet — no market row has been written — so no new entry " +
      "is allowed and no close moves the ladder until the detectors have run."
    );
  }
  if (ladder.gate === "RED") {
    return (
      `The tape is RED, so the ladder goes to rung 1 ${of} and allows no new entries whatever ` +
      "the next close says; it can climb again only once the gate turns AMBER or GREEN."
    );
  }
  if (streak >= STEP_DOWN_LOSS_STREAK) {
    if (ladder.level === 0) {
      return (
        `${plural(streak, "loss", "losses")} in a row, and the ladder is already at rung 1 ${of}, ` +
        "the bottom. Another losing close keeps it there; a close that is not a loss ends the " +
        `streak, and the rung can rise only after ${LOOKBACK_TRADES} closes net positive in a GREEN tape.`
      );
    }
    return (
      `${plural(streak, "loss", "losses")} in a row: the ladder falls a rung each evening this ` +
      `streak stands, so rung ${rung} ${of} becomes ${rung - 1} at the next settlement. Another ` +
      "losing close keeps it falling; a close that is not a loss ends the streak, and the rung " +
      `follows the last ${LOOKBACK_TRADES} closes again.`
    );
  }
  // Below the step-down threshold two rules can still apply — the streak growing, or the
  // climb — so the sentence says both: what a losing close does, then what happens otherwise.
  const toStep = STEP_DOWN_LOSS_STREAK - streak;
  const oneMore = toStep === 1;
  const streakClause =
    streak === 0 ? "No losing streak" : `${plural(streak, "loss", "losses")} in a row`;
  const losing = oneMore ? "one more losing close" : `${toStep} losing closes in a row`;
  const lossClause =
    ladder.level === 0
      ? `${losing} would call for a step down, but the ladder is already at rung 1 ${of}`
      : oneMore
        ? `${losing} makes ${STEP_DOWN_LOSS_STREAK} and steps the ladder down from rung ${rung} ${of} to ${rung - 1}`
        : `${losing} would step the ladder down from rung ${rung} ${of}`;

  let upClause: string;
  if (closes < LOOKBACK_TRADES) {
    upClause =
      `it cannot climb until ${LOOKBACK_TRADES} trades have closed net positive in a GREEN tape ` +
      `(${closes === 0 ? "none" : plural(closes, "close", "closes")} so far)`;
  } else if (ladder.gate === "GREEN" && net > 0) {
    upClause =
      ladder.level >= RUNGS - 1
        ? `the last ${LOOKBACK_TRADES} closes are net ${signedR(net)} in a GREEN tape and the ladder is at the top, rung ${RUNGS} ${of}`
        : `the last ${LOOKBACK_TRADES} closes are net ${signedR(net)} in a GREEN tape, so it climbs to rung ${rung + 1} at the next settlement, and again each evening that holds`;
  } else if (ladder.gate === "GREEN") {
    upClause = `the last ${LOOKBACK_TRADES} closes are net ${signedR(net)}, so rung ${rung} ${of} holds until they are net positive`;
  } else {
    upClause =
      `AMBER holds rung ${rung} ${of} with entries allowed at its size; it climbs only in a ` +
      `GREEN tape with the last ${LOOKBACK_TRADES} closes net positive`;
  }
  return `${streakClause}: ${lossClause}; otherwise ${upClause}.`;
}
