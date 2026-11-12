import type { LiveQuote } from "@/lib/screens/live-marks";
import type { WatchBoardItem } from "@/lib/watchlist/board-types";

/** One board row with today's move resolved: a Kite quote while live, else the last close. */
export interface BoardRow extends WatchBoardItem {
  /** Price shown: live last price, or the last session's close. */
  price: number | null;
  /** Today's move as a fraction: live change vs previous close, or the last session's move. */
  dayFraction: number | null;
  /** "live" (fresh Kite quote), "stale" (old Kite quote, muted), "close" (published session). */
  source: "live" | "stale" | "close";
}

export type BoardSortKey =
  | "symbol"
  | "day"
  | "day_volume"
  | "week_volume"
  | "month_volume"
  | "week"
  | "month"
  | "three_month"
  | "doubling"
  | "swing";

function num(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Overlay a Kite quote only while the overlay is live; never claim live without one. */
export function boardRows(
  items: readonly WatchBoardItem[],
  quotes: Readonly<Record<string, LiveQuote>>,
  liveOverlay: boolean,
): BoardRow[] {
  return items.map((item) => {
    const quote = liveOverlay ? quotes[item.symbol.trim().toUpperCase()] : undefined;
    if (quote !== undefined && quote.changePct !== null) {
      return {
        ...item,
        price: quote.lastPrice,
        dayFraction: quote.changePct / 100,
        source: quote.stale ? "stale" : "live",
      };
    }
    return {
      ...item,
      price: num(item.close),
      dayFraction: num(item.day_change_fraction),
      source: "close",
    };
  });
}

function sortValue(row: BoardRow, key: BoardSortKey): number | string | null {
  switch (key) {
    case "symbol":
      return row.symbol;
    case "day":
      return row.dayFraction;
    case "day_volume":
      return row.day_volume;
    case "week_volume":
      return row.week_volume;
    case "month_volume":
      return row.month_volume;
    case "week":
      return num(row.week_change_fraction);
    case "month":
      return num(row.month_change_fraction);
    case "three_month":
      return num(row.three_month_change_fraction);
    case "doubling":
      return num(row.doubling_score);
    case "swing":
      return num(row.swing_score);
  }
}

/** Symbol reads A→Z first; every number reads biggest first. */
export function defaultAscending(key: BoardSortKey): boolean {
  return key === "symbol";
}

/** Sort by one column; rows with no value always sink to the bottom, ties break on symbol. */
export function sortBoard(rows: readonly BoardRow[], key: BoardSortKey, ascending: boolean): BoardRow[] {
  const dir = ascending ? 1 : -1;
  return [...rows].sort((a, b) => {
    const va = sortValue(a, key);
    const vb = sortValue(b, key);
    if (va === null && vb !== null) return 1;
    if (vb === null && va !== null) return -1;
    if (va !== null && vb !== null && va !== vb) {
      if (typeof va === "string" || typeof vb === "string") {
        return String(va).localeCompare(String(vb)) * dir;
      }
      return (va - vb) * dir;
    }
    return a.symbol.localeCompare(b.symbol);
  });
}

/** What the Add box sends: trimmed, upper-cased, `NSE:` dropped; null when it cannot be a symbol. */
export function normaliseSymbol(input: string): string | null {
  let s = input.trim().toUpperCase();
  if (s.startsWith("NSE:")) s = s.slice(4).trim();
  return /^[A-Z0-9][A-Z0-9&_.-]{0,29}$/.test(s) ? s : null;
}

/** "▲ +1.24%" / "▼ −0.80%" / "— 0.00%"; the glyph pairs with colour, never replaced by it. */
export function formatMove(fraction: number | null): string {
  if (fraction === null) return "—";
  const pct = fraction * 100;
  const rounded = Math.abs(pct).toFixed(2);
  if (rounded === "0.00") return "— 0.00%";
  return pct > 0 ? `▲ +${rounded}%` : `▼ −${rounded}%`;
}

/** Colour class for a move; flat and missing stay muted. */
export function moveTone(fraction: number | null): string {
  if (fraction === null || Math.abs(fraction) < 0.00005) return "text-muted-foreground";
  return fraction > 0 ? "text-positive" : "text-negative";
}

/** "×2.3 vs 20-day avg" for a volume spike hint; empty when unknown. */
export function formatVolumeRatio(ratio: string | null): string {
  const n = num(ratio);
  if (n === null) return "";
  return `×${n.toFixed(1)} vs 20-day avg`;
}

/** The Add box's answer to a failed POST, in words. */
export function addErrorMessage(status: number, symbol: string): string {
  if (status === 404) return `No listed stock called ${symbol}. Check the NSE symbol.`;
  if (status === 400) return `${symbol} is not a valid symbol.`;
  if (status === 401) return "Your session has ended. Sign in again.";
  return `Could not add ${symbol} (${status}).`;
}
