/**
 * Unit counts beside rupee amounts — and the blank that is not a zero.
 *
 * A sleeve allocation used to be amounts and weights only. It now carries `units` and `price` per
 * row, because "₹5,00,000 of CUPID" is not something a reader can check against a demat statement
 * and "1,757 shares" is.
 *
 * The rule this module exists to keep: **when a price is unavailable, `units` and `price` arrive
 * as `null`, never `0`.** A zero share count renders as a real answer — "the allocator decided
 * you buy none of this" — when the truth is "nobody knows what this costs". So an absent count
 * renders as an em dash *with the reason attached*, and a genuine zero (capital that does not
 * cover one share) renders as `0` with its own, different reason. Two different facts, two
 * different cells.
 *
 * The reason text prefers the server's own `units_note`, because the server knows why it could
 * not price the row and this module only knows that it could not.
 */

import type { AllocationOut, AllocationRowOut, SleeveAllocationOut } from "@baskfy/api-client";

import { NO_FIGURE, formatQuantity, formatRupees } from "@/lib/portfolios/decimal";

export interface UnitsCell {
  /** What the cell shows. {@link NO_FIGURE} when there is no count — never a stand-in `0`. */
  text: string;
  /** No price was available, so no count could exist. */
  unpriced: boolean;
  /** Why the cell reads the way it does. Always set when `text` is the dash. */
  reason: string | null;
}

export interface PriceCell {
  text: string;
  unpriced: boolean;
}

export function priceCell(row: AllocationRowOut): PriceCell {
  const price = row.price ?? null;
  if (price === null || price === "") return { text: NO_FIGURE, unpriced: true };
  return { text: formatRupees(price), unpriced: false };
}

/**
 * The units cell for one allocation row.
 *
 * `sleeve` is optional so the cell can be rendered from a row alone; when it is supplied its
 * `units_note` is used verbatim, which is how the reader gets the server's own explanation rather
 * than this file's guess at one.
 */
export function unitsCell(
  row: AllocationRowOut,
  sleeve?: Pick<SleeveAllocationOut, "units_note" | "unpriced"> | null,
): UnitsCell {
  const units = row.units ?? null;
  const price = row.price ?? null;

  if (units === null || price === null || price === "") {
    const note = sleeve?.units_note ?? null;
    return {
      text: NO_FIGURE,
      unpriced: true,
      reason: note ?? `No price for ${row.symbol}, so this row has no unit count.`,
    };
  }

  if (units === 0) {
    return {
      text: "0",
      unpriced: false,
      reason: `${formatRupees(row.amount)} does not cover one share at ${formatRupees(price)}.`,
    };
  }

  return { text: formatQuantity(String(units)), unpriced: false, reason: null };
}

export interface UnpricedSummary {
  symbols: string[];
  count: number;
  /** The sentence to show under the sleeve. `null` when every row was priced. */
  note: string | null;
}

export function sleeveUnpriced(
  sleeve: Pick<SleeveAllocationOut, "units_note" | "unpriced">,
): UnpricedSummary {
  const symbols = sleeve.unpriced;
  if (symbols.length === 0) return { symbols: [], count: 0, note: null };
  return {
    symbols,
    count: symbols.length,
    note:
      sleeve.units_note ??
      `No price for ${symbols.join(", ")}, so ${symbols.length === 1 ? "that row has" : "those rows have"} an amount but no unit count.`,
  };
}

export interface AllocationUnitsSummary {
  /** The date the prices came from. `null` when nothing was priced. */
  pricedAsOf: string | null;
  unpriced: string[];
  /** Whole-allocation sentence, or `null` when every row across every sleeve was priced. */
  note: string | null;
}

export function allocationUnits(allocation: AllocationOut): AllocationUnitsSummary {
  const pricedAsOf = allocation.priced_as_of ?? null;
  const unpriced = allocation.unpriced;
  if (unpriced.length === 0) {
    return { pricedAsOf, unpriced: [], note: null };
  }
  return {
    pricedAsOf,
    unpriced,
    note: `${unpriced.length} name${unpriced.length === 1 ? "" : "s"} could not be priced — ${unpriced.join(", ")}. ${unpriced.length === 1 ? "Its" : "Their"} amount stands; the unit count is blank rather than zero.`,
  };
}
