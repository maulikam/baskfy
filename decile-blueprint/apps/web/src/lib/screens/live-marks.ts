import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

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
 *
 * Honest end to end (LV1, 27 Sep 2026): every quote carries the exchange's own time and the
 * server's `stale` verdict (older than 120 s at `served_at`); the answer counts `requested` and
 * `covered` names; and the browser drops the whole overlay once its last good answer is older
 * than `LIVE_MAX_AGE_MS` or the last fetch failed — react-query keeps the previous `data` on an
 * error, and a price that stopped moving must not keep reading as live.
 */

export type LiveReason = "market_closed" | "no_session" | "unavailable";

export interface LiveQuote {
  lastPrice: number;
  prevClose: number | null;
  /** Today's move against the exchange's previous close, in percent, rounded by the server. */
  changePct: number | null;
  /**
   * The server's verdict: the exchange's stamp was more than 120 s before `served_at`. A stale
   * row is shown muted, never as a live number. A quote with no `asOf` is never stale — its age
   * is unknown, not zero — and the cell says the exchange sent no time.
   */
  stale: boolean;
  /** The exchange's own time for the print (ISO, IST offset), or `null` when Kite sent none. */
  asOf: string | null;
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
  /** Browser epoch ms when this answer arrived; `null` for the empty value and parsed fixtures. */
  receivedAt: number | null;
  /** Distinct names the page asked for. */
  requested: number;
  /** How many of them the answer quotes. The rest keep the close; the status line counts them. */
  covered: number;
}

export const EMPTY_LIVE_MARKS: LiveMarks = {
  liveOverlay: false,
  marks: {},
  quotes: {},
  reason: null,
  marketOpen: false,
  asOf: null,
  receivedAt: null,
  requested: 0,
  covered: 0,
};

/** 30 s while the market is open — the server memo is 20 s, so this is one Kite batch a window. */
export const LIVE_POLL_MS = 30_000;
/** While closed, a cheap re-check (no Kite call server-side) so the page turns live at 09:15. */
export const CLOSED_POLL_MS = 5 * 60_000;
/**
 * A live answer older than this is not live any more: three missed 30 s polls. The overlay is
 * dropped and the page returns to the close with `reason: "unavailable"`.
 */
export const LIVE_MAX_AGE_MS = 90_000;
/** How often the age is re-checked without a fetch, so a tab that lost the API goes dark on time. */
export const LIVE_AGE_TICK_MS = 15_000;

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
  served_at?: string | null;
  requested?: number;
  covered?: number;
  marks?: Record<string, string | number>;
  quotes?: Record<
    string,
    {
      last_price?: string | number;
      prev_close?: string | number | null;
      change_pct?: string | number | null;
      as_of?: string | null;
      stale?: boolean;
    }
  >;
}

function toCount(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}

/**
 * Parse the wire shape (decimal strings, house rule 9) into display numbers. `receivedAt` is the
 * browser's clock at receipt; the parser itself reads no clock so a fixture parses the same way
 * every time.
 */
export function parseLiveMarks(
  payload: LiveMarksPayload,
  receivedAt: number | null = null,
): LiveMarks {
  const quotes: Record<string, LiveQuote> = {};
  for (const [symbol, quote] of Object.entries(payload.quotes ?? {})) {
    const lastPrice = toNumber(quote.last_price);
    if (lastPrice === null || lastPrice <= 0) continue;
    quotes[symbol] = {
      lastPrice,
      prevClose: toNumber(quote.prev_close),
      changePct: toNumber(quote.change_pct),
      stale: quote.stale === true,
      asOf: typeof quote.as_of === "string" && quote.as_of !== "" ? quote.as_of : null,
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
  const requested = toCount(payload.requested);
  // Never trust a count the quotes do not back: `covered` is what the page can actually show.
  const covered = live ? Math.min(toCount(payload.covered), Object.keys(marks).length) : 0;
  return {
    liveOverlay: live,
    marks: live ? marks : {},
    quotes: live ? quotes : {},
    reason: live ? null : (reason ?? "unavailable"),
    marketOpen: payload.market_open === true,
    asOf: payload.as_of ?? null,
    receivedAt,
    requested: Math.max(requested, covered),
    covered,
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
  return parseLiveMarks((await response.json()) as LiveMarksPayload, Date.now());
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

/** What the last fetch left behind, as react-query reports it. */
export interface LiveFetchState {
  /** The last fetch threw (network, timeout, abort). react-query keeps the previous `data`. */
  isError: boolean;
  /** react-query's receipt time for `data` (epoch ms); 0 before the first answer. */
  dataUpdatedAt: number;
  /** The browser's clock now (epoch ms). */
  now: number;
}

/** The overlay with everything live removed, and the reason the page is on the close. */
function droppedOverlay(data: LiveMarks): LiveMarks {
  return { ...data, liveOverlay: false, marks: {}, quotes: {}, reason: "unavailable", covered: 0 };
}

/**
 * Decide what the page may show from the last answer and how it was obtained. Pure, so the
 * rule is testable without react-query: an errored fetch or an answer older than
 * `LIVE_MAX_AGE_MS` is not live, whatever the answer said when it arrived. Epoch ms throughout;
 * the exchange's own stamps are the server's business (`stale`).
 */
export function resolveLiveMarks(data: LiveMarks | undefined, state: LiveFetchState): LiveMarks {
  if (data === undefined) return EMPTY_LIVE_MARKS;
  if (state.isError) return droppedOverlay(data);
  if (!data.liveOverlay) return data;
  const receivedAt = state.dataUpdatedAt > 0 ? state.dataUpdatedAt : data.receivedAt;
  if (receivedAt === null) return droppedOverlay(data);
  return state.now - receivedAt > LIVE_MAX_AGE_MS ? droppedOverlay(data) : data;
}

/** The browser's clock, re-read every `LIVE_AGE_TICK_MS` only while there is something to age. */
function useNowTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    // Only the interval sets state (the lint rule against setting state synchronously in an
    // effect); the first reading is the mount-time clock, so a page that goes live later can
    // be at most one tick (15 s) generous before the age check catches up.
    if (!active) return undefined;
    const timer = setInterval(() => setNow(Date.now()), LIVE_AGE_TICK_MS);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

export function useLiveMarks(symbols: readonly string[]): LiveMarks {
  const key = liveMarksKey(symbols);
  const { data, isError, dataUpdatedAt } = useQuery({
    queryKey: ["live-marks", key],
    queryFn: () => fetchLiveMarks(symbols),
    enabled: key.length > 0,
    staleTime: 15_000,
    refetchInterval: (query) => livePollInterval(query.state.data),
  });
  const now = useNowTick(data?.liveOverlay === true);
  return useMemo(
    () => resolveLiveMarks(data, { isError, dataUpdatedAt, now }),
    [data, isError, dataUpdatedAt, now],
  );
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

/** "13:02:11 IST" — a quote's exchange time, for the stale tooltip. IST, never the browser's zone. */
export function formatQuoteTimeIST(iso: string | null): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "";
  const rendered = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  }).format(parsed);
  return `${rendered} IST`;
}

/** The cell's tooltip: live, live without an exchange time, or stale with the last print's time. */
export function quoteTitle(quote: LiveQuote): string {
  if (quote.stale) {
    const at = formatQuoteTimeIST(quote.asOf);
    return at ? `Stale — last quote ${at}` : "Stale — last quote time unknown";
  }
  return quote.asOf === null
    ? "Live — Kite last price · the exchange sent no time for this quote"
    : "Live — Kite last price";
}

const REASON_COPY: Record<LiveReason, string> = {
  market_closed: "market closed",
  no_session: "no Kite session today, so no live prices",
  unavailable: "live prices unavailable right now",
};

/**
 * The one sentence every screen shows above its table, so a reader knows which numbers move.
 *
 * Live: "Live prices · today's change vs previous close · ranks as of 18 Sep 2026"; when the
 * answer quoted fewer names than the page asked for, "Live prices · 42 of 50 live · …" so the
 * rows on the close are counted, not hidden.
 * Not live: "Close as of 18 Sep 2026 · market closed" (or the reason there is no quote).
 */
export function liveStatusLine(live: LiveMarks, asOf: string | null): string {
  const date = formatTradeDate(asOf ?? live.asOf);
  if (live.liveOverlay) {
    const coverage =
      live.requested > 0 && live.covered < live.requested
        ? ` · ${live.covered} of ${live.requested} live`
        : "";
    return `Live prices${coverage} · today's change vs previous close · ranks as of ${date}`;
  }
  return !live.reason
    ? `Close as of ${date}`
    : `Close as of ${date} · ${REASON_COPY[live.reason]}`;
}
