import { DESK, deskGet } from "@/lib/desk-actions/client";

/** Desk decimals arrive as strings (Python `str(Decimal)`); a few as numbers. */
export type Num = string | number | null;

export interface FnoLeg {
  seq: number;
  role: string;
  symbol: string;
  side: string;
  quantity: number;
  strike: Num;
  option_type: string;
  bid: Num;
  ask: Num;
  spread_pct: Num;
  oi: number | null;
  live: boolean;
  reference: Num;
  status: string | null;
  filled: number | null;
  avg_price: Num;
}

export interface FnoPlan {
  plan_id: string;
  sleeve: string;
  group: string;
  symbol: string;
  structure: string;
  status: string;
  reason: string | null;
  issued_at: string;
  expires_at: string;
  expires_epoch_ms: number;
  seconds_left: number;
  confirmable: boolean;
  paper: boolean;
  sentence: string;
  legs: FnoLeg[];
  entry_sequence: string;
  credit: Num;
  live_credit: Num;
  scan_credit: Num;
  credit_drift_pct: Num;
  credit_drift_amber: boolean;
  width: Num;
  profit_take: Num;
  loss_close: Num;
  entry_reference: Num;
  stop: Num;
  gtt_trigger: Num;
  time_exit_date: string | null;
  roll_date: string | null;
  lots: number;
  lots_sized: number | null;
  lots_at_ceiling: number | null;
  lot_size: number;
  max_loss_inr: Num;
  risk_budget_inr: Num;
  max_loss_r: Num;
  margin_required_inr: Num;
  margin_message: string | null;
  cost_inr: Num;
  cost_share_pct: string | null;
  hard_exit_date: string | null;
  kind: string;
  direction: string | null;
  level: Num;
  index_last: Num;
  decay_target: Num;
  loss_cut: Num;
  rule: string | null;
}

export interface LastMark {
  trade_date: string | null;
  mark_points: Num;
  pnl_inr: Num;
}

export interface PositionLeg {
  role: string | null;
  symbol: string | null;
  quantity?: Num;
  avg_price: Num;
  mid: Num;
}

export interface FnoStructure {
  id: number;
  sleeve: string;
  symbol: string;
  entry_plan_id: string;
  lots: number;
  lot_size: number;
  entry_credit: Num;
  live_close_cost: Num;
  live_pnl_inr: Num;
  max_loss_inr: Num;
  profit_take: Num;
  loss_close: Num;
  to_profit_take: Num;
  to_loss_close: Num;
  spot: Num;
  short_sigma: Record<string, Num>;
  hard_exit_date: string | null;
  next_rule: string;
  last_mark: LastMark | null;
  simulated: boolean;
  legs: PositionLeg[];
}

export interface FnoSpread {
  id: number;
  sleeve: string;
  symbol: string;
  entry_plan_id: string;
  lots: number;
  lot_size: number;
  direction: string;
  level: Num;
  spot: Num;
  entry_credit: Num;
  mark: Num;
  live_pnl_inr: Num;
  max_loss_inr: Num;
  decay_target: Num;
  loss_cut: Num;
  expiry: string;
  added: boolean;
  next_rule: string;
  last_mark: LastMark | null;
  simulated: boolean;
  legs: PositionLeg[];
}

export interface FnoFuture {
  id: number;
  sleeve: string;
  symbol: string;
  contract: string;
  entry_plan_id: string;
  lots: number;
  lot_size: number;
  entry_price: Num;
  last: Num;
  live_pnl_inr: Num;
  gtt_id: string | null;
  gtt_trigger: Num;
  gtt_resting: boolean;
  trail_stop: Num;
  rolls: number | null;
  roll_date: string | null;
  time_exit_date: string | null;
  next_rule: string;
  last_mark: LastMark | null;
  simulated: boolean;
}

export interface FnoAction {
  plan_id: string;
  parent_plan_id: string | null;
  sleeve: string | null;
  symbol: string | null;
  kind: string;
  status: string;
  code: string | null;
  message: string | null;
  reason: string | null;
  issued_at: string | null;
  confirmable: boolean;
  awaits_click: boolean;
  sentence: string | null;
  expires_epoch_ms: number;
}

export interface FnoViolation {
  position_id: number;
  symbol: string;
  code?: string;
  message?: string;
}

export interface FnoView {
  ok: boolean;
  error?: string;
  day?: string;
  now?: string;
  modes?: Record<string, string>;
  live_note?: string | null;
  plans: FnoPlan[];
  structures?: FnoStructure[];
  futures?: FnoFuture[];
  spreads?: FnoSpread[];
  f3_auto_exit?: boolean;
  actions?: FnoAction[];
  violations?: FnoViolation[];
}

export interface NiftyLeg {
  seq: number;
  role: string;
  symbol: string;
  side: string;
  quantity: number;
}

export interface NiftyPlan {
  plan_id: string;
  sleeve: string;
  structure: string;
  status: string;
  session_state: string;
  expires_at: string;
  confirmable: boolean;
  paper: boolean;
  sentence: string;
  legs: NiftyLeg[];
}

export interface NiftyPosition {
  session_id: number;
  sleeve: string;
  entry_points: string;
  last_mark_points: string | null;
  exit_plan_id: string | null;
  hard_exit_at: string | null;
}

export interface NiftyView {
  ok: boolean;
  error?: string;
  day?: string;
  plans: NiftyPlan[];
  positions: NiftyPosition[];
}

export function fetchFno(): Promise<FnoView> {
  return deskGet<FnoView>("/fno/data");
}

export function fetchNifty(): Promise<NiftyView> {
  return deskGet<NiftyView>("/nifty-options/data");
}

export interface DeskAnswer {
  ok: boolean;
  status: number;
  code: string | null;
  outcome: string | null;
  detail: string | null;
  orders: number | null;
  simulated: boolean | null;
}

/** The three desk POSTs this console may send; each carries `confirm=true`. */
export type ConsolePost =
  | { path: "/fno/execute", plan_id: string }
  | { path: "/nifty-options/execute", plan_id: string }
  | { path: "/nifty-options/close", session_id: number };

/** One POST, same-origin `/desk`, never retried. Keeps the desk's refusal `code` (deskForm drops it). */
export async function sendToDesk(post: ConsolePost): Promise<DeskAnswer> {
  const fields: Record<string, string> =
    post.path === "/nifty-options/close"
      ? { session_id: String(post.session_id), confirm: "true" }
      : { plan_id: post.plan_id, confirm: "true" };
  const response = await fetch(`${DESK}${post.path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams(fields).toString(),
  });
  const text = await response.text();
  let body: Record<string, unknown>;
  try {
    body = JSON.parse(text) as Record<string, unknown>;
  } catch {
    body = { detail: text || response.statusText };
  }
  const str = (v: unknown) => (typeof v === "string" && v ? v : null);
  const outcome = str(body.outcome);
  return {
    ok: response.ok && (outcome === "OPEN" || outcome === "CLOSED"),
    status: response.status,
    code: str(body.code),
    outcome,
    detail: str(body.detail),
    orders: typeof body.orders === "number" ? body.orders : null,
    simulated: typeof body.simulated === "boolean" ? body.simulated : null,
  };
}

export function answerText(a: DeskAnswer): string {
  if (a.ok) return `${a.outcome}${a.simulated ? " (simulated)" : ""} — ${a.orders ?? 0} orders`;
  const head = a.code ?? a.outcome ?? String(a.status);
  return a.detail && a.detail !== head ? `${head} — ${a.detail}` : head;
}

export function d(x: Num | undefined): string {
  return x === null || x === undefined || x === "" ? "—" : String(x);
}

export function inr(x: Num | undefined): string {
  if (x === null || x === undefined || x === "") return "—";
  const n = Number(x);
  return Number.isFinite(n) ? `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}` : `₹${x}`;
}

export function signTone(x: Num | undefined): string {
  const n = Number(x);
  if (x === null || x === undefined || !Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "text-positive" : "text-negative";
}

export function hhmm(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso;
  return t.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  });
}
