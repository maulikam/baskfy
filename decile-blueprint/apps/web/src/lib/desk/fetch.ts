import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import {
  ServerFetchStatusError,
  ServerFetchTimeoutError,
  serverFetchJson,
} from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";

/**
 * Server-side reads for the desk surfaces — M26.
 *
 * Read-only, like `lib/basket/fetch.ts` and for the same reason: the desk places orders, and
 * these pages show what it decided and what happened. There is no write helper in this file and
 * there will not be one.
 *
 * `no-store`, also for the same reason. These are statements about a portfolio as it stands; a
 * cached one is a statement about a portfolio that has moved on.
 */

export class DeskUnavailable extends Error {
  /** The API's HTTP status, when it answered at all. 404 on `/desk/regime` means "no evaluation yet". */
  readonly status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.status = status;
  }
}

export interface NavPoint {
  date: string;
  nav: number;
  invested: number;
  cash: number;
  index_value: number | null;
  benchmark_value: number | null;
}

export interface Performance {
  as_of: string;
  nav: number;
  invested: number;
  cash: number;
  return_pct: number | null;
  benchmark_return_pct: number | null;
  excess_pct: number | null;
  series: NavPoint[];
}

export interface Holding {
  symbol: string;
  quantity: number;
  average_price: number | null;
  price: number | null;
  value: number | null;
  unrealised: number | null;
  unrealised_pct: number | null;
  pledged_qty: number;
  excluded: boolean;
}

export interface Holdings {
  as_of: string;
  rows: Holding[];
  total_value: number;
  excluded_value: number;
}

export interface Trade {
  symbol: string;
  quantity: number;
  entry_date: string | null;
  exit_date: string | null;
  entry_price: number | null;
  exit_price: number | null;
  pnl: number | null;
  pnl_pct: number | null;
  costs: number | null;
  exit_reason: string | null;
  open: boolean;
}

export interface Tradebook {
  rows: Trade[];
  total: number;
  open_count: number;
  closed_count: number;
  realised_pnl: number;
  winners: number;
  losers: number;
}

export interface Regime {
  evaluated_at: string;
  signal_date: string | null;
  tier: string;
  previous_tier: string | null;
  new_buys: string | null;
  mode: string | null;
  breadth_pct: number | null;
  actual_equity_pct: number | null;
  target_equity_cap_pct: number | null;
  reasons: string[];
  data_stale: boolean;
  manual_action_required: boolean;
  next_evaluation_date: string | null;
}

export interface ReconcileRow {
  symbol: string;
  side: string;
  planned_qty: number;
  filled_qty: number;
  planned_ref_price: number | null;
  avg_fill_price: number | null;
  status: string | null;
  shortfall: number;
  slippage: number | null;
}

export interface Reconcile {
  plan_id: string;
  created_at: string;
  note: string | null;
  rows: ReconcileRow[];
  planned_count: number;
  complete_count: number;
  partial_count: number;
  unfilled_count: number;
  settled: boolean;
}

/**
 * Every `/desk/*` route has required a signed-in caller since `079d2ba` (AFA 0.1). This reader
 * kept calling them anonymously, so each one answered 401 and the portfolio page's regime panel
 * said "the desk did not answer" about a desk that was up (found 14 Sep 2026). The token is read
 * per call, never cached, for the reason `lib/api/server.ts` gives.
 */
async function readJson(path: string): Promise<unknown> {
  const token = (await auth())?.accessToken;
  try {
    return await serverFetchJson({
      url: `${serverApiOrigin()}/api/v1${path}`,
      ...(token ? { headers: { Authorization: `Bearer ${token}` } } : {}),
    });
  } catch (error) {
    if (error instanceof ServerFetchTimeoutError) {
      throw new DeskUnavailable(`${path} timed out after ${error.timeoutMs}ms`);
    }
    if (error instanceof ServerFetchStatusError) {
      throw new DeskUnavailable(error.message, error.status);
    }
    throw new DeskUnavailable(error instanceof Error ? error.message : `${path} unavailable`);
  }
}

export async function fetchPerformance(): Promise<Performance> {
  return (await readJson("/desk/performance")) as Performance;
}

export async function fetchHoldings(): Promise<Holdings> {
  return (await readJson("/desk/holdings")) as Holdings;
}

export async function fetchTradebook(): Promise<Tradebook> {
  return (await readJson("/desk/tradebook")) as Tradebook;
}

export async function fetchRegime(): Promise<Regime> {
  return (await readJson("/desk/regime")) as Regime;
}

export async function fetchReconcile(): Promise<Reconcile> {
  return (await readJson("/desk/reconcile")) as Reconcile;
}
