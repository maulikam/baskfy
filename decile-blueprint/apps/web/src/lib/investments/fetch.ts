import "server-only";

import { auth } from "@/lib/auth";
import { apiOrigin } from "@/lib/api/config";
import { serverFetchJsonOrNull } from "@/lib/api/server-fetch";

/**
 * Server-side reads for SC6 investor surfaces (`/investments`, `/watchlist`, `/fees`).
 *
 * Prefer real APIs when they answer; otherwise return empty shapes so the UI can show honest
 * empty states. **No write helpers that place orders** — Invest more / Exit / Rebalance end in
 * `PlanHandoffPanel` or `MarketClosedModal`.
 */

export class InvestorUnavailable extends Error {}

export interface InvestorSnapshot {
  money_put_in: string;
  current_investment: string;
  current_value: string;
  current_returns: string;
  current_returns_pct: string;
  realized_pnl: string;
  dividends: string;
  xirr: string | null;
  xirr_displayable: boolean;
}

export interface InvestmentRow {
  id: string;
  basket_slug: string;
  basket_name: string;
  /** "ACTIVE" | "EXITED" today; string because the API owns the enum. */
  status: string;
  invested_at: string | null;
  last_invested_at: string | null;
  days_since_last_investment: number | null;
  rebalance_pending: boolean;
  snapshot: InvestorSnapshot | null;
}

export interface PendingActionBrief {
  id: string;
  type: string;
  title: string;
  body: string | null;
}

export interface InvestmentList {
  items: InvestmentRow[];
  total: number;
  net_worth: string | null;
  pending_actions: PendingActionBrief[];
}

export interface InvestmentDetail extends InvestmentRow {
  holdings: Array<{
    symbol: string;
    qty: string;
    weight: string | null;
    returns_pct: string | null;
  }>;
  orders: OrderBatchRow[];
}

export interface OrderBatchRow {
  id: string;
  kind: string;
  status: string;
  created_at: string | null;
  desk_plan_id: string | null;
}

export interface WatchlistItem {
  basket_slug: string;
  basket_name: string;
  watched_at: string;
  nav_at_watch: string | number | null;
  moved_pct: string | number | null;
  daily_change_pct?: string | number | null;
}

export interface Watchlist {
  items: WatchlistItem[];
  count: number;
}

export interface FeeLedgerRow {
  id: string;
  kind: string;
  base_fee: string;
  gst: string;
  total: string;
  accrued_at: string;
  collected: boolean;
  basket_name: string | null;
}

export interface FeeLedger {
  items: FeeLedgerRow[];
  accrued_total: string | null;
  collected_total: string | null;
}

async function authHeaders(): Promise<HeadersInit> {
  const session = await auth();
  const token = session?.accessToken;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function tryJson(path: string): Promise<unknown> {
  return serverFetchJsonOrNull({
    url: `${apiOrigin()}/api/v1${path}`,
    headers: await authHeaders(),
  });
}

const EMPTY_INVESTMENTS: InvestmentList = {
  items: [],
  total: 0,
  net_worth: null,
  pending_actions: [],
};

const EMPTY_FEES: FeeLedger = {
  items: [],
  accrued_total: null,
  collected_total: null,
};

/** Prefer a future ledger list; empty until the investments API lands. */
export async function fetchInvestments(): Promise<InvestmentList> {
  const data = await tryJson("/cb/investments");
  if (data === null) return EMPTY_INVESTMENTS;
  return data as InvestmentList;
}

export async function fetchInvestment(id: string): Promise<InvestmentDetail | null> {
  const data = await tryJson(`/cb/investments/${encodeURIComponent(id)}`);
  if (data === null) return null;
  return data as InvestmentDetail;
}

export async function fetchWatchlist(): Promise<Watchlist> {
  const data = await tryJson("/watchlist");
  if (data === null) return { items: [], count: 0 };
  return data as Watchlist;
}

/** Prefer a future fee ledger; empty until the journal writer lands (SC4 service). */
export async function fetchFeeLedger(): Promise<FeeLedger> {
  const data = await tryJson("/cb/fees");
  if (data === null) return EMPTY_FEES;
  return data as FeeLedger;
}
