"use client";

import { MoneyDelta } from "@/components/portfolio/amounts";
import { Badge } from "@/components/ui/badge";
import { formatNumber, formatTradeDate } from "@/lib/format";
import { toneFor, type ActivityItem } from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * §7's activity block: *"buys/sells, internal cash assignments, dividends, corporate actions,
 * rebalances, reconciliation history."*
 *
 * One ordered feed rather than five tabs, because the question a reader brings here — *why does
 * that number look like that* — is answered by the sequence of events, and splitting the sequence
 * across tabs is that sequence taken apart. The API merges the five sources for the same reason.
 *
 * ## The one thing a row must never imply
 *
 * §4.5 and §11 criterion 6: a split or a bonus changes quantity and average price and produces
 * **zero P&L**. A bare "+200 shares" row reads like a windfall, so every row the API marks
 * `is_pnl_event = false` — corporate actions and reconciliation entries alike — carries the
 * sentence saying so. The client does not decide which rows those are; it prints the flag the
 * server set, because the server is where the accounting rule is tested.
 *
 * An internal cash assignment (§4.4) is a real row here and a real XIRR event, but it is not a
 * gain either — it is the reader's own money arriving. Its own description says which it is.
 */

const KIND_LABELS: Readonly<Record<string, string>> = {
  BUY: "Buy",
  SELL: "Sell",
  DIVIDEND: "Dividend",
  ASSIGN: "Cash assigned",
  RELEASE: "Cash released",
  EXTERNAL_DEPOSIT: "Deposit",
  EXTERNAL_WITHDRAWAL: "Withdrawal",
  CORPORATE_ACTION: "Corporate action",
  RECONCILIATION: "Reconciliation",
};

const RECONCILIATION_LABELS: Readonly<Record<string, string>> = {
  OPEN: "Waiting on your answer",
  RESOLVED: "Resolved",
  DISMISSED: "Dismissed",
};

/** The sentence criterion 6 turns on. */
export const NOT_A_PNL_EVENT = "No profit or loss comes from this.";

export interface DetailActivityProps {
  items: readonly ActivityItem[] | null;
  /** The sentence to print instead of a feed when the read failed. */
  unavailableReason?: string | null;
}

export function DetailActivity({ items, unavailableReason = null }: DetailActivityProps) {
  return (
    <section
      aria-label="Activity"
      className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4"
    >
      <h2 className="text-sm font-semibold">What has happened here</h2>

      {items === null ? (
        <p data-testid="detail-activity-unavailable" className="text-sm text-muted-foreground">
          {unavailableReason ?? "The activity feed did not load."}
        </p>
      ) : items.length === 0 ? (
        <p data-testid="detail-activity-empty" className="text-sm text-muted-foreground">
          Nothing has happened in this portfolio yet. Buys, sells, cash you assign, dividends,
          corporate actions and reconciliation answers all land here.
        </p>
      ) : (
        <ul className="divide-y divide-border/60" data-testid="detail-activity">
          {items.map((item, index) => (
            <li
              key={`${item.on}-${item.kind}-${index}`}
              data-testid={`detail-activity-${item.kind}`}
              className="flex items-start justify-between gap-3 py-2.5"
            >
              <div className="min-w-0 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={item.is_pnl_event === false ? "neutral" : "outline"}>
                    {KIND_LABELS[item.kind] ?? item.kind}
                  </Badge>
                  <span className="text-sm">{item.description}</span>
                </div>
                <p className="text-xs text-muted-foreground">
                  {formatTradeDate(item.on)}
                  {item.broker ? ` · ${item.broker.label}` : ""}
                  {item.quantity === null || item.quantity === undefined
                    ? ""
                    : ` · ${formatNumber(item.quantity, { decimals: 0 })} shares`}
                  {item.reconciliation_state
                    ? ` · ${RECONCILIATION_LABELS[item.reconciliation_state] ?? item.reconciliation_state}`
                    : ""}
                </p>
                {item.is_pnl_event === false ? (
                  <p className="text-xs text-muted-foreground">{NOT_A_PNL_EVENT}</p>
                ) : null}
              </div>
              <span
                className={cn(
                  "shrink-0 text-sm tabular-nums",
                  item.is_pnl_event === false ? "text-muted-foreground" : toneFor(item.amount),
                )}
              >
                <MoneyDelta value={item.amount ?? null} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
