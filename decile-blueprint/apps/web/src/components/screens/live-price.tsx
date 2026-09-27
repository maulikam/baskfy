"use client";

import { createContext, useContext, type ReactNode } from "react";

import {
  EMPTY_LIVE_MARKS,
  formatTodayChange,
  liveStatusLine,
  quoteTitle,
  useLiveMarks,
  type LiveMarks,
} from "@/lib/screens/live-marks";
import { cn } from "@/lib/utils";

/**
 * The screens' live price overlay, shared by every screen-style table (21 Sep 2026).
 *
 * A server-rendered page wraps its table in `<LiveMarksProvider symbols=…>`; each price cell is a
 * `<LivePrice>`, and the page states which numbers are live with `<LiveStatus>`. One query per
 * page, polled every 30 s only while the market is open. Ranks, patterns and every other column
 * stay on the published session — this touches the price cell and nothing else.
 *
 * A row whose print the server judged stale (LV1) is muted with the last quote's exchange time
 * in its tooltip; a page whose answer is too old or whose last fetch failed is back on the close.
 */

const LiveMarksContext = createContext<LiveMarks>(EMPTY_LIVE_MARKS);

export function LiveMarksProvider({
  symbols,
  children,
}: {
  symbols: readonly string[];
  children: ReactNode;
}) {
  const live = useLiveMarks(symbols);
  return <LiveMarksContext.Provider value={live}>{children}</LiveMarksContext.Provider>;
}

export function useLiveMarksContext(): LiveMarks {
  return useContext(LiveMarksContext);
}

function changeTone(changePct: number | null): string {
  if (changePct === null || changePct === 0) return "text-muted-foreground";
  return changePct > 0 ? "text-positive" : "text-negative";
}

/** "+1.36% today", coloured, for a live row. Renders nothing when there is no change to show. */
export function TodayChange({
  changePct,
  className,
}: {
  changePct: number | null;
  className?: string;
}) {
  if (changePct === null) return null;
  return (
    <span
      className={cn("block text-xs tabular-nums", changeTone(changePct), className)}
      data-testid="live-change"
    >
      {formatTodayChange(changePct)}
    </span>
  );
}

/**
 * One row's price: the live last price and today's change while live, else the close the page
 * already had, unchanged. `close` is whatever the table showed before the overlay existed.
 */
export function LivePrice({
  symbol,
  close = null,
  fallback,
  className,
}: {
  symbol: string;
  close?: number | null;
  /** What the cell showed before the overlay, when it is richer than a bare number. */
  fallback?: ReactNode;
  className?: string;
}) {
  const live = useLiveMarksContext();
  const quote = live.liveOverlay ? live.quotes[symbol.trim().toUpperCase()] : undefined;
  if (quote === undefined) {
    if (fallback !== undefined) return <>{fallback}</>;
    return (
      <span className={cn("tabular-nums", className)} data-testid="close-price">
        {close === null ? "—" : close.toFixed(2)}
      </span>
    );
  }
  return (
    <span
      className={cn("tabular-nums", quote.stale && "text-muted-foreground", className)}
      title={quoteTitle(quote)}
      data-stale={quote.stale ? "true" : "false"}
    >
      <span data-testid="live-price">{quote.lastPrice.toFixed(2)}</span>
      <TodayChange
        changePct={quote.changePct}
        {...(quote.stale ? { className: "text-muted-foreground" } : {})}
      />
    </span>
  );
}

/** The line above a table that says which numbers are live and which are the close. */
export function LiveStatus({ asOf, className }: { asOf: string | null; className?: string }) {
  const live = useLiveMarksContext();
  return (
    <p
      className={cn(
        "flex items-center gap-1.5 text-xs text-muted-foreground",
        className,
      )}
      data-testid="live-status"
      data-live={live.liveOverlay ? "true" : "false"}
    >
      {live.liveOverlay ? (
        <span aria-hidden="true" className="inline-block size-1.5 rounded-full bg-positive" />
      ) : null}
      {liveStatusLine(live, asOf)}
    </p>
  );
}
