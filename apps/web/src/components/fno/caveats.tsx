import { AlertTriangle } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * The FO pages' disclaimers — **components, placed above the numbers they govern, never a footer**
 * (house rule 9). Their words are the documents', verbatim:
 *
 * * `<F2AgainstResearchBanner/>` — `docs/fno/05` §2: "A **permanent amber banner**, not
 *   dismissible". It has no close control and no state; it renders wherever F2 does.
 * * `<NotASignalBanner/>` — `04` §5's one sentence, linking the research's verdict table (the
 *   "Families tested and rejected" section on the same page, which is `RESEARCH.md`'s table).
 * * `<Tier2ECaveat/>` — `07` §4: "No Tier 2E number is shown without its caveat". The text comes
 *   from the API row it qualifies (`fo_backtest_run.caveat`, or the evidence card's quote).
 * * `<DeskOnlyPrices/>` — `05` §2: "The page never prices an option live. Live leg prices exist
 *   only on the desk, where they decide the plan. The page says so."
 */

export const F2_BANNER_TEXT =
  "Built by choice against the research: +0.017R a trade after rolls, negative in 2022, 2024, " +
  "2025 and 2026";

export const NOT_A_SIGNAL_TEXT =
  "None of these numbers predicted a profitable trade after costs in 2022–2026.";

export function F2AgainstResearchBanner({ className }: { className?: string }) {
  return (
    <aside
      role="note"
      data-testid="fno-f2-banner"
      className={cn(
        "flex items-start gap-2 rounded-lg border border-warning/50 bg-warning-muted p-3 text-sm text-foreground",
        className,
      )}
    >
      <AlertTriangle
        aria-hidden="true"
        className="mt-0.5 size-4 shrink-0 text-warning"
      />
      <p>
        {F2_BANNER_TEXT} (<span className="font-mono text-xs">RESEARCH.md</span>
        ).
      </p>
    </aside>
  );
}

export function NotASignalBanner({ className }: { className?: string }) {
  return (
    <aside
      role="note"
      data-testid="fno-not-a-signal"
      className={cn(
        "rounded-lg border border-warning/40 bg-warning-muted p-3 text-sm text-foreground",
        className,
      )}
    >
      <p>
        {NOT_A_SIGNAL_TEXT}{" "}
        <a href="#families" className="underline underline-offset-2">
          The research&rsquo;s verdict, family by family
        </a>
        .
      </p>
    </aside>
  );
}

export function Tier2ECaveat({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  return (
    <p
      data-testid="fno-tier-caveat"
      className={cn(
        "rounded-md border border-border bg-muted/60 px-3 py-2 text-xs leading-relaxed text-muted-foreground",
        className,
      )}
    >
      <span className="font-medium text-foreground">Tier 2E. </span>
      {text}
    </p>
  );
}

export function DeskOnlyPrices({ className }: { className?: string }) {
  return (
    <p
      data-testid="fno-desk-only"
      className={cn("text-xs text-muted-foreground", className)}
    >
      This page never prices an option live. Live leg prices exist only on the
      desk, where they decide the plan; everything here is the last close or its
      settle.
    </p>
  );
}
