"use client";

import { useMemo, useState } from "react";

import { NOT_A_PNL_EVENT } from "@/components/portfolio/detail-activity";
import { MetricValue, Panel, TradeDate } from "@/components/portfolio/detail/primitives";
import { Badge } from "@/components/ui/badge";
import { formatQuantity } from "@/lib/portfolios/decimal";
import { metric } from "@/lib/portfolio/command-center";
import { ACTIVITY_LABELS, activityCounts } from "@/lib/portfolio/detail-tabs";
import type { ActivityItem } from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * The Activity tab: the feed, with the shape of it stated before the rows.
 *
 * ## The one rule a row must never break
 *
 * §4.5 and §11 criterion 6: a split or a bonus changes quantity and average price and produces
 * **zero** profit. A bare "+200 shares" row reads like a windfall, so every row the server marks
 * `is_pnl_event = false` carries the sentence saying so. The sentence itself is imported from
 * `detail-activity.tsx` as {@link NOT_A_PNL_EVENT} rather than retyped, so the two surfaces
 * cannot end up wording the same accounting rule differently. The client never decides which rows
 * those are; it prints the flag the server set, because the server is where the rule is tested.
 *
 * ## Why the rows are rendered here rather than by `DetailActivity`
 *
 * `DetailActivity` renders a null amount as an em dash, which is correct for the page it was
 * built for and is exactly what this workspace forbids. An amount is genuinely absent on a
 * corporate action and on a reconciliation entry, and "no cash moved" is a far more useful cell
 * than a dash: the dash makes a reader wonder whether the figure failed to load. So the amount
 * goes through `metric()` like every other figure in this directory, and carries its reason.
 *
 * ## Three different messages that all look like an empty list
 *
 * "We could not ask" (`items === null`), "nothing has ever happened here" (`items` empty) and
 * "nothing of that kind happened" (a filter matched none) are three different facts and are three
 * different sentences. Rendering any of them as the others is how a page lies quietly.
 */

const RECONCILIATION_LABELS: Readonly<Record<string, string>> = {
  OPEN: "Waiting on your answer",
  RESOLVED: "Resolved",
  DISMISSED: "Dismissed",
};

const NO_AMOUNT_REASON =
  "No cash amount is recorded against this event. A corporate action or a reconciliation answer changes what you hold without money moving.";

export interface ActivityTabProps {
  items: readonly ActivityItem[] | null;
  unavailableReason: string | null;
}

export function ActivityTab({ items, unavailableReason }: ActivityTabProps) {
  const [kind, setKind] = useState<string | null>(null);
  const counts = useMemo(() => activityCounts(items), [items]);
  const filtered = useMemo(
    () => (items === null || kind === null ? items : items.filter((item) => item.kind === kind)),
    [items, kind],
  );

  return (
    <>
      {counts.length > 0 ? (
        <Panel
          title="What is in this feed"
          blurb="Every kind of event recorded against this portfolio, and how many of each."
          testId="activity-counts"
        >
          <div
            role="group"
            aria-label="Filter activity by kind"
            className="flex flex-wrap gap-1.5 px-4 py-3"
          >
            <FilterChip
              label="Everything"
              count={items?.length ?? 0}
              active={kind === null}
              onClick={() => setKind(null)}
              testId="activity-filter-all"
            />
            {counts.map((entry) => (
              <FilterChip
                key={entry.kind}
                label={entry.label}
                count={entry.count}
                active={kind === entry.kind}
                onClick={() => setKind(entry.kind === kind ? null : entry.kind)}
                testId={`activity-filter-${entry.kind}`}
              />
            ))}
          </div>
        </Panel>
      ) : null}

      <Panel
        title="What has happened here"
        blurb="Buys, sells, cash you assign, dividends, corporate actions and reconciliation answers, newest first."
        testId="activity-feed"
      >
        {items === null ? (
          <p data-testid="activity-unavailable" className="px-4 py-6 text-sm text-muted-foreground">
            {unavailableReason ??
              "The activity feed did not load. What happened here is missing rather than nothing."}
          </p>
        ) : items.length === 0 ? (
          <p data-testid="activity-empty" className="max-w-[76ch] px-4 py-6 text-sm text-muted-foreground">
            Nothing has happened in this portfolio yet. Every buy, sell, cash assignment, dividend,
            corporate action and reconciliation answer lands here as it happens.
          </p>
        ) : filtered !== null && filtered.length === 0 ? (
          <p
            data-testid="activity-filter-empty"
            className="max-w-[76ch] px-4 py-6 text-sm text-muted-foreground"
          >
            Nothing of that kind has happened in this portfolio. Every other event is still here;
            only this view is narrowed.
          </p>
        ) : (
          <ul className="divide-y divide-border/60" data-testid="activity-list">
            {(filtered ?? []).map((item, index) => {
              const amount = metric(
                "Amount",
                "The cash this event moved.",
                item.amount,
                NO_AMOUNT_REASON,
              );
              return (
                <li
                  key={`${item.on}-${item.kind}-${index}`}
                  data-testid={`activity-row-${item.kind}`}
                  className="flex items-start justify-between gap-3 px-4 py-2.5"
                >
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={item.is_pnl_event === false ? "neutral" : "outline"}>
                        {ACTIVITY_LABELS[item.kind] ?? item.kind}
                      </Badge>
                      <span className="text-sm">{item.description}</span>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      <TradeDate iso={item.on} />
                      {item.broker ? ` · ${item.broker.label}` : ""}
                      {item.quantity === null || item.quantity === undefined
                        ? ""
                        : ` · ${formatQuantity(item.quantity)} shares`}
                      {item.reconciliation_state
                        ? ` · ${RECONCILIATION_LABELS[item.reconciliation_state] ?? item.reconciliation_state}`
                        : ""}
                    </p>
                    {item.is_pnl_event === false ? (
                      <p className="text-xs text-muted-foreground">{NOT_A_PNL_EVENT}</p>
                    ) : null}
                  </div>
                  <span className="shrink-0 text-right text-sm">
                    <MetricValue
                      metric={amount}
                      kind="rupees"
                      signed={item.is_pnl_event !== false}
                      compact={amount.value === null}
                      short="no cash moved"
                    />
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </Panel>
    </>
  );
}

function FilterChip({
  label,
  count,
  active,
  onClick,
  testId,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  testId: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      data-testid={testId}
      className={cn(
        "flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors duration-150",
        active
          ? "border-brand bg-brand-muted text-brand-strong"
          : "border-border text-muted-foreground hover:text-foreground",
      )}
    >
      {label}
      <span className="tabular-nums opacity-70">{count}</span>
    </button>
  );
}
