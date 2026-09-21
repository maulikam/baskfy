import { useQuery } from "@tanstack/react-query";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { formatTradeDate } from "@/lib/format";

/**
 * The screens' live price overlay (Maulik, 21 Sep 2026: "screens should have live data").
 *
 * Screen runs stay byte-identical on the published session (docs/06), and so do their ranks,
 * factors, patterns and `as_of` — root CLAUDE.md, "Which date the product shows". This fetch is a
 * second request, `GET /meta/live-marks`, that overlays Kite's last price and today's change in
 * the browser while the NSE session is open and a Kite session exists. Outside those hours the
 * server answers `live: false` with a reason and never calls Kite, and the page keeps the close
 * and says so.
 */

export type LiveReason = "market_closed" | "no_session" | "unavailable";

export interface LiveQuote {
  lastPrice: number;
  prevClose: number | null;
  /** Today's move against the exchange's previous close, in percent, rounded by the server. */
  changePct: number | null;
}

export interface LiveMarks {
  /** True only when the market is open and Kite answered. Same as `live`, older name. */
  liveOverlay: boolean;
  marks: Readonly<Record<string, number>>;
  quotes: Readonly<Record<string, LiveQuote>>;
  /** Why the page is on the close. `null` while live, or before the first answer. */
  reason: LiveReason | null;
  marketOpen: boolean;
  /** The published session the ranks were computed on. The overlay never moves it. */
  asOf: string | null;
}

export const EMPTY_LIVE_MARKS: LiveMarks = {
  liveOverlay: false,
  marks: {},
  quotes: {},
  reason: null,
  marketOpen: false,
  asOf: null,
};

/** 30 s while the market is open — the server memo is 20 s, so this is one Kite batch a window. */
export const LIVE_POLL_MS = 30_000;
/** While closed, a cheap re-check (no Kite call server-side) so the page turns live at 09:15. */
export const CLOSED_POLL_MS = 5 * 60_000;

const REASONS: ReadonlySet<string> = new Set(["market_closed", "no_session", "unavailable"]);

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

interface LiveMarksPayload {
  live?: boolean;
  live_overlay?: boolean;
  reason?: string | null;
  market_open?: boolean;
  as_of?: string | null;
  marks?: Record<string, string | number>;
  quotes?: Record<
    string,
    { last_price?: string | number; prev_close?: string | number | null; change_pct?: string | number | null }
  >;
}

/** Parse the wire shape (decimal strings, house rule 9) into display numbers. */
export function parseLiveMarks(payload: LiveMarksPayload): LiveMarks {
  const quotes: Record<string, LiveQuote> = {};
  for (const [symbol, quote] of Object.entries(payload.quotes ?? {})) {
    const lastPrice = toNumber(quote.last_price);
    if (lastPrice === null || lastPrice <= 0) continue;
    quotes[symbol] = {
      lastPrice,
      prevClose: toNumber(quote.prev_close),
      changePct: toNumber(quote.change_pct),
    };
  }
  const marks: Record<string, number> = {};
  for (const [symbol, value] of Object.entries(payload.marks ?? {})) {
    const numeric = toNumber(value);
    if (numeric !== null && numeric > 0) marks[symbol] = numeric;
  }
  for (const [symbol, quote] of Object.entries(quotes)) marks[symbol] ??= quote.lastPrice;
  const live =
    Boolean(payload.live ?? payload.live_overlay) && Object.keys(marks).length > 0;
  const reason =
    typeof payload.reason === "string" && REASONS.has(payload.reason)
      ? (payload.reason as LiveReason)
      : null;
  return {
    liveOverlay: live,
    marks: live ? marks : {},
    quotes: live ? quotes : {},
    reason: live ? null : (reason ?? "unavailable"),
    marketOpen: payload.market_open === true,
    asOf: payload.as_of ?? null,
  };
}

export async function fetchLiveMarks(symbols: readonly string[]): Promise<LiveMarks> {
  const unique = [
    ...new Set(symbols.map((symbol) => symbol.trim().toUpperCase()).filter(Boolean)),
  ].slice(0, 500);
  if (unique.length === 0) return EMPTY_LIVE_MARKS;
  const token = await accessToken();
  if (!token) return { ...EMPTY_LIVE_MARKS, reason: "unavailable" };
  const url = new URL("/api/v1/meta/live-marks", apiOrigin());
  url.searchParams.set("symbols", unique.join(","));
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    signal: AbortSignal.timeout(4000),
  });
  if (!response.ok) return { ...EMPTY_LIVE_MARKS, reason: "unavailable" };
  return parseLiveMarks((await response.json()) as LiveMarksPayload);
}

export function liveMarksKey(symbols: readonly string[]): string {
  return [...new Set(symbols.map((symbol) => symbol.trim().toUpperCase()).filter(Boolean))]
    .sort()
    .join(",");
}

/** Poll every 30 s while the market is open; every 5 min otherwise (no Kite call then). */
export function livePollInterval(data: LiveMarks | undefined): number {
  return data?.marketOpen ? LIVE_POLL_MS : CLOSED_POLL_MS;
}

export function useLiveMarks(symbols: readonly string[]): LiveMarks {
  const key = liveMarksKey(symbols);
  const { data } = useQuery({
    queryKey: ["live-marks", key],
    queryFn: () => fetchLiveMarks(symbols),
    enabled: key.length > 0,
    staleTime: 15_000,
    refetchInterval: (query) => livePollInterval(query.state.data),
  });
  return data ?? EMPTY_LIVE_MARKS;
}

/** Prefer the live mark on the Price column; leave every other field on the published row. */
export function withLivePrice<T>(
  row: T,
  marks: Readonly<Record<string, number>>,
  quotes: Readonly<Record<string, LiveQuote>> = {},
): T {
  if (typeof row !== "object" || row === null || !("symbol" in row)) return row;
  const raw = row.symbol;
  const symbol = typeof raw === "string" ? raw.trim().toUpperCase() : "";
  const live = symbol === "" ? undefined : marks[symbol];
  if (live === undefined) return row;
  const change = quotes[symbol]?.changePct;
  return change === undefined || change === null
    ? { ...row, last_price: live }
    : { ...row, last_price: live, live_change_pct: change };
}

/** "+1.36% today" / "−0.40% today" — the sign is always shown, so flat never reads as up. */
export function formatTodayChange(changePct: number | null): string {
  if (changePct === null) return "";
  const sign = changePct > 0 ? "+" : changePct < 0 ? "−" : "±";
  return `${sign}${Math.abs(changePct).toFixed(2)}% today`;
}

const REASON_COPY: Record<LiveReason, string> = {
  market_closed: "market closed",
  no_session: "no Kite session today, so no live prices",
  unavailable: "live prices unavailable right now",
};

/**
 * The one sentence every screen shows above its table, so a reader knows which numbers move.
 *
 * Live: "Live prices · today's change vs previous close · ranks as of 18 Sep 2026".
 * Not live: "Close as of 18 Sep 2026 · market closed" (or the reason there is no quote).
 */
export function liveStatusLine(live: LiveMarks, asOf: string | null): string {
  const date = formatTradeDate(asOf ?? live.asOf);
  if (live.liveOverlay) {
    return `Live prices · today's change vs previous close · ranks as of ${date}`;
  }
  return !live.reason
    ? `Close as of ${date}`
    : `Close as of ${date} · ${REASON_COPY[live.reason]}`;
}
