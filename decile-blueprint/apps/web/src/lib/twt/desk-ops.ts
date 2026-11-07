import { deskForm, deskGet } from "@/lib/desk-actions/client";

/** Typed calls to the desk's TWT lane, same-origin `/desk/twt/*`. Orders stay on the desk. */

export type TwtKind = "BUY_AT_OPEN" | "ARM_GTT" | "RAISE_GTT_STOP" | "SELL_AT_OPEN";

export interface TwtDeskPlan {
  id: number;
  plan_id: string;
  session_date: string;
  source: string;
  built_at: string;
  expires_at: string;
  gate: string;
  sleeve_equity_inr: string | null;
  total_new_exposure_inr: string | null;
}

export interface TwtDeskLine {
  id: number;
  plan_id: string;
  kind: TwtKind;
  instrument_id: number;
  symbol: string;
  quantity: number;
  stop_price: string | null;
  value_inr: string | null;
  high_since: string | null;
  previous_trigger: string | null;
  note: string | null;
  state: string;
  label: string;
  expired: boolean;
  confirmable: boolean;
}

export interface TwtDeskSkip {
  symbol: string;
  reason: string;
  detail: string | null;
}

export interface TwtDeskPosition {
  id: number;
  instrument_id: number;
  symbol: string;
  entry_date: string;
  entry_avg: string;
  quantity_open: number;
  initial_stop: string | null;
  stop_price: string | null;
  high_since: string | null;
  gtt_id: string | null;
  gtt_trigger: string | null;
  next_trigger: string | null;
  next_trigger_for: string | null;
  half_size: boolean;
  partial_done: boolean;
  trail: string | null;
  exit_queued_for: string | null;
  exit_reason_queued: string | null;
  partial_queued_for: string | null;
  partial_quantity: number | null;
  simulated: boolean;
}

export interface TwtDeskView {
  available: boolean;
  reason: string;
  now: string;
  plan: TwtDeskPlan | null;
  countdown: string;
  expired: boolean;
  lines: TwtDeskLine[];
  exits: TwtDeskLine[];
  entries: TwtDeskLine[];
  skips: TwtDeskSkip[];
  book: TwtDeskPosition[];
  exit_rule?: string;
  naked: string[];
  breadth: {
    date: string;
    measured_count: number;
    above_count: number;
    pct_above_dma: string;
    gate: string;
    thin_session: boolean;
  } | null;
  session: {
    session_date: string;
    mode: string;
    confirms: number;
    fills: number;
    ratchets: number;
    exits: number;
    naked_at_1515: number;
  } | null;
  config: { sleeve_capital_inr?: string | null };
  money: { equity_inr?: string | null; cash_available_inr?: string | null; open_exposure_inr?: string | null };
  first_live_entries_left: number | null;
  max_new_entries_per_session: number;
  sweep_due: boolean;
  plan_ttl_minutes: number;
  /** The sleeve's own mode: simulated unless DRY_RUN=false and the sleeve's switch is on. */
  dry_run: boolean;
  execution_enabled: boolean;
  desk_dry_run: boolean;
}

export interface TwtOutcome {
  status: string;
  reason: string;
  simulated: boolean;
  naked: boolean;
  position_id: number | null;
  line_id?: number;
}

export interface TwtScanRunStatus {
  id: number;
  status: string;
  session_date: string | null;
  requested_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export const fetchTwtDesk = (): Promise<TwtDeskView> => deskGet<TwtDeskView>("/twt/data");

/** One line, one POST, the plan_id the desk issued. Never retried by the caller. */
export const confirmTwtLine = (planId: string, lineId: number): Promise<TwtOutcome> =>
  deskForm<TwtOutcome>("/twt/execute", { plan_id: planId, line_id: String(lineId), confirm: "true" });

export const rearmTwtStop = (positionId: number): Promise<TwtOutcome> =>
  deskForm<TwtOutcome>("/twt/rearm", { position_id: String(positionId), confirm: "true" });

export const sweepTwtNaked = (): Promise<{ naked_before: number; rearmed: number; naked: number }> =>
  deskForm("/twt/sweep", { confirm: "true" });

export const reconcileTwtStops = (): Promise<{ attached: number; unmatched: number }> =>
  deskForm("/twt/reconcile", { confirm: "true" });

export const haltTwtSleeve = (): Promise<{ halted: boolean; plans_expired: number; message: string }> =>
  deskForm("/twt/halt", { confirm: "true" });

export const queueTwtDeskScan = (): Promise<{ run_id: number; status: string }> =>
  deskForm("/twt/scan", {});

export const fetchTwtScanRun = (runId: number): Promise<TwtScanRunStatus> =>
  deskGet<TwtScanRunStatus>(`/twt/scan/${runId}`);

export function inr(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function riskInr(p: TwtDeskPosition): string {
  const entry = Number(p.entry_avg);
  const stop = Number(p.stop_price);
  if (!Number.isFinite(entry) || !Number.isFinite(stop) || p.stop_price === null) return "—";
  return inr((entry - stop) * p.quantity_open);
}

export function outcomeLine(o: TwtOutcome): string {
  const sim = o.simulated ? " (simulated)" : "";
  const naked = o.naked ? " — no stop resting, re-arm it" : "";
  return `${o.status}${sim}${o.reason ? `: ${o.reason}` : ""}${naked}`;
}
