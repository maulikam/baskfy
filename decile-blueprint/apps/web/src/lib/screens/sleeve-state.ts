import { useQuery } from "@tanstack/react-query";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

/**
 * Per-sleeve state for the Scan buttons (LV4, review P1.3): the wire shape, its parser, the
 * poll and the words. One request for all three sleeves (`GET /sleeves/state`) every 30 s; the
 * server derives every word from stored rows and the clock. Nothing here can queue, plan or
 * place.
 */

export type SleeveName = "swing" | "twt" | "vbt";
export type SleeveStateName =
  | "closed"
  | "waiting_for_login"
  | "scanning"
  | "signal_ready"
  | "plan_ready"
  | "monitoring"
  | "missed_window"
  | "blocked"
  | "idle";

export interface SleeveState {
  sleeve: SleeveName;
  state: SleeveStateName;
  reason: string;
  scanMeans: string;
  asOf: string | null;
  next: string;
  updatedAt: string;
}

export const SLEEVE_STATE_POLL_MS = 30_000;

const STATE_NAMES: ReadonlySet<string> = new Set([
  "closed",
  "waiting_for_login",
  "scanning",
  "signal_ready",
  "plan_ready",
  "monitoring",
  "missed_window",
  "blocked",
  "idle",
]);

interface SleeveStatePayload {
  sleeve?: string;
  state?: string;
  reason?: string;
  scan_means?: string;
  as_of?: string | null;
  next?: string;
  updated_at?: string;
}

export function parseSleeveStates(payload: unknown): SleeveState[] {
  if (!Array.isArray(payload)) return [];
  const out: SleeveState[] = [];
  for (const row of payload as SleeveStatePayload[]) {
    if (
      (row.sleeve !== "swing" && row.sleeve !== "twt" && row.sleeve !== "vbt") ||
      typeof row.state !== "string" ||
      !STATE_NAMES.has(row.state)
    ) {
      continue;
    }
    out.push({
      sleeve: row.sleeve,
      state: row.state as SleeveStateName,
      reason: row.reason ?? "",
      scanMeans: row.scan_means ?? "",
      asOf: row.as_of ?? null,
      next: row.next ?? "",
      updatedAt: row.updated_at ?? "",
    });
  }
  return out;
}

export async function fetchSleeveStates(): Promise<SleeveState[]> {
  const token = await accessToken();
  if (!token) return [];
  const response = await fetch(new URL("/api/v1/sleeves/state", apiOrigin()), {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    signal: AbortSignal.timeout(4000),
  });
  if (!response.ok) return [];
  return parseSleeveStates(await response.json());
}

/** The word on the chip. Short, and never "live" or "intraday" for a closed-session scan. */
export const STATE_LABEL: Record<SleeveStateName, string> = {
  closed: "Market closed",
  waiting_for_login: "Waiting for Kite login",
  scanning: "Scanning…",
  signal_ready: "Signals ready",
  plan_ready: "Plan ready",
  monitoring: "Monitoring",
  missed_window: "Entry window missed",
  blocked: "Blocked",
  idle: "Idle",
};

type Tone = "neutral" | "accent" | "positive" | "negative" | "warning";

export function stateTone(state: SleeveStateName): Tone {
  switch (state) {
    case "blocked":
      return "negative";
    case "missed_window":
    case "waiting_for_login":
      return "warning";
    case "monitoring":
    case "plan_ready":
    case "signal_ready":
      return "positive";
    case "scanning":
      return "accent";
    default:
      return "neutral";
  }
}

export function useSleeveStates(): SleeveState[] {
  const { data } = useQuery({
    queryKey: ["sleeve-states"],
    queryFn: fetchSleeveStates,
    staleTime: 15_000,
    refetchInterval: SLEEVE_STATE_POLL_MS,
  });
  return data ?? [];
}

