export type CashProduct = "CNC" | "MTF";
export type OrderType = "MARKET" | "LIMIT";
export type AmountBasis = "order_value" | "own";
export type BuySource = "overlap" | "opportunities" | "swing" | "vbt" | "twt" | "doubling" | "swing-picks";

export interface DeskTicket {
  symbol: string;
  exchange: string;
  kite_authed: boolean;
  dry_run: boolean;
  simulated: boolean;
  swing_execution_enabled: boolean;
  account: { broker: string; user_id: string | null; user_name: string | null } | null;
  available_funds: string | null;
  quote: { last_price: string; at: string | null; stale: boolean } | null;
  market_open: boolean;
  mtf: {
    eligible: boolean;
    reason: string | null;
    leverage: string | null;
    margin_per_share: string | null;
    interest_pct_per_day: string;
    interest_basis: string;
  };
  gate: string;
  new_entries_allowed: boolean;
}

export interface DeskAnalyze {
  confirmable: boolean;
  plan_id: string | null;
  line_id: number | null;
  symbol: string;
  exchange: string;
  product: CashProduct;
  order_type: OrderType;
  limit_price: string | null;
  quantity: number;
  amount_inr: string | null;
  amount_basis: AmountBasis;
  last_price: string;
  order_price: string;
  quote_at: string | null;
  quote_live: boolean;
  stale_quote: boolean;
  order_value: string;
  required_margin_inr: string | null;
  own_contribution: string | null;
  broker_funding: string | null;
  charges: string | null;
  leverage: string | null;
  available_funds: string | null;
  margin_error: string | null;
  mtf_interest_pct_per_day: string;
  mtf_interest_per_day: string | null;
  stop: string;
  stop_basis: "setup" | "band";
  trigger: string;
  skip: string | null;
  simulated: boolean;
  kite_authed: boolean;
  dry_run: boolean;
  swing_execution_enabled: boolean;
  expires_at: string | null;
  gtt_note: string;
  gate: string;
}

export interface DeskExecute {
  status: string;
  reason: string;
  simulated: boolean;
  line_id: number | null;
  position_id: number | null;
  filled_quantity?: number;
  order?: { order_id?: string | number | null; status?: string } | null;
}

export type OrderDisplayStatus =
  | "NOT_SENT"
  | "IN_FLIGHT"
  | "SUBMITTED"
  | "OPEN"
  | "PARTIALLY_FILLED"
  | "PARTIALLY_FILLED_CANCELLED"
  | "FILLED"
  | "CANCELLED"
  | "REJECTED"
  | "EXPIRED"
  | "SIMULATED";

export interface DeskOrderStatus {
  line_id: number;
  plan_id: string;
  symbol: string;
  line_state: string;
  product: CashProduct;
  quantity: number;
  order_id: string | null;
  position_id: number | null;
  status: OrderDisplayStatus;
  filled_quantity: number;
  broker: {
    status: string | null;
    status_message: string | null;
    filled_quantity: number | null;
    pending_quantity: number | null;
    average_price: number | null;
    order_timestamp: string | null;
  } | null;
  needs_reconcile: boolean;
}

export const TERMINAL_STATUSES: readonly OrderDisplayStatus[] = [
  "FILLED",
  "CANCELLED",
  "PARTIALLY_FILLED_CANCELLED",
  "REJECTED",
  "EXPIRED",
  "SIMULATED",
];

export const REVIEW_AGAIN_CODES = [
  "QUOTE_MOVED",
  "STALE_QUOTE",
  "INSUFFICIENT_FUNDS",
  "MTF_UNAVAILABLE",
  "MARGIN_UNAVAILABLE",
  "FUNDS_UNAVAILABLE",
];

export class DeskBuyError extends Error {
  readonly status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "DeskBuyError";
    this.status = status;
  }
  get reviewAgain(): boolean {
    return this.status === 409 && REVIEW_AGAIN_CODES.some((code) => this.message.startsWith(code));
  }
}

const DESK = "/desk";

async function readDetail(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const body = JSON.parse(text) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) return body.detail;
    if (Array.isArray(body.detail) && body.detail[0] && typeof body.detail[0] === "object") {
      const first = body.detail[0] as { msg?: string };
      if (first.msg) return first.msg;
    }
  } catch {
    return text || response.statusText;
  }
  return text || response.statusText;
}

async function deskGet<T>(path: string, params: Record<string, string>): Promise<T> {
  const response = await fetch(`${DESK}${path}?${new URLSearchParams(params).toString()}`, {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) throw new DeskBuyError(response.status, await readDetail(response));
  return (await response.json()) as T;
}

async function deskForm<T>(path: string, fields: Record<string, string>): Promise<T> {
  const response = await fetch(`${DESK}${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(fields).toString(),
  });
  if (!response.ok) throw new DeskBuyError(response.status, await readDetail(response));
  return (await response.json()) as T;
}

export function fetchDeskTicket(symbol: string, exchange = "NSE"): Promise<DeskTicket> {
  return deskGet<DeskTicket>("/swing/ticket", { symbol, exchange });
}

export interface MtfLeverage {
  kite_authed: boolean;
  data: Record<string, { eligible: boolean; leverage: string | null; reason: string | null }>;
}

/** Stored daily MTF leverage for many symbols in one call (the desk quotes misses live). */
export function fetchMtfLeverage(symbols: readonly string[]): Promise<MtfLeverage> {
  return deskGet<MtfLeverage>("/swing/mtf-leverage", { symbols: symbols.join(",") });
}

export interface AnalyzeInput {
  symbol: string;
  instrument_id: number;
  setup: string;
  source: BuySource;
  product: CashProduct;
  order_type: OrderType;
  limit_price: string;
  quantity: number;
  amount_inr: string;
  amount_basis: AmountBasis;
  last_price: string;
  trigger: string;
  stop: string;
  lot_size: number;
}

export function analyzeDeskBuy(input: AnalyzeInput): Promise<DeskAnalyze> {
  const fields: Record<string, string> = {
    symbol: input.symbol,
    instrument_id: String(input.instrument_id),
    setup: input.setup,
    source: input.source,
    product: input.product,
    order_type: input.order_type,
    amount_basis: input.amount_basis,
    last_price: input.last_price,
    lot_size: String(input.lot_size),
  };
  if (input.quantity > 0) fields.quantity = String(input.quantity);
  else fields.amount_inr = input.amount_inr;
  if (input.order_type === "LIMIT") fields.limit_price = input.limit_price;
  if (input.trigger) fields.trigger = input.trigger;
  if (input.stop) fields.stop = input.stop;
  return deskForm<DeskAnalyze>("/swing/analyze", fields);
}

export function executeDeskBuy(planId: string, lineId: number): Promise<DeskExecute> {
  return deskForm<DeskExecute>("/swing/execute", {
    plan_id: planId,
    line_id: String(lineId),
    confirm: "true",
  });
}

export function fetchOrderStatus(lineId: number): Promise<DeskOrderStatus> {
  return deskGet<DeskOrderStatus>("/swing/order-status", { line_id: String(lineId) });
}

export function reconcileFills(): Promise<unknown> {
  return deskForm<unknown>("/swing/reconcile", { confirm: "true" });
}

export function lotQuantity(amount: number, lastPrice: number, lotSize: number): number {
  if (!(amount > 0) || !(lastPrice > 0)) return 0;
  const lot = Math.max(1, Math.floor(lotSize) || 1);
  return Math.floor(Math.floor(amount / lastPrice) / lot) * lot;
}

export function confirmLabel(live: boolean, product: CashProduct, qty: number, symbol: string): string {
  return `Place ${live ? "" : "simulated "}${product === "MTF" ? "MTF" : "delivery"} buy order · ${qty} ${symbol}`;
}

export function materialChange(prev: DeskAnalyze, next: DeskAnalyze): string[] {
  const out: string[] = [];
  const pct = (a: string | null, b: string | null) => {
    const x = Number(a);
    const y = Number(b);
    return x > 0 && Math.abs(y - x) / x > 0.005;
  };
  if (prev.quantity !== next.quantity) out.push(`quantity ${prev.quantity} → ${next.quantity}`);
  if (pct(prev.order_price, next.order_price)) out.push(`price ₹${prev.order_price} → ₹${next.order_price}`);
  if (pct(prev.own_contribution, next.own_contribution))
    out.push(`own funds ₹${prev.own_contribution} → ₹${next.own_contribution}`);
  return out;
}

export const STATUS_LABEL: Record<OrderDisplayStatus, string> = {
  NOT_SENT: "Not sent",
  IN_FLIGHT: "Sending — checking the broker",
  SUBMITTED: "Submitted to the broker",
  OPEN: "Open at the exchange",
  PARTIALLY_FILLED: "Partially filled",
  PARTIALLY_FILLED_CANCELLED: "Partially filled, rest cancelled",
  FILLED: "Filled",
  CANCELLED: "Cancelled",
  REJECTED: "Rejected",
  EXPIRED: "Plan expired",
  SIMULATED: "Simulated — no broker order",
};
