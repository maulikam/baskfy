import type { Schemas } from "@baskfy/api-client";

import { formatTradeDate } from "@/lib/format";
import type { DetailHolding, DisplayReturn } from "@/lib/portfolio/overview";

/**
 * The presentation layer for `PORTFOLIO_REDESIGN.md` §7 — the portfolio detail page.
 *
 * ## Why this is separate from `overview.ts`
 *
 * `overview.ts` holds what §6's single screen needs and is imported by nine components; §7 needs
 * four more facts and none of §6's tab filtering. They are split so the detail page does not have
 * to grow the overview's module to say anything, and so a change to one screen's vocabulary
 * cannot silently move the other's.
 *
 * Everything §7 shows that the server can decide, the server has already decided:
 * `history_source` states *why* an average price is missing rather than leaving the browser to
 * guess from a null, `headline_return` and `model_return` arrive as two separate figures so
 * criterion 5 cannot be reached by accident, `excluded_note` carries §4.1's sentence, and
 * `SourcePanelOut.headline` is already worded to §9 — never "managed", never "advisory", never
 * "PMS". None of that is recomputed here.
 *
 * What is genuinely the browser's job, and therefore lives here:
 *
 * * **Turning "we do not know" into a sentence.** A null average price is a fact with four
 *   possible causes (`portfolio_holding_history_source_known`: NONE / CAS / BROKER / MANUAL) and
 *   the reader deserves the one that applies. {@link avgPriceReason} is that mapping.
 * * **The benchmark gap as a labelled figure.** `BenchmarkOut.difference` is a bare fraction on
 *   the wire; §11 criterion 3 forbids a bare percentage on the screen, so it is lifted into a
 *   {@link DisplayReturn} before any component can render it.
 * * **Target weight and drift** — see the note on {@link DetailHoldingRow}.
 * * **The two timestamps**, formatted and kept apart (§6.1's rule, which §7's "last sync"
 *   inherits): a price close and a holdings sync are two different clocks and merging them into
 *   one "last updated" is how a stale price hides behind a fresh sync.
 */

export type PortfolioSummary = Schemas["PortfolioSummaryOut"];
export type SourcePanel = Schemas["SourcePanelOut"];
export type Benchmark = Schemas["BenchmarkOut"];
export type BrokerRef = Schemas["BrokerRefOut"];
export type PortfolioSourceKey = Schemas["PortfolioSource"];

/* ------------------------------------------------------------------ *
 * §5.2 / §5.3 — why an average price is missing
 * ------------------------------------------------------------------ */

/**
 * `portfolio_holding.history_source` in the reader's words.
 *
 * §5.2 is explicit that a holding group shows no since-purchase P&L until a CAS import exists,
 * and the API honours that by sending `avg_price = null` and `total_contribution = null` rather
 * than a zero. A zero is the dangerous rendering: it cannot be told apart from a position bought
 * at exactly today's price, so every unimported holding would read as break-even. The em dash is
 * only half the fix — the other half is saying which of the four reasons applies, because "we
 * never had it" and "your broker did not send it" have different remedies.
 */
export const HISTORY_REASONS: Readonly<Record<string, string>> = {
  NONE: "No purchase price has ever been recorded for these shares. Import your account statement and the cost, and the profit since you bought, appear here.",
  CAS: "Your imported account statement did not carry a price for this line.",
  BROKER: "Your broker did not send a purchase price for this line.",
  MANUAL: "This holding was entered by hand and no purchase price came with it.",
};

/** Used when a payload sends a `history_source` this build has never heard of. */
export const HISTORY_REASON_FALLBACK =
  "No purchase price is on record for this holding, so its cost cannot be shown.";

/** The short form for a table cell; the full sentence goes in the footnote and the tooltip. */
export const AVG_PRICE_SHORT = "no purchase price";

/** Why this holding's average price is an em dash. Never returns an empty string. */
export function avgPriceReason(holding: Pick<DetailHolding, "history_source">): string {
  return HISTORY_REASONS[holding.history_source] ?? HISTORY_REASON_FALLBACK;
}

/** True when the payload knows what these shares cost. */
export function hasCostBasis(holding: Pick<DetailHolding, "avg_price">): boolean {
  return holding.avg_price !== null && holding.avg_price !== undefined && holding.avg_price !== "";
}

/** §5.2's rule, as one sentence, for the total-contribution column. */
export const NO_SINCE_PURCHASE =
  "Profit since purchase needs a purchase price. Import your account statement to unlock it.";

/* ------------------------------------------------------------------ *
 * §7 — target weight and drift on a basket-backed portfolio
 * ------------------------------------------------------------------ */

/**
 * §7: *"For basket-backed portfolios add target weight + drift."*
 *
 * `DetailHoldingOut` does not carry a target weight today. The model's weights live on the
 * published basket version, and `GET /portfolio/{id}` joins the ledger, not the catalog — so the
 * field is **optional here rather than invented here**. The columns are rendered for a
 * basket-backed portfolio either way, and a row with no target shows the em dash and says the
 * targets have not been published against this portfolio yet.
 *
 * The alternative — computing a target from the current holdings, or from an equal split across
 * however many names are held — would produce a drift of zero for every row, on every portfolio,
 * forever, and it would look exactly like a portfolio that is perfectly on target. That is the
 * same failure mode as a fabricated zero average price, one column to the right.
 */
export interface TargetWeightBearing {
  readonly target_weight?: string | null;
}

/** A §7 holding row, with the model's target weight when the payload has one. */
export type DetailHoldingRow = DetailHolding & TargetWeightBearing;

/** The model's intended share of this portfolio, as a stored fraction, or `null`. */
export function targetWeightOf(holding: DetailHoldingRow): string | null {
  const target = holding.target_weight;
  return target === null || target === undefined || target === "" ? null : target;
}

/**
 * How far this holding sits from its target, as a signed fraction.
 *
 * `null` whenever either side is unknown — a drift computed against a missing target is a drift
 * equal to the whole weight, which reads as a catastrophe rather than as an absence.
 */
export function driftOf(holding: DetailHoldingRow): number | null {
  const target = targetWeightOf(holding);
  const actual = holding.weight ?? null;
  if (target === null || actual === null || actual === "") return null;
  const targetValue = Number(target);
  const actualValue = Number(actual);
  if (Number.isNaN(targetValue) || Number.isNaN(actualValue)) return null;
  return actualValue - targetValue;
}

/** No target weights have been published against any row on this portfolio. */
export const NO_TARGET_WEIGHTS =
  "The model's target weights have not been published against this portfolio yet, so there is " +
  "nothing to measure the drift against.";

/**
 * True when this portfolio tracks a model whose weights §7 wants compared against.
 *
 * The test is whether the source panel names a basket, not what the source is: a subscribed
 * portfolio whose basket has not been linked has no targets to show, and a strategy-driven one
 * whose basket has been linked does.
 */
export function isBasketBacked(panel: Pick<SourcePanel, "basket_slug" | "basket_name">): boolean {
  const slug = panel.basket_slug ?? null;
  const name = panel.basket_name ?? null;
  return slug !== null || name !== null;
}

/* ------------------------------------------------------------------ *
 * §7 — the benchmark difference, labelled
 * ------------------------------------------------------------------ */

/**
 * `BenchmarkOut.difference` lifted into a figure that cannot be rendered bare.
 *
 * The label names the index and the window, because "+4.70%" beside a portfolio is meaningless
 * until the reader knows it is measured against Nifty 500 and over which stretch of days.
 */
export function benchmarkGap(benchmark: Benchmark): DisplayReturn {
  return {
    label: `Ahead of ${benchmark.name}`,
    value: benchmark.difference ?? null,
    since: benchmark.benchmark.since,
    unavailableReason:
      benchmark.difference === null || benchmark.difference === undefined
        ? `There is not enough overlap with ${benchmark.name} to compare the two yet.`
        : null,
    isModel: false,
  };
}

/* ------------------------------------------------------------------ *
 * §7 — last sync, which is two clocks and not one
 * ------------------------------------------------------------------ */

/** "Valued at close of 21 Aug 2026" — §5.1's sentence, verbatim. */
export function valuedAtLine(summary: Pick<PortfolioSummary, "prices_as_of">): string {
  const date = summary.prices_as_of ?? null;
  return date === null
    ? "No end-of-day valuation has been recorded for this portfolio yet."
    : `Valued at close of ${formatTradeDate(date)}`;
}

/** "Holdings synced: 25 Aug 2026", kept separate from the price date on purpose. */
export function syncedAtLine(summary: Pick<PortfolioSummary, "holdings_synced_on">): string {
  const date = summary.holdings_synced_on ?? null;
  return date === null
    ? "No broker sync has run yet."
    : `Holdings synced: ${formatTradeDate(date)}`;
}

/* ------------------------------------------------------------------ *
 * §4.1 — a monitoring view says so, everywhere
 * ------------------------------------------------------------------ */

/** True when this portfolio must never reach a consolidated total. */
export function isMonitoringView(
  summary: Pick<PortfolioSummary, "counts_toward_total">,
): boolean {
  return summary.counts_toward_total === false;
}
