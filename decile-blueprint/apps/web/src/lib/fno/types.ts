/**
 * The wire shapes of `GET /fno/overnight` and `GET /fno/info` (FO5, `services/api` `fno_read.py`).
 * Every money figure is a decimal string (house rule 9); `detail` is the scan's JSONB passed
 * through as the worker wrote it.
 */

export type Decimalish = string | null;

export interface FnoScan {
  sleeve: string;
  trade_date: string;
  symbol: string;
  state: string;
  reasons: string[];
  detail: Record<string, unknown>;
  credit: Decimalish;
  max_loss_per_lot: Decimalish;
  cost_share: Decimalish;
  iv: Decimalish;
  rv20: Decimalish;
  iv_rv: Decimalish;
}

export interface FnoMark {
  trade_date: string;
  mark_points: string;
  pnl_inr: string;
  pnl_r: Decimalish;
  stop_price: Decimalish;
  detail: Record<string, unknown>;
}

export interface FnoPosition {
  id: number;
  sleeve: string;
  symbol: string;
  structure: string;
  entry_plan_id: string;
  legs: Record<string, unknown>;
  lots: number;
  lot_size: number;
  entry_price: Decimalish;
  entry_credit: Decimalish;
  max_loss_inr: Decimalish;
  profit_take_points: Decimalish;
  loss_close_points: Decimalish;
  stop_price: Decimalish;
  gtt_id: string | null;
  hard_exit_date: string | null;
  next_roll_date: string | null;
  opened_on: string;
  sessions_held: number | null;
  simulated: boolean;
  mark: FnoMark | null;
}

export interface FnoJournalRow {
  position_id: number;
  sleeve: string;
  symbol: string;
  structure: string;
  opened_on: string;
  closed_on: string;
  net_pnl_inr: string;
  costs_inr: string;
  r_multiple: string;
  closed_reason: string;
  sessions_held: number;
  rolls: number;
  simulated: boolean;
  sizing_mode: string;
}

export interface FnoBacktest {
  family: string;
  tier: string;
  caveat: string;
  sample_from: string;
  sample_to: string;
  n: number;
  net_r: Decimalish;
  gross_r: Decimalish;
  per_year: Record<string, unknown>;
  slippage_source: string;
  run_at: string;
}

export interface FnoUnderlying {
  symbol: string;
  sleeve: string;
  level: { level: Decimalish; close_of: string | null; live_symbol: string };
  scan: FnoScan | null;
  next_entry_date: string | null;
}

export interface FnoTally {
  sleeve: string;
  target: string;
  closed: number;
  opened_cycles: number;
  rolls: number;
  first_opened: string | null;
  violations: number | null;
}

export interface FnoEvidence {
  tier: string;
  line: string;
  loss_close_line: string;
  slippage_note: string;
  caveat: string;
  n: number;
  run_date: string;
  sample: string;
  why_paper: string[];
  backtests: FnoBacktest[];
  tally: FnoTally[];
}

export interface FnoF2 {
  research_line: string;
  scan_date: string | null;
  candidates: FnoScan[];
  sleeve_row: FnoScan | null;
  state_counts: Record<string, number>;
  open: FnoPosition[];
  closed: FnoJournalRow[];
}

export type FnoEmptyReason = "scan_off" | "never_scanned";

export interface FnoOvernight {
  today: string;
  next_session: string | null;
  scan_date: string | null;
  empty_reason: FnoEmptyReason | null;
  scan_enabled: boolean;
  monitor_enabled: boolean;
  gates: { group: string; mode: "PAPER" | "LIVE" }[];
  underlyings: FnoUnderlying[];
  hard_exit_tomorrow: FnoPosition[];
  open_structures: FnoPosition[];
  journal: FnoJournalRow[];
  evidence: FnoEvidence;
  f2: FnoF2;
}

export interface FnoInfoRow {
  symbol: string;
  lot_size: number | null;
  near_monthly: string | null;
  days_to_near_monthly: number | null;
  fut_settle: Decimalish;
  basis_ann: Decimalish;
  oi_change_5d_pct: Decimalish;
  iv: Decimalish;
  rv20: Decimalish;
  iv_rv: Decimalish;
  iv_pct_1y: Decimalish;
  iv_sessions_1y: number;
  in_ban: boolean;
  fut_turnover_20d: Decimalish;
}

export interface FnoVerdict {
  family: string;
  best_variant: string;
  net_r: string;
  robust: string;
  verdict: string;
  retest_families: string[];
}

export interface FnoInfo {
  as_of: string | null;
  rows: FnoInfoRow[];
  families: { verdict: FnoVerdict; latest_retest: FnoBacktest | null }[];
  research_run_date: string;
  research_sample: string;
  spread_sample: {
    sessions: number;
    first: string | null;
    last: string | null;
    symbols: number;
  };
  ingest: {
    latest_date: string | null;
    latest_status: string | null;
    missing_days: string[];
    ban_for_session: string | null;
    ban_symbols: string[];
  };
}
