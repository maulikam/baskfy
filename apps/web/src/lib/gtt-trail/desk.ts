/** GTT-TRAIL: the desk's trailing-stop policy, preview and journal (same-origin `/desk/gtt-trail/*`). */
import { deskGet, deskJson } from "@/lib/desk-actions/client";

export const STRATEGY_LABELS: Record<string, string> = {
  swing: "Swing",
  twt: "Three weeks tight",
  vbt: "Volume breakout",
  screen: "Screen invest (MTF)",
  weekly: "Weekly rebalance",
};

export type TrailPolicy = {
  strategy: string;
  enabled: boolean;
  breakeven_after_pct: number;
  breakeven_buffer_pct: number;
  trail_after_pct: number;
  trail_pct: number;
  min_step_pct: number;
};

export type TrailSettings = {
  master_enabled: boolean;
  dry_run: boolean;
  wired: string[];
  policies: TrailPolicy[];
};

export type TrailRow = {
  strategy: string;
  position_id: number;
  symbol: string;
  qty: number;
  entry: number | null;
  entry_date: string | null;
  as_of: string | null;
  last_close: number | null;
  highest_close: number | null;
  current_stop: number | null;
  proposed_stop: number | null;
  locked_pct: number | null;
  would_change: boolean;
  reason: string;
  blocker: string;
  gtt_id: string | null;
  policy_enabled: boolean;
};

export type TrailPreview = {
  as_of: string;
  master_enabled: boolean;
  dry_run: boolean;
  policies: TrailPolicy[];
  rows: TrailRow[];
};

export type TrailJournalRow = {
  id: number;
  at: string | null;
  source: string | null;
  strategy: string | null;
  symbol: string | null;
  old_stop: number | null;
  new_stop: number | null;
  reason: string | null;
  outcome: string | null;
  detail: string | null;
};

export function fetchTrailSettings(): Promise<TrailSettings> {
  return deskGet<TrailSettings>("/gtt-trail/settings");
}

export function saveTrailSettings(policies: TrailPolicy[]): Promise<TrailSettings> {
  return deskJson<TrailSettings>("PUT", "/gtt-trail/settings", { policies });
}

export function fetchTrailPreview(): Promise<TrailPreview> {
  return deskGet<TrailPreview>("/gtt-trail/preview");
}

export function fetchTrailJournal(limit = 50): Promise<{ rows: TrailJournalRow[] }> {
  return deskGet<{ rows: TrailJournalRow[] }>("/gtt-trail/journal", { limit: String(limit) });
}

/** Client-side bounds, mirroring the desk's (the desk validates again). */
export function policyProblem(p: TrailPolicy): string | null {
  const within = (v: number, lo: number, hi: number) => Number.isFinite(v) && v >= lo && v <= hi;
  if (!within(p.breakeven_after_pct, 0.5, 100)) return "Breakeven after must be 0.5–100%.";
  if (!within(p.breakeven_buffer_pct, 0, 5)) return "Breakeven buffer must be 0–5%.";
  if (p.breakeven_buffer_pct >= p.breakeven_after_pct) return "Buffer must be below breakeven after.";
  if (!within(p.trail_after_pct, 0.5, 200)) return "Trail after must be 0.5–200%.";
  if (!within(p.trail_pct, 1, 50)) return "Trail must be 1–50%.";
  if (p.strategy === "twt" && p.trail_pct < 18) return "TWT's trail may not be tighter than 18% (TW0.5).";
  if (!within(p.min_step_pct, 0, 10)) return "Min step must be 0–10%.";
  return null;
}
