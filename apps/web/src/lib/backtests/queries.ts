"use client";

import type {
  BacktestAcceptedOut,
  BacktestConfigIn,
  BacktestOut,
  BacktestSummaryOut,
  ExportLinkOut,
  HoldingPage,
  TradePage,
} from "@baskfy/api-client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { accessToken, browserApi } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { ApiError } from "@/lib/api/errors";

/**
 * The backtests data layer — docs/07 §Backtests.
 *
 * docs/08 §Routes marks `/backtests` and `/backtests/[id]` as **"client + polling/SSE"**, and
 * both are implemented: `useBacktest` polls while a run is queued or running, and
 * `subscribeToProgress` opens the SSE stream for the progress bar. The poll is not a fallback
 * bolted on — it is what refreshes the *result* once the stream says the run is done, and it is
 * what keeps the page correct if the stream never connects at all.
 *
 * Why the stream is read with `fetch` rather than `EventSource`
 * -------------------------------------------------------------
 * `EventSource` cannot set an `Authorization` header, and `GET /backtests/{id}/events` is a
 * bearer-authenticated, entitlement-gated route (docs/07 §Entitlements). Putting the token in the
 * query string to satisfy `EventSource` would put a credential in every proxy log. So the stream
 * is read from a `fetch` response body, which carries the header and parses the same wire format.
 */

export const backtestKeys = {
  list: ["backtests"] as const,
  one: (id: string) => ["backtests", id] as const,
  trades: (id: string) => ["backtests", id, "trades"] as const,
  holdings: (id: string, on: string | null) => ["backtests", id, "holdings", on ?? "all"] as const,
};

/** How often a queued or running backtest is re-fetched. */
const RUNNING_POLL_MS = 4_000;

export function isTerminal(status: string | undefined): boolean {
  return status === "done" || status === "failed";
}

export function useBacktests(initial?: BacktestSummaryOut[]) {
  return useQuery({
    queryKey: backtestKeys.list,
    ...(initial ? { initialData: initial } : {}),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((row) => !isTerminal(row.status)) ? RUNNING_POLL_MS : false,
    queryFn: async (): Promise<BacktestSummaryOut[]> => {
      const { data, error } = await browserApi().GET("/api/v1/backtests");
      if (!data) throw ApiError.from(error, "Your backtests could not be loaded.");
      return data.data;
    },
  });
}

export function useBacktest(id: string, initial?: BacktestOut) {
  return useQuery({
    queryKey: backtestKeys.one(id),
    ...(initial ? { initialData: initial } : {}),
    refetchInterval: (query) =>
      isTerminal(query.state.data?.status) ? false : RUNNING_POLL_MS,
    queryFn: async (): Promise<BacktestOut> => {
      const { data, error } = await browserApi().GET("/api/v1/backtests/{public_id}", {
        params: { path: { public_id: id } },
      });
      if (!data) throw ApiError.from(error, "That backtest could not be loaded.");
      return data;
    },
  });
}

export function useQueueBacktest() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (config: BacktestConfigIn): Promise<BacktestAcceptedOut> => {
      const { data, error } = await browserApi().POST("/api/v1/backtests", {
        body: { config, fragility: true },
      });
      if (!data) throw ApiError.from(error, "The backtest could not be queued.");
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: backtestKeys.list }),
  });
}

export function useDeleteBacktest() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (id: string): Promise<void> => {
      const { error } = await browserApi().DELETE("/api/v1/backtests/{public_id}", {
        params: { path: { public_id: id } },
      });
      if (error) throw ApiError.from(error, "The backtest could not be deleted.");
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: backtestKeys.list }),
  });
}

export function useTrades(id: string, enabled: boolean) {
  return useQuery({
    queryKey: backtestKeys.trades(id),
    enabled,
    queryFn: async (): Promise<TradePage> => {
      const { data, error } = await browserApi().GET("/api/v1/backtests/{public_id}/trades", {
        params: { path: { public_id: id }, query: { limit: 500 } },
      });
      if (!data) throw ApiError.from(error, "The trade log could not be loaded.");
      return data;
    },
  });
}

export function useHoldings(id: string, rebalanceDate: string | null, enabled: boolean) {
  return useQuery({
    queryKey: backtestKeys.holdings(id, rebalanceDate),
    enabled,
    queryFn: async (): Promise<HoldingPage> => {
      const { data, error } = await browserApi().GET("/api/v1/backtests/{public_id}/holdings", {
        params: {
          path: { public_id: id },
          query: {
            limit: 500,
            ...(rebalanceDate ? { rebalance_date: rebalanceDate } : {}),
          },
        },
      });
      if (!data) throw ApiError.from(error, "The holdings could not be loaded.");
      return data;
    },
  });
}

export type Artefact = "trades" | "holdings" | "equity";

/** docs/07: `GET /backtests/{id}/export` → a signed URL. The link is short-lived, so it is
 * fetched on the click rather than rendered into the page and left to go stale. */
export async function exportLink(id: string, artefact: Artefact): Promise<ExportLinkOut> {
  const { data, error } = await browserApi().GET("/api/v1/backtests/{public_id}/export", {
    params: { path: { public_id: id }, query: { artefact } },
  });
  if (!data) throw ApiError.from(error, "The download link could not be created.");
  return data;
}

export interface ProgressFrame {
  public_id: string;
  status: string;
  stage: string;
  completed: number;
  total: number;
  percent: number;
  as_of: string | null;
  detail: string | null;
}

function isProgressFrame(value: unknown): value is ProgressFrame {
  if (typeof value !== "object" || value === null) return false;
  const frame = value as Record<string, unknown>;
  return typeof frame.status === "string" && typeof frame.percent === "number";
}

/**
 * Read `GET /backtests/{id}/events` and call `onFrame` for each `data:` line.
 *
 * Returns an abort function. Errors are swallowed *into the callback contract* rather than
 * thrown: the page always has the poll behind it, so a stream that cannot connect must degrade to
 * a slower progress bar and not to an error boundary.
 */
export function subscribeToProgress(
  id: string,
  onFrame: (frame: ProgressFrame) => void,
): () => void {
  const controller = new AbortController();

  void (async () => {
    let token: string | undefined;
    try {
      token = await accessToken();
    } catch {
      return;
    }
    let response: Response;
    try {
      response = await fetch(`${apiOrigin()}/api/v1/backtests/${id}/events`, {
        headers: {
          Accept: "text/event-stream",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        signal: controller.signal,
      });
    } catch {
      return;
    }
    if (!response.ok || !response.body) return;

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() ?? "";
        for (const chunk of chunks) {
          const frame = parseFrame(chunk);
          if (frame) onFrame(frame);
        }
      }
    } catch {
      // An aborted or broken stream is not an error the page shows; the poll covers it.
    }
  })();

  return () => controller.abort();
}

/** One SSE event block. Comment lines (`: keep-alive`) and unparsable payloads are ignored. */
export function parseFrame(chunk: string): ProgressFrame | null {
  const line = chunk.split("\n").find((candidate) => candidate.startsWith("data: "));
  if (!line) return null;
  try {
    const parsed: unknown = JSON.parse(line.slice("data: ".length));
    return isProgressFrame(parsed) ? parsed : null;
  } catch {
    return null;
  }
}
