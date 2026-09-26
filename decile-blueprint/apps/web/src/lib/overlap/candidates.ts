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

// --- Sorting the candidates table (OV12, 26 Sep 2026) ------------------------------------------
//
// Maulik: "I want to have the table which can be sorted in any of the ways I select on the UI."
// The server's order — how many strategies raised the name, actionable first, then symbol — is
// the starting order and stays available as `null`. A person picks a column; this is what each
// column means as a sort, and it is a display order only, never read by any rank or order path.

/** One of the eight headers, in the order the table shows them. */
export type CandidateSortKey =
  | "name"
  | "on"
  | "strategies"
  | "price"
  | "results"
  | "filing"
  | "laya"
  | "screens";

export const CANDIDATE_SORT_KEYS: readonly CandidateSortKey[] = [
  "name",
  "on",
  "strategies",
  "price",
  "results",
  "filing",
  "laya",
  "screens",
];

/** `1` ascends, `-1` descends — the same shape the holdings table uses. */
export type CandidateSortDirection = 1 | -1;

/**
 * The direction a first click on the header takes. Text and dates ascend (A first, the soonest
 * result first); figures and priorities descend (the most strategies, the highest price, the
 * filing worth opening first, "look first" first, the most screens) — nobody opens a table of
 * values wanting the smallest.
 */
export const CANDIDATE_SORT_DEFAULT_DIRECTION: Record<CandidateSortKey, CandidateSortDirection> = {
  name: 1,
  on: -1,
  strategies: 1,
  price: -1,
  results: 1,
  filing: -1,
  laya: -1,
  screens: -1,
};

/** What a header sorts by, in a person's words — the button's tooltip. */
export const CANDIDATE_SORT_MEANING: Record<CandidateSortKey, string> = {
  name: "Sort by symbol",
  on: "Sort by how many strategies raised the name",
  strategies: "Sort by which strategies raised the name, actionable first",
  price: "Sort by the close (the live overlay does not reorder the rows)",
  results: "Sort by the result date, soonest first; names without one last",
  filing: "Sort by the filing's priority — order wins, results and approvals first — then newest; names without a filing last",
  laya: "Sort by the word on the row — look first, worth a look, skip — then by Laya's percentage",
  screens: "Sort by how many screens the name is on, then its best rank",
};

const PRIORITY_RANK: Record<OverlapTag["review_priority"], number> = { high: 3, medium: 2, low: 1 };
const OPINION_RANK: Record<OverlapOpinionLabel, number> = { look_first: 3, worth_a_look: 2, skip: 1 };

/**
 * The comparable value of a row under a key, or `null` when the row has nothing to compare —
 * a null always sorts last, whichever way the column is pointing, because "no filing" is not
 * the smallest filing. A tuple compares element by element.
 */
function sortValue(row: OverlapCandidate, key: CandidateSortKey): (number | string)[] | null {
  switch (key) {
    case "name":
      return [row.symbol];
    case "on":
      return [row.strategy_count, row.actionable ? 1 : 0];
    case "strategies":
      return [
        row.actionable ? 0 : 1,
        row.strategies.map((hit) => hit.name).join(", "),
      ];
    case "price":
      return row.close === null ? null : [row.close];
    case "results":
      return row.catalyst?.earnings_date ? [row.catalyst.earnings_date] : null;
    case "filing": {
      const catalyst = row.catalyst;
      if (!catalyst || catalyst.headline === null) return null;
      return [
        catalyst.tag ? PRIORITY_RANK[catalyst.tag.review_priority] : 0,
        catalyst.published_at ?? "",
      ];
    }
    case "laya":
      return row.opinion === null
        ? null
        : [OPINION_RANK[row.opinion.label], row.opinion.source === "laya" ? row.opinion.confidence : 0];
    case "screens": {
      if (row.screens.length === 0) return null;
      const ranks = row.screens.map((hit) => hit.rank).filter((rank): rank is number => rank !== null);
      /* More screens first; among equals the best rank first, so the rank is negated. */
      return [row.screens.length, ranks.length ? -Math.min(...ranks) : -Infinity];
    }
  }
}

function compareValues(a: (number | string)[], b: (number | string)[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const x = a[i];
    const y = b[i];
    if (x === undefined || y === undefined) return x === undefined ? (y === undefined ? 0 : -1) : 1;
    if (typeof x === "string" || typeof y === "string") {
      const c = String(x).localeCompare(String(y), "en", { sensitivity: "base" });
      if (c !== 0) return c;
    } else if (x !== y) {
      return x < y ? -1 : 1;
    }
  }
  return 0;
}

/**
 * The rows under one header's order. `null` is the server's order, untouched. Stable: rows the
 * key cannot tell apart keep the server's order, and rows with nothing to compare go last in
 * both directions. Pure — a new array, the rows themselves untouched.
 */
export function sortCandidates(
  rows: readonly OverlapCandidate[],
  key: CandidateSortKey | null,
  direction: CandidateSortDirection,
): OverlapCandidate[] {
  if (key === null) return [...rows];
  return rows
    .map((row, index) => ({ row, index, value: sortValue(row, key) }))
    .sort((a, b) => {
      if (a.value === null || b.value === null) {
        if (a.value === null && b.value === null) return a.index - b.index;
        return a.value === null ? 1 : -1;
      }
      const c = compareValues(a.value, b.value) * direction;
      return c !== 0 ? c : a.index - b.index;
    })
    .map((entry) => entry.row);
}
