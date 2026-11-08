"use client";

import { TriangleAlert } from "lucide-react";

/**
 * docs/10 §"What to show the user (honesty features)":
 *
 *     "An assumptions panel stating execution timing, costs, dividend policy, and the fact that
 *      index membership before the first available NSE constituent file is reconstructed."
 *     "A prominent 'Past backtest results do not predict future results' line — the reference
 *      product says this and it is both ethically right and legally necessary."
 *
 * Neither string is written here. Both come from `GET /backtests/{id}` — the assumptions from
 * `baskfy_api.backtests.assumptions`, which reads the run's own configuration, and the disclaimer
 * from the server's constant. A page that composed its own sentences could describe a cost model
 * the run did not use, which is exactly the failure this panel exists to prevent.
 */
export interface AssumptionsPanelProps {
  assumptions: readonly string[];
  disclaimer: string;
}

export function AssumptionsPanel({ assumptions, disclaimer }: AssumptionsPanelProps) {
  /*
   * A statement is worth making once. The server deduplicates too (`build_payload`), but rows
   * written before it did still carry "the screen returned nothing on ..." twice, and a panel
   * that prints the same sentence twice reads as a rendering fault to anyone looking at it. The
   * line is also the list key, so a repeat was a React key collision as well — the two failures
   * have one fix.
   */
  const lines = [...new Set(assumptions)];

  return (
    <section className="space-y-3" aria-labelledby="assumptions">
      <h2 id="assumptions" className="text-sm font-semibold">
        Assumptions
      </h2>
      <p
        className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm font-medium"
        role="note"
      >
        <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
        {disclaimer}
      </p>
      <ul className="space-y-2 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
        {lines.map((line) => (
          <li key={line} className="flex gap-2">
            <span aria-hidden="true" className="text-muted-foreground/60">
              ·
            </span>
            <span>{line}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
