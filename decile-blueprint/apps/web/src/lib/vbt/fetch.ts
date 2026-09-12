import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import {
  ServerFetchTimeoutError,
  serverFetchJson,
} from "@/lib/api/server-fetch";
import {
  SleeveUnavailableError,
  sleeveUnavailable,
} from "@/lib/api/sleeve-read";
import { auth } from "@/lib/auth";

/**
 * Server-side reads for the `/vbt` hub — VB8, `docs/vbt/05` §2.
 *
 * **Read-only, and structurally so.** There is no write helper in this file and there is not
 * going to be one: `docs/vbt/02-scope-and-gating.md` Track C §4 says the web app "gets no route
 * under `/vbt` that can reach the gateway". A volume-breakout line becomes an order in the desk
 * console, on a click Maulik makes, and nowhere else. `__tests__/read-only.test.ts` asserts that
 * over the source of this module and of every page that uses it.
 *
 * `no-store`, like the swing surfaces and for the same reason: a candidate is a claim about a
 * particular session, and the failure mode of serving a cached one is somebody acting on a level
 * that expired two evenings ago.
 */

export class VbtUnavailable extends Error {}

/** How long an RSC render will wait. Beyond this the page says so rather than hanging. */
const TIMEOUT_MS = 4000;

/** One `vb_signal_daily` row: a candidate when `state` is SIGNAL, a reject when SCAN_ONLY. */
export interface VbtCandidate {
  instrument_id: number;
  symbol: string;
  name: string;
  state: string;
  /**
   * Which of `04` §3.2's six trend filters said no — `["B", "F"]`. Empty on a signal. `01` §3's
   * ablation table is the argument for the filters, and a page that hides the rejects makes it
   * unreadable.
   */
  failed_filters: string[];
  close: number;
  limit_price: number;
  stop_price: number;
  change_pct: number | null;
  rvol: number | null;
  close_position: number | null;
  ret_20_pct: number | null;
  turnover_avg_20: number | null;
  sma_200: number | null;
  ema_21: number | null;
  high_20_prior: number | null;
  pct_above_dma: number | null;
  locked_upper_circuit: boolean;
  rank_key: number;
}

/** The counts `05` §2's empty state is written from, as `vb_breadth_daily.detail` stores them. */
export interface VbtFunnel {
  funnel?: {
    universe?: number;
    with_bar?: number;
    with_dma?: number;
    scan_hits?: number;
    signals?: number;
    scan_only?: number;
  };
  [key: string]: unknown;
}

/**
 * One "Scan now" run — the vocabulary is the swing hub's, deliberately (`PLAN-SCAN-SYNC.md`
 * "The contract"): `QUEUED -> RUNNING -> DONE | FAILED`, and a sleeve that invented its own
 * spelling would be a second thing to learn for no reason.
 *
 * `error` is read but never rendered. The reason a run failed is a sentence written for whoever
 * can fix it — it names jobs, quote sources and tables — and 11 Sep 2026 established that such a
 * sentence does not belong on a customer-facing page. It stays in the payload for the operator
 * surfaces and the page says, in a reader's words, that the run did not finish.
 */
export type VbtScanStatus = "QUEUED" | "RUNNING" | "DONE" | "FAILED";

export interface VbtScanRun {
  id: number;
  status: VbtScanStatus;
  requested_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface VbtToday {
  /** Null means the detector has never written a session — not that today was quiet. */
  as_of: string | null;
  gate: string | null;
  pct_above_dma: number | null;
  above_count: number | null;
  measured_count: number | null;
  gate_threshold_pct: number;
  thin_session: boolean;
  funnel: VbtFunnel | null;
  shut_sessions_recent: number;
  shut_window: number;
  candidates: VbtCandidate[];
  rejects: VbtCandidate[];
  /**
   * This user's newest "Scan now" run, if the payload carries it inline — the shape `/swing`
   * serves. Absent is not "no run"; see `fetchLastScan`.
   */
  last_scan?: VbtScanRun | null;
  /** Or just its id, if the payload names the run and leaves the status to its own route. */
  last_scan_id?: number | null;
}

export interface VbtBreadthPoint {
  date: string;
  pct_above_dma: number;
  above_count: number;
  measured_count: number;
  gate: string;
  thin_session: boolean;
}

export interface VbtBreadth {
  threshold_pct: number;
  data: VbtBreadthPoint[];
}

export interface VbtWorkingOrder {
  id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  limit_price: number;
  stop_price: number;
  quantity: number;
  value_inr: number;
  state: string;
  signal_date: string;
  working_from: string | null;
  expires_after_session: string | null;
  sessions_worked: number;
  sessions_allowed: number;
  /** `04` §7.2: the third session's close cancels it. The page says "cancels tonight". */
  expires_tonight: boolean;
  broker_order_id: string | null;
  filled_quantity: number;
  simulated: boolean;
}

export interface VbtPosition {
  id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  entry_date: string;
  entry_avg: number;
  quantity_open: number;
  initial_stop: number;
  stop_price: number;
  gtt_id: string | null;
  /** Shares open with no resting GTT — the one state the method forbids. Red, always. */
  naked: boolean;
  last_close: number | null;
  ema_21: number | null;
  distance_to_ema_pct: number | null;
  return_pct: number | null;
  r_multiple: number | null;
  sessions_held: number | null;
  exit_queued_for: string | null;
  exit_reason_queued: string | null;
  simulated: boolean;
}

export interface VbtClosedPosition {
  id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  entry_date: string;
  closed_on: string | null;
  entry_avg: number;
  exit_avg: number | null;
  quantity_entered: number;
  close_reason: string | null;
  return_pct: number | null;
  r_multiple: number | null;
  simulated: boolean;
}

/**
 * `04` §7.3: the honest early warning. `rate_pct` is null until an order has resolved — a book
 * with three working limits and no history has no fill rate, and 0% would be a claim about the
 * market rather than a fact about an empty book.
 */
export interface VbtFillRate {
  filled: number;
  resolved: number;
  rate_pct: number | null;
  modelled_pct: number;
}

export interface VbtBook {
  working: VbtWorkingOrder[];
  open_positions: VbtPosition[];
  closed_positions: VbtClosedPosition[];
  fill_rate: VbtFillRate;
}

/** STRATEGY §4's numbers, served rather than transcribed into a template. */
export interface VbtPublished {
  start: string;
  end: string;
  years: number;
  cagr_pct: number;
  max_drawdown_pct: number;
  trades: number;
  win_rate_pct: number;
  profit_factor: number;
  avg_hold_sessions: number;
  exposure_pct: number;
  sharpe: number;
  in_sample_cagr_pct: number;
  in_sample_dd_pct: number;
  out_of_sample_cagr_pct: number;
  out_of_sample_dd_pct: number;
  modelled_fill_rate_pct: number;
}

/** One point on the equity curve. Money arrives as a string of its exact decimal (house rule 9). */
export interface VbtEquityPoint {
  date: string;
  equity_inr: string;
}

export interface VbtYearRow {
  year: number;
  return_pct: number;
  trades: number;
  win_rate_pct: number;
}

/**
 * One of `04` §11's three books. `full` is the strategy; `gate_off` replaces breadth with OPEN
 * every session; `raw_scan` is the five Chartink lines with the same gate and execution.
 * Breadth's contribution is `full - gate_off`, the trend filters' is `full - raw_scan`.
 */
export interface VbtBacktestBook {
  cagr_pct?: number;
  max_drawdown_pct?: number;
  trades?: number;
  win_rate_pct?: number;
  profit_factor?: number | null;
  avg_hold_sessions?: number;
  avg_trade_pct?: number;
  exposure_pct?: number;
  sharpe?: number;
  fill_rate_pct?: number | null;
  orders_offered?: number;
  equity_curve?: VbtEquityPoint[];
  yearly?: VbtYearRow[];
  [key: string]: unknown;
}

export interface VbtBacktestRun {
  id: number;
  source: string;
  started_at: string;
  finished_at: string | null;
  params: Record<string, unknown>;
  /** The three books, plus the universe and session counts the run saw. */
  stats: {
    full?: VbtBacktestBook;
    gate_off?: VbtBacktestBook;
    raw_scan?: VbtBacktestBook;
    universe?: number;
    sessions?: number;
    [key: string]: unknown;
  } | null;
  /** VB9's comparison, and whether it is more than a CAGR point out. */
  drift: {
    flagged?: boolean;
    cagr_pct_delta?: number;
    max_dd_pct_delta?: number;
    trades_delta?: number;
    threshold_cagr_points?: number;
    [key: string]: unknown;
  } | null;
  error: string | null;
}

export interface VbtBacktest {
  published: VbtPublished;
  runs: VbtBacktestRun[];
}

async function readJson(
  path: string,
  search: Record<string, string> = {},
): Promise<unknown> {
  const session = await auth();
  const token = session?.accessToken;
  const url = new URL(`${serverApiOrigin()}/api/v1${path}`);
  for (const [key, value] of Object.entries(search))
    url.searchParams.set(key, value);
  try {
    return await serverFetchJson({
      url: url.toString(),
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      timeoutMs: TIMEOUT_MS,
    });
  } catch (error) {
    if (error instanceof ServerFetchTimeoutError) {
      throw new VbtUnavailable(`${path} timed out after ${error.timeoutMs}ms`);
    }
    /* C7 (`gates/sleeve-read-contract.md`): a refusal, a degraded deployment and a 500 are not
       "the detector has not run". They leave here as `SleeveUnavailableError` so no page can
       render them as its empty state; everything else keeps the old behaviour exactly. */
    const unavailable = sleeveUnavailable(error);
    if (unavailable !== null) throw new SleeveUnavailableError(path, unavailable);
    throw new VbtUnavailable(
      error instanceof Error ? error.message : `${path} unavailable`,
    );
  }
}

/**
 * Read, or answer `null` when the surface is not there yet.
 *
 * Before the first detection run there is nothing to show and that is not an error — the page
 * renders its empty state. A page that threw here would turn "the job has not run" into a 500,
 * and this sleeve's whole first month is a database with nothing in it.
 *
 * **`null` means the API answered and there was nothing there** — not "the read failed".
 * `gates/sleeve-read-contract.md` C7: a refusal (a second account on a single-tenant
 * deployment), a 503 and a 5xx leave `readJson` as `SleeveUnavailableError` and are *not*
 * caught here, so no page can render one of them as its empty state. A timeout or an
 * unreachable API still answers `null`, and that limit is named at the top of
 * `@/lib/api/sleeve-read`.
 */
async function readOrNull<T>(
  path: string,
  search: Record<string, string> = {},
): Promise<T | null> {
  try {
    return (await readJson(path, search)) as T;
  } catch (error) {
    if (error instanceof VbtUnavailable) return null;
    throw error;
  }
}

export async function fetchToday(date?: string): Promise<VbtToday | null> {
  return readOrNull<VbtToday>("/vbt/today", date ? { date } : {});
}

export async function fetchBreadth(params: {
  from?: string;
  to?: string;
} = {}): Promise<VbtBreadth | null> {
  const search: Record<string, string> = {};
  if (params.from) search.from = params.from;
  if (params.to) search.to = params.to;
  return readOrNull<VbtBreadth>("/vbt/breadth", search);
}

export async function fetchBook(): Promise<VbtBook | null> {
  return readOrNull<VbtBook>("/vbt/book");
}

export async function fetchBacktest(): Promise<VbtBacktest | null> {
  return readOrNull<VbtBacktest>("/vbt/backtest");
}

export async function fetchBars(
  instrumentId: number,
  date?: string,
): Promise<{ instrument_id: number; data: { date: string; close: number }[] } | null> {
  return readOrNull<{
    instrument_id: number;
    data: { date: string; close: number }[];
  }>(`/vbt/today/${instrumentId}/bars`, date ? { date } : {});
}

/** `GET /vbt/scan/{run_id}` — one run's state. Null while the route or the run is absent. */
export async function fetchScanRun(runId: number): Promise<VbtScanRun | null> {
  return readOrNull<VbtScanRun>(`/vbt/scan/${runId}`);
}

/**
 * The last scan's state, whichever way the payload names it.
 *
 * Two shapes are accepted because this page was built to the contract rather than to a running
 * route, and the contract fixes the *route* (`GET /vbt/scan/{run_id}`) rather than whether the
 * day's payload inlines the run. An inlined run costs no request; an id costs one and is read
 * through the contract's own route. Neither costs anything before the first scan, when there is
 * no run to name and this answers `null` without asking.
 */
export async function fetchLastScan(today: VbtToday | null): Promise<VbtScanRun | null> {
  if (today?.last_scan) return today.last_scan;
  const id = today?.last_scan_id;
  return typeof id === "number" ? fetchScanRun(id) : null;
}
