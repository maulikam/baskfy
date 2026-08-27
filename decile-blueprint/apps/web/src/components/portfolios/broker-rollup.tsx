import { AlertTriangle, Info } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { formatQuantity, formatRupees } from "@/lib/portfolios/decimal";
import type { ConsolidatedHoldings } from "@/lib/portfolios/rollup";

/**
 * The consolidated view: what this portfolio's subtree holds, split by broker account.
 *
 * Three things are said here that a naive total leaves out, and each of them is the difference
 * between a number and a *checkable* number:
 *
 * 1. **Which accounts are in the figure**, by name, with their own share of it.
 * 2. **Which brokers cannot be in it.** Only Zerodha has a wired holdings adapter; the other nine
 *    report `holdings_sync "planned"`. A consolidated total that omits nine brokers in silence is
 *    a wrong number wearing a right label, so the coverage sentence names them.
 * 3. **Whether the arithmetic reconciles.** The API's stated invariant is
 *    `total == sum(by_broker) + unattributed`, exact. It is checked in `bigint`, not assumed, and
 *    a failure is shown rather than swallowed.
 *
 * `cost` is money put in. This endpoint receives no quote and cannot value anything, so nothing
 * on this panel is a mark to market and the copy says so where the total is.
 */

export interface BrokerRollupProps {
  view: ConsolidatedHoldings;
  portfolioName?: string;
}

export function BrokerRollup({ view, portfolioName }: BrokerRollupProps) {
  const { coverage, invariant } = view;
  const attributed = view.lines.filter((line) => !line.unattributed).length;

  return (
    <section
      aria-label="Holdings by broker"
      data-testid="broker-rollup"
      data-spans-brokers={view.spansBrokers ? "true" : "false"}
      data-invariant={invariant.holds ? "holds" : "broken"}
      className="space-y-4 rounded-xl border border-border/70 bg-card p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">
            Whose money is where{portfolioName ? ` — ${portfolioName}` : ""}
          </h2>
          <p className="text-xs text-muted-foreground">
            Every holding in this portfolio and the {view.subtreeSize - 1} portfolio
            {view.subtreeSize - 1 === 1 ? "" : "s"} beneath it, summed per broker account.
          </p>
        </div>
        <Badge variant={view.spansBrokers ? "neutral" : "outline"}>
          {view.spansBrokers
            ? `Connected to ${attributed} broker${attributed === 1 ? "" : "s"}`
            : "One broker account"}
        </Badge>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">
            Holdings by broker account: money put in, quantity and number of holdings.
          </caption>
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="py-2 pr-3">
                Broker account
              </th>
              <th scope="col" className="py-2 pr-3 text-right">
                Money put in
              </th>
              <th scope="col" className="py-2 pr-3 text-right">
                Units
              </th>
              <th scope="col" className="py-2 text-right">
                Holdings
              </th>
            </tr>
          </thead>
          <tbody>
            {view.lines.length === 0 ? (
              <tr>
                <td colSpan={4} className="py-6 text-center text-sm text-muted-foreground">
                  Nothing has been placed in this portfolio yet.
                </td>
              </tr>
            ) : null}
            {view.lines.map((line) => (
              <tr
                key={line.key}
                data-testid="broker-line"
                data-unattributed={line.unattributed ? "true" : "false"}
                className="border-b last:border-0"
              >
                <td className="py-2 pr-3">
                  <span className="font-medium">{line.label}</span>
                  {line.unattributed ? (
                    <span className="ml-2 text-xs text-muted-foreground">
                      no account on the row
                    </span>
                  ) : null}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums">{formatRupees(line.cost)}</td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  {formatQuantity(line.quantity)}
                </td>
                <td className="py-2 text-right tabular-nums">{line.holdings}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t font-semibold" data-testid="broker-total">
              <td className="py-2 pr-3">Total filed here</td>
              <td className="py-2 pr-3 text-right tabular-nums">{formatRupees(view.total.cost)}</td>
              <td className="py-2 pr-3 text-right tabular-nums">
                {formatQuantity(view.total.quantity)}
              </td>
              <td className="py-2 text-right tabular-nums">{view.total.holdings}</td>
            </tr>
          </tfoot>
        </table>
      </div>

      <p
        className="flex gap-2 text-xs leading-relaxed text-muted-foreground"
        data-testid="rollup-coverage"
      >
        <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
        <span>
          {coverage.sentence} Money put in is what was paid, not what it is worth today — this view
          reads no price.
        </span>
      </p>

      {coverage.cannotSync.length > 0 ? (
        <ul
          data-testid="rollup-cannot-sync"
          className="flex flex-wrap gap-1.5"
          aria-label="Brokers that cannot sync holdings"
        >
          {coverage.cannotSync.map((broker) => (
            <li key={broker.id}>
              <Badge variant="warning" data-broker-id={broker.id}>
                {broker.name}: cannot sync
              </Badge>
            </li>
          ))}
        </ul>
      ) : null}

      {!invariant.holds ? (
        <p
          data-testid="rollup-invariant-broken"
          className="flex gap-2 rounded-md border border-negative/40 bg-negative-muted/40 p-3 text-xs leading-relaxed"
        >
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          <span>
            These lines do not add up to the total: the brokers and the unattributed row come to{" "}
            {formatRupees(invariant.sum)}, while the total says {formatRupees(invariant.total)}.
            Treat every figure here as unreliable until that is explained.
          </span>
        </p>
      ) : null}

      {view.declarationConflicts ? (
        <p
          data-testid="rollup-declaration-conflict"
          className="flex gap-2 rounded-md border border-warning/40 bg-warning-muted/40 p-3 text-xs leading-relaxed"
        >
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          <span>
            This portfolio declares one broker account, but holdings underneath it sit at another.
            Neither side has been rewritten — the declaration and the rows are both shown as they
            are, and which one is wrong is yours to say.
          </span>
        </p>
      ) : null}
    </section>
  );
}
