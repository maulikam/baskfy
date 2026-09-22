import { AlertTriangle } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * The F&O loss caveat and the paper label — **components, above the numbers, never a footer**
 * (house rule 9; `docs/options/01` §0 is the text's source).
 *
 * Every options page carries `<FnoRiskCaveat/>`: SEBI's own studies of individual traders in
 * equity derivatives, which the pack cites before any strategy is described, and the plain
 * statement that nothing here is shown to be profitable after costs. It is not fine print about a
 * result; it is the condition under which every number on these pages is to be read.
 *
 * `<ScanOnly/>` says what the page is: scans and paper. No options sleeve has traded real money,
 * every money switch is off, and a plan is confirmed only on the desk.
 */
export function FnoRiskCaveat({ className }: { className?: string }) {
  return (
    <aside
      aria-labelledby="options-risk-heading"
      data-testid="options-fno-caveat"
      className={cn(
        "rounded-lg border border-warning/40 bg-warning-muted p-4",
        className,
      )}
    >
      <h2
        id="options-risk-heading"
        className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-warning"
      >
        <AlertTriangle aria-hidden="true" className="size-4" />
        Most individual traders lose money in F&amp;O
      </h2>
      <div className="mt-2 max-w-[80ch] space-y-2 text-sm leading-relaxed text-foreground">
        <p>
          SEBI&rsquo;s study of individual traders in equity derivatives found
          that <strong>91% lost money in FY25</strong>, with ₹1,05,603 crore of
          net losses after costs, and its FY25&ndash;FY26 study found the same
          shape again. Option buyers lose most often; option sellers lose less
          often and more at a time.
        </p>
        <p>
          Nothing on these pages has been shown to be profitable after costs.
          These are scans and paper records, not advice and not a recommendation
          to trade.
        </p>
      </div>
    </aside>
  );
}

export function ScanOnly({ className }: { className?: string }) {
  return (
    <span
      data-testid="options-scan-only"
      className={cn(
        "inline-flex items-center rounded-md border border-border bg-muted px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-muted-foreground",
        className,
      )}
    >
      Scan · paper only
    </span>
  );
}
