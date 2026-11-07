import { deskForm, deskGet } from "@/lib/desk-actions/client";

/** `GET /desk/swing/data` — the desk's swing view (Rust `lanes/swing/desk.rs::build_view`). Decimals arrive as strings. */
type Dec = string | null;

export interface SwCatalyst {
  headline: string | null;
  published_at: string | null;
  url: string | null;
  earnings_date: string | null;
}

export interface SwPosition {
  id: number;
  instrument_id: number;
  symbol: string;
  setup: string;
  entry_date: string | null;
  entry_avg: Dec;
  quantity_entered: number;
  quantity_open: number;
  initial_stop: Dec;
  stop: Dec;
  gtt_id: string | null;
  gtt_trigger: Dec;
  gtt_armed_at: string | null;
  trail: string;
  partial_done: boolean;
  partial_date: string | null;
  state: string;
  simulated: boolean;
  half_risk: boolean;
  naked?: boolean;
  value_inr?: Dec;
}

export interface SwLine {
  id: number;
  plan_id: string;
  kind: string;
  instrument_id: number;
  symbol: string;
  setup: string | null;
  quantity: number;
  trigger: Dec;
  stop: Dec;
  risk_inr: Dec;
  position_value: Dec;
  trail: string | null;
  note: string | null;
  state: string;
  journal_ref: string | null;
  position_id: number | null;
  as_of: string | null;
  plan_source: string;
  label: string;
  plan_expires_at?: string | null;
  countdown?: string;
  expired?: boolean;
  held?: boolean;
  pct_of_sleeve?: Dec;
  cap_pct?: Dec;
  confirmable?: boolean;
  why_not?: string;
  catalyst?: SwCatalyst | null;
}

export interface SwSkip {
  id: number;
  instrument_id: number | null;
  symbol: string;
  reason: string;
  detail: string | null;
}

export interface SwPlan {
  id: number;
  plan_id: string;
  as_of: string | null;
  source: string;
  built_at: string | null;
  expires_at: string | null;
  gate: string;
  exposure_level: number;
  total_risk_inr: Dec;
  total_new_exposure_inr: Dec;
  expired: boolean;
  countdown: string;
  exits: SwLine[];
  buys: SwLine[];
  pending: SwLine[];
  skips: SwSkip[];
  line_count: number;
}

export interface SwTrigger {
  id: number;
  instrument_id: number;
  symbol: string;
  setup: string;
  raised_at: string | null;
  state: string;
  entry: Dec;
  stop: Dec;
  focus: boolean;
  text: string;
  locked: boolean;
  line: SwLine | null;
  skip: { reason: string; detail: string | null; plan_id: string } | null;
  catalyst?: SwCatalyst | null;
}

export interface SwScanStatus {
  date: string | null;
  provisional: boolean;
  scanned_at: string | null;
  label: string;
  run: { id: number; status: string } | null;
  run_label: string;
  in_flight: boolean;
}

export interface SwingDeskView {
  available: boolean;
  reason: string;
  now: string;
  today: string;
  status: {
    dry_run: boolean;
    execution_enabled: boolean;
    monitor_enabled: boolean;
    simulated: boolean;
    mode_label: string;
    monitor: { state: string; label: string };
    token: { present: boolean; label: string; age_minutes: number | null; expired: boolean };
    session: {
      recorded: boolean;
      mode: string | null;
      signals: number;
      confirms: number;
      fills: number;
      manage_actions: number;
      monitor_ran: boolean;
    };
    scan: SwScanStatus;
  };
  sleeve: {
    capital_inr: Dec;
    risk_per_trade_pct: Dec;
    max_position_pct: Dec;
    max_open_positions: number;
    exposure_level: number;
    first_live_sessions_left: number;
    first_live_header: string;
    configured: boolean;
  } | null;
  sent_lines?: { id: number; symbol: string; quantity: number; journal_ref: string | null; position_id: number | null }[];
  triggers: SwTrigger[];
  plans: { morning: SwPlan | null; preview: SwPlan | null };
  exit_rule: string;
  book: { positions: SwPosition[]; naked_count: number; manage: SwLine[] };
  poll_ms: number;
  window_opens_in_ms: number | null;
  fingerprint: string;
}

/** `ExecOutcome` as `/swing/execute` and `/swing/rearm` return it. */
export interface SwOutcome {
  status: string;
  reason: string;
  simulated: boolean;
  position_id: number | null;
  gtt?: { status?: string } | null;
}

export interface SwCutoff {
  reconciled: number;
  cancelled: number;
  cancel_failed: number;
  slots_freed: number;
}

export function fetchSwingDesk(): Promise<SwingDeskView> {
  return deskGet<SwingDeskView>("/swing/data");
}

/** Places a GTT stop for a naked position; one position, confirm=true, sent once. */
export function rearmGtt(positionId: number): Promise<SwOutcome> {
  return deskForm<SwOutcome>("/swing/rearm", { position_id: String(positionId), confirm: "true" });
}

/** The 10:45 sweep: cancels unfilled remainders of today's sent buys. */
export function cutoffSweep(): Promise<SwCutoff> {
  return deskForm<SwCutoff>("/swing/cutoff", { confirm: "true" });
}

export function num(value: Dec | number | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function inr(value: Dec | number | undefined): string {
  const n = num(value);
  return n === null ? "—" : `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function px(value: Dec | number | undefined): string {
  const n = num(value);
  return n === null ? "—" : n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function pctText(value: Dec | number | undefined): string {
  const n = num(value);
  return n === null ? "—" : `${n.toFixed(2)}%`;
}

export function istTime(stamp: string | null, seconds = false): string {
  if (!stamp) return "—";
  const d = new Date(stamp);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
    hour12: false,
  });
}

export function istStamp(stamp: string | null): string {
  if (!stamp) return "—";
  const d = new Date(stamp);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-GB", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** `m:ss` left on a plan, from its own `expires_at`; `0:00` once past. */
export function countdown(expiresAt: string | null | undefined, nowMs: number): { text: string; expired: boolean } {
  const at = expiresAt ? new Date(expiresAt).getTime() : Number.NaN;
  if (Number.isNaN(at)) return { text: "0:00", expired: true };
  const left = Math.floor((at - nowMs) / 1000);
  if (left <= 0) return { text: "0:00", expired: true };
  return { text: `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`, expired: false };
}
