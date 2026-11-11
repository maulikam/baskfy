import { useEffect, useState } from "react";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import type { MbVerdict } from "@/lib/mb/flags";

/**
 * The model's verdict for the stocks a Build table shows (PM7, Maulik 11 Nov 2026): one request per
 * table, `GET /mb/lookup`. Read-only and information only: no strategy list is filtered by it.
 */
export interface MbScores {
  /** The scored session, or null before the first night (or when this account cannot read it). */
  asOf: string | null;
  universeN: number | null;
  bySymbol: Readonly<Record<string, MbVerdict>>;
  /** False until an answer arrives; a cell shows nothing rather than "not ranked" while waiting. */
  loaded: boolean;
}

export const EMPTY_MB_SCORES: MbScores = {
  asOf: null,
  universeN: null,
  bySymbol: {},
  loaded: false,
};

interface Payload {
  as_of: string | null;
  universe_n: number | null;
  names: readonly (MbVerdict & { symbol: string })[];
}

export function mbScoresKey(symbols: readonly string[]): string {
  return [
    ...new Set(symbols.map((s) => s.trim().toUpperCase()).filter(Boolean)),
  ]
    .sort()
    .join(",");
}

export function parseMbScores(payload: Payload): MbScores {
  const bySymbol: Record<string, MbVerdict> = {};
  for (const n of payload.names ?? []) bySymbol[n.symbol.toUpperCase()] = n;
  return {
    asOf: payload.as_of,
    universeN: payload.universe_n,
    bySymbol,
    loaded: true,
  };
}

async function fetchMbScores(key: string): Promise<MbScores> {
  const token = await accessToken();
  if (!token) return EMPTY_MB_SCORES;
  const url = new URL("/api/v1/mb/lookup", apiOrigin());
  url.searchParams.set("symbols", key.split(",").slice(0, 500).join(","));
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    signal: AbortSignal.timeout(4000),
  });
  if (!response.ok) return EMPTY_MB_SCORES;
  return parseMbScores((await response.json()) as Payload);
}

/** Scores change once a day, after the nightly; a 10-minute refresh is plenty. No react-query: the
 * column must render anywhere a table does, with or without a query client. */
export function useMbScores(symbols: readonly string[]): MbScores {
  const key = mbScoresKey(symbols);
  const [scores, setScores] = useState<MbScores>(EMPTY_MB_SCORES);
  useEffect(() => {
    if (!key) return;
    let live = true;
    const load = () =>
      fetchMbScores(key)
        .then((s) => live && setScores(s))
        .catch(() => undefined);
    void load();
    const timer = setInterval(() => void load(), 600_000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [key]);
  return scores;
}
