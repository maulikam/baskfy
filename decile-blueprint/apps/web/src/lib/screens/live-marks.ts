import { useQuery } from "@tanstack/react-query";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

/**
 * Live last prices for a page of names.
 *
 * Screen runs stay byte-identical on the published session (docs/06). This fetch is a second
 * request that overlays `last_price` in the browser so a Kite session can mark the Price column
 * without rewriting the cached screen body.
 */

export interface LiveMarks {
  liveOverlay: boolean;
  marks: Readonly<Record<string, number>>;
}

const EMPTY: LiveMarks = { liveOverlay: false, marks: {} };

export async function fetchLiveMarks(symbols: readonly string[]): Promise<LiveMarks> {
  const unique = [
    ...new Set(symbols.map((symbol) => symbol.trim().toUpperCase()).filter(Boolean)),
  ].slice(0, 500);
  if (unique.length === 0) return EMPTY;
  const token = await accessToken();
  if (!token) return EMPTY;
  const url = new URL("/api/v1/meta/live-marks", apiOrigin());
  url.searchParams.set("symbols", unique.join(","));
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    signal: AbortSignal.timeout(4000),
  });
  if (!response.ok) return EMPTY;
  const payload = (await response.json()) as {
    live_overlay?: boolean;
    marks?: Record<string, string | number>;
  };
  const marks: Record<string, number> = {};
  for (const [symbol, value] of Object.entries(payload.marks ?? {})) {
    const numeric = typeof value === "number" ? value : Number(value);
    if (Number.isFinite(numeric) && numeric > 0) marks[symbol] = numeric;
  }
  return { liveOverlay: Boolean(payload.live_overlay) && Object.keys(marks).length > 0, marks };
}

export function useLiveMarks(symbols: readonly string[]): LiveMarks {
  const key = symbols
    .map((symbol) => symbol.trim().toUpperCase())
    .filter(Boolean)
    .sort()
    .join(",");
  const { data } = useQuery({
    queryKey: ["live-marks", key],
    queryFn: () => fetchLiveMarks(symbols),
    enabled: key.length > 0,
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
  return data ?? EMPTY;
}

/** Prefer the live mark on the Price column; leave every other field on the published row. */
export function withLivePrice<T extends { symbol?: unknown }>(
  row: T,
  marks: Readonly<Record<string, number>>,
): T {
  const symbol = typeof row.symbol === "string" ? row.symbol.trim().toUpperCase() : "";
  const live = symbol === "" ? undefined : marks[symbol];
  if (live === undefined) return row;
  return { ...row, last_price: live };
}
