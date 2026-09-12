import type {
  Attention,
  Hero,
  NavSeries,
  Overview,
  PortfolioRow,
} from "@/lib/portfolio/overview";

/**
 * A payload shaped exactly like `GET /api/v1/portfolio/overview` answers.
 *
 * Built from the generated `Schemas` types, so a field the router renames breaks these tests at
 * compile time rather than at runtime in a browser. The numbers are the ones the assertions read;
 * rates are **fractions** (`0.1842` is 18.42%), because that is how `portfolio_nav` stores them.
 *
 * The account deliberately contains one of each thing the screen has a rule about: a subscribed
 * model with a publisher and a separate model figure (§11 criterion 5), a holding group whose
 * headline return is unavailable with a reason (§5.2's CAS caveat), and a monitoring view that
 * must never reach a total (§11 criterion 2).
 */

const BROKER = { broker_account_id: 11, broker_id: "zerodha", label: "Zerodha" };
const SECOND_BROKER = { broker_account_id: 12, broker_id: "upstox", label: "Upstox" };

export const SUBSCRIBED_ROW: PortfolioRow = {
  portfolio_id: 1,
  name: "Momentum 30",
  kind: "CAPITAL",
  source: "SUBSCRIBED",
  source_badge: "Subscribed model by Bramha Research",
  publisher: "Bramha Research",
  counts_toward_total: true,
  value: "480000.00",
  cash: "2500.00",
  started_on: "2025-04-01",
  status: "On target",
  todays_pnl: {
    label: "Today, against yesterday's close",
    amount: "3120.00",
    pct: "0.0065",
    since: "2026-08-20",
  },
  headline_return: {
    kind: "TWR_SINCE_SUBSCRIBED",
    label: "TWR since you subscribed",
    since: "2025-04-01",
    value: "0.1842",
    is_model: false,
  },
  model_return: {
    kind: "TWR_SINCE_SUBSCRIBED",
    label: "Model TWR since you subscribed",
    since: "2025-04-01",
    value: "0.2130",
    is_model: true,
  },
  brokers: [BROKER, SECOND_BROKER],
  broker_count: 2,
  holdings_count: 30,
  pending_reconciliation: false,
};

export const HOLDING_GROUP_ROW: PortfolioRow = {
  portfolio_id: 2,
  name: "Long-term core",
  kind: "CAPITAL",
  source: "HOLDING_GROUP",
  source_badge: "Holding group",
  counts_toward_total: true,
  value: "220000.00",
  cash: "0.00",
  started_on: "2026-02-14",
  status: "Synced",
  todays_pnl: {
    label: "Today, against yesterday's close",
    amount: "-840.00",
    pct: "-0.0038",
    since: "2026-08-20",
  },
  headline_return: {
    kind: "SINCE_GROUPED",
    label: "Since grouped",
    since: "2026-02-14",
    value: null,
    is_model: false,
    unavailable_reason:
      "We have no purchase prices for these shares yet. Import your account statement to unlock it.",
  },
  brokers: [BROKER],
  broker_count: 1,
  holdings_count: 8,
  pending_reconciliation: false,
};

export const MONITORING_ROW: PortfolioRow = {
  portfolio_id: 3,
  name: "Defence stocks",
  kind: "MONITORING",
  source: "HOLDING_GROUP",
  source_badge: "Holding group",
  counts_toward_total: false,
  excluded_note: "Monitoring view — overlaps with other portfolios, excluded from totals.",
  value: "150000.00",
  cash: "0.00",
  started_on: "2026-05-02",
  status: "Synced",
  todays_pnl: {
    label: "Today, against yesterday's close",
    amount: "410.00",
    pct: "0.0027",
    since: "2026-08-20",
  },
  headline_return: {
    kind: "SINCE_GROUPED",
    label: "Since grouped",
    since: "2026-05-02",
    value: "0.0412",
    is_model: false,
  },
  brokers: [BROKER],
  broker_count: 1,
  holdings_count: 5,
  pending_reconciliation: false,
};

export const HERO: Hero = {
  current_value: "702500.00",
  holdings_without_cost_basis: 0,
  pending_reconciliation: false,
  todays_pnl: {
    label: "Today, against yesterday's close",
    amount: "2280.00",
    pct: "0.0033",
    since: "2026-08-20",
  },
  total_pnl: {
    label: "Total, since you started",
    amount: "94300.00",
    pct: "0.1553",
    since: "2025-04-01",
  },
  xirr: {
    label: "XIRR since your first purchase",
    since: "2025-04-01",
    value: "0.1712",
  },
  twr: {
    label: "TWR since your first purchase",
    since: "2025-04-01",
    value: "0.1490",
  },
  invested: "608200.00",
  secondary: {
    cash: "2500.00",
    realised_pnl: { label: "Realised", amount: "12000.00" },
    unrealised_pnl: { label: "Unrealised", amount: "82300.00" },
    dividends: "4100.00",
    broker_count: 2,
  },
};

export const CHART: NavSeries = {
  range: "1Y",
  pending_reconciliation: false,
  from_on: "2025-08-21",
  to_on: "2026-08-21",
  total_return: {
    label: "TWR over this range",
    since: "2025-08-21",
    value: "0.1490",
  },
  points: [
    { on: "2026-08-19", value: "690000.00", cash: "2500.00", net_flow: "0.00", pending_reconciliation: false },
    { on: "2026-08-20", value: "700220.00", cash: "2500.00", net_flow: "0.00", pending_reconciliation: false },
    { on: "2026-08-21", value: "702500.00", cash: "2500.00", net_flow: "0.00", pending_reconciliation: false },
  ],
  drawdown: [
    { on: "2026-08-19", index: "100.000000", peak: "100.000000", drawdown: "0.000000" },
    { on: "2026-08-20", index: "101.481159", peak: "101.481159", drawdown: "0.000000" },
    { on: "2026-08-21", index: "101.811594", peak: "101.811594", drawdown: "0.000000" },
  ],
  benchmark: {
    name: "Nifty 500",
    portfolio: {
      kind: "TWR_SINCE_CREATED",
      label: "Your TWR over this range",
      since: "2025-08-21",
      value: "0.1490",
      is_model: false,
    },
    benchmark: {
      kind: "TWR_SINCE_CREATED",
      label: "Nifty 500 over this range",
      since: "2025-08-21",
      value: "0.1020",
      is_model: false,
    },
    difference: "0.0470",
    points: [
      { on: "2026-08-19", value: "23100.00", cash: "0.00", net_flow: "0.00", pending_reconciliation: false },
      { on: "2026-08-20", value: "23260.00", cash: "0.00", net_flow: "0.00", pending_reconciliation: false },
      { on: "2026-08-21", value: "23180.00", cash: "0.00", net_flow: "0.00", pending_reconciliation: false },
    ],
  },
  max_drawdown: { drawdown: "-0.0842", peak_on: "2026-01-04", trough_on: "2026-03-18" },
};

export const ATTENTION: Attention[] = [
  {
    kind: "HOLDINGS_UNALLOCATED",
    count: 12,
    message: "12 holdings are not in any portfolio yet.",
    since: "2026-08-01",
  },
  {
    kind: "BROKER_CONNECTION_EXPIRED",
    count: 1,
    message: "Your Upstox connection needs reconnecting.",
  },
];

export const OVERVIEW: Overview = {
  prices_as_of: "2026-08-21",
  prices_label: "Prices: close of 2026-08-21",
  holdings_synced_on: "2026-08-25",
  holdings_synced_label: "Holdings synced: 2026-08-25",
  sync_summary: "Upstox has never synced",
  sync_status: [
    { broker: BROKER, label: "Holdings synced: 2026-08-25", synced_on: "2026-08-25" },
    { broker: SECOND_BROKER, label: "Holdings not synced yet", synced_on: null },
  ],
  hero: HERO,
  chart: CHART,
  attention: ATTENTION,
  portfolios: [SUBSCRIBED_ROW, HOLDING_GROUP_ROW],
  monitoring_views: [MONITORING_ROW],
  monitoring_excluded_note:
    "Monitoring view — overlaps with other portfolios, excluded from totals.",
  open_reconciliation_count: 0,
  unallocated: {
    cash: "2500.00",
    holdings_count: 12,
    holdings_value: "150000.00",
    total_value: "152500.00",
    cta: "Organize into portfolios",
    pending_reconciliation: false,
  },
};

/** §11 criterion 8: no broker, no portfolio, nothing loose. */
export const EMPTY_OVERVIEW: Overview = {
  prices_as_of: null,
  prices_label: "No closing prices yet",
  holdings_synced_on: null,
  holdings_synced_label: "Holdings not synced yet",
  sync_summary: "No broker connected",
  sync_status: [],
  hero: {
    current_value: "0.00",
    holdings_without_cost_basis: 0,
    pending_reconciliation: false,
    todays_pnl: {
      label: "Today, against yesterday's close",
      amount: null,
      pct: null,
      unavailable_reason: "There is no previous close to compare with yet.",
    },
    total_pnl: {
      label: "Total, since you started",
      amount: null,
      pct: null,
      unavailable_reason: "Nothing has been recorded yet.",
    },
    xirr: {
      label: "XIRR since your first purchase",
      value: null,
      unavailable_reason: "We need at least one cash flow to compute this.",
    },
    twr: {
      label: "TWR since your first purchase",
      value: null,
      unavailable_reason: "We need at least two valuations to compute this.",
    },
    invested: null,
    invested_unavailable_reason: "No purchases have been recorded.",
    secondary: {
      cash: "0.00",
      realised_pnl: { label: "Realised", amount: null },
      unrealised_pnl: { label: "Unrealised", amount: null },
      dividends: "0.00",
      broker_count: 0,
    },
  },
  chart: {
    range: "1Y",
    total_return: {
      label: "TWR over this range",
      value: null,
      unavailable_reason: "There are no end-of-day valuations yet.",
    },
    points: [],
    drawdown: [],
    pending_reconciliation: false,
  },
  attention: [],
  portfolios: [],
  monitoring_views: [],
  monitoring_excluded_note:
    "Monitoring view — overlaps with other portfolios, excluded from totals.",
  open_reconciliation_count: 0,
  unallocated: {
    cash: "0.00",
    holdings_count: 0,
    holdings_value: "0.00",
    total_value: "0.00",
    cta: "Organize into portfolios",
    pending_reconciliation: false,
  },
};
