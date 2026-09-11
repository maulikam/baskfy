"use client";

import { Building2, CircleAlert, HandCoins } from "lucide-react";

import type { ComparisonRow, RebalancePreview } from "@/lib/portfolio/rebalance-preview";
import { cn } from "@/lib/utils";

/**
 * The brief's fourth step — *"confirm broker quantities"* — as it can honestly exist here.
 *
 * There are no broker quantities to confirm. `RebalanceOut` carries none, this drawer derives
 * none, and the quantity a person ends up entering depends on the cash they mean to deploy and
 * the price at the moment they enter it. What this step can do, it does:
 *
 *   · **Where each name is held today**, grouped by broker account. That part of "orders grouped
 *     by broker" is real — `DetailHoldingOut.broker` says which account holds each position, so
 *     an exit can be routed to the account that actually has the shares.
 *   · **Which names have no account yet**, because Baskfy does not pick one for you. An entry is
 *     a name you do not hold; where the cash for it sits is your decision, not a derivation.
 *   · **The acknowledgement.** The reader states, in as many words, that the quantities are
 *     theirs to enter. The plan does not assemble until they have.
 *
 * Nothing on this step sends anything. Baskfy's web app has no path to a broker order at all.
 */

function BrokerGroup({ label, rows }: { label: string; rows: readonly ComparisonRow[] }) {
  return (
    <li className="px-3 py-2.5" data-testid={`broker-group-${label}`}>
      <p className="flex items-center gap-1.5 text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
        <Building2 aria-hidden="true" className="size-3.5 shrink-0" />
        {label}
        <span className="ml-auto tabular-nums">{rows.length}</span>
      </p>
      <ul className="mt-1 space-y-0.5">
        {rows.map((row) => (
          <li key={row.instrumentId} className="text-sm">
            <span className="font-medium">{row.symbol}</span>{" "}
            <span className="text-xs text-muted-foreground">{row.ruleLine}</span>
          </li>
        ))}
      </ul>
    </li>
  );
}

export function ConfirmStep({
  preview,
  acknowledged,
  onAcknowledge,
}: {
  preview: RebalancePreview;
  acknowledged: boolean;
  onAcknowledge: (value: boolean) => void;
}) {
  const exits = preview.rows.filter((row) => row.side === "exit" && !row.excluded);
  const entries = preview.rows.filter((row) => row.side === "entry" && !row.excluded);

  const byBroker = new Map<string, ComparisonRow[]>();
  const homeless: ComparisonRow[] = [];
  for (const row of exits) {
    if (row.brokers.length === 0) {
      homeless.push(row);
      continue;
    }
    for (const broker of row.brokers) {
      const bucket = byBroker.get(broker);
      if (bucket) bucket.push(row);
      else byBroker.set(broker, [row]);
    }
  }
  const groups = [...byBroker.entries()].sort(([a], [b]) => a.localeCompare(b));

  return (
    <section aria-label="Confirm" data-testid="confirm-step" className="space-y-4">
      <div
        className="flex items-start gap-2 rounded-xl border border-brand-border bg-brand-muted px-3 py-2.5"
        data-testid="quantity-notice"
      >
        <HandCoins aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-brand-strong" />
        <div>
          <p className="text-sm font-semibold">There are no quantities here, and that is deliberate</p>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
            A rebalance answers with names and target weights. A quantity needs a fill price, a lot
            size and the cash you actually mean to deploy, and it is not derived from weight, value
            and last price here: a derived quantity reads as an instruction, and Baskfy places real
            orders elsewhere with real money. You enter each line at your broker, where the
            quantity, the price and the charges are shown before you commit.
          </p>
        </div>
      </div>

      <div className="rounded-xl border border-border bg-card" data-testid="exit-routing">
        <header className="border-b border-border px-3 py-2.5">
          <h3 className="text-sm font-semibold">Where the exits are held</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            The account that holds the shares is the account the sale has to come from.
          </p>
        </header>
        {groups.length === 0 && homeless.length === 0 ? (
          <p className="px-3 py-3 text-xs text-muted-foreground">
            Nothing to exit — every holding is still inside the buffer, or you have excluded the
            exits.
          </p>
        ) : (
          <ul className="divide-y divide-border/60">
            {groups.map(([label, rows]) => (
              <BrokerGroup key={label} label={label} rows={rows} />
            ))}
            {homeless.length > 0 ? (
              <li className="flex gap-2 px-3 py-2.5" data-testid="broker-unknown">
                <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
                <div>
                  <p className="text-sm font-medium">
                    No account is recorded for {homeless.map((row) => row.symbol).join(", ")}
                  </p>
                  <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
                    The holdings payload has not been loaded for this portfolio, or these positions
                    were imported rather than synced. Check which account holds them before you
                    enter the sale.
                  </p>
                </div>
              </li>
            ) : null}
          </ul>
        )}
      </div>

      <div className="rounded-xl border border-border bg-card" data-testid="entry-routing">
        <header className="border-b border-border px-3 py-2.5">
          <h3 className="text-sm font-semibold">Where the entries go</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Baskfy does not choose. An entry is a name you do not hold, so which account funds it
            is your decision about where your cash sits.
          </p>
        </header>
        {entries.length === 0 ? (
          <p className="px-3 py-3 text-xs text-muted-foreground">
            Nothing to enter — you already hold the whole top {preview.topN ?? "N"}, or you have
            excluded the entries.
          </p>
        ) : (
          <ul className="flex flex-wrap gap-x-4 gap-y-1 px-3 py-3 text-sm">
            {entries.map((row) => (
              <li key={row.instrumentId}>
                <span className="font-medium">{row.symbol}</span>{" "}
                <span className="text-xs tabular-nums text-muted-foreground">
                  {row.target.value === null ? row.target.unavailable : `${row.target.value}%`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <label
        className={cn(
          "flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-3 transition-colors duration-150",
          acknowledged ? "border-brand/50 bg-brand-muted" : "border-border bg-card",
        )}
      >
        <input
          type="checkbox"
          checked={acknowledged}
          data-testid="acknowledge"
          onChange={(event) => onAcknowledge(event.target.checked)}
          className="mt-0.5 size-4 shrink-0 accent-[var(--brand)]"
        />
        <span className="text-sm leading-snug">
          I will enter these myself at my broker. Baskfy has not sent anything and will not.
          <span className="mt-0.5 block text-xs text-muted-foreground">
            The plan on the next step is a document. It has no send button because there is nothing
            for one to call.
          </span>
        </span>
      </label>
    </section>
  );
}
