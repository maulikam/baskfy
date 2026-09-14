import "server-only";

import type { Schemas } from "@baskfy/api-client";
import type { Route } from "next";

import { serverApiOrigin } from "@/lib/api/config";
import { SERVER_FETCH_TIMEOUT_MS } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";

/**
 * `GET /instruments/{symbol}/appearances` — the screens and strategy scans whose latest stored
 * result names this stock (14 Sep 2026). Per user and `no-store`: the factsheet itself is cached
 * for everyone, this strip is the caller's own.
 */

export type Appearances = Schemas["AppearancesOut"];
export type Appearance = Schemas["AppearanceOut"];

export async function fetchAppearances(symbol: string): Promise<Appearances | null> {
  const token = (await auth())?.accessToken;
  if (!token) return null;
  const response = await fetch(
    `${serverApiOrigin()}/api/v1/instruments/${encodeURIComponent(symbol)}/appearances`,
    {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(SERVER_FETCH_TIMEOUT_MS),
    },
  );
  if (!response.ok) return null;
  return (await response.json()) as Appearances;
}

/** Where an appearance links: a screen opens in Build, a strategy on its own hub. */
export function appearanceHref(item: Appearance): Route {
  if (item.kind === "screen" || item.kind === "template") {
    return `/build/${encodeURIComponent(item.ref)}` as Route;
  }
  return item.ref as Route;
}
