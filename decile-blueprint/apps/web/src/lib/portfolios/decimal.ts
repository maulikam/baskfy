/**
 * Exact decimal arithmetic over the strings the API sends.
 *
 * CLAUDE.md house rule 9: "Money and prices are `numeric`, never `float`." The API keeps that
 * promise on the wire — every money field in `PortfolioRollupOut` is an unrounded `Decimal`
 * serialised as a **string**, and the response's stated invariant is
 * `total == sum(by_broker) + unattributed` *to the last digit*. Parsing those through `Number()`
 * to add them up throws the invariant away at the first value past 2^53 or the first repeating
 * binary fraction, and a rupee lost in the browser is indistinguishable from a rupee the server
 * never sent.
 *
 * So the sums here are `bigint` over scaled integers. Nothing in this module converts a money
 * string to a `number`, and `checkTotals` can therefore assert the server's invariant rather than
 * assume it.
 *
 * Presentation is a separate act: {@link formatRupees} rounds half-up — again on `bigint` — and
 * groups the digits the Indian way by hand, because `toLocaleString` would need a `number` first.
 */

/** `-12`, `12.5`, `0.0001`. No exponent form: the API does not send one and guessing is worse. */
const DECIMAL = /^[+-]?\d+(\.\d+)?$/;

export interface ScaledDecimal {
  /** The value as an integer, multiplied by 10 ** `scale`. */
  units: bigint;
  scale: number;
}

export function parseDecimal(value: string | null | undefined): ScaledDecimal | null {
  if (value === null || value === undefined) return null;
  const text = value.trim();
  if (text === "" || !DECIMAL.test(text)) return null;
  const negative = text.startsWith("-");
  const unsigned = text.replace(/^[+-]/, "");
  const [whole = "0", fraction = ""] = unsigned.split(".");
  const units = BigInt(`${whole}${fraction}`);
  return { units: negative ? -units : units, scale: fraction.length };
}

function rescale(value: ScaledDecimal, scale: number): bigint {
  if (scale === value.scale) return value.units;
  return value.units * 10n ** BigInt(scale - value.scale);
}

export function toDecimalString(value: ScaledDecimal): string {
  const negative = value.units < 0n;
  const digits = (negative ? -value.units : value.units).toString().padStart(value.scale + 1, "0");
  const whole = digits.slice(0, digits.length - value.scale);
  const fraction = value.scale === 0 ? "" : `.${digits.slice(digits.length - value.scale)}`;
  return `${negative ? "-" : ""}${whole}${fraction}`;
}

/**
 * Add money strings exactly.
 *
 * `null` when nothing summable was supplied — which is not the same answer as `"0"`, and the
 * difference is the whole point: "no figure" and "no money" read identically once one is rendered
 * as the other. A value that is not a decimal string is refused rather than coerced.
 */
export function addDecimalStrings(values: ReadonlyArray<string | null | undefined>): string | null {
  const parsed = values
    .map(parseDecimal)
    .filter((value): value is ScaledDecimal => value !== null);
  if (parsed.length === 0) return null;
  const scale = parsed.reduce((max, value) => Math.max(max, value.scale), 0);
  const units = parsed.reduce((sum, value) => sum + rescale(value, scale), 0n);
  return toDecimalString({ units, scale });
}

export function compareDecimalStrings(left: string, right: string): number {
  const a = parseDecimal(left);
  const b = parseDecimal(right);
  if (a === null || b === null) return Number.NaN;
  const scale = Math.max(a.scale, b.scale);
  const difference = rescale(a, scale) - rescale(b, scale);
  if (difference > 0n) return 1;
  if (difference < 0n) return -1;
  return 0;
}

/** Numerically equal, whatever the trailing zeros say: `"1.50" === "1.5"` here, `!==` in JS. */
export function decimalStringsEqual(left: string, right: string): boolean {
  return compareDecimalStrings(left, right) === 0;
}

export function isZeroDecimal(value: string | null | undefined): boolean {
  const parsed = parseDecimal(value);
  return parsed !== null && parsed.units === 0n;
}

export function isPositiveDecimal(value: string | null | undefined): boolean {
  const parsed = parseDecimal(value);
  return parsed !== null && parsed.units > 0n;
}

/** Round half away from zero, on integers. Never a `number`, so never a float's idea of `.5`. */
export function roundDecimalString(value: string, decimals: number): string | null {
  const parsed = parseDecimal(value);
  if (parsed === null) return null;
  if (parsed.scale <= decimals) {
    return toDecimalString({ units: rescale(parsed, decimals), scale: decimals });
  }
  const factor = 10n ** BigInt(parsed.scale - decimals);
  const negative = parsed.units < 0n;
  const magnitude = negative ? -parsed.units : parsed.units;
  const quotient = magnitude / factor;
  const remainder = magnitude % factor;
  const rounded = remainder * 2n >= factor ? quotient + 1n : quotient;
  return toDecimalString({ units: negative ? -rounded : rounded, scale: decimals });
}

/** `"1234567"` → `"12,34,567"`. Last three, then pairs — the Indian grouping, done on digits. */
export function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits;
  const head = digits.slice(0, digits.length - 3);
  const tail = digits.slice(digits.length - 3);
  return `${head.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${tail}`;
}

export const NO_FIGURE = "—";

export interface RupeeOptions {
  /** Decimal places to show. The value is rounded for display only; the sum stays exact. */
  decimals?: number;
}

/**
 * A money string as rupees, formatted without ever becoming a `number`.
 *
 * A value that is not a decimal string renders as {@link NO_FIGURE} rather than as `₹NaN` — an
 * unparseable figure is a missing figure, and inventing a zero for it is the failure this module
 * exists to prevent.
 */
export function formatRupees(value: string | null | undefined, options: RupeeOptions = {}): string {
  if (value === null || value === undefined || value.trim() === "") return NO_FIGURE;
  const decimals = options.decimals ?? 2;
  const rounded = roundDecimalString(value, decimals);
  if (rounded === null) return NO_FIGURE;
  const negative = rounded.startsWith("-");
  const [whole = "0", fraction = ""] = rounded.replace("-", "").split(".");
  const body = fraction === "" ? groupIndian(whole) : `${groupIndian(whole)}.${fraction}`;
  return `${negative ? "-" : ""}₹${body}`;
}

/** A share count. Whole units are shown whole; a fractional quantity keeps what it was sent. */
export function formatQuantity(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim() === "") return NO_FIGURE;
  const parsed = parseDecimal(value);
  if (parsed === null) return NO_FIGURE;
  const text = toDecimalString(parsed);
  const negative = text.startsWith("-");
  const [whole = "0", fraction = ""] = text.replace("-", "").split(".");
  const trimmed = fraction.replace(/0+$/, "");
  const body = trimmed === "" ? groupIndian(whole) : `${groupIndian(whole)}.${trimmed}`;
  return `${negative ? "-" : ""}${body}`;
}
