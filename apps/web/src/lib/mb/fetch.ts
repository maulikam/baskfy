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
 * Server-side reads for `/mb` (PM7). **Read-only, and structurally so:** no write helper lives here,
 * and `app/(app)/mb/__tests__/read-only.test.tsx` asserts it. Money and prices are decimal strings;
 * `*_fraction` fields are fractions, scaled once by `formatFraction`.
 */

export class MbUnavailable extends Error {}

const TIMEOUT_MS = 4000;

export interface MbModel {
  id: number;
  trained_on: string;
  label: string;
  rows: number;
  positives: number;
  spearman_prev: string | null;
  published_at: string | null;
  published_by: string | null;
}

export interface MbName {
  rank: number;
  instrument_id: number;
  symbol: string;
  name: string | null;
  sector: string | null;
  score: string;
  investable: boolean;
  hi63_trigger: boolean;
  hi63_trigger_date: string | null;
  /** The session's close, rupees, as stored. */
  close: string | null;
  /** 20-session mean turnover, rupees. */
  turnover20: string | null;
  r252_fraction: string | null;
  dist_ath_fraction: string | null;
  sessions_listed: number | null;
  ttm_rev_yoy_fraction: string | null;
  ttm_pat_yoy_fraction: string | null;
}

export interface MbToday {
  /** The scored session. Null before the first night has scored. */
  as_of: string | null;
  model: MbModel | null;
  universe_n: number | null;
  investable_n: number | null;
  names: readonly MbName[];
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
      throw new MbUnavailable(`${path} timed out after ${error.timeoutMs}ms`);
    }
    // A refusal, a degraded deployment and a 500 are not "nothing scored yet" (sleeve-read C7).
    const unavailable = sleeveUnavailable(error);
    if (unavailable !== null)
      throw new SleeveUnavailableError(path, unavailable);
    throw new MbUnavailable(
      error instanceof Error ? error.message : `${path} unavailable`,
    );
  }
}

async function readOrNull<T>(
  path: string,
  search: Record<string, string> = {},
): Promise<T | null> {
  try {
    return (await readJson(path, search)) as T;
  } catch (error) {
    if (error instanceof MbUnavailable) return null;
    throw error;
  }
}

/** `all`: the model's top 50 as ranked; otherwise the top 50 investable names. */
export async function fetchMbToday(
  options: { date?: string; all?: boolean } = {},
): Promise<MbToday | null> {
  const search: Record<string, string> = {};
  if (options.date) search.date = options.date;
  if (options.all) search.all = "true";
  return readOrNull<MbToday>("/mb/today", search);
}

/** PM8: one paper book's marks, summarised. `source` "backtest" is the seeded walk-forward history. */
export interface MbPaperBook {
  source: "backtest" | "live";
  book: "book" | "hi63_20" | "hi63_16" | "book_overlay";
  start: string;
  end: string;
  growth_fraction: string;
  cagr_fraction: string | null;
  max_dd_fraction: string;
  invested_fraction: string;
}

export interface MbPaperOutcome {
  source: "backtest" | "live";
  line: "book" | "hi63";
  matured: boolean;
  lines: number;
  touched_2x_fraction: string | null;
  halved_fraction: string | null;
  median_return_fraction: string | null;
}

export interface MbPaperTranche {
  signal_date: string;
  model_id: number | null;
  sessions_held: number | null;
  hi63_lines: number;
  lines: number;
  touched_2x_fraction: string | null;
  halved_fraction: string | null;
  median_return_fraction: string | null;
}

export interface MbPaper {
  /** "YYYY-MM": the first month of the live record. Null before the first night has run. */
  live_from_month: string | null;
  books: readonly MbPaperBook[];
  outcomes: readonly MbPaperOutcome[];
  tranches: readonly MbPaperTranche[];
}

export async function fetchMbPaper(): Promise<MbPaper | null> {
  return readOrNull<MbPaper>("/mb/paper");
}

/** PM11: the swing-picks model. Paper only; prices are rupees as stored, in today's split-adjusted terms. */
export interface MbSwingPick {
  signal_date: string;
  instrument_id: number;
  symbol: string;
  name: string | null;
  pick_rank: number;
  score: string;
  status: "waiting" | "open" | "closed";
  entry_date: string | null;
  entry_px: string | null;
  /** The stop in force for the next session. */
  stop_px: string | null;
  peak_px: string | null;
  last_date: string | null;
  last_px: string | null;
  return_fraction: string | null;
  sessions_held: number | null;
  exit_date: string | null;
  exit_px: string | null;
  exit_reason: "stop" | "trail" | "time" | null;
}

export interface MbSwingName {
  rank: number;
  instrument_id: number;
  symbol: string;
  name: string | null;
  sector: string | null;
  score: string;
  investable: boolean;
  close: string | null;
}

export interface MbSwingSwitch {
  date: string;
  is_on: boolean;
  above_200: boolean;
  up_10_20: boolean;
  ew_close: string;
  ma10: string | null;
  ma20: string | null;
  ma200: string | null;
}

export interface MbSwingBacktest {
  period: string;
  trades: number;
  net_fraction: string;
  win_rate_fraction: string | null;
  profit_factor: string | null;
  worst_fraction: string | null;
  on_share_fraction: string | null;
}

export interface MbSwing {
  as_of: string | null;
  model: { id: number; trained_on: string; label: string; rows: number; positives: number; published_at: string | null } | null;
  switch: MbSwingSwitch | null;
  /** [date, is_on], oldest first. */
  switch_history: readonly (readonly [string, boolean])[];
  universe_n: number;
  investable_n: number;
  picks: readonly MbSwingPick[];
  names: readonly MbSwingName[];
  backtest: readonly MbSwingBacktest[];
}

export async function fetchMbSwing(options: { all?: boolean } = {}): Promise<MbSwing | null> {
  return readOrNull<MbSwing>("/mb/swing", options.all ? { all: "true" } : {});
}
