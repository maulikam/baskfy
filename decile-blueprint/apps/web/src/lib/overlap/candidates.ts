import type { OverlapOut, OverlapScanOut } from "@baskfy/api-client";

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

/** The eight words a headline can be tagged with — the wire's vocabulary, verbatim. */
export type OverlapEventType =
  | "earnings"
  | "order"
  | "approval"
  | "fundraising"
  | "governance"
  | "corporate_action"
  | "routine"
  | "other";

export const OVERLAP_EVENT_TYPES: readonly OverlapEventType[] = [
  "earnings",
  "order",
  "approval",
  "fundraising",
  "governance",
  "corporate_action",
  "routine",
  "other",
];

/** What the correction action answers. The sentence is this app's copy, never the wire's. */
export type CorrectTagResult =
  | { readonly ok: true }
  | { readonly ok: false; readonly error: string };

/** The last filings scan, as `GET /overlap/catalyst-scan` answers it. */
export type FilingsScan = OverlapScanOut;

/** What the scan actions answer: the scan's status, or this app's sentence for a refusal. */
export type FilingsScanResult =
  | { readonly ok: true; readonly scan: FilingsScan }
  | { readonly ok: false; readonly error: string };

/** The two server actions behind the "Scan filings with Laya" button. */
export type FilingsScanActions = {
  readonly start: (scope: OverlapScope) => Promise<FilingsScanResult>;
  readonly status: () => Promise<FilingsScanResult>;
};

/** The server action a chip's select calls: a word on the headline, or `null` to take it back. */
export type CorrectTagAction = (
  headline: string,
  eventType: OverlapEventType | null,
) => Promise<CorrectTagResult>;

/** What the label action answers. The sentence is this app's copy, never the wire's. */
export type LabelRowResult =
  | { readonly ok: true }
  | { readonly ok: false; readonly error: string };

/** The server action the Laya cell's select calls: a word on the row, or `null` to take it back. */
export type LabelRowAction = (
  instrumentId: number,
  label: OverlapOpinionLabel | null,
) => Promise<LabelRowResult>;

/**
 * The word on the headline. Display context only — never an input to a rank, a filter, a size
 * or an order — and `source` says what produced it: `rules` for the keyword baseline, `laya`
 * for the model when it was sure, `corrected` for a person's word, which wins over both.
 */
export interface OverlapTag {
  event_type: OverlapEventType;
  review_priority: "high" | "medium" | "low";
  /** The phrases that decided the type — the "why", shown on hover. */
  matched: readonly string[];
  /** `rules` for the keyword baseline, `laya` for the model's answer when it was sure, `corrected` for a person's. */
  source: string;
  /** The model's probability for its choice; null for the rules and for a correction. */
  confidence: number | null;
  /**
   * `"<source>:<event_type>"` of the other reader when the two read the headline differently —
   * or, on a corrected tag, of the reader the person overruled.
   */
  disagrees_with: string | null;
  /** `source === "corrected"`: a person's word, not a reader's. */
  corrected: boolean;
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
  /** The opinion on the row — a label, Laya when sure, else the rules. Null only off-wire. */
  opinion: OverlapOpinion | null;
}

/** The three words a row's opinion can be — the wire's vocabulary, verbatim. */
export type OverlapOpinionLabel = "look_first" | "worth_a_look" | "skip";

export const OPINION_LABELS: readonly OverlapOpinionLabel[] = ["look_first", "worth_a_look", "skip"];

/**
 * Attention, never a trade: `look_first | worth_a_look | skip` answers "how much does this row
 * deserve a look before the others". `source` says whose word it is, resolved like the filing
 * tag: `labelled` for a person's, which wins; `laya` for the model, only when it cleared the
 * confidence floor; `rules` for the baseline from the facts already on the row, which always
 * has a word and a `reason`. The column therefore never reads "not sure" — measured on the base
 * checkpoint, the model clears the floor on almost no row today, so the rules carry it.
 */
export interface OverlapOpinion {
  label: OverlapOpinionLabel;
  confidence: number;
  source: string;
  shown: boolean;
  floor: number;
  /** `source === "labelled"`: a person's word, not the model's. */
  labelled: boolean;
  /** `source === "rules"`: why, in a sentence. Null for the model and a label. */
  reason: string | null;
  /**
   * What Laya answered on this row's state, whichever source is shown — its word and the
   * probability it gave it. Null until the sidecar has answered. Served under the floor too, so
   * the page can show the model's percentage beside the rules' word or a label.
   */
  laya: OverlapLayaAnswer | null;
}

export interface OverlapLayaAnswer {
  label: OverlapOpinionLabel;
  confidence: number;
}

export const OPINION_WORDS: Record<OverlapOpinionLabel, string> = {
  look_first: "Look first",
  worth_a_look: "Worth a look",
  skip: "Skip",
};

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
  /** Is Laya working: the sidecar's last pass, and how many of these filings it spoke on. */
  laya: OverlapLayaStatus;
  data: readonly OverlapCandidate[];
}

export interface OverlapLayaStatus {
  /** ISO timestamp of the sidecar's last pass; null when it has never run (or its note expired). */
  last_pass_at: string | null;
  /** Rows whose filing Laya answered on, whichever reader the page shows. */
  answered: number;
  /** Rows whose shown tag is Laya's (it was sure). */
  shown: number;
  /** Rows with a filing at all. */
  of: number;
}

/** The one line that says whether Laya is working, in a reader's words. */
export function layaStatusLine(status: OverlapLayaStatus, formatWhen: (iso: string) => string): string {
  if (status.of === 0) return "No filings on this page for Laya to read.";
  if (status.last_pass_at === null) {
    return "Laya has not read these filings yet — its sidecar has not reported a pass.";
  }
  const when = formatWhen(status.last_pass_at);
  if (status.answered === 0) {
    return `Laya last passed ${when} and has not read these ${status.of} filings yet.`;
  }
  const sure =
    status.shown === 0
      ? "was not sure on any, so the rules stand"
      : status.shown === status.answered
        ? "is shown on every one"
        : `is shown on ${status.shown}; on the rest it was not sure, so the rules stand`;
  return `Laya read ${status.answered} of ${status.of} filings (last pass ${when}) and ${sure}.`;
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
    laya: payload.laya,
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
      opinion:
        row.opinion === null
          ? null
          : { ...row.opinion, reason: row.opinion.reason ?? null, laya: row.opinion.laya ?? null },
    })),
  };
}
