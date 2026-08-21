import type { PlanSummary } from "@/lib/marketing/plan-summary";

/**
 * The December 2026 price change, read from `GET /plans` rather than written down.
 *
 * PROMPTS.md Prompt 13 acceptance criterion 4 — "No price or entitlement is hard-coded in the web
 * app; all read from the API" — applies to an announcement about prices at least as strongly as it
 * applies to the checkout page. So the announcement's MDX carries the *prose* and takes this table
 * as a prop; both columns come off the wire (`price_inr` and `price_from_dec_2026`).
 *
 * The consequence is that this table is only as correct as the plan rows are. That is the intended
 * failure mode: if an operator changes a price and forgets the announcement, the announcement is
 * already right.
 */
export function PriceChangeTable({ plans }: { plans: readonly PlanSummary[] }) {
  if (plans.length === 0) {
    return (
      <p className="mt-4 max-w-prose text-sm text-muted-foreground">
        The plan prices are served by the billing service, which did not answer when this page was
        built. The pricing page reads them live.
      </p>
    );
  }

  return (
    <div className="mt-6 overflow-x-auto">
      <table className="w-full border-collapse text-sm tabular-nums">
        <thead>
          <tr className="border-b border-border text-xs uppercase tracking-wide text-muted-foreground">
            <th scope="col" className="py-2 pr-4 text-left font-medium">
              Plan
            </th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">
              Now
            </th>
            <th scope="col" className="py-2 text-right font-medium">
              From December 2026
            </th>
          </tr>
        </thead>
        <tbody>
          {plans.map((plan) => (
            <tr key={plan.code} className="border-b border-border/60 last:border-0">
              <th scope="row" className="py-2 pr-4 text-left font-medium">
                {plan.name}
              </th>
              <td className="py-2 pr-4 text-right">{plan.display}</td>
              <td className="py-2 text-right">{plan.displayFromDec2026 ?? "unchanged"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
