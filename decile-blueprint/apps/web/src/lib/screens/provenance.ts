import type { ScreenProvenanceOut } from "@baskfy/api-client";

import { EMPTY_CELL, formatTradeDate } from "@/lib/format";

/**
 * What produced a screen payload, as the results header reads it — docs/ranking/PLAN.md C6,
 * gates/ranking-2.H-web.md G3.
 *
 * Every value comes from the run response's `provenance`. Nothing is inferred from the definition
 * on the page: the header describes the rows the server returned, and the definition may already
 * have moved on while the next preview is in flight.
 */

export const PROVENANCE_MODE_LABEL: Record<ScreenProvenanceOut["mode"], string> = {
  single: "Single",
  sequential: "Sequential",
  composite: "Composite",
};

export const PROVENANCE_SCOPE_LABEL: Record<ScreenProvenanceOut["scope"], string> = {
  fixed_universe: "Fixed universe",
  filtered_results: "Filtered results",
  within_sector: "Within sector",
};

/** The engine version a definition without ranking terms reports (docs/06's SQL ranking). */
export const LEGACY_ENGINE_VERSION = "legacy-sql";

export interface ProvenanceItem {
  key: "universe" | "as_of" | "data_version" | "engine" | "desk_score" | "scope" | "mode";
  label: string;
  value: string;
}

/**
 * The seven header items, always in this order and always present. A version the run did not use
 * is said plainly ("Not used") rather than hidden, so a missing item is never ambiguous.
 */
export function provenanceItems(provenance: ScreenProvenanceOut): ProvenanceItem[] {
  return [
    {
      key: "universe",
      label: "Stocks from",
      value: provenance.universe_label || provenance.universe,
    },
    { key: "as_of", label: "Prices as of", value: formatTradeDate(provenance.as_of) },
    { key: "data_version", label: "Data update", value: `#${provenance.data_version}` },
    {
      key: "engine",
      label: "Ranking method",
      value: provenance.ranking_engine_version || EMPTY_CELL,
    },
    {
      key: "desk_score",
      label: "Desk score method",
      value: provenance.desk_score_version ?? "Not used",
    },
    { key: "scope", label: "Compared against", value: PROVENANCE_SCOPE_LABEL[provenance.scope] },
    { key: "mode", label: "Ranking mode", value: PROVENANCE_MODE_LABEL[provenance.mode] },
  ];
}

/** A one-line plain reading of the engine version, for the item's tooltip. */
export function engineNote(provenance: ScreenProvenanceOut): string {
  if (provenance.ranking_engine_version === LEGACY_ENGINE_VERSION) {
    return "No ranking terms: ranked by Sort By with the original SQL ranking.";
  }
  return "Ranked by the ranking terms with this version of the ranking engine.";
}

/** A one-line plain reading of the desk score version, for the item's tooltip. */
export function deskScoreNote(provenance: ScreenProvenanceOut): string {
  return provenance.desk_score_version === null
    ? "The desk score does not decide this order."
    : "The desk score decides this order; this is the version of its formula.";
}
