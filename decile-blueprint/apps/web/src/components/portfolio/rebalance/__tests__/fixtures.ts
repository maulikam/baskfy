import type { RebalanceNameOut, RebalanceOut, TargetWeightOut } from "@baskfy/api-client";

import type { DetailHolding, PortfolioDetail } from "@/lib/portfolio/overview";

/**
 * Schema-exact fixtures for the rebalance preview. No `as unknown as`, no `any`.
 *
 * PC1 found three fields in its own fixtures that `OverviewOut` does not contain, because the
 * objects had been cast into shape. A cast fixture describes a payload the server never sends and
 * takes the type checker out of the one job it is good at here, so every object below is typed as
 * the generated model and `tsc` fails if a field is invented or misspelt.
 *
 * THE BOOK
 * --------
 * Five held instruments, one of them at two broker accounts and one of them unpriced; a screen of
 * five with a hold buffer of two, so the band is ranks 6–7. The numbers are chosen so that the
 * arithmetic has an answer worth asserting: the priced weights add to exactly 1, and the six
 * target weights are `1/6` with the last absorbing the remainder, exactly as
 * `baskfy_core.rank_buffer._target_weights` builds them.
 */

export const TOP_N = 5;
export const HOLD_BUFFER = 2;

const ZERODHA = { broker_account_id: 1, broker_id: "zerodha", label: "Zerodha" };
const UPSTOX = { broker_account_id: 2, broker_id: "upstox", label: "Upstox" };

function line(
  instrument: { instrument_id: number; symbol: string; name: string },
  broker: { broker_account_id: number; broker_id: string; label: string },
  quantity: string,
  weight: string | null,
  over: Partial<DetailHolding> = {},
): DetailHolding {
  return {
    instrument,
    broker,
    quantity,
    weight,
    history_source: "BROKER",
    pending_reconciliation: false,
    price: weight === null ? null : "1000.00",
    value: weight === null ? null : "100000.00",
    avg_price: "900.00",
    ...over,
  };
}

const TCS = { instrument_id: 101, symbol: "TCS", name: "Tata Consultancy Services" };
const INFY = { instrument_id: 102, symbol: "INFY", name: "Infosys" };
const RELIANCE = { instrument_id: 103, symbol: "RELIANCE", name: "Reliance Industries" };
const SUZLON = { instrument_id: 104, symbol: "SUZLON", name: "Suzlon Energy" };
const DHFL = { instrument_id: 105, symbol: "DHFL", name: "Dewan Housing Finance" };

const HDFCBANK = { instrument_id: 201, symbol: "HDFCBANK", name: "HDFC Bank" };
const ITC = { instrument_id: 202, symbol: "ITC", name: "ITC" };
const WIPRO = { instrument_id: 203, symbol: "WIPRO", name: "Wipro" };

export const HOLDINGS: readonly DetailHolding[] = [
  line(TCS, ZERODHA, "100", "0.300000"),
  /* One name, two accounts. The weights add; the labels are both carried, because "where is this
     held" is the part of the brief's broker grouping that Baskfy genuinely knows. */
  line(INFY, ZERODHA, "60", "0.150000"),
  line(INFY, UPSTOX, "40", "0.100000"),
  line(RELIANCE, ZERODHA, "80", "0.200000"),
  /* No price today, so the server sends no weight — and this row must never read as 0%. */
  line(SUZLON, ZERODHA, "500", null, { price: null, value: null }),
  line(DHFL, ZERODHA, "200", "0.250000"),
];

export function detail(over: Partial<PortfolioDetail["summary"]> = {}): PortfolioDetail {
  return {
    holdings: [...HOLDINGS],
    brokers: [ZERODHA, UPSTOX],
    source_panel: {
      execution_note:
        "Baskfy never places an order. A rebalance produces a plan you take to your broker.",
      headline: "Grouped from your Zerodha and Upstox holdings",
      source: "HOLDING_GROUP",
    },
    summary: {
      portfolio_id: 7,
      name: "Momentum 20",
      kind: "CAPITAL",
      source: "HOLDING_GROUP",
      source_badge: "Grouped",
      started_on: "2026-01-01",
      status: "Synced",
      counts_toward_total: true,
      pending_reconciliation: false,
      cash: "0",
      value: "400000.00",
      prices_as_of: "2026-09-10",
      holdings_synced_on: "2026-09-10",
      headline_return: {
        kind: "SINCE_GROUPED",
        label: "Since grouped",
        since: "2026-01-01",
        is_model: false,
        value: "12.50",
      },
      todays_pnl: { amount: "1200.00", label: "today", pct: "0.30" },
      total_pnl: { amount: "40000.00", label: "total" },
      xirr: { label: "XIRR", since: "2026-01-01", value: "18.20" },
      ...over,
    },
  };
}

function named(
  instrument: { instrument_id: number; symbol: string; name: string },
  over: Partial<RebalanceNameOut> = {},
): RebalanceNameOut {
  return { ...instrument, ...over };
}

function target(
  instrument: { instrument_id: number; symbol: string; name: string },
  rank: number,
  weight: string,
  action: TargetWeightOut["action"],
): TargetWeightOut {
  return { ...instrument, rank, weight, action };
}

/** `1/6` rounded down at `WEIGHT_STEP`, five times, with the last name absorbing the remainder. */
const SIXTH = "0.166666";
const REMAINDER = "0.166670";

export function rebalance(over: Partial<RebalanceOut> = {}): RebalanceOut {
  return {
    id: 77,
    as_of: "2026-09-10",
    created_at: "2026-09-10T12:45:00Z",
    data_version: 3,
    delisted_count: 1,
    holdings_count: 5,
    screen_result_count: 30,
    screen: { name: "Momentum 12-1", public_id: "scr_momentum" },
    top_n: TOP_N,
    hold_buffer: HOLD_BUFFER,
    requested_as_of: null,
    entries: [
      named(HDFCBANK, { rank: 2 }),
      named(ITC, { rank: 4 }),
      named(WIPRO, { rank: 5 }),
    ],
    exits: [
      named(DHFL, { rank: null, reason: "delisted" }),
      named(SUZLON, { rank: null, reason: "not_in_screen" }),
    ],
    inside_wrh: [named(RELIANCE, { rank: 6 })],
    holds: [named(TCS, { rank: 1 }), named(INFY, { rank: 3 })],
    target_weights: [
      target(TCS, 1, SIXTH, "hold"),
      target(HDFCBANK, 2, SIXTH, "enter"),
      target(INFY, 3, SIXTH, "hold"),
      target(ITC, 4, SIXTH, "enter"),
      target(WIPRO, 5, SIXTH, "enter"),
      target(RELIANCE, 6, REMAINDER, "hold"),
    ],
    ...over,
  };
}

export const SCREENS = [
  { public_id: "scr_momentum", name: "Momentum 12-1" },
  { public_id: "scr_quality", name: "Quality at a price" },
];

export const IDS = {
  TCS: TCS.instrument_id,
  INFY: INFY.instrument_id,
  RELIANCE: RELIANCE.instrument_id,
  SUZLON: SUZLON.instrument_id,
  DHFL: DHFL.instrument_id,
  HDFCBANK: HDFCBANK.instrument_id,
  ITC: ITC.instrument_id,
  WIPRO: WIPRO.instrument_id,
} as const;
