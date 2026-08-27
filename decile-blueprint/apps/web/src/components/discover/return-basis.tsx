import type { ExploreMetrics } from "@/lib/explore/fetch";
import { cn } from "@/lib/utils";

/**
 * What the return beside it actually counts, said where the return is shown.
 *
 * The catalogue used to put `ReturnConventionNote` and `DisclosureBlock` at the very bottom of
 * `/baskets` (now `/discover/all`), after every card. A reader who scanned six returns and left never met either. The
 * convention is part of the number — a price return is roughly the dividend yield lower than a
 * total return, about 1.2% a year on NSE, compounding — so it belongs against the number, not in
 * a footer.
 *
 * Deliberately not a second disclaimer. `DisclosureBlock` still carries the standing legal text
 * once per page (house rule 9: disclaimers are components, not footers); this says the one thing
 * that changes the meaning of the figure the reader is looking at right now.
 */
export function ReturnBasis({
  metrics,
  className,
  tone = "quiet",
}: {
  metrics: ExploreMetrics | null;
  className?: string;
  tone?: "quiet" | "block";
}) {
  if (!metrics) return null;

  const headline = metrics.dividends_included
    ? "Includes dividends"
    : "Price only — dividends not included";

  return (
    <details
      className={cn(
        "group text-xs",
        tone === "block" && "rounded-lg border border-border/70 bg-muted/30 p-3",
        className,
      )}
      data-testid="return-basis"
      data-dividends={metrics.dividends_included ? "true" : "false"}
    >
      <summary
        className={cn(
          "inline-flex cursor-help list-none items-center gap-1 text-muted-foreground decoration-dotted underline-offset-2 hover:underline",
          "marker:content-none [&::-webkit-details-marker]:hidden",
        )}
      >
        {headline}
        <span aria-hidden="true" className="text-[9px] opacity-60 group-open:opacity-100">
          ⓘ
        </span>
      </summary>
      <p className="mt-1.5 max-w-[60ch] leading-relaxed text-muted-foreground">
        {metrics.return_convention_note}
        {metrics.as_of_date ? ` Figures as of ${metrics.as_of_date}.` : ""}
      </p>
    </details>
  );
}
