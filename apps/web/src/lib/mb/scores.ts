import { useEffect, useState } from "react";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import type { MbVerdict, SwingScope } from "@/lib/mb/flags";

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
  /** Set once every retry failed (no token, 429, 5xx, timeout): the cell reads "unavailable", not "·". */
  error?: string | null;
  /** PM11: the swing-picks model's verdicts, from its own newest session. */
  swing: SwingScores;
}

export interface SwingVerdict {
  rank: number;
  score: string;
  investable: boolean;
  /** One of that session's five paper picks. */
  picked: boolean;
  /** Older rows lack it: read as "universe". */
  scope?: SwingScope;
}

export interface SwingScores {
  asOf: string | null;
  universeN: number | null;
  switchOn: boolean | null;
  bySymbol: Readonly<Record<string, SwingVerdict>>;
}

const EMPTY_SWING: SwingScores = { asOf: null, universeN: null, switchOn: null, bySymbol: {} };

export const EMPTY_MB_SCORES: MbScores = {
  asOf: null,
  universeN: null,
  bySymbol: {},
  loaded: false,
  swing: EMPTY_SWING,
};

interface Payload {
  as_of: string | null;
  universe_n: number | null;
  names: readonly (MbVerdict & { symbol: string })[];
  swing_as_of?: string | null;
  swing_universe_n?: number | null;
  swing_switch_on?: boolean | null;
  swing?: readonly (SwingVerdict & { symbol: string })[];
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
  const swingBy: Record<string, SwingVerdict> = {};
  for (const n of payload.swing ?? []) swingBy[n.symbol.toUpperCase()] = n;
  return {
    asOf: payload.as_of,
    universeN: payload.universe_n,
    bySymbol,
    loaded: true,
    swing: {
      asOf: payload.swing_as_of ?? null,
      universeN: payload.swing_universe_n ?? null,
      switchOn: payload.swing_switch_on ?? null,
      bySymbol: swingBy,
    },
  };
}

/** The API answers at most this many symbols a request (`MAX_LOOKUP`); a bigger table asks in batches. */
export const LOOKUP_BATCH = 500;

/** Merges batch answers; the session facts come from the first batch that has them. */
export function mergeMbScores(parts: readonly MbScores[]): MbScores {
  if (parts.length === 0) return EMPTY_MB_SCORES;
  const pick = <T,>(f: (p: MbScores) => T | null): T | null => {
    for (const p of parts) {
      const v = f(p);
      if (v !== null) return v;
    }
    return null;
  };
  return {
    asOf: pick((p) => p.asOf),
    universeN: pick((p) => p.universeN),
    bySymbol: Object.fromEntries(parts.flatMap((p) => Object.entries(p.bySymbol))),
    loaded: parts.every((p) => p.loaded),
    swing: {
      asOf: pick((p) => p.swing.asOf),
      universeN: pick((p) => p.swing.universeN),
      switchOn: pick((p) => p.swing.switchOn),
      bySymbol: Object.fromEntries(parts.flatMap((p) => Object.entries(p.swing.bySymbol))),
    },
  };
}

/** A lookup that did not answer: why, and how long the server asked us to wait (429 Retry-After). */
export class MbLookupError extends Error {
  constructor(
    message: string,
    readonly retryAfterMs: number | null = null,
  ) {
    super(message);
    this.name = "MbLookupError";
  }
}

function retryAfterMs(header: string | null): number | null {
  if (!header) return null;
  const secs = Number(header);
  if (Number.isFinite(secs) && secs >= 0) return secs * 1000;
  const at = Date.parse(header);
  return Number.isNaN(at) ? null : Math.max(0, at - Date.now());
}

async function fetchBatch(token: string, symbols: readonly string[]): Promise<MbScores> {
  const url = new URL("/api/v1/mb/lookup", apiOrigin());
  url.searchParams.set("symbols", symbols.join(","));
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) {
    const why = response.status === 429 ? "rate limited (429)" : `HTTP ${response.status}`;
    throw new MbLookupError(why, retryAfterMs(response.headers.get("Retry-After")));
  }
  return parseMbScores((await response.json()) as Payload);
}

/** Throws `MbLookupError` (or a network error) on any failure; never returns a fake "loading" value. */
export async function fetchMbScores(key: string): Promise<MbScores> {
  const token = await accessToken();
  if (!token) throw new MbLookupError("not signed in (no access token)");
  const all = key.split(",");
  const batches: string[][] = [];
  for (let i = 0; i < all.length; i += LOOKUP_BATCH) batches.push(all.slice(i, i + LOOKUP_BATCH));
  return mergeMbScores(await Promise.all(batches.map((b) => fetchBatch(token, b))));
}

/** Retries after a failed lookup; Retry-After wins when the server sends one (capped). */
export const MB_RETRY_DELAYS_MS: readonly number[] = [1_000, 4_000];
const MAX_RETRY_WAIT_MS = 30_000;

function describe(err: unknown): string {
  if (err instanceof MbLookupError) return err.message;
  if (err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError"))
    return "timed out";
  return "network error";
}

/** Scores change once a day, after the nightly; a 10-minute refresh is plenty. No react-query: the
 * column must render anywhere a table does, with or without a query client. */
export function useMbScores(symbols: readonly string[]): MbScores {
  const key = mbScoresKey(symbols);
  const [state, setState] = useState<{ key: string; scores: MbScores }>({
    key: "",
    scores: EMPTY_MB_SCORES,
  });
  useEffect(() => {
    if (!key) return;
    let live = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const load = (attempt: number) => {
      fetchMbScores(key)
        .then((scores) => {
          if (live) setState({ key, scores });
        })
        .catch((err: unknown) => {
          if (!live) return;
          const base = MB_RETRY_DELAYS_MS[attempt];
          if (base === undefined) {
            // Keep an earlier good answer for this key; otherwise say the lookup is unavailable.
            setState((prev) =>
              prev.key === key && prev.scores.loaded && !prev.scores.error
                ? prev
                : { key, scores: { ...EMPTY_MB_SCORES, loaded: true, error: describe(err) } },
            );
            return;
          }
          const hinted = err instanceof MbLookupError ? err.retryAfterMs : null;
          const wait = Math.min(MAX_RETRY_WAIT_MS, Math.max(base, hinted ?? 0));
          retry = setTimeout(() => load(attempt + 1), wait);
        });
    };
    load(0);
    const timer = setInterval(() => {
      clearTimeout(retry);
      load(0);
    }, 600_000);
    return () => {
      live = false;
      clearInterval(timer);
      clearTimeout(retry);
    };
  }, [key]);
  // A new symbol set (a fresh scan) reads as loading until its own answer arrives, never as the
  // previous table's "not scored".
  return state.key === key ? state.scores : EMPTY_MB_SCORES;
}
