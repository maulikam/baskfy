import type { AggregatedHolding, PortfolioKind } from "@/lib/portfolio/organize";
import type { PortfolioRow } from "@/lib/portfolio/overview";

/**
 * Schema-exact fixtures for the manage drawer's component tests.
 *
 * No `as unknown as`, no `any` — §6.2 rule 6. PC1 learned why the hard way: three fields its
 * fixtures asserted against (`hero.broker_count`, `hero.cash`, `hero.dividends`) do not exist in
 * `OverviewOut` at all, and the casts hid it. Everything below is typed against the generated
 * schema, so a fixture that describes a payload the API does not send fails `tsc`.
 *
 * The book is deliberately awkward, because the awkward cases are the ones with rules attached:
 * one holding wholly inside a capital portfolio (exclusivity), one split between a portfolio and
 * the free pile (the partial case), one wholly free, and one with no price at all.
 */

export const LONG_TERM = 1;
export const MOMENTUM = 2;
export const WATCHLIST = 3;

function slice(portfolioId: number, name: string, kind: PortfolioKind, quantity: string) {
  return {
    portfolio: { portfolio_id: portfolioId, name, kind, source: "HOLDING_GROUP" as const },
    quantity,
  };
}

function holding(
  instrumentId: number,
  symbol: string,
  name: string,
  spec: {
    quantity: string;
    unallocated: string;
    value?: string | undefined;
    slices?: ReadonlyArray<ReturnType<typeof slice>>;
  },
): AggregatedHolding {
  return {
    instrument: { instrument_id: instrumentId, symbol, name },
    quantity: spec.quantity,
    allocated: (spec.slices ?? []).length > 0,
    pending_reconciliation: false,
    split_across_portfolios: false,
    ...(spec.value === undefined ? {} : { value: spec.value }),
    brokers: [
      {
        broker: { broker_account_id: 11, broker_id: "zerodha", label: "Zerodha" },
        quantity: spec.quantity,
        unallocated_quantity: spec.unallocated,
        history_source: "NONE",
        pending_reconciliation: false,
        ...(spec.value === undefined ? {} : { value: spec.value }),
        allocations: [...(spec.slices ?? [])],
      },
    ],
  };
}

/** Every share is inside Long term — the exclusivity case. */
export const ITC = holding(101, "ITC", "ITC Ltd", {
  quantity: "100",
  unallocated: "0",
  value: "10000",
  slices: [slice(LONG_TERM, "Long term", "CAPITAL", "100")],
});

/** 20 of 50 free, 30 in Long term — the partial case. */
export const HDFC = holding(102, "HDFCBANK", "HDFC Bank", {
  quantity: "50",
  unallocated: "20",
  value: "5000",
  slices: [slice(LONG_TERM, "Long term", "CAPITAL", "30")],
});

/** Wholly unallocated. */
export const INFY = holding(103, "INFY", "Infosys", {
  quantity: "40",
  unallocated: "40",
  value: "8000",
});

/** Unallocated and unpriced — absent is not zero. */
export const TCS = holding(104, "TCS", "Tata Consultancy", {
  quantity: "10",
  unallocated: "10",
});

export const ROWS: readonly AggregatedHolding[] = [ITC, HDFC, INFY, TCS];

export function portfolioRow(over: Partial<PortfolioRow> = {}): PortfolioRow {
  return {
    portfolio_id: LONG_TERM,
    name: "Long term",
    kind: "CAPITAL",
    source: "HOLDING_GROUP",
    source_badge: "Grouped",
    started_on: "2025-04-01",
    value: "480000.00",
    cash: "12000.00",
    counts_toward_total: true,
    status: "Synced",
    holdings_count: 7,
    broker_count: 1,
    pending_reconciliation: false,
    brokers: [{ broker_account_id: 11, broker_id: "zerodha", label: "Zerodha" }],
    todays_pnl: { amount: "1200.00", label: "today" },
    headline_return: {
      kind: "SINCE_GROUPED",
      label: "Since grouped",
      since: "2025-04-01",
      is_model: false,
      value: "14.2",
    },
    ...over,
  };
}

export const CAPITAL: readonly PortfolioRow[] = [
  portfolioRow(),
  portfolioRow({ portfolio_id: MOMENTUM, name: "Momentum", value: "210000.00", holdings_count: 4 }),
];

export const VIEWS: readonly PortfolioRow[] = [
  portfolioRow({
    portfolio_id: WATCHLIST,
    name: "Watchlist",
    kind: "MONITORING",
    counts_toward_total: false,
    excluded_note: "Monitoring view — overlaps with other portfolios, excluded from totals.",
    holdings_count: 5,
  }),
];

export const BROKERS = [
  { broker_account_id: 11, broker_id: "zerodha", label: "Zerodha" },
  { broker_account_id: 12, broker_id: "upstox", label: "Upstox" },
];
