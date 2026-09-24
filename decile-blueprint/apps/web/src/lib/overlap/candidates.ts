import type { OverlapOut } from "@baskfy/api-client";

/**
 * The shape `/build/overlap` renders from `GET /overlap`, and the one place the wire is
 * reconciled with it. Pure — no fetch, no session — so the table and its tests import it
 * without pulling the server-only read (and next-auth) into a component test.
 */
export type OverlapScope = "actionable" | "all";

export interface OverlapStrategy {
  strategy: "swing" | "volume_breakout" | "three_weeks_tight";
  name: string;
  ref: string;
  as_of: string;
  detail: string;
  actionable: boolean;
}

export interface OverlapScreen {
  name: string;
  public_id: string;
  is_template: boolean;
  as_of: string;
  rank: number | null;
  of: number | null;
  definition_changed: boolean;
}

/**
 * The rules baseline's word on the headline. Display context only — never an input to a rank,
 * a filter, a size or an order — and `source` says what produced it (`rules` today).
 */
export interface OverlapTag {
  event_type:
    | "earnings"
    | "order"
    | "approval"
    | "fundraising"
    | "governance"
    | "corporate_action"
    | "routine"
    | "other";
  review_priority: "high" | "medium" | "low";
  /** The phrases that decided the type — the "why", shown on hover. */
  matched: readonly string[];
  /** `rules` for the keyword baseline, `laya` for the model's answer when it was sure. */
  source: string;
  /** The model's probability for its choice; null for the rules. */
  confidence: number | null;
  /** `"<source>:<event_type>"` of the other reader when the two read the headline differently. */
  disagrees_with: string | null;
}

export interface OverlapCatalyst {
  headline: string | null;
  published_at: string | null;
  url: string | null;
  earnings_date: string | null;
  /** Present exactly when there is a headline to read. */
  tag: OverlapTag | null;
}

export interface OverlapCandidate {
  instrument_id: number;
  symbol: string;
  name: string;
  /** An exchange print. Null when no strategy carried one. */
  close: number | null;
  /** Kite last price while a session exists; null otherwise, and the page keeps the close. */
  last_price: number | null;
  strategy_count: number;
  actionable: boolean;
  strategies: readonly OverlapStrategy[];
  screens: readonly OverlapScreen[];
  catalyst: OverlapCatalyst | null;
}

export interface OverlapCandidates {
  sessions: {
    swing: string | null;
    volume_breakout: string | null;
    three_weeks_tight: string | null;
  };
  scope: OverlapScope;
  /** False for a caller who is not the sole tenant: the sleeves are one person's scans. */
  strategies_read: boolean;
  screens_checked: number;
  data: readonly OverlapCandidate[];
}

/**
 * The API's canonical encoder serves a `Decimal` as a JSON number at its stored precision
 * (house rule 8); the generated schema types it as a string. Accept either and keep a number,
 * so the price cell is one type whichever the wire carried.
 */
function asNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function parseCandidates(payload: OverlapOut): OverlapCandidates {
  return {
    sessions: payload.sessions,
    scope: payload.scope,
    strategies_read: payload.strategies_read,
    screens_checked: payload.screens_checked,
    data: payload.data.map((row) => ({
      instrument_id: row.instrument_id,
      symbol: row.symbol,
      name: row.name,
      close: asNumber(row.close),
      last_price: asNumber(row.last_price),
      strategy_count: row.strategy_count,
      actionable: row.actionable,
      strategies: row.strategies,
      screens: row.screens,
      catalyst: row.catalyst,
    })),
  };
}
