import { DESK, DeskError, deskForm, deskGet } from "@/lib/desk-actions/client";

/**
 * The weekly rebalance and Stops pages' calls to the desk, same-origin `/desk/*`.
 * Orders and GTTs stay on the desk: analyze issues a plan_id, and only an explicit confirm sends it.
 * The weekly rebalancer has no auto-execute and never gains one.
 */

export const PLAN_TTL_SECONDS = 30 * 60;

export interface CollectionHealth {
  healthy: boolean;
  ever_ran: boolean;
  consecutive_failures: number;
  message: string;
  outcome: string | null;
  stale_days: number | null;
}

export interface OpsRun {
  id: number | null;
  name?: string;
  status: string;
  started_at: string;
}

export interface AutoRun {
  just_logged_in: boolean;
  note: string;
  running: OpsRun | null;
  last: OpsRun | null;
}

export interface ProtectionFinding {
  kind: string;
  symbol: string;
  qty: number;
  value: number;
  detail: string;
}

export interface Protection {
  checked: number;
  active_gtts: number;
  protected: number;
  findings: ProtectionFinding[];
  counts: Record<string, number>;
  unprotected_value: number;
  tradeable_value: number;
  coverage_pct: number | null;
  healthy: boolean;
}

/** `GET /desk/` with `Accept: application/json`: the home page's context. */
export interface DeskHome {
  authed: boolean;
  dry_run: boolean;
  collection: CollectionHealth | null;
  auto: AutoRun | null;
  protection: Protection | null;
}

/** BUY, ADD, SELL, TRIM, HOLD, EXIT, … as the desk names them. */
export type PlanAction = string;

export interface PlanOrder {
  symbol: string;
  rank: number | null;
  score: number | null;
  action: PlanAction;
  qty_now: number;
  delta: number;
  qty_final: number;
  ref_price: number | null;
  value: number | null;
  weight: number | null;
  stop: number | null;
  pledged: number | null;
  note?: string | null;
}

export interface Funding {
  checked: boolean;
  required?: number;
  available?: number;
  shortfall?: number;
  error?: string;
  reason?: string;
}

/** `POST /desk/analyze`: the full plan. Nothing is executed at this step. */
export interface RebalancePlan {
  plan_id: string;
  created_at: number;
  capital: number;
  book_value: number;
  cash: number;
  breadth_above_20dma: number | null;
  cash_target_pct: number | null;
  buys_value: number;
  sells_value: number;
  net_cash_use: number;
  pledged_sells_margin_note: string[];
  cluster_warnings?: string[];
  orders: PlanOrder[];
  funding?: Funding;
  scan?: { source?: string; filename?: string; warnings?: string[] };
}

export interface ExecutedOrder {
  symbol: string;
  action?: string;
  status?: string;
  error?: string;
}

export interface Reconciliation {
  orders?: number;
  planned_sell_value?: number;
  planned_buy_value?: number;
  submitted_value?: number;
  failed_value?: number;
  aborted_value?: number;
  aborted_symbols?: string[];
  not_submitted?: string[];
  reached_exchange?: boolean;
}

/** `POST /desk/execute`: what the gateway did with each line. */
export interface ExecuteResult {
  dry_run: boolean;
  orders: ExecutedOrder[];
  gtt: { symbol: string; status?: string; error?: string; action?: string }[];
  stops_note?: string;
  stops_pending?: boolean;
  preflight?: Funding;
  aborted_for?: string;
  log?: string;
  reconciliation?: Reconciliation;
}

export interface StopRow {
  symbol: string;
  qty: number;
  last_price: number;
  trigger: number;
  drop_pct: number;
  value: number;
  at_risk: number;
  vol: number | null;
  vol_source: string;
  reason: string;
}

export interface StopCancel {
  symbol: string;
  gtt_id: number;
  reason: string;
  detail: string;
}

export interface StopPlan {
  plan_id: string;
  created_at: number;
  rows: StopRow[];
  cancels: StopCancel[];
  count: number;
  cancel_count: number;
  value: number;
  at_risk: number;
  using_default_vol: string[];
  review: Protection;
}

/** `GET /desk/stops` with `Accept: application/json`: a review (issues a plan_id, places nothing). */
export interface StopsReview {
  plan: StopPlan | null;
  review: Protection | null;
  problem: string;
  dry_run: boolean;
  armed: string;
  error: string;
  band: [number, number];
  daily_loss_cap: number;
}

export interface ArmResult {
  armed: string;
  dry_run: boolean;
  cancelled: { symbol: string; gtt_id?: number; status?: string; error?: string; reason?: string }[];
  results: { symbol: string; qty?: number; trigger?: number; status?: string; error?: string }[];
}

export function fetchDeskHome(): Promise<DeskHome> {
  return deskGet<DeskHome>("/");
}

async function errorText(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const body = JSON.parse(text) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      const first = body.detail[0] as { msg?: string } | undefined;
      if (first?.msg) return first.msg;
    }
  } catch {
    return text || response.statusText;
  }
  return text || response.statusText;
}

/** Multipart upload, as the desk's own page sends it. Builds a plan; sends nothing. */
export async function analyzeScan(file: File): Promise<RebalancePlan> {
  const body = new FormData();
  body.append("scan", file);
  const response = await fetch(`${DESK}/analyze`, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    body,
  });
  if (!response.ok) throw new DeskError(response.status, await errorText(response));
  return (await response.json()) as RebalancePlan;
}

/** The one order call: explicit confirm plus the desk-issued plan_id. Never retried. */
export function executePlan(planId: string): Promise<ExecuteResult> {
  return deskForm<ExecuteResult>("/execute", {
    plan_id: planId,
    confirm: "true",
    place_stops: "true",
  });
}

export function fetchStopsReview(): Promise<StopsReview> {
  return deskGet<StopsReview>("/stops");
}

/** Arms the reviewed plan (cancels first). Explicit confirm plus the desk-issued plan_id. */
export function armStops(planId: string): Promise<ArmResult> {
  return deskForm<ArmResult>("/stops/arm", { plan_id: planId, confirm: "true" });
}

export function planSecondsLeft(createdAt: number, nowMs: number): number {
  return Math.max(0, Math.floor(createdAt + PLAN_TTL_SECONDS - nowMs / 1000));
}

export function inr(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "–";
  return `₹${Math.round(value).toLocaleString("en-IN")}`;
}

export function num(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "–";
  return value.toLocaleString("en-IN");
}
