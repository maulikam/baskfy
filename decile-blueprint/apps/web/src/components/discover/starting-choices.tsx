import { DiscoverBasketCard } from "@/components/discover/basket-card";
import type { StartingChoice } from "@/lib/discover/match";
import { cn } from "@/lib/utils";

/**
 * Three baskets to start from, and why each one is there.
 *
 * The brief's shape: closest match, a lower-swing option, a higher-growth option. Each column
 * states its reason as a property of the basket — "annualised volatility of 12.4%, the least of
 * the three shown here" — rather than as a judgement about the reader.
 *
 * **Fewer than three columns is a valid answer.** With a catalogue of six, all momentum, there
 * may be no distinct third basket; a column is dropped rather than filled with a repeat. The
 * count is stated so a reader is not left wondering whether something failed to load.
 */
export function StartingChoices({
  choices,
  className,
}: {
  choices: readonly StartingChoice[];
  className?: string;
}) {
  if (choices.length === 0) {
    return (
      <p
        data-testid="starting-choices-empty"
        className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground"
      >
        No basket in the catalogue matched enough of what you asked for to be worth starting from.
        Widen the amount or the volatility range, or browse everything under All baskets.
      </p>
    );
  }

  return (
    <div className={cn("space-y-3", className)} data-testid="starting-choices">
      <div className="grid gap-4 lg:grid-cols-3">
        {choices.map((choice) => (
          <section
            key={choice.kind}
            data-testid="starting-choice"
            data-kind={choice.kind}
            className="flex flex-col gap-2"
          >
            <div>
              <h3 className="text-sm font-semibold tracking-tight">{choice.title}</h3>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                {choice.rationale}
              </p>
            </div>
            <DiscoverBasketCard basket={choice.basket} className="flex-1" />
          </section>
        ))}
      </div>

      {choices.length < 3 ? (
        <p className="text-xs text-muted-foreground" data-testid="starting-choices-short">
          Only {choices.length} distinct {choices.length === 1 ? "basket" : "baskets"} matched, so
          fewer than three are shown. The same basket under three headings would tell you nothing.
        </p>
      ) : null}
    </div>
  );
}

/**
 * The full match breakdown for one basket: every preference, matched or not, with its reason.
 *
 * Collapsed by default. The card already carries the one-line summary; this is for the reader who
 * wants to disagree with the filter, which they can only do if they can see what it checked.
 */
export function MatchBreakdown({ choice }: { choice: StartingChoice }) {
  return (
    <details className="text-xs" data-testid="match-breakdown">
      <summary className="cursor-pointer text-muted-foreground underline-offset-2 hover:underline">
        What was checked
      </summary>
      <ul className="mt-2 space-y-1.5">
        {choice.match.checks.map((check) => (
          <li key={check.key} className="flex gap-2" data-testid="match-check">
            <span
              aria-hidden="true"
              className={cn(
                "mt-0.5 shrink-0",
                !check.examinable
                  ? "text-muted-foreground"
                  : check.matched
                    ? "text-positive"
                    : "text-muted-foreground",
              )}
            >
              {!check.examinable ? "–" : check.matched ? "✓" : "✕"}
            </span>
            <span className="leading-relaxed text-muted-foreground">
              <span className="text-foreground">{check.label}</span>
              {": "}
              {check.reason}
              {!check.examinable ? " (not counted either way)" : ""}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}
