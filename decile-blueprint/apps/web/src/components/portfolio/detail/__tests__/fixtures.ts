import type { Schemas } from "@baskfy/api-client";

import type { DetailHoldingRow, PortfolioSummary, SourcePanel } from "@/lib/portfolio/detail-view";
import type { ActivityItem, NavSeries, PortfolioDetail } from "@/lib/portfolio/overview";

/**
 * Payloads for PC3's tests, typed from the generated `Schemas` and nothing else.
 *
 * No `as unknown as`, no `any`, no partial object standing in for a response (house rule 3, and
 * §6.2 rule 6). PC1 found the cost of the alternative the hard way: both of its test files
 * asserted against objects cast with `as unknown as Overview`, and three fields in them did not
 * exist in the schema at all. A fixture that lies about the payload makes every test over it a
 * test of a product that does not exist. Typed this way, `tsc` fails the moment the router
 * renames a field, which is a compile error rather than a browser bug.
 *
 * ## Two fixtures, deliberately at the extremes
 *
 * {@link richDetail} has every optional field populated: cost basis, prices, targets, a benchmark,
 * a model return. {@link barrenDetail} has none of them, plus no brokers, no valuation date and no
 * sync date. The barren one is the important one. It is the payload a brand-new portfolio
 * actually sends, it is the payload that produced every "—" the brief forbids, and it is what
 * `unavailable.test.tsx` renders all eight tabs against.
 *
 * Rates are **fractions**, because that is how `baskfy_core.portfolio_nav` stores them: `0.1842`
 * is 18.42%. Money is a decimal string, never a number (house rule 9).
 */

export const ZERODHA: Schemas["BrokerRefOut"] = {
  broker_account_id: 11,
  broker_id: "zerodha",
  label: "Zerodha",
};

export const UPSTOX: Schemas["BrokerRefOut"] = {
  broker_account_id: 12,
  broker_id: "upstox",
  label: "Upstox",
};

const EXECUTION_NOTE =
  "Baskfy never places an order. A rebalance produces a plan you take to your broker.";

export function holding(overrides: Partial<DetailHoldingRow> = {}): DetailHoldingRow {
  return {
    instrument: { instrument_id: 501, symbol: "HDFCBANK", name: "HDFC Bank" },
    broker: ZERODHA,
    quantity: "320",
    avg_price: "1420.50",
    price: "1610.00",
    value: "515200.00",
    weight: "0.520000",
    todays_contribution: "960.00",
    total_contribution: "60640.00",
    first_bought_on: "2025-04-02",
    history_source: "CAS",
    pending_reconciliation: false,
    ...overrides,
  };
}

/**
 * Four holdings that between them exercise every cell state the table has.
 *
 * TCS is the ordinary row. INFY is a loser, which the winners/losers filters need. WIPRO has no
 * cost basis at all and no target, because `history_source = "BROKER"` is the commonest real
 * case. RELIANCE could not be priced, which is the row that must never be counted as zero.
 */
export const RICH_HOLDINGS: readonly DetailHoldingRow[] = [
  holding({
    instrument: { instrument_id: 501, symbol: "HDFCBANK", name: "HDFC Bank" },
    quantity: "320",
    avg_price: "1420.50",
    price: "1610.00",
    value: "515200.00",
    weight: "0.500000",
    target_weight: "0.400000",
    todays_contribution: "960.00",
    total_contribution: "60640.00",
  }),
  holding({
    instrument: { instrument_id: 502, symbol: "TCS", name: "Tata Consultancy Services" },
    broker: ZERODHA,
    quantity: "80",
    avg_price: "3100.00",
    price: "3450.00",
    value: "276000.00",
    weight: "0.270000",
    target_weight: "0.300000",
    todays_contribution: "-1200.00",
    total_contribution: "28000.00",
    first_bought_on: "2025-06-11",
  }),
  holding({
    instrument: { instrument_id: 503, symbol: "INFY", name: "Infosys" },
    broker: UPSTOX,
    quantity: "150",
    avg_price: "1600.00",
    price: "1480.00",
    value: "222000.00",
    weight: "0.215000",
    target_weight: "0.200000",
    todays_contribution: "-450.00",
    total_contribution: "-18000.00",
    first_bought_on: "2026-03-02",
  }),
  holding({
    instrument: { instrument_id: 504, symbol: "WIPRO", name: "Wipro" },
    broker: UPSTOX,
    quantity: "90",
    avg_price: null,
    price: "265.00",
    value: "23850.00",
    weight: "0.015000",
    todays_contribution: "90.00",
    total_contribution: null,
    first_bought_on: null,
    history_source: "BROKER",
  }),
  holding({
    instrument: { instrument_id: 505, symbol: "RELIANCE", name: "Reliance Industries" },
    broker: ZERODHA,
    quantity: "40",
    avg_price: "2800.00",
    price: null,
    value: null,
    weight: null,
    todays_contribution: null,
    total_contribution: null,
    first_bought_on: "2024-12-01",
    history_source: "CAS",
    pending_reconciliation: true,
  }),
];

export function richSummary(overrides: Partial<PortfolioSummary> = {}): PortfolioSummary {
  return {
    portfolio_id: 7,
    name: "Momentum 30",
    kind: "CAPITAL",
    source: "SUBSCRIBED",
    source_badge: "Subscribed model by Bramha Research",
    publisher: "Bramha Research",
    started_on: "2025-04-01",
    value: "1037050.00",
    cash: "42500.00",
    invested: "870000.00",
    counts_toward_total: true,
    status: "On target",
    pending_reconciliation: false,
    prices_as_of: "2026-09-10",
    holdings_synced_on: "2026-09-10",
    todays_pnl: {
      label: "Change since the previous close",
      amount: "-600.00",
      pct: "-0.000578",
      since: "2026-09-09",
    },
    total_pnl: {
      label: "Total P&L since purchase",
      amount: "167050.00",
      pct: "0.192011",
      since: "2025-04-01",
    },
    headline_return: {
      kind: "TWR_SINCE_SUBSCRIBED",
      label: "TWR since you subscribed",
      since: "2025-04-01",
      value: "0.184200",
      is_model: false,
    },
    model_return: {
      kind: "TWR_SINCE_SUBSCRIBED",
      label: "Model TWR since you subscribed",
      since: "2025-04-01",
      value: "0.213000",
      is_model: true,
    },
    xirr: {
      label: "XIRR since your first cash assignment",
      since: "2025-04-01",
      value: "0.171200",
    },
    benchmark: {
      name: "Nifty 500",
      difference: "0.047000",
      portfolio: {
        kind: "TWR_SINCE_SUBSCRIBED",
        label: "Your TWR over this range",
        since: "2025-04-01",
        value: "0.184200",
        is_model: false,
      },
      benchmark: {
        kind: "TWR_SINCE_SUBSCRIBED",
        label: "Nifty 500 over this range",
        since: "2025-04-01",
        value: "0.137200",
        is_model: false,
      },
    },
    ...overrides,
  };
}

export const BASKET_PANEL: SourcePanel = {
  source: "SUBSCRIBED",
  headline: "Subscribed model by Bramha Research",
  publisher: "Bramha Research",
  basket_slug: "momentum-30",
  basket_name: "Momentum 30",
  brokers: [ZERODHA, UPSTOX],
  execution_note: EXECUTION_NOTE,
};

export const GROUP_PANEL: SourcePanel = {
  source: "HOLDING_GROUP",
  headline: "Holdings you grouped on 2026-02-14",
  grouped_on: "2026-02-14",
  brokers: [ZERODHA],
  execution_note: EXECUTION_NOTE,
};

export function richDetail(overrides: Partial<PortfolioDetail> = {}): PortfolioDetail {
  return {
    summary: richSummary(),
    source_panel: BASKET_PANEL,
    brokers: [ZERODHA, UPSTOX],
    holdings: [...RICH_HOLDINGS],
    ...overrides,
  };
}

/** The same book, grouped by hand: no basket, therefore no target weight anywhere. */
export function groupedDetail(): PortfolioDetail {
  return {
    summary: richSummary({
      source: "HOLDING_GROUP",
      source_badge: "Holdings you grouped",
      publisher: null,
      model_return: null,
    }),
    source_panel: GROUP_PANEL,
    brokers: [ZERODHA],
    holdings: RICH_HOLDINGS.map((row) => {
      const { target_weight: _target, ...rest } = row;
      return rest;
    }),
  };
}

/**
 * A brand-new portfolio: nothing priced, nothing bought, nothing synced, no broker.
 *
 * Every optional field is absent, including `invested_unavailable_reason`, so the fallback inside
 * `metric()` is exercised rather than assumed.
 */
export function barrenDetail(): PortfolioDetail {
  return {
    summary: {
      portfolio_id: 9,
      name: "New group",
      kind: "CAPITAL",
      source: "HOLDING_GROUP",
      source_badge: "Holdings you grouped",
      started_on: "2026-09-01",
      value: "0",
      cash: "0",
      counts_toward_total: true,
      status: "Not started",
      pending_reconciliation: false,
      todays_pnl: { label: "Change since the previous close" },
      total_pnl: { label: "Total P&L since purchase" },
      headline_return: {
        kind: "SINCE_GROUPED",
        label: "Return since you grouped these",
        since: "2026-09-01",
        is_model: false,
        unavailable_reason: "There is not enough history to measure a return yet.",
      },
      xirr: { label: "XIRR since your first cash assignment" },
    },
    source_panel: {
      source: "HOLDING_GROUP",
      headline: "Holdings you grouped",
      execution_note: EXECUTION_NOTE,
    },
    brokers: [],
    holdings: [
      {
        instrument: { instrument_id: 900, symbol: "SUZLON", name: "Suzlon Energy" },
        broker: ZERODHA,
        quantity: "1000",
        history_source: "NONE",
        pending_reconciliation: false,
      },
    ],
  };
}

/** A portfolio with a name and nothing in it at all. */
export function emptyDetail(): PortfolioDetail {
  const base = barrenDetail();
  return { ...base, holdings: [] };
}

/* ------------------------------------------------------------------ *
 * The valuation series
 * ------------------------------------------------------------------ */

function indexPoint(on: string, index: string, peak: string, drawdown: string) {
  return { on, index, peak, drawdown };
}

/**
 * Eight months of sessions, one per month end plus a mid-month print, with a real fall in it.
 *
 * Short enough to read in a fixture and long enough that the monthly grid, the calendar year, the
 * drawdown episodes and the shortest rolling window all have something to say. The longer rolling
 * windows deliberately do not: a window longer than the series must report that rather than
 * measure over whatever is there, and that is only testable against a series that is too short.
 */
const INDEX_SERIES: readonly { on: string; index: string; peak: string; drawdown: string }[] = [
  indexPoint("2026-01-30", "100.000000", "100.000000", "0.000000"),
  indexPoint("2026-02-27", "104.000000", "104.000000", "0.000000"),
  indexPoint("2026-03-16", "98.800000", "104.000000", "-0.050000"),
  indexPoint("2026-03-31", "101.920000", "104.000000", "-0.020000"),
  indexPoint("2026-04-30", "106.000000", "106.000000", "0.000000"),
  indexPoint("2026-05-29", "103.000000", "106.000000", "-0.028302"),
  indexPoint("2026-06-30", "109.000000", "109.000000", "0.000000"),
  indexPoint("2026-07-31", "113.000000", "113.000000", "0.000000"),
  indexPoint("2026-08-31", "111.000000", "113.000000", "-0.017699"),
];

export function richNav(overrides: Partial<NavSeries> = {}): NavSeries {
  const points = INDEX_SERIES.map((point, position) => ({
    on: point.on,
    /* Two decimals, like the API: `money()` quantizes every rupee figure before it is
       serialised, and a fixture that drops them is a fixture testing a payload nobody sends. */
    value: `${900000 + position * 15000}.00`,
    cash: "42500.00",
    net_flow: position === 2 ? "50000.00" : position === 6 ? "-10000.00" : "0.00",
    pending_reconciliation: false,
  }));

  return {
    range: "1Y",
    pending_reconciliation: false,
    portfolio_id: 7,
    from_on: "2026-01-30",
    to_on: "2026-08-31",
    total_return: {
      label: "TWR over this range",
      since: "2026-01-30",
      value: "0.110000",
    },
    points,
    drawdown: [...INDEX_SERIES],
    max_drawdown: { drawdown: "-0.050000", peak_on: "2026-02-27", trough_on: "2026-03-16" },
    daily_pnl: [
      { on: "2026-03-16", amount: "-52000.00", pct: "-0.050000" },
      { on: "2026-04-30", amount: "31000.00", pct: "0.031000" },
      { on: "2026-08-31", amount: "-4000.00", pct: "-0.004000" },
    ],
    benchmark: {
      name: "Nifty 500",
      difference: "0.047000",
      portfolio: {
        kind: "TWR_SINCE_SUBSCRIBED",
        label: "Your TWR over this range",
        since: "2026-01-30",
        value: "0.110000",
        is_model: false,
      },
      benchmark: {
        kind: "TWR_SINCE_SUBSCRIBED",
        label: "Nifty 500 over this range",
        since: "2026-01-30",
        value: "0.063000",
        is_model: false,
      },
      points: INDEX_SERIES.map((point, position) => ({
        on: point.on,
        value: `${100 + position}.00`,
        cash: "0.00",
        net_flow: "0.00",
        pending_reconciliation: false,
      })),
    },
    ...overrides,
  };
}

/** A series covering more than a year, so the compound annual rate has a window it may use. */
export function longNav(): NavSeries {
  const base = richNav();
  return { ...base, from_on: "2025-01-30", to_on: "2026-08-31" };
}

/* ------------------------------------------------------------------ *
 * Activity
 * ------------------------------------------------------------------ */

export const RICH_ACTIVITY: readonly ActivityItem[] = [
  {
    on: "2026-08-28",
    kind: "BUY",
    description: "Bought 40 HDFCBANK at 1,590.00",
    amount: "-63600.00",
    quantity: "40",
    broker: ZERODHA,
    instrument: { instrument_id: 501, symbol: "HDFCBANK", name: "HDFC Bank" },
    is_pnl_event: true,
  },
  {
    on: "2026-07-14",
    kind: "DIVIDEND",
    description: "Dividend from TCS",
    amount: "2400.00",
    broker: ZERODHA,
    is_pnl_event: true,
  },
  {
    on: "2026-06-02",
    kind: "CORPORATE_ACTION",
    description: "INFY 1:1 bonus issue",
    quantity: "150",
    broker: UPSTOX,
    is_pnl_event: false,
  },
  {
    on: "2026-05-18",
    kind: "RECONCILIATION",
    description: "Quantity at the broker did not match the ledger",
    broker: UPSTOX,
    reconciliation_state: "OPEN",
    reconciliation_item_id: 44,
    is_pnl_event: false,
  },
];
