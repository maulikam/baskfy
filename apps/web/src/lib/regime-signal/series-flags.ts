"use client";

import { useEffect, useState } from "react";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

type Flags = Readonly<Record<string, string>>;

let cache: Flags | null = null;
let pending: Promise<Flags> | null = null;

function load(): Promise<Flags> {
  pending ??= accessToken()
    .then((token) =>
      fetch(new URL("/api/v1/meta/series-flags", apiOrigin()), {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: AbortSignal.timeout(8000),
      }),
    )
    .then((r) => (r.ok ? (r.json() as Promise<{ data?: Flags }>) : { data: {} }))
    .then((body) => {
      cache = body.data ?? {};
      return cache;
    })
    .catch(() => {
      pending = null;
      return {};
    });
  return pending;
}

/** The NSE series for a symbol when it is not EQ (BE, BZ, SM, ST...), else null. One fetch per page load. */
export function useSeriesOf(symbol: string | null | undefined): string | null {
  const [flags, setFlags] = useState<Flags | null>(cache);
  useEffect(() => {
    if (flags) return;
    let live = true;
    void load().then((f) => {
      if (live) setFlags(f);
    });
    return () => {
      live = false;
    };
  }, [flags]);
  const key = (symbol ?? "").trim().toUpperCase();
  return key && flags ? (flags[key] ?? null) : null;
}
