import type { TwtDeskPlan, TwtPlanLine, TwtPlanSkip } from "@/lib/twt/desk";
import type {
  TwtBacktest,
  TwtBacktestRun,
  TwtGate,
  TwtHalfSize,
  TwtOpenPosition,
  TwtTightName,
  TwtToday,
} from "@/lib/twt/fetch";

/**
 * Fixtures for `/twt`, built to `docs/twt/03-data-model.md` — TW8.
 *
 * **This module is how the pages exist before their data does.** TW4 (the nightly job) and TW5
 * (the money and the positions) have not landed, so nothing serves the payloads these fixtures
 * imitate. The portfolio panels were built the same way against their schemas before anything was
 * wired, and the lesson from that is the reason these live here rather than inline in one test:
 * a fixture written to the *document* is a check on the document, and the moment the real payload
 * arrives the difference between the two is a diff rather than an argument.
 *
 * Every money and price value is a decimal **string**, and every rate carries its unit in the
 * field name — `*_pct` is already a percentage, `*_fraction` is not. `03` stores them as
 * `numeric`, so a fixture that used a `number` would be testing a contract the API does not have.
 */

export function gate(overrides: Partial<TwtGate> = {}): TwtGate {
  return {
    date: "2026-09-10",
    gate: "OPEN",
    pct_above_dma: "56.4000",
    threshold_pct: "40.00",
    universe_count: 4855,
    with_bar_count: 3120,
    measured_count: 2894,
    above_count: 1632,
    thin_session: false,
    ...overrides,
  };
}

export function tightName(overrides: Partial<TwtTightName> = {}): TwtTightName {
  return {
    instrument_id: 101,
    symbol: "TIGHTCO",
    name: "TIGHTCO LIMITED",
    close_raw: "482.35",
    week_close_0: "482.35",
    week_close_1: "478.90",
    week_close_2: "480.10",
    week_range_pct: "0.7205",
    month_low_ratio: "1.4200",
    sessions_in_state: 4,
    signal_state: "SIGNAL",
    failed_filters: [],
    turnover_avg_20: "341200000",
    locked_upper_circuit: false,
    ...overrides,
  };
}

export function position(overrides: Partial<TwtOpenPosition> = {}): TwtOpenPosition {
  return {
    id: 7,
    instrument_id: 202,
    symbol: "HOLDCO",
    name: "HOLDCO LIMITED",
    entry_date: "2026-03-12",
    entry_avg: "310.55",
    quantity_open: "805",
    high_since: "455.00",
    high_since_date: "2026-08-28",
    gtt_trigger: "364.00",
    gtt_id: "gtt-99",
    next_trigger: "366.40",
    next_trigger_for: "2026-09-10",
    last_price: "441.20",
    unrealised_inr: "105153.25",
    /* A FRACTION: 0.4207 is a 42.07% gain. The page multiplies by a hundred exactly once, in the
       view model — the mistake the portfolio band made on 11 Sep 2026 was doing it twice. */
    unrealised_fraction: "0.420700",
    hold_sessions: 124,
    half_size: true,
    simulated: true,
    ...overrides,
  };
}

/** An open position with nothing resting at the exchange — the one state the method forbids. */
export function nakedPosition(overrides: Partial<TwtOpenPosition> = {}): TwtOpenPosition {
  return position({
    id: 8,
    instrument_id: 203,
    symbol: "NAKEDCO",
    name: "NAKEDCO LIMITED",
    gtt_id: null,
    gtt_trigger: null,
    next_trigger: null,
    next_trigger_for: null,
    ...overrides,
  });
}

export function halfSize(overrides: Partial<TwtHalfSize> = {}): TwtHalfSize {
  return { entries_left: 10, entries_total: 10, execution_enabled: false, ...overrides };
}

export function today(overrides: Partial<TwtToday> = {}): TwtToday {
  return {
    as_of: "2026-09-10",
    gate: gate(),
    tight: [tightName()],
    positions: [position()],
    half_size: halfSize(),
    ...overrides,
  };
}

/** The state `05` §4 says the pages must render honestly: nothing has happened yet. */
export function emptyToday(): TwtToday {
  return {
    as_of: null,
    gate: gate({
      date: null,
      gate: null,
      pct_above_dma: null,
      universe_count: null,
      with_bar_count: null,
      measured_count: null,
      above_count: null,
    }),
    tight: [],
    positions: [],
    half_size: halfSize(),
  };
}

export function backtestRun(overrides: Partial<TwtBacktestRun> = {}): TwtBacktestRun {
  return {
    id: 1,
    source: "PLANT",
    started_at: "2026-09-10T20:10:00+05:30",
    finished_at: "2026-09-10T20:41:00+05:30",
    params: { sleeve_inr: "1000000", start: "2017-01-02", end: "2026-09-09" },
    stats: {
      cagr_pct: "20.90",
      max_drawdown_pct: "-31.40",
      calmar: "0.67",
      sharpe: "1.20",
      trades: 164,
      win_rate_pct: "41.50",
      profit_factor: "2.10",
      avg_hold_sessions: "105",
      exposure_pct: "71.30",
      in_sample_cagr_pct: "11.10",
      out_of_sample_cagr_pct: "36.00",
      gate_off_cagr_pct: "14.20",
      gate_off_max_drawdown_pct: "-41.90",
      yearly: [
        { year: 2024, return_pct: "36.40", trades: 15, win_rate_pct: "46.70" },
        { year: 2025, return_pct: "-4.10", trades: 12, win_rate_pct: "25.00" },
      ],
      equity_curve: [
        { date: "2017-01-02", equity_inr: "1000000.00" },
        { date: "2021-01-04", equity_inr: "2140000.00" },
        { date: "2026-09-09", equity_inr: "5820000.00" },
      ],
    },
    drift: {
      flagged: false,
      cagr_pct_delta: "0.10",
      published_cagr_pct: "20.90",
      run_cagr_pct: "21.00",
      threshold_cagr_points: "1.0",
    },
    error: null,
    ...overrides,
  };
}

export function backtest(runs: readonly TwtBacktestRun[] = [backtestRun()]): TwtBacktest {
  return { runs };
}

export function planLine(overrides: Partial<TwtPlanLine> = {}): TwtPlanLine {
  return {
    id: 1,
    kind: "BUY_AT_OPEN",
    state: "PROPOSED",
    instrument_id: 101,
    symbol: "TIGHTCO",
    name: "TIGHTCO LIMITED",
    quantity: "250",
    value_inr: "120587.50",
    stop_price: "385.88",
    old_trigger: null,
    high_since: null,
    last_price: "482.35",
    cap_note: "Sized down by the limit on how much of one name may be held.",
    rank_key: "341200000",
    ...overrides,
  };
}

export function deskPlan(overrides: Partial<TwtDeskPlan> = {}): TwtDeskPlan {
  const skips: TwtPlanSkip[] = [
    { instrument_id: 404, symbol: "SKIPCO", reason: "BELOW_LIQUIDITY_FLOOR" },
  ];
  return {
    plan_id: "7f3c1a2b-9d4e-4f61-8a0c-2b7d5e6f1234",
    built_at: "2026-09-10T18:00:00+05:30",
    expires_at: "2026-09-10T18:30:00+05:30",
    source: "EVENING",
    gate: "OPEN",
    mode: "DRY_RUN",
    execution_enabled: false,
    session: {
      date: "2026-09-10",
      states: 47,
      signals: 3,
      orders_placed: 1,
      confirms: 1,
      fills: 1,
      ratchets: 4,
      exits: 0,
      naked_at_1515: 1,
    },
    lines: [
      planLine(),
      planLine({
        id: 2,
        kind: "RAISE_GTT_STOP",
        symbol: "HOLDCO",
        name: "HOLDCO LIMITED",
        instrument_id: 202,
        quantity: "805",
        value_inr: null,
        stop_price: "366.40",
        old_trigger: "364.00",
        high_since: "458.00",
        last_price: "441.20",
        cap_note: null,
        rank_key: null,
      }),
      planLine({
        id: 3,
        kind: "ARM_GTT",
        symbol: "NAKEDCO",
        name: "NAKEDCO LIMITED",
        instrument_id: 203,
        quantity: "805",
        value_inr: null,
        stop_price: "352.96",
        old_trigger: null,
        high_since: "441.20",
        last_price: "441.20",
        cap_note: null,
        rank_key: null,
      }),
    ],
    skips,
    positions: [position(), nakedPosition()],
    sweep: { at: "2026-09-10T15:15:00+05:30", naked: [{ position_id: 8, symbol: "NAKEDCO" }] },
    ...overrides,
  };
}
