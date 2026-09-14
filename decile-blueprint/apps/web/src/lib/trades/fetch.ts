import "server-only";

import type { Schemas } from "@baskfy/api-client";

import { serverApiOrigin } from "@/lib/api/config";
import { SERVER_FETCH_TIMEOUT_MS } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";

/**
 * Trade history — `/trades` (docs/07 §"Trade history").
 *
 * Kite's API has today's trades and nothing else (NEEDS-MAULIK §32), so history arrives two ways:
 * a Zerodha Console tradebook CSV, and a same-day capture. Every call carries the signed-in user's
 * bearer from the server; nothing here can place an order.
 */

export type TradebookImport = Schemas["TradebookImportOut"];
export type BrokerTrades = Schemas["BrokerTradesOut"];

/** A result, or the sentence saying why there is none. */
export type TradeWrite = { ok: true; report: TradebookImport } | { ok: false; error: string };

async function bearer(): Promise<Record<string, string> | null> {
  const token = (await auth())?.accessToken;
  return token ? { Authorization: `Bearer ${token}` } : null;
}

async function problemText(response: Response, fallback: string): Promise<string> {
  const body: unknown = await response.json().catch(() => null);
  if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
    return body.detail;
  }
  return `${fallback} (${response.status})`;
}

export async function fetchTrades(limit = 50): Promise<BrokerTrades | null> {
  const headers = await bearer();
  if (!headers) return null;
  const response = await fetch(`${serverApiOrigin()}/api/v1/trades?limit=${limit}`, {
    headers,
    cache: "no-store",
    signal: AbortSignal.timeout(SERVER_FETCH_TIMEOUT_MS),
  });
  if (!response.ok) return null;
  return (await response.json()) as BrokerTrades;
}

export async function importTradebook(file: File): Promise<TradeWrite> {
  const headers = await bearer();
  if (!headers) return { ok: false, error: "Sign in to import a tradebook." };
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(`${serverApiOrigin()}/api/v1/trades/import`, {
    method: "POST",
    body,
    headers,
    cache: "no-store",
  });
  if (!response.ok) {
    return { ok: false, error: await problemText(response, "The tradebook could not be imported") };
  }
  return { ok: true, report: (await response.json()) as TradebookImport };
}

export async function syncTodaysTrades(): Promise<TradeWrite> {
  const headers = await bearer();
  if (!headers) return { ok: false, error: "Sign in to capture today's trades." };
  const response = await fetch(`${serverApiOrigin()}/api/v1/trades/sync-today`, {
    method: "POST",
    headers,
    cache: "no-store",
    signal: AbortSignal.timeout(SERVER_FETCH_TIMEOUT_MS),
  });
  if (!response.ok) {
    return { ok: false, error: await problemText(response, "Today's trades could not be read") };
  }
  return { ok: true, report: (await response.json()) as TradebookImport };
}
