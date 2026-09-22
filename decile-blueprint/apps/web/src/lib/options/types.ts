/**
 * The options tab's payloads — `services/api/.../routers/options.py`, OP5 (`docs/options/05` §2).
 *
 * UNITS, DECLARED ONCE, HERE
 * --------------------------
 * **Money, prices and index levels are decimal strings** (house rule 9), exactly as the API sends
 * them — `Decimal` is a quoted string on this wire. A component formats them; it never does
 * arithmetic on a float of them.
 *
 * `numbers` and `candidates` on a scan row are the JSON the worker wrote (OP4), passed through:
 * every money figure in them is already a string, and their keys differ per sleeve. They are read
 * through `@/lib/options/view`, which is the one place that knows each sleeve's keys.
 *
 * Rates carry their unit in the name: `*_pct` is a PERCENTAGE (`"0.9400"` is 0.94 %), `er` is a
 * ratio 0–1, `cost_share` is a FRACTION (`"0.0164"` is 1.64 %).
 */

export type SleeveCode = "O1M" | "O1W" | "O2" | "O3A" | "O3B";
export type SleeveGroupCode = "O1M" | "O1W" | "O2" | "O3";

export type EmptyReason =
  "collector_off" | "scan_off" | "no_scan_yet_today" | "never_scanned";

export interface OptionsExpiry {
  expiry_date: string;
  /** `WEEKLY` or `MONTHLY`. */
  kind: string;
  lot_size: number;
  event_day: boolean;
  event_reason: string | null;
}

export interface OptionsRole {
  sleeve: SleeveCode;
  today: boolean;
  reason: string;
  next_date: string | null;
}

export interface OptionsIndexLevel {
  symbol: string;
  level: string | null;
  /** The collector's minute, or null when `level` is a daily close. */
  at: string | null;
  close_of: string | null;
}

export interface OptionsPause {
  scope: string;
  paused_until: string | null;
  paused_reason: string | null;
}

export interface CandidateLeg {
  role: string;
  strike: string;
  /** `CE` or `PE`. */
  option_type: string;
  /** `BUY` or `SELL`. */
  side: string;
  bid: string | null;
  ask: string | null;
  iv: string | null;
  delta: string | null;
  limit_price: string | null;
  expiry?: string;
}

export interface Candidate {
  structure: string;
  expiry: string;
  direction: "UP" | "DOWN" | null;
  legs: CandidateLeg[];
  points: string | null;
  lot_size: number | null;
  lots: number | null;
  sizing_mode: string | null;
  max_loss_inr: string | null;
  round_trip_inr: string | null;
  cost_share: string | null;
  rejection: string | null;
  [key: string]: unknown;
}

export interface OptionsScan {
  sleeve: SleeveCode;
  trade_date: string;
  ts: string;
  state: string;
  reasons: string[];
  numbers: Record<string, unknown>;
  candidates: unknown[];
  as_of_minute: string | null;
  stale: boolean;
}

export interface OptionsPosition {
  session_id: number;
  sleeve: SleeveCode;
  trade_date: string;
  lots: number;
  entry_points: string;
  entry_inr: string;
  opened_at: string;
  hard_exit_at: string;
  last_mark_points: string | null;
  last_mark_at: string | null;
  minutes_to_hard_exit: number | null;
  simulated: boolean;
}

export interface OptionsClosedTrade {
  session_id: number;
  sleeve: SleeveCode;
  trade_date: string;
  structure: string;
  net_pnl_inr: string;
  r_multiple: string;
  closed_reason: string;
  minutes_held: number;
  simulated: boolean;
  sizing_mode: string;
}

export interface OptionsWeekR {
  sleeve: SleeveCode;
  simulated: boolean;
  r: string;
  trades: number;
}

export interface OptionsGate {
  group: SleeveGroupCode;
  execution_enabled: boolean;
  /** `PAPER`, or `DESK_DECIDES` when this server's execution switch is on. Never `LIVE`. */
  mode: string;
}

export interface OptionsToday {
  today: string;
  session_day: boolean;
  market_open: boolean;
  scan_date: string | null;
  as_of_minute: string | null;
  live: boolean;
  stale: boolean;
  empty_reason: EmptyReason | null;
  collect_enabled: boolean;
  scan_enabled: boolean;
  gates: OptionsGate[];
  roles: OptionsRole[];
  expiries: OptionsExpiry[];
  pauses: OptionsPause[];
  nifty: OptionsIndexLevel;
  vix: OptionsIndexLevel;
  scans: OptionsScan[];
  positions: OptionsPosition[];
  closed_today: OptionsClosedTrade[];
  week_r: OptionsWeekR[];
}

export interface OptionsChainRow {
  strike: string;
  /** `CE` or `PE`. */
  option_type: string;
  bid: string | null;
  ask: string | null;
  last: string | null;
  oi: number | null;
  oi_change: number | null;
  iv: string | null;
  delta: string | null;
  gamma: string | null;
  theta: string | null;
}

export interface OptionsChainExpiry {
  expiry: string;
  ts: string;
  spot: string | null;
  forward: string | null;
  atm_strike: string | null;
  atm_iv: string | null;
  pcr_oi: string | null;
  rows: OptionsChainRow[];
}

export interface OptionsChain {
  expiries: OptionsChainExpiry[];
}

export interface OptionsBucket {
  label: string;
  count: number;
  mean_r: string;
}

export interface OptionsSummary {
  sleeve: SleeveCode;
  simulated: boolean;
  sizing_mode: string;
  count: number;
  traded: number;
  win_rate: string | null;
  mean_r: string | null;
  expectancy_inr: string | null;
  expectancy_r: string | null;
  worst_r: string | null;
  max_drawdown_r: string;
  max_drawdown_inr: string;
  r_values: string[];
  by_closed_reason: OptionsBucket[];
}

export interface OptionsProgress {
  group: SleeveGroupCode;
  sessions_needed: number;
  traded_needed: number;
  sessions_done: number;
  traded: number;
}

export interface OptionsJournal {
  summaries: OptionsSummary[];
  skips_by_reason: Record<string, Record<string, number>>;
  progress: OptionsProgress[];
  min_sessions: Record<string, number>;
  recent: OptionsClosedTrade[];
}

export interface OptionsBacktestRun {
  id: number;
  sleeve: SleeveCode;
  tier: number;
  date_from: string;
  date_to: string;
  sessions: number;
  signals: number;
  traded: number;
  skipped_by_reason: Record<string, unknown>;
  win_rate: string | null;
  expectancy_r: string | null;
  net_pnl_inr: string | null;
  max_drawdown_r: string | null;
  /** Verbatim from the row (`docs/options/07` §4). Printed as stored, never paraphrased. */
  caveats: string;
  ran_at: string;
}

export interface OptionsBacktest {
  runs: OptionsBacktestRun[];
  reason: string | null;
}

export interface OptionsEventDay {
  date: string;
  reason: string;
  /** `SEED` (verified against a published calendar) or `USER`. */
  source: string;
  source_url: string | null;
  note: string | null;
  removable: boolean;
}

export interface OptionsCalendar {
  year: number;
  expiries: OptionsExpiry[];
  event_days: OptionsEventDay[];
}

export interface OptionsBookConfig {
  underlying: string;
  account_inr: string;
  margin_pool_inr: string;
  daily_loss_limit_inr: string;
  monthly_pause_inr: string;
  paused_until: string | null;
  paused_reason: string | null;
}

export interface OptionsSleeveConfig {
  sleeve: SleeveGroupCode;
  sleeve_capital_inr: string;
  risk_per_trade_pct: string;
  max_lots: number;
  paper_enabled: boolean;
  hard_exit_time: string;
  paused_until: string | null;
  paused_reason: string | null;
}

export interface OptionsCeilings {
  risk_per_trade_inr_max: string;
  risk_pct_max: string;
  max_lots_max: number;
  book_daily_loss_inr_max: string;
  book_monthly_loss_inr_max: string;
  hard_exit_latest: string;
}

export interface OptionsThreshold {
  section: string;
  key: string;
  value: string;
  anchor: string;
}

export interface OptionsAudit {
  scope: string;
  key: string;
  old_value: string | null;
  new_value: string | null;
  changed_at: string;
  changed_by: string;
}

export interface OptionsConfig {
  seeded: boolean;
  book: OptionsBookConfig | null;
  sleeves: OptionsSleeveConfig[];
  ceilings: OptionsCeilings;
  gates: OptionsGate[];
  thresholds: OptionsThreshold[];
  audit: OptionsAudit[];
}
