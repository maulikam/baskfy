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

import type {
  OptionsBacktest,
  OptionsCalendar,
  OptionsChain,
  OptionsConfig,
  OptionsJournal,
  OptionsToday,
} from "./types";

/**
 * Server-side reads for the options tab — OP5, `docs/options/05` §2.
 *
 * **Read-only, and structurally so.** There is no write helper in this file: the tab's two
 * money-free writes (an event day, the settings) go through `./write.ts`, whose path type is a
 * closed union of those two strings. `docs/options/02` Track C §4 gives the web app no route that
 * can reach the gateway; the desk console's the desk's options page is the only surface with a Confirm.
 * `src/app/(app)/options/__tests__/read-only.test.tsx` asserts that over this tree.
 *
 * **Null is "the API answered and there was nothing"**, never "you were refused" — a refusal,
 * a 503 or a 500 leaves as `SleeveUnavailableError` (`gates/sleeve-read-contract.md` C7), which
 * the tab's error boundary renders, so no page can show one as its empty state. A timeout or an
 * unreachable API still answers `null`, the limit named in `@/lib/api/sleeve-read`.
 */

export class OptionsUnavailable extends Error {}

/** How long an RSC render waits before saying so rather than hanging. */
const TIMEOUT_MS = 4000;

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
      throw new OptionsUnavailable(
        `${path} timed out after ${error.timeoutMs}ms`,
      );
    }
    const unavailable = sleeveUnavailable(error);
    if (unavailable !== null)
      throw new SleeveUnavailableError(path, unavailable);
    throw new OptionsUnavailable(
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
    if (error instanceof OptionsUnavailable) return null;
    throw error;
  }
}

export async function fetchOptionsToday(): Promise<OptionsToday | null> {
  return readOrNull<OptionsToday>("/options/today");
}

export async function fetchOptionsChain(): Promise<OptionsChain | null> {
  return readOrNull<OptionsChain>("/options/chain");
}

export async function fetchOptionsJournal(): Promise<OptionsJournal | null> {
  return readOrNull<OptionsJournal>("/options/journal");
}

export async function fetchOptionsBacktest(): Promise<OptionsBacktest | null> {
  return readOrNull<OptionsBacktest>("/options/backtest");
}

export async function fetchOptionsCalendar(
  year?: number,
): Promise<OptionsCalendar | null> {
  return readOrNull<OptionsCalendar>(
    "/options/calendar",
    year ? { year: String(year) } : {},
  );
}

export async function fetchOptionsConfig(): Promise<OptionsConfig | null> {
  return readOrNull<OptionsConfig>("/options/config");
}
