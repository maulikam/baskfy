import type {
  ScreenDefinition,
  ScreenSelectionRequest,
  SelectionConstraintsIn,
  SelectionRowOut,
} from "@baskfy/api-client";

/**
 * Portfolio fit — `POST /screens/selection` (docs/ranking/PLAN.md C5/C6, gates G5).
 *
 * **Informational only.** The words here describe what the selection *would* do to a list of
 * names; none of them names a trade, because nothing here places one. The quality ranking on the
 * page is read-only to this module: rows are shown in the order the server sent them, with the
 * engine's `quality_rank` verbatim.
 */

export const FIT_ACTION_LABEL: Record<SelectionRowOut["action"], string> = {
  hold: "Keep",
  enter: "Would add",
  exit: "Would remove",
  skip: "Skipped",
};

export const FIT_ACTION_TONE: Record<
  SelectionRowOut["action"],
  "positive" | "accent" | "negative" | "neutral"
> = {
  hold: "positive",
  enter: "accent",
  exit: "negative",
  skip: "neutral",
};

const REASON_LABEL: Readonly<Record<string, string>> = {
  RANK_WITHIN_RETENTION: "Still ranked inside the keep limit",
  RANK_OUTSIDE_RETENTION: "Ranked below the keep limit",
  NOT_IN_RESULTS: "Not in this screen's results",
  RETAINED_BY_TURNOVER_BUDGET: "Kept because the limit on changes was reached",
  RANK_WITHIN_ENTRY: "Ranked inside the add limit",
  SECTOR_CAP: "Its sector is already at the limit",
  CAPACITY: "Too large for the stock's daily trading value",
  CORRELATION: "Moves too closely with a stock already in the list",
  TURNOVER_BUDGET: "The limit on changes was reached",
  FULL: "Every slot is taken",
};

const FLAG_LABEL: Readonly<Record<string, string>> = {
  SECTOR_UNCLASSIFIED: "No sector on record",
  CAPACITY_UNKNOWN: "Daily trading value unknown",
  HOLD_ABOVE_CAPACITY: "Current holding is large for its daily trading value",
  PRICE_UNKNOWN: "No price, so no size",
  PRICE_ABOVE_SLOT_VALUE: "One share costs more than a slot",
  CORRELATION_HISTORY_SHORT: "Too little history to measure how it moves with others",
  CORRELATION_UNDEFINED: "How it moves with others could not be measured",
};

/** A reason code in plain words; an unknown code is shown as sent rather than dropped. */
export function reasonLabel(code: string): string {
  return REASON_LABEL[code] ?? code;
}

export function flagLabel(code: string): string {
  return FLAG_LABEL[code] ?? code;
}

/** The constraint inputs. `null` means "leave it to the server's default" or "no limit". */
export interface FitConstraintsDraft {
  max_names: number | null;
  entry_rank: number | null;
  retention_rank: number | null;
  max_per_sector: number | null;
  max_correlation: number | null;
  turnover_budget_names: number | null;
  /** Money stays a string end to end (house rule 9). */
  capital_inr: string;
}

export const EMPTY_CONSTRAINTS: FitConstraintsDraft = {
  max_names: null,
  entry_rank: null,
  retention_rank: null,
  max_per_sector: null,
  max_correlation: null,
  turnover_budget_names: null,
  capital_inr: "",
};

const CAPITAL_PATTERN = /^\d+(\.\d{1,2})?$/;

/** `null` when the draft is sendable, else the first plain reason it is not. */
export function constraintsProblem(draft: FitConstraintsDraft): string | null {
  const whole = (value: number | null, min: number) =>
    value === null || (Number.isInteger(value) && value >= min);
  if (!whole(draft.max_names, 1)) return "Most stocks must be a whole number of 1 or more.";
  if (!whole(draft.entry_rank, 1)) return "The add limit must be a whole number of 1 or more.";
  if (!whole(draft.retention_rank, 1)) {
    return "The keep limit must be a whole number of 1 or more.";
  }
  if (
    draft.entry_rank !== null &&
    draft.retention_rank !== null &&
    draft.retention_rank < draft.entry_rank
  ) {
    return "The keep limit cannot be tighter than the add limit.";
  }
  if (!whole(draft.max_per_sector, 1)) {
    return "Most stocks per sector must be a whole number of 1 or more.";
  }
  if (!whole(draft.turnover_budget_names, 0)) {
    return "The limit on changes must be a whole number of 0 or more.";
  }
  if (
    draft.max_correlation !== null &&
    !(draft.max_correlation >= -1 && draft.max_correlation <= 1)
  ) {
    return "The correlation limit must be between -1 and 1.";
  }
  const capital = draft.capital_inr.trim();
  if (capital !== "" && (!CAPITAL_PATTERN.test(capital) || /^0+(\.0+)?$/.test(capital))) {
    return "Capital must be a rupee amount above zero, with at most two decimals.";
  }
  return null;
}

/** Only the constraints the user set; everything left blank takes the server's default. */
export function constraintsPayload(draft: FitConstraintsDraft): SelectionConstraintsIn {
  const payload: Partial<SelectionConstraintsIn> = {};
  if (draft.max_names !== null) payload.max_names = draft.max_names;
  if (draft.entry_rank !== null) payload.entry_rank = draft.entry_rank;
  if (draft.retention_rank !== null) payload.retention_rank = draft.retention_rank;
  if (draft.max_per_sector !== null) payload.max_per_sector = draft.max_per_sector;
  if (draft.max_correlation !== null) payload.max_correlation = draft.max_correlation;
  if (draft.turnover_budget_names !== null) {
    payload.turnover_budget_names = draft.turnover_budget_names;
  }
  const capital = draft.capital_inr.trim();
  if (capital !== "") payload.capital_inr = capital;
  // openapi-typescript marks every field with a server default as required, but the API fills a
  // missing one in (Pydantic defaults). Sending only what the user set is what keeps the server's
  // defaults authoritative instead of copying them here, where they would drift.
  return payload as SelectionConstraintsIn;
}

/** What the fit is computed over: one of the caller's portfolios, or an empty list of holdings. */
export type FitSource = { kind: "portfolio"; id: number } | { kind: "empty" };

export function fitRequest(input: {
  definition: ScreenDefinition;
  source: FitSource;
  constraints: FitConstraintsDraft;
  asOf?: string | undefined;
  dataVersion?: number | undefined;
}): ScreenSelectionRequest {
  return {
    definition: input.definition,
    ...(input.asOf === undefined ? {} : { as_of: input.asOf }),
    ...(input.dataVersion === undefined ? {} : { data_version: input.dataVersion }),
    ...(input.source.kind === "portfolio"
      ? { portfolio_id: input.source.id }
      : { holdings: [] }),
    constraints: constraintsPayload(input.constraints),
  };
}
