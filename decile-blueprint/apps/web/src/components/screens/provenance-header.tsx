import type { ScreenProvenanceOut } from "@baskfy/api-client";

import { deskScoreNote, engineNote, provenanceItems } from "@/lib/screens/provenance";
import { cn } from "@/lib/utils";

/**
 * The results header's provenance line — gates/ranking-2.H-web.md G3.
 *
 * Universe, as-of, data version, ranking engine version, desk score version, scope and mode, every
 * one from the run's `provenance`. Always rendered with a result: a legacy screen shows
 * `legacy-sql` and a desk score that is not used, rather than dropping the two items.
 */
export interface ProvenanceHeaderProps {
  provenance: ScreenProvenanceOut;
  className?: string | undefined;
}

export function ProvenanceHeader({ provenance, className }: ProvenanceHeaderProps) {
  const notes: Partial<Record<string, string>> = {
    engine: engineNote(provenance),
    desk_score: deskScoreNote(provenance),
  };
  return (
    <dl
      data-testid="results-provenance"
      aria-label="Where these results come from"
      className={cn("flex flex-wrap gap-x-4 gap-y-1 text-xs", className)}
    >
      {provenanceItems(provenance).map((item) => (
        <div
          key={item.key}
          className="flex items-baseline gap-1"
          data-testid={`provenance-${item.key}`}
          title={notes[item.key]}
        >
          <dt className="text-muted-foreground">{item.label}</dt>
          <dd className="tabular-nums">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
