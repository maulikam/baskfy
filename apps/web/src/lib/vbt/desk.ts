import { deskForm, deskGet } from "@/lib/desk-actions/client";

/** The desk's `GET /desk/vbt/data` (Rust `lanes/vbt.rs::build_view`). Decimals arrive as strings. */
export interface VbtDeskPlan {
  id: number;
  plan_id: string;
  session_date: string;
  source: string;
  built_at: string;
  expires_at: string | null;
  gate: string | null;
  sleeve_equity_inr: string | null;
  total_new_exposure_inr: string | null;
}

export interface VbtDeskLine {
  id: number;
  plan_id: string;
  kind: string;
  instrument_id: number;
  symbol: string;
  quantity: number;
  limit_price: string | null;
  stop_price: string | null;
  value_inr: string | null;
  size_cap: string | null;
  reason: string | null;
  note: string | null;
  state: string;
  client_id: string | null;
  order_id: string | number | null;
  position_id: number | null;
  expires_at: string | null;
  label: string;
  confirmable: boolean;
  expired: boolean;
  limit_text: string;
  stop_text: string;
  value_text: string;
}

export interface VbtDeskSkip {
  symbol: string;
  reason: string;
  detail: string | null;
}

export interface VbtDeskWorking {
  id: number;
  symbol: string;
  signal_date: string | null;
  quantity: number;
  filled_quantity: number;
  state: string;
  broker_order_id: string | null;
  simulated: boolean;
  sessions_text: string;
  expires_tonight: boolean;
  limit_text: string;
  remaining: number;
}

export interface VbtDeskPosition {
  id: number;
  symbol: string;
  entry_date: string | null;
  quantity_open: number;
  gtt_id: string | number | null;
  simulated: boolean;
  naked: boolean;
  entry_text: string;
  stop_text: string;
  initial_stop_text: string;
  risk_text: string;
  sells_tomorrow: boolean;
}

export interface VbtDeskScan {
  id: number;
  status: string;
  source: string | null;
  session_date: string | null;
  requested_at: string | null;
  finished_at: string | null;
  detail: { signals?: number; scan_hits?: number; reason?: string } | null;
  error: string | null;
}

export interface VbtDeskView {
  available: boolean;
  reason: string;
  now: string;
  plan: VbtDeskPlan | null;
  countdown: string;
  lines: VbtDeskLine[];
  skips: VbtDeskSkip[];
  working: VbtDeskWorking[];
  book: VbtDeskPosition[];
  exit_rule: string;
  breadth: {
    date: string;
    measured_count: number;
    pct_above_dma: string | null;
    gate: string;
  } | null;
  dry_run: boolean;
  execution_enabled: boolean;
  desk_dry_run: boolean;
  dry_run_sessions: number;
  dry_run_sessions_required: number;
  scan: VbtDeskScan | null;
}

export type VbtRescan =
  | { accepted: true; run_id: number; status: string }
  | { accepted: false; reason: string; run_id: number; status: string; retry_after_seconds?: number };

export interface VbtScanRunStatus {
  run_id: number;
  status: string;
  session_date: string | null;
  detail: { signals?: number; scan_hits?: number } | null;
  error: string | null;
}

export interface VbtExecuteOutcome {
  status: string;
  reason: string;
  simulated: boolean;
  line_id: number;
  position_id: number | null;
  order: { order_id?: string | number | null; status?: string } | null;
}

export function fetchVbtDesk(): Promise<VbtDeskView> {
  return deskGet<VbtDeskView>("/vbt/data");
}

/** Queues a re-detect of the last published session. Writes signals and breadth; places nothing. */
export function rescanVbt(): Promise<VbtRescan> {
  return deskForm<VbtRescan>("/vbt/rescan", {});
}

export function fetchVbtScanRun(runId: number): Promise<VbtScanRunStatus> {
  return deskGet<VbtScanRunStatus>(`/vbt/scan/${runId}`);
}

/** One line, one POST, on an explicit confirm: the desk's plan_id + line_id, never a batch. */
export function confirmVbtLine(planId: string, lineId: number): Promise<VbtExecuteOutcome> {
  return deskForm<VbtExecuteOutcome>("/vbt/execute", {
    plan_id: planId,
    line_id: String(lineId),
    confirm: "true",
  });
}

export const SCAN_IN_FLIGHT = ["QUEUED", "RUNNING"];
