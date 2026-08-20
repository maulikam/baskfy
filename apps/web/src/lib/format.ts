/**
 * Number and date formatting — docs/08 §"Design principles" and docs/11 §Accessibility.
 *
 *     "Colour is never the sole carrier of meaning (pair positive/negative colour with sign and,
 *      where dense, an arrow glyph)."
 *
 * So every signed number renders its sign, and `directionGlyph` supplies the arrow for dense
 * surfaces. A red cell and a green cell must still be distinguishable in greyscale, by a
 * colourblind reader, and to a screen reader — which is why `describeChange` exists.
 *
 * Values arrive from the API already rounded to their stored precision (CLAUDE.md house rule 8:
 * "Round at write time. Storage precision is the contract"), so nothing here re-rounds. These
 * functions choose *presentation*: grouping, sign, suffix.
 */

/** docs/13 §2 finding 7: marketcap is an integer in ₹ crore. */
export const CRORE_SUFFIX = "cr";

const INDIAN_LOCALE = "en-IN";

export type Direction = "up" | "down" | "flat";

export function direction(value: number | null | undefined): Direction {
  if (value === null || value === undefined || Number.isNaN(value)) return "flat";
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "flat";
}

/** ▲ / ▼ / — . Paired with colour, never replaced by it. */
export function directionGlyph(value: number | null | undefined): string {
  const dir = direction(value);
  if (dir === "up") return "▲";
  if (dir === "down") return "▼";
  return "—";
}

/** Screen-reader text for a signed number, so the colour is not the only cue. */
export function describeChange(value: number | null | undefined, unit = ""): string {
  const dir = direction(value);
  if (value === null || value === undefined || Number.isNaN(value)) return "no value";
  const magnitude = Math.abs(value).toLocaleString(INDIAN_LOCALE);
  if (dir === "up") return `up ${magnitude}${unit}`;
  if (dir === "down") return `down ${magnitude}${unit}`;
  return `unchanged at ${magnitude}${unit}`;
}

export interface NumberFormatOptions {
  /** Decimal places. Omit to keep whatever the API sent. */
  decimals?: number;
  /** Prefix the sign even when positive (returns, changes). */
  signed?: boolean;
  suffix?: string;
}

/** The em dash the reference product uses for "no value" (docs/08 §Dashboard: `-`). */
export const EMPTY_CELL = "—";

export function formatNumber(
  value: number | string | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  const numeric = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(numeric)) return EMPTY_CELL;

  const { decimals, signed = false, suffix = "" } = options;
  const body = numeric.toLocaleString(INDIAN_LOCALE, {
    minimumFractionDigits: decimals ?? 0,
    maximumFractionDigits: decimals ?? 10,
  });
  const sign = signed && numeric > 0 ? "+" : "";
  return `${sign}${body}${suffix}`;
}

/** A percentage as the API stores it (docs/13 §4: returns and sharpe at 2 dp). */
export function formatPercent(
  value: number | string | null | undefined,
  decimals = 2,
): string {
  return formatNumber(value, { decimals, signed: true, suffix: "%" });
}

/**
 * Volatility is stored as a decimal fraction and displayed as a percentage.
 *
 * docs/13 §2 finding 4: "range 0.179–0.618; the UI multiplies by 100". docs/06a §10 and
 * docs/07a §13 record that the API deliberately does *not* do this multiplication, so that there
 * is exactly one place in the system where it happens. This is that place.
 */
export function formatFraction(
  value: number | string | null | undefined,
  decimals = 2,
): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  const numeric = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(numeric)) return EMPTY_CELL;
  return formatNumber(numeric * 100, { decimals, suffix: "%" });
}

/** ₹ crore, integer (docs/13 §2 finding 7). */
export function formatCrore(value: number | string | null | undefined): string {
  return formatNumber(value, { decimals: 0, suffix: ` ${CRORE_SUFFIX}` });
}

/** "19 Aug 2026" — the wording docs/08 §"App shell" shows on the freshness pill. */
export function formatTradeDate(iso: string | null | undefined): string {
  if (!iso) return EMPTY_CELL;
  const parsed = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return EMPTY_CELL;
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(parsed);
}
