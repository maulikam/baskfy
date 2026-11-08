import type {
  OptionsChain,
  OptionsConfig,
  OptionsJournal,
  OptionsToday,
} from "@/lib/options/types";

/**
 * `/options` fixtures, written to the API's own shapes (`routers/options.py`) and to `06` OP5's
 * AC: an O1 `WOULD_SKIP` with all its reasons, an O2 `ARMED` with its distance to trigger, an O3
 * candidate spread — and the state the box is really in when OP5 ships, the collector off.
 */

const ROLES: OptionsToday["roles"] = [
  {
    sleeve: "O1M",
    today: false,
    reason: "NOT_MONTHLY",
    next_date: "2026-09-29",
  },
  { sleeve: "O1W", today: true, reason: "TRADES", next_date: "2026-10-13" },
  { sleeve: "O2", today: true, reason: "TRADES", next_date: null },
  { sleeve: "O3A", today: true, reason: "TRADES", next_date: "2026-09-29" },
  { sleeve: "O3B", today: true, reason: "TRADES", next_date: "2026-09-29" },
];

const BASE: OptionsToday = {
  today: "2026-09-22",
  session_day: true,
  market_open: true,
  scan_date: null,
  as_of_minute: null,
  live: false,
  stale: false,
  empty_reason: "collector_off",
  collect_enabled: false,
  scan_enabled: false,
  gates: [
    { group: "O1M", execution_enabled: false, mode: "PAPER" },
    { group: "O1W", execution_enabled: false, mode: "PAPER" },
    { group: "O2", execution_enabled: false, mode: "PAPER" },
    { group: "O3", execution_enabled: false, mode: "PAPER" },
  ],
  roles: ROLES,
  expiries: [
    {
      expiry_date: "2026-09-22",
      kind: "WEEKLY",
      lot_size: 75,
      event_day: false,
      event_reason: null,
    },
    {
      expiry_date: "2026-09-29",
      kind: "MONTHLY",
      lot_size: 75,
      event_day: false,
      event_reason: null,
    },
    {
      expiry_date: "2026-10-06",
      kind: "WEEKLY",
      lot_size: 75,
      event_day: true,
      event_reason: "RBI_POLICY",
    },
  ],
  pauses: [],
  nifty: {
    symbol: "NIFTY 50",
    level: "25120.35",
    at: null,
    close_of: "2026-09-21",
  },
  vix: {
    symbol: "INDIA VIX",
    level: "11.42",
    at: null,
    close_of: "2026-09-21",
  },
  scans: [],
  positions: [],
  closed_today: [],
  week_r: [],
};

/** The box when OP5 ships: the collector and the scan are off, so there is no row at all. */
export const collectorOff: OptionsToday = BASE;

const SCANS: OptionsToday["scans"] = [
  {
    sleeve: "O1M",
    trade_date: "2026-09-22",
    ts: "2026-09-22T07:44:00Z",
    state: "NOT_TODAY",
    reasons: ["NOT_MONTHLY"],
    numbers: { next_date: "2026-09-29" },
    candidates: [],
    as_of_minute: null,
    stale: false,
  },
  {
    sleeve: "O1W",
    trade_date: "2026-09-22",
    ts: "2026-09-22T07:44:00Z",
    state: "WOULD_SKIP",
    reasons: ["RANGE_TOO_WIDE", "NOT_CONTAINED", "ER_TOO_HIGH"],
    numbers: {
      gap_pct: "0.2100",
      gap_max_pct: "0.75",
      range_pct: "0.9400",
      range_max_pct: "0.80",
      contained: false,
      er: "0.4100",
      er_max: "0.30",
    },
    candidates: [],
    as_of_minute: "2026-09-22T07:44:00Z",
    stale: false,
  },
  {
    sleeve: "O2",
    trade_date: "2026-09-22",
    ts: "2026-09-22T07:44:00Z",
    state: "ARMED",
    reasons: [],
    numbers: {
      trend: "UP",
      ema: "24980.10",
      prev_close: "25101.20",
      gap_pct: "0.1200",
      gap_max_pct: "1.0",
      or_pct: "0.4100",
      or_max_pct: "0.90",
      vix: "11.42",
      vix_max: "22",
      trigger_level: "25162.55",
      last_close: "25131.10",
      distance_points: "31.45",
      distance_pct: "0.1251",
      counter_trend_breaks: [
        { close_time: "10:04:00", close: "25040.15", direction: "DOWN" },
      ],
    },
    candidates: [],
    as_of_minute: "2026-09-22T07:44:00Z",
    stale: false,
  },
  {
    sleeve: "O3A",
    trade_date: "2026-09-22",
    ts: "2026-09-22T07:44:00Z",
    state: "TRIGGERED",
    reasons: [],
    numbers: {
      range_pct: "0.6100",
      range_max_pct: "1.20",
      level_up: "25150.00",
      level_down: "24990.00",
      er_min: "0.40",
    },
    candidates: [
      {
        structure: "DEBIT_SPREAD",
        expiry: "2026-09-22",
        direction: "UP",
        legs: [
          {
            role: "LONG",
            strike: "25100.00",
            option_type: "CE",
            side: "BUY",
            bid: "61.20",
            ask: "61.60",
            iv: "0.118000",
            delta: "0.520000",
            limit_price: "61.60",
          },
          {
            role: "SHORT",
            strike: "25200.00",
            option_type: "CE",
            side: "SELL",
            bid: "34.45",
            ask: "34.85",
            iv: "0.121000",
            delta: "0.330000",
            limit_price: "34.45",
          },
        ],
        points: "27.15",
        lot_size: 75,
        lots: 1,
        sizing_mode: "PAPER_ONE_LOT",
        max_loss_inr: "2036.25",
        round_trip_inr: "118.40",
        cost_share: "0.0164",
        rejection: null,
      },
    ],
    as_of_minute: "2026-09-22T07:44:00Z",
    stale: false,
  },
  {
    sleeve: "O3B",
    trade_date: "2026-09-22",
    ts: "2026-09-22T07:44:00Z",
    state: "DAY_SKIPPED",
    reasons: ["GAP_TOO_SMALL"],
    numbers: {
      gap_pct: "0.1200",
      gap_min_pct: "0.50",
      gap_max_pct: "1.50",
      half_gap: "25110.00",
      held_so_far: true,
    },
    candidates: [],
    as_of_minute: null,
    stale: false,
  },
];

/** `06` OP5's AC morning, live at 13:14. */
export const acMorning: OptionsToday = {
  ...BASE,
  collect_enabled: true,
  scan_enabled: true,
  empty_reason: null,
  scan_date: "2026-09-22",
  as_of_minute: "2026-09-22T07:44:00Z",
  live: true,
  scans: SCANS,
};

/** The same rows read after the close: `As of close`, not live. */
export const afterClose: OptionsToday = {
  ...acMorning,
  market_open: false,
  live: false,
};

export const stale: OptionsToday = { ...acMorning, stale: true };

export const noScanYet: OptionsToday = {
  ...BASE,
  collect_enabled: true,
  scan_enabled: true,
  empty_reason: "no_scan_yet_today",
  scan_date: "2026-09-21",
  scans: SCANS.map((scan) => ({ ...scan, trade_date: "2026-09-21" })),
};

export const emptyChain: OptionsChain = { expiries: [] };

export const emptyJournal: OptionsJournal = {
  summaries: [],
  skips_by_reason: {},
  progress: [
    {
      group: "O1M",
      sessions_needed: 6,
      traded_needed: 3,
      sessions_done: 0,
      traded: 0,
    },
    {
      group: "O1W",
      sessions_needed: 12,
      traded_needed: 6,
      sessions_done: 0,
      traded: 0,
    },
    {
      group: "O2",
      sessions_needed: 60,
      traded_needed: 25,
      sessions_done: 17,
      traded: 9,
    },
    {
      group: "O3",
      sessions_needed: 20,
      traded_needed: 8,
      sessions_done: 0,
      traded: 0,
    },
  ],
  min_sessions: { O1M: 12, O1W: 20, O2: 60, O3A: 20, O3B: 20 },
  recent: [],
};

export const unseededConfig: OptionsConfig = {
  seeded: false,
  book: null,
  sleeves: [],
  ceilings: {
    risk_per_trade_inr_max: "25000",
    risk_pct_max: "1.0",
    max_lots_max: 10,
    book_daily_loss_inr_max: "30000",
    book_monthly_loss_inr_max: "75000",
    hard_exit_latest: "15:00:00",
  },
  gates: BASE.gates,
  thresholds: [
    {
      section: "condor_monthly",
      key: "er_max",
      value: "0.30",
      anchor: "04 §3",
    },
  ],
  audit: [],
};
