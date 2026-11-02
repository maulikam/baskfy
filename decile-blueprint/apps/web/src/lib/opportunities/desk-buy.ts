/**
 * Same-origin desk order path for Opportunities row Buy.
 * Hits Caddy's `/desk` reverse-proxy (desk:8420). Never a Next.js execute route.
 */

export type CashProduct = "CNC" | "MTF";

export interface DeskStatus {
  authed: boolean;
  dry_run: boolean;
  cash: number | null;
}

export interface DeskAnalyze {
  confirmable: boolean;
  plan_id: string | null;
  line_id: number | null;
  symbol: string;
  product: CashProduct;
  quantity: number;
  amount_inr: string;
  last_price: string | null;
  required_margin_inr: string | null;
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
}

export class DeskBuyError extends Error {
  readonly status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "DeskBuyError";
    this.status = status;
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
    /* body is not JSON */
  }
  return text || response.statusText;
}

async function deskForm<T>(path: string, fields: Record<string, string>): Promise<T> {
  const response = await fetch(`${DESK}${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(fields).toString(),
  });
  if (!response.ok) {
    throw new DeskBuyError(response.status, await readDetail(response));
  }
  return (await response.json()) as T;
}

export async function fetchDeskStatus(): Promise<DeskStatus> {
  const response = await fetch(`${DESK}/status`, { credentials: "same-origin" });
  if (!response.ok) {
    throw new DeskBuyError(response.status, await readDetail(response));
  }
  const body = (await response.json()) as { authed?: boolean; dry_run?: boolean; cash?: number | null };
  return {
    authed: body.authed === true,
    dry_run: body.dry_run === true,
    cash: typeof body.cash === "number" ? body.cash : null,
  };
}

export interface AnalyzeInput {
  symbol: string;
  instrument_id: number;
  setup: string;
  amount_inr: string;
  product: CashProduct;
  last_price: string;
  trigger: string;
  stop: string;
  lot_size: number;
}

export function analyzeDeskBuy(input: AnalyzeInput): Promise<DeskAnalyze> {
  return deskForm<DeskAnalyze>("/swing/analyze", {
    symbol: input.symbol,
    instrument_id: String(input.instrument_id),
    setup: input.setup,
    amount_inr: input.amount_inr,
    product: input.product,
    last_price: input.last_price,
    trigger: input.trigger,
    stop: input.stop,
    lot_size: String(input.lot_size),
  });
}

export function executeDeskBuy(planId: string, lineId: number): Promise<DeskExecute> {
  return deskForm<DeskExecute>("/swing/execute", {
    plan_id: planId,
    line_id: String(lineId),
    confirm: "true",
  });
}

export function lotQuantity(amount: number, lastPrice: number, lotSize: number): number {
  if (!(amount > 0) || !(lastPrice > 0)) return 0;
  const lot = Math.max(1, Math.floor(lotSize) || 1);
  const shares = Math.floor(amount / lastPrice);
  return Math.floor(shares / lot) * lot;
}

export function confirmLabel(
  live: boolean,
  product: CashProduct,
  qty: number,
  symbol: string,
  amount: string,
): string {
  const kind = live ? "LIVE" : "SIMULATED";
  return `Place ${kind} ${product} buy of ${qty} ${symbol} (~₹${amount})`;
}
