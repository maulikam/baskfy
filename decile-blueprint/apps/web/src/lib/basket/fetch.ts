import "server-only";

import { apiOrigin } from "@/lib/api/config";

/**
 * Server-side reads for the basket surfaces — M22.
 *
 * Read-only, and that is the whole point. The desk places orders; this shows what it decided.
 * There is no write helper in this file and there will not be one: execution stays in the desk
 * console, so nothing here crosses the SEBI gate and the desk's rule that `packages/execution`
 * is the only path to an order is untouched.
 *
 * `no-store` rather than a revalidate window. A basket is a trading decision about today, and a
 * cached one is a decision about a day that has passed — the failure mode of showing a stale
 * basket is somebody acting on it.
 */

export class BasketUnavailable extends Error {}

export interface BasketRow {
  rank: number;
  symbol: string;
  score: number;
  weight: number;
  ref_price: number;
  stop: number;
  value: number;
  a_trend: number | null;
  b_momentum: number | null;
  c_sharpe: number | null;
  d_consistency: number | null;
  e_liquidity: number | null;
  f_penalty: number | null;
}

export interface Basket {
  as_of: string;
  screen_run_id: string;
  data_version: number;
  capital: number;
  cash_target_pct: number;
  breadth_above_20dma: number;
  suspect_symbols: string[];
  rows: BasketRow[];
}

export interface PlanOrder {
  symbol: string;
  side: string;
  planned_qty: number;
  planned_ref_price: number | null;
  filled_qty: number | null;
  avg_fill_price: number | null;
  status: string | null;
  order_id: string | null;
  reconciled_at: string | null;
}

export interface Plan {
  plan_id: string;
  created_at: string;
  note: string | null;
  evaluation_id: string | null;
  constituents: string[];
  weights: Record<string, number>;
  orders: PlanOrder[];
}

async function readJson(path: string): Promise<unknown> {
  const response = await fetch(`${apiOrigin()}/api/v1${path}`, { cache: "no-store" });
  if (!response.ok) throw new BasketUnavailable(`${path} responded ${response.status}`);
  return response.json();
}

export async function fetchBasket(): Promise<Basket> {
  return (await readJson("/baskets")) as Basket;
}

export async function fetchLatestPlan(): Promise<Plan> {
  return (await readJson("/baskets/plan")) as Plan;
}
