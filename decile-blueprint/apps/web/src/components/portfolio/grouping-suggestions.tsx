"use client";

import { useMemo } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  formatCoverage,
  NO_FIGURE,
  rankSuggestions,
  SUGGESTION_BASIS_LABEL,
  type GroupingSuggestion,
} from "@/lib/portfolio/organize";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * PORTFOLIO_REDESIGN.md §6.6's first-run helper, rendered.
 *
 * > connect broker → everything lands in Unallocated → the product actively helps sort it
 * > (suggest groupings by sector, by purchase era, by overlap with a subscribed basket). Getting
 * > from 40 unallocated holdings to 4 named portfolios IS activation.
 *
 * Each card is one `baskfy_core.grouping_suggestions.GroupingSuggestion`, printed as that module
 * produced it: its `proposed_name`, its `rationale` sentence, its exact `value`, and — for a
 * basket overlap and only for one — its coverage and what is missing from the model. Nothing is
 * re-derived in the browser. The order is the module's own ranking (biggest decision first);
 * {@link rankSuggestions} re-applies it locally so a list is never shown in arrival order.
 *
 * The basis is always visible. "These are all Financials" and "you bought these in the same year"
 * are different claims, and the user is about to attach money and a return series to whichever
 * one they accept.
 */

export interface GroupingSuggestionsProps {
  suggestions: readonly GroupingSuggestion[];
  /** Why there are none. `null` means suggestions were computed — an empty list then means none. */
  unavailableReason: string | null;
  onAccept: (suggestion: GroupingSuggestion) => void;
}

export function GroupingSuggestions({
  suggestions,
  unavailableReason,
  onAccept,
}: GroupingSuggestionsProps) {
  const ranked = useMemo(() => rankSuggestions(suggestions), [suggestions]);

  return (
    <section aria-label="Ways to sort this" className="space-y-3" data-testid="grouping-suggestions">
      <div>
        <h3 className="text-sm font-semibold">Ways to sort this</h3>
        <p className="text-xs text-muted-foreground">
          Groups we can already see in what you hold. Accepting one fills in the next screen — you
          still choose the kind, the name and the benchmark.
        </p>
      </div>

      {unavailableReason !== null ? (
        <p
          className="rounded-lg border border-dashed border-border px-3 py-4 text-sm text-muted-foreground"
          data-testid="suggestions-unavailable"
        >
          <span aria-hidden="true">{NO_FIGURE}</span>{" "}
          <span className="sr-only">No suggestions.</span>
          {unavailableReason}
        </p>
      ) : ranked.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-3 py-4 text-sm text-muted-foreground">
          Nothing here groups obviously — no two of these holdings share a sector, a purchase year
          or a model you subscribe to. Pick them by hand below.
        </p>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2">
          {ranked.map((suggestion) => {
            const monitoring = suggestion.suggested_kind === "MONITORING";
            const missing = suggestion.missing_instrument_ids.length;
            return (
              <li
                key={`${suggestion.basis}:${suggestion.proposed_name}`}
                data-testid="suggestion-card"
                data-basis={suggestion.basis}
                className={cn(
                  "flex flex-col gap-2 rounded-xl border p-4",
                  monitoring ? "border-border bg-muted/40 text-muted-foreground" : "border-border bg-card",
                )}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <h4
                    className={cn(
                      "text-sm font-semibold",
                      monitoring ? "text-muted-foreground" : "text-foreground",
                    )}
                  >
                    {suggestion.proposed_name}
                  </h4>
                  <Badge variant={monitoring ? "neutral" : "outline"}>
                    {SUGGESTION_BASIS_LABEL[suggestion.basis]}
                  </Badge>
                  {monitoring ? <Badge variant="neutral">Monitoring view</Badge> : null}
                </div>

                <p className="text-xs leading-relaxed">{suggestion.rationale}</p>

                <p className="text-sm tabular-nums">
                  {formatRupees(suggestion.value)}{" "}
                  <span className="text-xs text-muted-foreground">
                    · {suggestion.keys.length} holding{suggestion.keys.length === 1 ? "" : "s"}
                  </span>
                </p>

                {suggestion.basis === "BASKET_OVERLAP" ? (
                  <p className="text-xs text-muted-foreground" data-testid="suggestion-coverage">
                    You hold {formatCoverage(suggestion.basket_coverage)} of this model
                    {missing > 0
                      ? ` — ${missing} stock${missing === 1 ? "" : "s"} in it you do not hold.`
                      : " — every stock in it."}
                  </p>
                ) : null}

                <Button
                  type="button"
                  variant={monitoring ? "outline" : "primary"}
                  size="sm"
                  className="self-start"
                  onClick={() => onAccept(suggestion)}
                >
                  Use this grouping
                </Button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
