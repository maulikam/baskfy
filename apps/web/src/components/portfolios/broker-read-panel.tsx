"use client";

import { Eye } from "lucide-react";
import { useState } from "react";

import { ErrorState } from "@/components/data/error-state";
import { HoldingsProvenance } from "@/components/portfolios/holdings-provenance";
import { Button } from "@/components/ui/button";
import { formatQuantity, formatRupees } from "@/lib/portfolios/decimal";
import { describeProvenance, type SyncHoldingsOut } from "@/lib/portfolios/provenance";
import { useSyncHoldings } from "@/lib/portfolios/queries";
import { HOLDINGS_SYNC_READY, type CoverageBroker } from "@/lib/portfolios/rollup";

/**
 * Ask a broker what it holds, and keep the label the server puts on the answer.
 *
 * This is a **read**. `POST /brokers/{id}/sync-holdings` fetches rows; it has no order path, and
 * neither does anything else in this application. Desk non-negotiable #1 keeps the only order
 * route on the desk console, behind an explicitly confirmed plan id that expires in thirty
 * minutes; the web app has never had that route and does not gain one here. The route's own name
 * is deliberately not written anywhere in this package, because a source scan for it is one of
 * the checks that keeps the promise.
 *
 * The point of the panel is the label. Whatever comes back is rendered through
 * `describeProvenance`, so a fixture is marked as a fixture, a degraded read is marked as
 * degraded, and a genuine live read is **not** marked — which was the defect: before leaf C1
 * every non-empty answer, live ones included, was captioned "fixture holdings".
 *
 * Quantity is the broker's `total_quantity`, which is `quantity + t1_quantity +
 * collateral_quantity` (desk non-negotiable #2) rather than the settled figure alone.
 */

export interface BrokerReadPanelProps {
  brokers: CoverageBroker[];
}

export function BrokerReadPanel({ brokers }: BrokerReadPanelProps) {
  const sync = useSyncHoldings();
  const [read, setRead] = useState<{ broker: CoverageBroker; result: SyncHoldingsOut } | null>(
    null,
  );

  const readable = brokers.filter((broker) => broker.holdingsSync === HOLDINGS_SYNC_READY);

  return (
    <section
      aria-label="Read holdings from a broker"
      data-testid="broker-read-panel"
      className="space-y-3 rounded-xl border border-border/70 bg-card p-4 sm:p-5"
    >
      <div>
        <h2 className="text-sm font-semibold">Check what a broker reports</h2>
        <p className="text-xs text-muted-foreground">
          A read, not a sync you have to undo, and never an order. Whatever comes back is labelled
          with where it came from.
        </p>
      </div>

      {readable.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No broker in the catalog can report holdings yet, so there is nothing to read.
        </p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {readable.map((broker) => (
            <Button
              key={broker.id}
              variant="outline"
              size="sm"
              disabled={sync.isPending}
              data-testid={`read-${broker.id}`}
              onClick={() => {
                sync.mutate(broker.id, {
                  onSuccess: (result) => setRead({ broker, result }),
                });
              }}
            >
              <Eye aria-hidden="true" />
              {sync.isPending ? "Reading…" : `Read ${broker.name}`}
            </Button>
          ))}
        </div>
      )}

      {sync.isError ? <ErrorState error={sync.error} /> : null}

      {read !== null ? (
        <div className="space-y-3">
          <HoldingsProvenance view={describeProvenance(read.result, read.broker.name)} />
          {read.result.holdings.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
                    <th scope="col" className="py-2 pr-3">
                      Name
                    </th>
                    <th scope="col" className="py-2 pr-3 text-right">
                      Units
                    </th>
                    <th scope="col" className="py-2 text-right">
                      Average price
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {read.result.holdings.map((holding) => (
                    <tr key={`${holding.exchange}-${holding.symbol}`} className="border-b last:border-0">
                      <td className="py-1.5 pr-3 font-medium">{holding.symbol}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {formatQuantity(holding.total_quantity)}
                      </td>
                      <td className="py-1.5 text-right tabular-nums">
                        {formatRupees(holding.average_price)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
