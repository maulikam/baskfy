"use client";

import { ShieldAlert } from "lucide-react";

import { MetricValue, NotYetMeasured } from "@/components/portfolio/detail/primitives";
import type { RiskView } from "@/lib/portfolio/detail-tabs";

/**
 * The Risk tab, which is mostly a list of what Baskfy does not measure, and says so on its face.
 *
 * §2.2's design consequence, verbatim: *"of its eighteen requested figures, two exist. It is
 * therefore scoped as one honest panel ... rather than eighteen dashes."*
 *
 * ## Plain language first, then the number
 *
 * Every reading here leads with a sentence and follows with a figure, not the other way round.
 * "How much of this portfolio rides on its single biggest name" is a question a person has;
 * "Top 1: 31.40%" is an answer to a question they have to already know how to ask. The figure is
 * still there, to the right, in tabular numerals, for the reader who wanted only that.
 *
 * ## Nothing below the line is computed, and that is the point
 *
 * `nav.daily_pnl[].pct` is in the payload and an annualised volatility is two lines of arithmetic
 * away. It is still not computed. A volatility with no stated window and no stated annualisation
 * convention is exactly the unstated model §2.2 refuses, and this product places live orders with
 * real money: a risk number invented to fill a panel is not a design shortcut, it is a number
 * somebody may size a position against. Each blocked figure names what it would actually take.
 */

export interface RiskTabProps {
  risk: RiskView;
}

export function RiskTab({ risk }: RiskTabProps) {
  return (
    <>
      <section
        aria-label="What is known about this portfolio's risk"
        data-testid="risk-known"
        className="rounded-xl border border-border bg-card"
      >
        <div className="border-b border-border px-4 py-3">
          <h2 className="flex items-center gap-1.5 text-sm font-semibold">
            <ShieldAlert aria-hidden="true" className="size-3.5 text-warning" />
            What is known
          </h2>
          <p className="mt-1 max-w-[76ch] text-xs leading-snug text-muted-foreground">
            {risk.opening}
          </p>
        </div>
        <ul className="divide-y divide-border/60">
          {risk.known.map((reading) => (
            <li
              key={reading.id}
              data-testid={`risk-${reading.id}`}
              className="flex flex-wrap items-start justify-between gap-x-6 gap-y-1 px-4 py-3"
            >
              <div className="min-w-[18rem] flex-1">
                <p className="text-sm font-medium">{reading.figure.label}</p>
                <p className="mt-0.5 max-w-[70ch] text-xs leading-snug text-muted-foreground">
                  {reading.plain}
                </p>
                {reading.figure.since ? (
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Measured over {reading.figure.since}.
                  </p>
                ) : null}
              </div>
              <div className="shrink-0 text-right">
                <MetricValue
                  metric={reading.figure}
                  kind={reading.id === "unpriced-exposure" || reading.id === "concentration" ? "count" : "percent"}
                  className="text-lg font-semibold"
                />
              </div>
            </li>
          ))}
        </ul>
        <p className="border-t border-border px-4 py-2.5 text-xs leading-snug text-muted-foreground">
          Everything above is a description of what has already happened or of what this portfolio
          is holding right now. None of it is a forecast, and none of it is a recommendation about
          a position.
        </p>
      </section>

      <NotYetMeasured
        items={risk.notMeasured}
        heading="What this tab will show, and what each needs first"
        intro="These are the rest of the brief's risk figures. None of them is estimated here. A risk number with an unstated model behind it is worse than an absent one, because somebody may size a position against it."
        testId="risk-blocked"
      />
    </>
  );
}
