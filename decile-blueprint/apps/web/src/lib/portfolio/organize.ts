import type { Schemas } from "@baskfy/api-client";

import {
  addDecimalStrings,
  compareDecimalStrings,
  parseDecimal,
  roundDecimalString,
  toDecimalString,
} from "@/lib/portfolios/decimal";

/**
 * The vocabulary and the arithmetic behind PORTFOLIO_REDESIGN.md §6.6 and §6.7 — the Unallocated
 * centrepiece and the New portfolio flow.
 *
 * Two contracts meet in this module and neither of them is invented here:
 *
 * 1. **The wire.** `UnallocatedOut`, `AggregatedHoldingOut` and `HoldingBrokerLineOut` come from
 *    `services/api/.../portfolio_overview.py`, re-used through the generated schema rather than
 *    re-typed. Re-typing them is how a UI and its API start disagreeing about what a field means.
 * 2. **The suggestions.** {@link GroupingSuggestion} is the TypeScript face of
 *    `packages/core/.../grouping_suggestions.py`'s frozen dataclass of the same name — field for
 *    field, snake_case for snake_case, including the three fields that are set for a basket
 *    overlap and only for a basket overlap. {@link rankSuggestions} is that module's `_rank_key`
 *    ported exactly, so a list re-ordered in the browser after a filter is the same list the
 *    server would have produced. If those two ever drift, the port is the thing that is wrong.
 *
 * Every total here is `bigint` arithmetic over decimal strings via `@/lib/portfolios/decimal`
 * (CLAUDE.md house rule 9). A `null` total means *no figure*, never zero — the live total in the
 * §6.7 picker has to be able to say "we cannot price two of these" rather than quietly under-count
 * the portfolio the user is building.
 */

export type Unallocated = Schemas["UnallocatedOut"];
export type UnallocatedHolding = Schemas["UnallocatedHoldingOut"];
export type AggregatedHolding = Schemas["AggregatedHoldingOut"];
export type HoldingBrokerLine = Schemas["HoldingBrokerLineOut"];
export type BrokerRef = Schemas["BrokerRefOut"];
export type InstrumentRef = Schemas["InstrumentRefOut"];
export type PortfolioRef = Schemas["PortfolioRefOut"];
export type BrokerCash = Schemas["BrokerCashOut"];

/** §4.1's two kinds. Mirrors `baskfy_core.allocation_ledger.PortfolioKind`. */
export type PortfolioKind = "CAPITAL" | "MONITORING";

/** Mirrors `baskfy_core.grouping_suggestions.SuggestionBasis`. */
export type SuggestionBasis = "SECTOR" | "PURCHASE_ERA" | "BASKET_OVERLAP";

/** Mirrors `baskfy_core.allocation_ledger.HoldingKey`: one instrument in one broker account. */
export interface HoldingKey {
  instrument_id: number;
  broker_account_id: number;
}

/** Mirrors `baskfy_core.grouping_suggestions.GroupingSuggestion`. */
export interface GroupingSuggestion {
  basis: SuggestionBasis;
  proposed_name: string;
  keys: HoldingKey[];
  /** Decimal string. The group's market value at the prices the suggestion was computed from. */
  value: string;
  rationale: string;
  suggested_kind: PortfolioKind;
  basket_id: number | null;
  /** Decimal string in 0..1, four places. Set for, and only for, a `BASKET_OVERLAP`. */
  basket_coverage: string | null;
  missing_instrument_ids: number[];
}

/**
 * `SuggestionBasis.confidence_tier` from the Python module, verbatim. Lower sorts first, and it
 * only ever decides a tie — the primary question the §6.6 screen answers is "what is the biggest
 * single decision available to me right now", which is value.
 */
const CONFIDENCE_TIER: Record<SuggestionBasis, number> = {
  BASKET_OVERLAP: 0,
  SECTOR: 1,
  PURCHASE_ERA: 2,
};

/** The words a basis is shown as. The user is entitled to know *why* these stocks were grouped. */
export const SUGGESTION_BASIS_LABEL: Record<SuggestionBasis, string> = {
  SECTOR: "Same sector",
  PURCHASE_ERA: "Bought around the same time",
  BASKET_OVERLAP: "Overlaps a model you subscribe to",
};

/** §4.1's required label, verbatim from `grouping_suggestions.MONITORING_OVERLAP_NOTE`. */
export const MONITORING_NOTE =
  "Monitoring view — overlaps with other portfolios, excluded from totals.";

/** §6.6's primary call to action, verbatim from `UnallocatedOut.cta`'s default. */
export const ORGANIZE_CTA = "Organize into portfolios";

export const NO_FIGURE = "—";

/**
 * §4.1, in the words a user picking between the two needs — and this constant exists so that the
 * explanation can be rendered *at the moment of choice* rather than linked to from it. A help
 * page the user has to leave the flow to read is a help page that decides nothing.
 */
export const KIND_EXPLAINER: Record<
  PortfolioKind,
  { title: string; sentence: string; consequences: string[] }
> = {
  CAPITAL: {
    title: "Capital portfolio",
    sentence: "Real money, counted once.",
    consequences: [
      "Each holding you pick moves out of Unallocated and into this portfolio.",
      "A holding can be in exactly one capital portfolio — never two.",
      "Capital portfolios plus Unallocated add up to your net worth, to the paisa.",
    ],
  },
  MONITORING: {
    title: "Monitoring view",
    sentence: "A lens. It watches holdings without owning them.",
    consequences: [
      "Holdings stay wherever they are — Unallocated stays Unallocated.",
      "The same holding can appear in as many monitoring views as you like.",
      MONITORING_NOTE,
    ],
  },
};

/** The stable id of a physical holding, used as the selection key everywhere in §6.7. */
export function holdingKeyId(key: HoldingKey): string {
  return `${key.instrument_id}:${key.broker_account_id}`;
}

/** The keys of one aggregated display row — one per broker line, because that is what allocates. */
export function rowKeys(row: AggregatedHolding): HoldingKey[] {
  return (row.brokers ?? []).map((line) => ({
    instrument_id: row.instrument.instrument_id,
    broker_account_id: line.broker.broker_account_id,
  }));
}

export function rowKeyIds(row: AggregatedHolding): string[] {
  return rowKeys(row).map(holdingKeyId);
}

export function suggestionKeyIds(suggestion: GroupingSuggestion): string[] {
  return suggestion.keys.map(holdingKeyId);
}

/**
 * §6.7's display rule: "HDFC Bank — 320 (Zerodha 200 · Upstox 120)".
 *
 * The parenthetical is dropped for a single-broker holding, where it would only repeat the number
 * that precedes it. It is never dropped for two, because the aggregate is not the thing that gets
 * allocated — the two legs are, separately, and hiding that is how a user comes to believe they
 * allocated 320 shares when they allocated 200.
 */
export function brokerBreakdown(row: AggregatedHolding): string | null {
  const lines = row.brokers ?? [];
  if (lines.length < 2) return null;
  return lines.map((line) => `${line.broker.label} ${formatShares(line.quantity)}`).join(" · ");
}

export function describeHolding(row: AggregatedHolding): string {
  const breakdown = brokerBreakdown(row);
  const head = `${row.instrument.name} — ${formatShares(row.quantity)}`;
  return breakdown === null ? head : `${head} (${breakdown})`;
}

/** A share count, grouped the Indian way, trailing zeros trimmed. Never a `number`. */
export function formatShares(value: string | null | undefined): string {
  if (value === null || value === undefined) return NO_FIGURE;
  const parsed = parseDecimal(value);
  if (parsed === null) return NO_FIGURE;
  const text = toDecimalString(parsed);
  const [whole = "0", fraction = ""] = text.replace("-", "").split(".");
  const trimmed = fraction.replace(/0+$/, "");
  const body = trimmed === "" ? groupShares(whole) : `${groupShares(whole)}.${trimmed}`;
  return `${text.startsWith("-") ? "-" : ""}${body}`;
}

function groupShares(digits: string): string {
  if (digits.length <= 3) return digits;
  const head = digits.slice(0, digits.length - 3);
  const tail = digits.slice(digits.length - 3);
  return `${head.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${tail}`;
}

/**
 * A 0..1 decimal string as a whole-number percentage: `"0.7333"` → `"73%"`.
 *
 * Rounded on integers, like every other figure here. An unparseable coverage renders as no figure
 * rather than as `NaN%`.
 */
export function formatCoverage(value: string | null | undefined): string {
  if (value === null || value === undefined) return NO_FIGURE;
  const parsed = parseDecimal(value);
  if (parsed === null) return NO_FIGURE;
  const scale = Math.max(parsed.scale - 2, 0);
  const units = parsed.scale >= 2 ? parsed.units : parsed.units * 10n ** BigInt(2 - parsed.scale);
  const rounded = roundDecimalString(toDecimalString({ units, scale }), 0);
  return rounded === null ? NO_FIGURE : `${rounded}%`;
}

/** What the right-hand panel of §6.7's picker shows while the user builds a portfolio. */
export interface SelectionTotal {
  /** Exact sum of the priced legs, as a decimal string. `null` when nothing priced is selected. */
  value: string | null;
  /** How many selected legs the market data cannot price. They are excluded from `value`. */
  unpriced: number;
  /** Selected legs, priced or not. */
  holdings: number;
  /** Distinct instruments across those legs — what the user would call "stocks". */
  instruments: number;
}

/**
 * The live total, summed exactly, with the unpriced legs counted rather than treated as zero.
 *
 * A leg with no `value` is a leg whose price we do not have. Adding it in as zero would produce a
 * total that is wrong and still adds up, which is the single failure this whole module is built to
 * avoid; the caller renders the count beside the figure so the user knows the figure is partial.
 */
export function selectionTotal(
  rows: readonly AggregatedHolding[],
  selected: ReadonlySet<string>,
): SelectionTotal {
  const values: Array<string | null> = [];
  const instruments = new Set<number>();
  let unpriced = 0;
  let holdings = 0;

  for (const row of rows) {
    for (const line of row.brokers ?? []) {
      const id = holdingKeyId({
        instrument_id: row.instrument.instrument_id,
        broker_account_id: line.broker.broker_account_id,
      });
      if (!selected.has(id)) continue;
      holdings += 1;
      instruments.add(row.instrument.instrument_id);
      if (line.value === null || line.value === undefined) {
        unpriced += 1;
        continue;
      }
      values.push(line.value);
    }
  }

  return { value: addDecimalStrings(values), unpriced, holdings, instruments: instruments.size };
}

/**
 * `grouping_suggestions._rank_key`, ported: value descending, holding count descending, basis
 * confidence tier, then the proposed name as the backstop that makes the key total.
 */
export function rankSuggestions(suggestions: readonly GroupingSuggestion[]): GroupingSuggestion[] {
  return [...suggestions].sort((left, right) => {
    const byValue = compareDecimalStrings(right.value, left.value);
    if (byValue !== 0 && !Number.isNaN(byValue)) return byValue;
    if (right.keys.length !== left.keys.length) return right.keys.length - left.keys.length;
    const tier = CONFIDENCE_TIER[left.basis] - CONFIDENCE_TIER[right.basis];
    if (tier !== 0) return tier;
    return left.proposed_name < right.proposed_name ? -1 : left.proposed_name > right.proposed_name ? 1 : 0;
  });
}

/** The filters §6.7 asks for on the left panel: broker, sector, stock. */
export interface HoldingFilters {
  /** `broker_account_id`, or `"ALL"`. */
  broker: string;
  /** A sector name, or `"ALL"`. */
  sector: string;
  /** Free text over symbol and name. */
  query: string;
}

export const NO_FILTER = "ALL";

export const EMPTY_FILTERS: HoldingFilters = { broker: NO_FILTER, sector: NO_FILTER, query: "" };

export function filterHoldings(
  rows: readonly AggregatedHolding[],
  filters: HoldingFilters,
  sectors: Readonly<Record<string, string>> = {},
): AggregatedHolding[] {
  const needle = filters.query.trim().toLowerCase();
  return rows.filter((row) => {
    if (filters.broker !== NO_FILTER) {
      const held = (row.brokers ?? []).some(
        (line) => String(line.broker.broker_account_id) === filters.broker,
      );
      if (!held) return false;
    }
    if (filters.sector !== NO_FILTER) {
      if (sectors[String(row.instrument.instrument_id)] !== filters.sector) return false;
    }
    if (needle !== "") {
      const haystack = `${row.instrument.symbol} ${row.instrument.name}`.toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

/** Every broker account that appears in the rows, de-duplicated, in first-seen order. */
export function brokersIn(rows: readonly AggregatedHolding[]): BrokerRef[] {
  const seen = new Map<number, BrokerRef>();
  for (const row of rows) {
    for (const line of row.brokers ?? []) {
      if (!seen.has(line.broker.broker_account_id)) seen.set(line.broker.broker_account_id, line.broker);
    }
  }
  return [...seen.values()];
}

/** Every sector present among the rows we have a sector for, sorted. Empty when we know none. */
export function sectorsIn(
  rows: readonly AggregatedHolding[],
  sectors: Readonly<Record<string, string>>,
): string[] {
  const seen = new Set<string>();
  for (const row of rows) {
    const sector = sectors[String(row.instrument.instrument_id)];
    if (sector !== undefined) seen.add(sector);
  }
  return [...seen].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

/** A holding in no capital portfolio. A monitoring view is not an allocation (§4.1). */
export function isUnallocated(row: AggregatedHolding): boolean {
  return !row.allocated;
}

/** The inverse of {@link holdingKeyId}. Returns `null` for anything that is not one. */
export function parseHoldingKeyId(id: string): HoldingKey | null {
  const [instrument, broker] = id.split(":");
  if (instrument === undefined || broker === undefined) return null;
  const instrumentId = Number(instrument);
  const brokerId = Number(broker);
  if (!Number.isInteger(instrumentId) || !Number.isInteger(brokerId)) return null;
  return { instrument_id: instrumentId, broker_account_id: brokerId };
}

/**
 * §6.3's benchmark default is Nifty 500, with a per-portfolio override, so that is the head of
 * this list and the rest are the broad NSE indices a retail holding group is plausibly measured
 * against. A caller with the real index list from the API should pass it instead.
 */
export const DEFAULT_BENCHMARKS: readonly string[] = [
  "Nifty 500",
  "Nifty 50",
  "Nifty Midcap 150",
  "Nifty Smallcap 250",
];

/** §6.7's five starting points for a new portfolio. */
export type NewPortfolioStart = "SUBSCRIBED" | "MY_SCREEN" | "MY_STRATEGY" | "HOLDINGS" | "EMPTY";

/** What the flow hands back on confirm. Not a write — the caller owns that. */
export interface PortfolioDraft {
  start: NewPortfolioStart;
  kind: PortfolioKind;
  name: string;
  benchmark: string;
  keys: HoldingKey[];
  /** The subscribed model / screen / strategy chosen, when the start was one of those. */
  sourceId: string | null;
}
