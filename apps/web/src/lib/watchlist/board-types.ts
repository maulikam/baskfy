/** The watchlist board (Maulik, 12 Nov 2026). Fractions are fractions; volumes are shares. */
export interface WatchBoardItem {
  symbol: string;
  name: string | null;
  sector: string | null;
  watched_at: string | null;
  /** The last published session for this stock. */
  last_date: string | null;
  /** That session's close, rupees. */
  close: string | null;
  day_change_fraction: string | null;
  week_change_fraction: string | null;
  month_change_fraction: string | null;
  three_month_change_fraction: string | null;
  day_volume: number | null;
  /** Last 5 sessions. */
  week_volume: number | null;
  /** Last 21 sessions. */
  month_volume: number | null;
  /** Last session's volume against the 20 before it. */
  volume_vs_avg20: string | null;
  doubling_score: string | null;
  doubling_rank: number | null;
  doubling_scope: string | null;
  swing_score: string | null;
  swing_rank: number | null;
  swing_scope: string | null;
  swing_picked: boolean;
}

export interface WatchBoard {
  as_of: string | null;
  items: readonly WatchBoardItem[];
}

/** Shares, Indian style: 1.4 Cr, 6.5 L, 12,340. */
export function formatShares(v: number | null): string {
  if (v === null) return "—";
  if (v >= 1e7) return `${(v / 1e7).toFixed(2)} Cr`;
  if (v >= 1e5) return `${(v / 1e5).toFixed(1)} L`;
  return v.toLocaleString("en-IN");
}
