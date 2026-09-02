import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { ServerFetchTimeoutError, serverFetchJson } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";

/**
 * Server-side reads for the `/swing` hub — SW4, `docs/swing/05` §2.
 *
 * **Read-only, and structurally so.** There is no write helper in this file and there will not
 * be one that moves money: `docs/swing/02-scope-and-gating.md` Track C §4 says the web app "gets
 * no route under `/swing` that can reach the gateway". A swing line becomes an order in the desk
 * console, on a click, and nowhere else. `__tests__/read-only.test.ts` asserts this over the
 * source of this module and of the pages that use it.
 *
 * `no-store`, like the basket surfaces and for the same reason: a setup is a claim about *today*,
 * and the failure mode of serving a cached one is somebody acting on yesterday's pivot.
 */

export class SwingUnavailable extends Error {}

/** How long an RSC render will wait. Beyond this, the page says so rather than hanging. */
const TIMEOUT_MS = 4000;

export interface SwingSetup {
  instrument_id: number;
  symbol: string;
  name: string;
  setup: "FLAG" | "EP" | "PARABOLIC_SHORT";
  status: string;
  score: number;
  close: number | null;
  trigger: number | null;
  stop_ref: number | null;
  pivot_high: number | null;
  stop_distance_pct: number | null;
  adr_pct: number | null;
  prior_move_pct: number | null;
  base_depth_pct: number | null;
  tightness_adr: number | null;
  dryup_ratio: number | null;
  rvol: number | null;
  gap_pct: number | null;
  turnover_avg: number | null;
  base_bars: number | null;
  up_streak: number | null;
  locked_upper_circuit: boolean;
  sector_slug: string | null;
  listed_within_2y: boolean;
}

/** The counts `05` §2's empty state is written from. */
export interface SwingFunnel {
  instruments?: number;
  bars?: number;
  with_a_bar_today?: number;
  liquid?: number;
  candidates?: Record<string, number>;
}

export interface SwingSetups {
  as_of: string | null;
  gate: string | null;
  exposure_level: number | null;
  max_open_positions: number | null;
  max_exposure_pct: number | null;
  new_entries_allowed: boolean | null;
  funnel: SwingFunnel | null;
  data: SwingSetup[];
}

export interface SwingMarketDay {
  date: string;
  constituent_count: number;
  pct_up_strong_1m: number | null;
  pct_new_52w_high: number | null;
  pct_above_ma_slow: number | null;
  index_slug: string | null;
  index_close: number | null;
  index_ma_fast: number | null;
  index_ma_slow: number | null;
  gate: string;
  exposure_level: number;
  max_open_positions: number;
  max_exposure_pct: number;
  new_entries_allowed: boolean;
  parabolic_count: number;
}

export interface SwingSector {
  slug: string;
  pct_above_ma_slow: number;
  members: number;
  candidates: number;
  hot: boolean;
}

async function readJson(
  path: string,
  search: Record<string, string> = {},
): Promise<unknown> {
  const session = await auth();
  const token = session?.accessToken;
  const url = new URL(`${serverApiOrigin()}/api/v1${path}`);
  for (const [key, value] of Object.entries(search)) url.searchParams.set(key, value);
  try {
    return await serverFetchJson({
      url: url.toString(),
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      timeoutMs: TIMEOUT_MS,
    });
  } catch (error) {
    if (error instanceof ServerFetchTimeoutError) {
      throw new SwingUnavailable(`${path} timed out after ${error.timeoutMs}ms`);
    }
    throw new SwingUnavailable(
      error instanceof Error ? error.message : `${path} unavailable`,
    );
  }
}

/**
 * Read, or answer `null` when the surface is not there yet.
 *
 * Before the first detection run there is nothing to show and that is not an error — the page
 * renders its empty state. A page that threw here would turn "the job has not run" into a 500.
 */
async function readOrNull<T>(
  path: string,
  search: Record<string, string> = {},
): Promise<T | null> {
  try {
    return (await readJson(path, search)) as T;
  } catch (error) {
    if (error instanceof SwingUnavailable) return null;
    throw error;
  }
}

export async function fetchSetups(params: {
  date?: string;
  setup?: string;
  status?: string;
}): Promise<SwingSetups | null> {
  const search: Record<string, string> = {};
  if (params.date) search.date = params.date;
  if (params.setup) search.setup = params.setup;
  if (params.status) search.status = params.status;
  return readOrNull<SwingSetups>("/swing/setups", search);
}

export async function fetchSectors(
  date?: string,
): Promise<{ as_of: string | null; data: SwingSector[] } | null> {
  return readOrNull<{ as_of: string | null; data: SwingSector[] }>(
    "/swing/sectors",
    date ? { date } : {},
  );
}

export interface SwingWatchRow {
  id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  setup: string;
  source: string;
  added_on: string;
  expires_on: string | null;
  trigger: number | null;
  stop_ref: number | null;
  distance_to_trigger_pct: number | null;
  last_close: number | null;
  note: string | null;
  catalyst: string | null;
  state: string;
}

export interface SwingPlanLine {
  id: number;
  kind: string;
  symbol: string;
  name: string;
  setup: string | null;
  quantity: number;
  trigger: number | null;
  stop: number | null;
  risk_inr: number;
  position_value: number;
  trail: string | null;
  note: string | null;
  state: string;
}

export interface SwingPlan {
  plan_id: string;
  as_of: string;
  source: string;
  built_at: string;
  expires_at: string;
  gate: string;
  exposure_level: number;
  total_risk_inr: number;
  total_new_exposure_inr: number;
  lines: SwingPlanLine[];
  skips: { symbol: string; reason: string; detail: string | null }[];
}

export interface SwingPosition {
  id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  setup: string;
  entry_date: string;
  entry_avg: number;
  quantity_entered: number;
  quantity_open: number;
  initial_stop: number;
  stop: number;
  gtt_id: string | null;
  naked: boolean;
  trail: string;
  partial_done: boolean;
  state: string;
  closed_on: string | null;
  exit_avg: number | null;
  close_reason: string | null;
  r_multiple: number | null;
  pnl_inr: number | null;
  simulated: boolean;
  last_close: number | null;
}

export async function fetchWatchlist(
  state?: string,
): Promise<{ data: SwingWatchRow[] } | null> {
  return readOrNull<{ data: SwingWatchRow[] }>("/swing/watch", state ? { state } : {});
}

export async function fetchPositions(): Promise<{
  data: SwingPosition[];
  plan: SwingPlan | null;
} | null> {
  return readOrNull<{ data: SwingPosition[]; plan: SwingPlan | null }>("/swing/positions");
}

export async function fetchMarket(params: {
  from?: string;
  to?: string;
}): Promise<{ data: SwingMarketDay[] } | null> {
  const search: Record<string, string> = {};
  if (params.from) search.from = params.from;
  if (params.to) search.to = params.to;
  return readOrNull<{ data: SwingMarketDay[] }>("/swing/market", search);
}

/*
  `GET /swing/journal` — SW8, contract C2. The shape is `SwingJournalOut` in
  `services/api/src/baskfy_api/routers/swing.py`; `Decimal` fields arrive as JSON numbers with
  their stored digits (`2.50`), which `JSON.parse` reads as a `number` — the pages restore the two
  places with `toFixed(2)`, as the positions page does for the same fields.
*/

/** `04` §10's statistics, in R. An empty record is a row of zeros, not an error. */
export interface SwingJournalStats {
  trades: number;
  win_rate_pct: number;
  avg_win_r: number;
  avg_loss_r: number;
  expectancy_r: number;
  /** Gross win R over gross loss R; `null` when there is no loss to divide by. */
  profit_factor: number | null;
  net_r: number;
  largest_win_r: number;
  largest_loss_r: number;
  current_loss_streak: number;
}

/** One of the six R buckets, always all six, in the API's order: `<-1` … `>3`. */
export interface SwingHistogramBar {
  bucket: string;
  count: number;
}

export interface SwingSetupStats {
  setup: string;
  trades: number;
  net_r: number;
  expectancy_r: number;
}

export interface SwingMonthStats {
  /** `YYYY-MM` of the exit date. */
  month: string;
  trades: number;
  net_r: number;
}

/** One closed trade, with the numbers that were written at its close. */
export interface SwingJournalTrade {
  symbol: string;
  setup: string;
  entry_date: string;
  exit_date: string;
  entry: number;
  initial_stop: number;
  exit_avg: number;
  quantity: number;
  r_multiple: number;
  pnl_inr: number;
  close_reason: string | null;
}

/** One record — real or simulated, never both (`04` §10). */
export interface SwingJournalCard {
  stats: SwingJournalStats;
  histogram: SwingHistogramBar[];
  by_setup: SwingSetupStats[];
  by_month: SwingMonthStats[];
  /** Newest first, at most 200. */
  trades: SwingJournalTrade[];
}

/** `02` §3.2: "14 of 20 paper sessions logged". */
export interface SwingSessions {
  logged: number;
  required: number;
}

/** The rung in force, what it allows, and the closes it was computed from. */
export interface SwingLadder {
  level: number;
  gate: string;
  max_open_positions: number;
  max_exposure_pct: number;
  new_entries_allowed: boolean;
  /** The last `lookback_trades` R values of the record the ladder reads, oldest first. */
  last_r: number[];
  /** `SIMULATED` while execution is disabled on the server, `REAL` after (PACK.6). */
  reads: "SIMULATED" | "REAL";
}

/**
 * SW9's card. `stats` and `params` are SW9's to shape (contract C3); the page renders them
 * generically and shows `caveats` verbatim.
 */
export interface SwingBacktestCard {
  run_id: number;
  params: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
  stats: Record<string, unknown>;
  caveats: string[];
}

export interface SwingJournal {
  real: SwingJournalCard;
  simulated: SwingJournalCard;
  sessions: SwingSessions;
  ladder: SwingLadder;
  /** `null` until SW9 has stored a run. */
  backtest: SwingBacktestCard | null;
}

export async function fetchJournal(): Promise<SwingJournal | null> {
  return readOrNull<SwingJournal>("/swing/journal");
}
