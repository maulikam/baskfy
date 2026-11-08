import { Info } from "lucide-react";

import type { ExploreMetrics } from "@/lib/explore/fetch";

/**
 * The return convention, stated where the returns are.
 *
 * House rule 9: "Disclaimers are components, not footers." The backtest assumptions panel has
 * said this since M39; the catalog said nothing, so a reader comparing a basket card against a
 * total-return benchmark was comparing two different things without being told. CLAUDE.md now
 * carries the rule outright — any new surface that shows a return owes the reader the sentence.
 *
 * Rendered once per surface rather than once per card: repeated on twenty cards it becomes
 * wallpaper, and wallpaper is not a disclosure.
 */
export function ReturnConventionNote({ metrics }: { metrics: ExploreMetrics | null }) {
  if (!metrics?.return_convention_note) return null;
  return (
    <aside
      className="flex items-start gap-2 rounded-lg border border-border/60 bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
      aria-label="How these returns are calculated"
    >
      <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      <p>{metrics.return_convention_note}</p>
    </aside>
  );
}
