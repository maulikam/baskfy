"use client";

import { cn } from "@/lib/utils";
import { KIND_EXPLAINER, type PortfolioKind } from "@/lib/portfolio/organize";

/**
 * PORTFOLIO_REDESIGN.md §6.7's third step: "choose kind (Capital portfolio vs Monitoring view,
 * with the §4.1 explanation)".
 *
 * The explanation is rendered **inside the choice**, not behind a link, and that is the whole
 * design of this component. §4.1's two kinds differ in *arithmetic* — one sums into net worth and
 * takes the holding out of Unallocated, the other never enters a total and moves nothing — and a
 * user who picks the wrong one finds out weeks later when their totals stop making sense. Sending
 * them to a help page to find that out is sending most of them nowhere.
 *
 * The monitoring option is muted in every state, matching how monitoring views are shown
 * everywhere else, so the visual language is learned here and holds on the Overview table.
 */

export interface PortfolioKindChoiceProps {
  value: PortfolioKind;
  onChange: (kind: PortfolioKind) => void;
  /** How many holdings the choice applies to — the sentence changes meaning without it. */
  holdingCount: number;
}

const ORDER: PortfolioKind[] = ["CAPITAL", "MONITORING"];

export function PortfolioKindChoice({ value, onChange, holdingCount }: PortfolioKindChoiceProps) {
  return (
    <fieldset className="space-y-3" data-testid="kind-choice">
      <legend className="text-sm font-semibold">What kind of portfolio is this?</legend>
      <p className="text-xs text-muted-foreground">
        This decides the arithmetic, not the styling. You can read both answers here — you should
        not have to go and look them up.
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        {ORDER.map((kind) => {
          const explainer = KIND_EXPLAINER[kind];
          const selected = value === kind;
          const monitoring = kind === "MONITORING";
          return (
            <label
              key={kind}
              data-testid={`kind-option-${kind}`}
              data-selected={selected ? "true" : "false"}
              className={cn(
                "flex cursor-pointer flex-col gap-2 rounded-xl border p-4 transition-colors",
                selected ? "border-accent bg-accent-muted/40" : "border-border bg-card",
                monitoring && "text-muted-foreground",
              )}
            >
              <span className="flex items-start gap-2">
                <input
                  type="radio"
                  name="portfolio-kind"
                  value={kind}
                  checked={selected}
                  onChange={() => onChange(kind)}
                  className="mt-1 size-4 accent-[color:var(--accent)]"
                />
                <span>
                  <span
                    className={cn(
                      "block text-sm font-semibold",
                      monitoring ? "text-muted-foreground" : "text-foreground",
                    )}
                  >
                    {explainer.title}
                  </span>
                  <span className="block text-xs">{explainer.sentence}</span>
                </span>
              </span>

              <ul className="ml-6 list-disc space-y-1 text-xs leading-relaxed">
                {explainer.consequences.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>

              <p className="ml-6 text-xs">
                {kind === "CAPITAL"
                  ? `${holdingCount} holding${holdingCount === 1 ? "" : "s"} would leave Unallocated.`
                  : `${holdingCount} holding${holdingCount === 1 ? "" : "s"} would stay exactly where they are.`}
              </p>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
