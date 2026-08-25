import { BadgeCheck, FlaskConical, Info, PlugZap } from "lucide-react";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import type { BadgeProps } from "@/components/ui/badge";
import type { ProvenanceTone, ProvenanceView } from "@/lib/portfolios/provenance";

/**
 * Where a broker read's numbers came from, on the screen.
 *
 * The old endpoint called every non-empty answer "fixture holdings", including a real Kite fetch,
 * so somebody looking at their own shares was told they were fake. `source` and `degraded` fixed
 * the wire; this component is what makes the fix visible.
 *
 * **Fixture and degraded rows are marked. Clean live rows are not.** The second half matters as
 * much as the first: a badge on real holdings is the old bug reversed, and a reader who is told
 * their real position is a sample stops believing the marking anywhere it appears.
 */

const TONE_VARIANT: Record<ProvenanceTone, NonNullable<BadgeProps["variant"]>> = {
  live: "positive",
  sample: "warning",
  empty: "neutral",
  unavailable: "neutral",
};

function ToneIcon({ tone }: { tone: ProvenanceTone }): ReactNode {
  if (tone === "live") return <BadgeCheck aria-hidden="true" className="size-3" />;
  if (tone === "sample") return <FlaskConical aria-hidden="true" className="size-3" />;
  if (tone === "unavailable") return <PlugZap aria-hidden="true" className="size-3" />;
  return <Info aria-hidden="true" className="size-3" />;
}

export function HoldingsProvenance({ view }: { view: ProvenanceView }) {
  return (
    <div
      data-testid="holdings-provenance"
      data-source={view.source}
      data-degraded={view.degraded ? "true" : "false"}
      data-marked={view.marked ? "true" : "false"}
      className={
        view.marked
          ? "space-y-1.5 rounded-md border border-warning/40 bg-warning-muted/40 p-3"
          : "space-y-1.5 rounded-md border border-border bg-muted/40 p-3"
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={TONE_VARIANT[view.tone]}>
          <ToneIcon tone={view.tone} />
          {view.label}
        </Badge>
        {view.marked && view.mark !== null ? (
          <span className="text-xs font-medium" data-testid="provenance-warning">
            {view.mark}
          </span>
        ) : null}
        {view.dryRun ? (
          <span className="text-xs text-muted-foreground">read-only session</span>
        ) : null}
      </div>
      <p className="text-xs leading-relaxed text-muted-foreground">{view.detail}</p>
    </div>
  );
}
