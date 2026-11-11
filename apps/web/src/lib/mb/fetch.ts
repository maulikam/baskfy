import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { ServerFetchTimeoutError, serverFetchJson } from "@/lib/api/server-fetch";
import { SleeveUnavailableError, sleeveUnavailable } from "@/lib/api/sleeve-read";
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

async function readJson(path: string, search: Record<string, string> = {}): Promise<unknown> {
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
      throw new MbUnavailable(`${path} timed out after ${error.timeoutMs}ms`);
    }
    // A refusal, a degraded deployment and a 500 are not "nothing scored yet" (sleeve-read C7).
    const unavailable = sleeveUnavailable(error);
    if (unavailable !== null) throw new SleeveUnavailableError(path, unavailable);
    throw new MbUnavailable(error instanceof Error ? error.message : `${path} unavailable`);
  }
}

async function readOrNull<T>(path: string, search: Record<string, string> = {}): Promise<T | null> {
  try {
    return (await readJson(path, search)) as T;
  } catch (error) {
    if (error instanceof MbUnavailable) return null;
    throw error;
  }
}

export async function fetchMbToday(date?: string): Promise<MbToday | null> {
  return readOrNull<MbToday>("/mb/today", date ? { date } : {});
}
