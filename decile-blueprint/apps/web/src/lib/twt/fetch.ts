import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import {
  ServerFetchTimeoutError,
  serverFetchJson,
} from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";

/**
 * Server-side reads for `/twt` — TW8, `docs/twt/05` §1 and §3.
 *
 * **Read-only, and structurally so.** There is no write helper in this file and there is not
 * going to be one: `docs/twt/02` Track C §4 gives the web app no route under `/twt` that can
 * reach the gateway, and `05` §2 says the desk console is the only place a TWT order is created.
 * `src/app/(app)/twt/__tests__/read-only.test.tsx` asserts that over this module and every page
 * that uses it.
 *
 * **Built ahead of its data (TW8).** TW4 (the nightly job) and TW5 (the cash and the positions)
 * are not finished, so nothing serves these paths yet. Every read answers `null` when the surface
 * is absent, the pages render their empty state, and the fixtures in `__tests__/fixtures.ts` —
 * written to `docs/twt/03-data-model.md` — are what the tests render. When the producing modules
 * land, the parent wires the routes below and nothing in the components changes.
 *
 * UNITS, DECLARED ONCE, HERE
 * --------------------------
 * **Money and prices are decimal strings** (house rule 9) — never a `number`, because a rupee
 * that passes through a float is a rupee that can disagree with the database by a paisa, and the
 * whole of `03` stores them as `numeric`.
 *
 * **Rates carry their unit in the field name and are converted exactly once**, in
 * `@/lib/twt/view`, never in a component. Two kinds arrive:
 *
 *  - `*_pct` — a PERCENTAGE, as `03` stores it (`week_range_pct`, `pct_above_dma`, `return_pct`).
 *    Storage precision is the contract (house rule 8), so it is served as stored and not scaled.
 *  - `*_fraction` — a FRACTION, `money / base`, the convention the portfolio payload uses
 *    (`0.019900` is a 1.99% day). It is multiplied by 100 once, by `asPercent`.
 *
 * That distinction is not pedantry: the portfolio band rendered a 1.99% day as 0.0199% on
 * 11 Sep 2026 because a component scaled a figure that had already been scaled. A unit that is
 * visible in the field name cannot be guessed at wrongly in a JSX expression.
 */

export class TwtUnavailable extends Error {}

/** How long an RSC render will wait. Beyond this the page says so rather than hanging. */
const TIMEOUT_MS = 4000;

/** `03` §4 — the gate's reading for one session, and the funnel that produced it. */
export interface TwtGate {
  /** The session the reading was taken on. Null before any session has been computed. */
  date: string | null;
  gate: "OPEN" | "SHUT" | null;
  /** A PERCENTAGE, as `tw_breadth_daily` stores it. Null on a thin session. */
  pct_above_dma: string | null;
  /** The threshold the gate opens above, a PERCENTAGE. Served, never hard-coded on the page. */
  threshold_pct: string;
  universe_count: number | null;
  /** Of the universe, the ones with a bar on the date. */
  with_bar_count: number | null;
  /** Of those, the ones with a valid 200-day average — the denominator. */
  measured_count: number | null;
  above_count: number | null;
  /** The date was dropped from the rolling calendar; the gate is SHUT and the percentages null. */
  thin_session: boolean;
}

/**
 * One row of `03` §2 joined to its `03` §3 event, if it had one.
 *
 * `signal_state` is `SIGNAL` when the entry event held and the name cleared the liquidity floor,
 * `SCAN_ONLY` when the floor rejected it, and `null` when the name is merely still in the state.
 * The rejects are served rather than filtered away: a screen that hides what it passed over
 * cannot be audited by the person whose money it is (`05` §1.2).
 */
export interface TwtTightName {
  instrument_id: number;
  symbol: string;
  name: string;
  /** The exchange print (`close_raw`), as a decimal string. */
  close_raw: string | null;
  /** The three weekly closes the tight test compared — this week's first. */
  week_close_0: string | null;
  week_close_1: string | null;
  week_close_2: string | null;
  /** `(max/min − 1) × 100`, a PERCENTAGE. */
  week_range_pct: string | null;
  /** `close / month_low_3` — a RATIO, e.g. `"1.4200"`. Not a percentage. */
  month_low_ratio: string | null;
  sessions_in_state: number | null;
  signal_state: "SIGNAL" | "SCAN_ONLY" | null;
  /** `["TURNOVER"]` on a rejected event, empty otherwise. */
  failed_filters: readonly string[];
  /** 20-session average turnover in rupees, a decimal string. */
  turnover_avg_20: string | null;
  locked_upper_circuit: boolean;
}

/** One `OPEN` row of `03` §5, marked live. */
export interface TwtOpenPosition {
  id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  entry_date: string;
  entry_avg: string | null;
  quantity_open: string | null;
  /** The highest high since entry, an exchange print, and the session that set it. */
  high_since: string | null;
  high_since_date: string | null;
  /** The resting trigger. `gtt_id` null with shares open is the one state the method forbids. */
  gtt_trigger: string | null;
  gtt_id: string | null;
  /** Tomorrow's trailing trigger, and the session it was computed for. Null when it does not rise. */
  next_trigger: string | null;
  next_trigger_for: string | null;
  /** The live mark. Null when quotes are not available, which is a reason, not a zero. */
  last_price: string | null;
  unrealised_inr: string | null;
  /** A FRACTION. `"0.1870"` is 18.70%. Converted once, in `@/lib/twt/view`. */
  unrealised_fraction: string | null;
  hold_sessions: number | null;
  half_size: boolean;
  simulated: boolean;
}

/** `05` §1.4 — the first-live discipline, as a counter a reader can see without a settings page. */
export interface TwtHalfSize {
  entries_left: number;
  entries_total: number;
  /** False for the whole of this run. The counter says so rather than counting down in the dark. */
  execution_enabled: boolean;
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
export type TwtScanStatus = "QUEUED" | "RUNNING" | "DONE" | "FAILED";

export interface TwtScanRun {
  id: number;
  status: TwtScanStatus;
  requested_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface TwtToday {
  /** The last published session. Null means nothing has ever been computed, not that today was quiet. */
  as_of: string | null;
  gate: TwtGate;
  tight: readonly TwtTightName[];
  positions: readonly TwtOpenPosition[];
  half_size: TwtHalfSize;
  /**
   * This user's newest "Scan now" run, if the payload carries it inline — the shape `/swing`
   * serves. Absent is not "no run"; see `fetchLastScan`.
   */
  last_scan?: TwtScanRun | null;
  /** Or just its id, if the payload names the run and leaves the status to its own route. */
  last_scan_id?: number | null;
}

/** One point on the equity curve. Money is a string of its exact decimal (house rule 9). */
export interface TwtEquityPoint {
  date: string;
  equity_inr: string;
}

export interface TwtYearRow {
  year: number;
  /** A PERCENTAGE. */
  return_pct: string;
  trades: number;
  win_rate_pct: string;
}

/** The metrics `05` §3 asks for, all as decimal strings so the page never rounds twice. */
export interface TwtBacktestStats {
  cagr_pct?: string;
  max_drawdown_pct?: string;
  calmar?: string;
  sharpe?: string;
  trades?: number;
  win_rate_pct?: string;
  profit_factor?: string;
  avg_hold_sessions?: string;
  exposure_pct?: string;
  in_sample_cagr_pct?: string;
  out_of_sample_cagr_pct?: string;
  in_sample_window?: string;
  out_of_sample_window?: string;
  /** `05` §3's one number that justifies the gate: the same run with the gate forced open. */
  gate_off_cagr_pct?: string;
  gate_off_max_drawdown_pct?: string;
  yearly?: readonly TwtYearRow[];
  equity_curve?: readonly TwtEquityPoint[];
}

/** `03` §9's comparison against `01` §6, and whether it is more than a CAGR point out. */
export interface TwtBacktestDrift {
  flagged?: boolean;
  /** PERCENTAGE POINTS, signed. */
  cagr_pct_delta?: string;
  max_dd_pct_delta?: string;
  trades_delta?: number;
  /** What `01` §6 published, so the warning can name both numbers. */
  published_cagr_pct?: string;
  run_cagr_pct?: string;
  threshold_cagr_points?: string;
}

export interface TwtBacktestRun {
  id: number;
  /** `PLANT` (the run's own bars) or `RESEARCH_EXPORT`. Never mixed, never averaged (`05` §3). */
  source: "PLANT" | "RESEARCH_EXPORT";
  started_at: string;
  /** Null while a run is in flight. The page only ever shows the latest FINISHED run per source. */
  finished_at: string | null;
  params: { sleeve_inr?: string; start?: string; end?: string; cost_bps_per_side?: string };
  stats: TwtBacktestStats | null;
  drift: TwtBacktestDrift | null;
  error: string | null;
}

export interface TwtBacktest {
  runs: readonly TwtBacktestRun[];
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
      throw new TwtUnavailable(`${path} timed out after ${error.timeoutMs}ms`);
    }
    throw new TwtUnavailable(
      error instanceof Error ? error.message : `${path} unavailable`,
    );
  }
}

/**
 * Read, or answer `null` when the surface is not there yet.
 *
 * Before TW4 has ever run there is nothing to show and that is not an error — the page renders
 * its empty state. A page that threw here would turn "the job has not run" into a 500, and this
 * strategy's whole first month is a database with nothing in it.
 */
async function readOrNull<T>(
  path: string,
  search: Record<string, string> = {},
): Promise<T | null> {
  try {
    return (await readJson(path, search)) as T;
  } catch (error) {
    if (error instanceof TwtUnavailable) return null;
    throw error;
  }
}

export async function fetchToday(date?: string): Promise<TwtToday | null> {
  return readOrNull<TwtToday>("/twt/today", date ? { date } : {});
}

export async function fetchBacktest(): Promise<TwtBacktest | null> {
  return readOrNull<TwtBacktest>("/twt/backtest");
}

/** `GET /twt/scan/{run_id}` — one run's state. Null while the route or the run is absent. */
export async function fetchScanRun(runId: number): Promise<TwtScanRun | null> {
  return readOrNull<TwtScanRun>(`/twt/scan/${runId}`);
}

/**
 * The last scan's state, whichever way the payload names it.
 *
 * Two shapes are accepted because this page was built to the contract rather than to a running
 * route, and the contract fixes the *route* (`GET /twt/scan/{run_id}`) rather than whether the
 * day's payload inlines the run. An inlined run costs no request; an id costs one and is read
 * through the contract's own route. Neither costs anything before the first scan, when there is
 * no run to name and this answers `null` without asking — which is the state this sleeve is
 * genuinely in today, with every detection table at zero rows.
 */
export async function fetchLastScan(today: TwtToday | null): Promise<TwtScanRun | null> {
  if (today?.last_scan) return today.last_scan;
  const id = today?.last_scan_id;
  return typeof id === "number" ? fetchScanRun(id) : null;
}
