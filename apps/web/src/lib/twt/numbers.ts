import { percentOf } from "@/lib/portfolio/analytics";
import {
  formatQuantity,
  formatRupees,
  groupIndian,
  parseDecimal,
  roundDecimalString,
  toDecimalString,
} from "@/lib/portfolios/decimal";

/**
 * The arithmetic behind `/twt`, and the only place in this tree where a rate changes unit.
 *
 * TWO RULES, BOTH LEARNED THE EXPENSIVE WAY
 * -----------------------------------------
 * **1. Money is a decimal string end to end.** Scaled-integer arithmetic in, a formatted string
 * out, never a `number` in between (house rule 9). A stop on a line held for 600 sessions is
 * derived from a specific number on a specific evening, and a float's idea of `.5` is not it.
 *
 * **2. A rate converts exactly once, here, never in a component.** On 11 Sep 2026 the portfolio
 * band rendered a 1.99% day as 0.0199% because the component scaled a figure the builder had
 * already scaled. {@link asPercent} is the single multiplication by a hundred in this tree, and
 * the payload names its unit in the field (`*_pct` is already a percentage, `*_fraction` is not),
 * so a JSX expression never has to guess.
 *
 * Nothing here renders a dash. A figure that cannot be computed comes back `null` and its caller
 * is required by {@link Figure} to supply the reason instead.
 */

/** A number the screen may show, or the reason it cannot. Never both, never neither. */
export interface Figure {
  /**
   * The formatted value, ready to render. `null` only ever means "see `unavailable`".
   *
   * The type is the contract, and the contract is what makes the no-bare-dash rule structural
   * rather than a thing to remember: a component cannot get a value out of this without also
   * having the explanation to hand. Copied deliberately from `@/lib/portfolio/command-center`'s
   * `Metric`, which established the rule after an empty account rendered a screen of dashes.
   */
  readonly value: string | null;
  /** Why there is no value, in a reader's words. Non-null whenever `value` is null. */
  readonly unavailable: string | null;
}

/**
 * Build a figure: a formatted value, or the reason there is none.
 *
 * `reason` is a fallback rather than a requirement of the type because the fallback is what
 * catches the case nobody thought of. It says what is true — that we do not know why — instead of
 * inventing a cause, and it is never a dash.
 */
export function figure(value: string | null | undefined, reason: string): Figure {
  const has = value !== null && value !== undefined && value !== "";
  return { value: has ? value : null, unavailable: has ? null : reason };
}

/** A figure that is known to be absent, with its reason. */
export function noFigure(reason: string): Figure {
  return { value: null, unavailable: reason };
}

function negate(value: string): string | null {
  const parsed = parseDecimal(value);
  if (parsed === null) return null;
  return toDecimalString({ units: -parsed.units, scale: parsed.scale });
}

/** `left − right`, exactly. `null` when either side is missing or is not a decimal string. */
export function subtractDecimals(
  left: string | null | undefined,
  right: string | null | undefined,
): string | null {
  if (left === null || left === undefined) return null;
  if (right === null || right === undefined) return null;
  const leftParsed = parseDecimal(left);
  const negated = negate(right);
  if (leftParsed === null || negated === null) return null;
  const rightParsed = parseDecimal(negated);
  if (rightParsed === null) return null;
  const scale = Math.max(leftParsed.scale, rightParsed.scale);
  const units =
    leftParsed.units * 10n ** BigInt(scale - leftParsed.scale) +
    rightParsed.units * 10n ** BigInt(scale - rightParsed.scale);
  return toDecimalString({ units, scale });
}

/**
 * A stored FRACTION as a PERCENTAGE, exactly.
 *
 * `percentOf(x, "1")` is `x / 1 * 100` on scaled integers, borrowed rather than rewritten so this
 * module and the portfolio tree cannot drift. `Number(x) * 100` would make a 1.99% day
 * `1.9900000000000002`, which is house rule 9's whole point.
 */
export function asPercent(fraction: string | null | undefined): string | null {
  return percentOf(fraction ?? null, "1");
}

/**
 * `01` §8's number: how far the last price is above the trigger that would sell the line.
 *
 * `(last − trigger) / last × 100`. It is on every open row because a 20% give-back on a ₹2.5 lakh
 * line is a ₹50,000 open loss this strategy sits through as a matter of routine, and the person
 * watching has to have agreed to that in advance rather than discovered it on the day.
 */
export function distanceToTriggerPct(
  last: string | null | undefined,
  trigger: string | null | undefined,
): string | null {
  const gap = subtractDecimals(last, trigger);
  if (gap === null || last === null || last === undefined) return null;
  return percentOf(gap, last);
}

/** `close / month_low_3` as a percentage above that low: `"1.42"` → `"42.00"`. */
export function ratioAsUpliftPct(ratio: string | null | undefined): string | null {
  const above = subtractDecimals(ratio, "1");
  return above === null ? null : percentOf(above, "1");
}

/** Rupees as crore, two decimals, on integers. `"340000000"` → `"34.00"`. */
export function toCrore(rupees: string | null | undefined): string | null {
  const parsed = parseDecimal(rupees ?? null);
  if (parsed === null) return null;
  return roundDecimalString(
    toDecimalString({ units: parsed.units, scale: parsed.scale + 7 }),
    2,
  );
}

/** `"1234567.50"` → `"₹12,34,567.50"`. Only ever called with a value that parsed. */
export function rupees(value: string, decimals = 2): string {
  return formatRupees(value, { decimals });
}

/** `"1250"` → `"1,250"`. */
export function quantity(value: string): string {
  return formatQuantity(value);
}

/** A percentage as text: `"18.70"` → `"18.7%"`, with the sign kept when it carries meaning. */
export function percent(value: string, decimals = 1, options: { sign?: boolean } = {}): string {
  const rounded = roundDecimalString(value, decimals);
  if (rounded === null) return `${value}%`;
  const positive = !rounded.startsWith("-") && parseDecimal(rounded)?.units !== 0n;
  return `${options.sign && positive ? "+" : ""}${rounded}%`;
}

/** A whole count with Indian grouping. `4855` → `"4,855"`. */
export function count(value: number): string {
  return groupIndian(String(Math.trunc(Math.abs(value)))).replace(/^/, value < 0 ? "-" : "");
}

/** Green above zero, red below, neutral at zero — the semantic tokens, never a raw palette. */
export function toneOf(value: string | null): string {
  const parsed = parseDecimal(value ?? null);
  if (parsed === null || parsed.units === 0n) return "";
  return parsed.units > 0n ? "text-positive" : "text-negative";
}
