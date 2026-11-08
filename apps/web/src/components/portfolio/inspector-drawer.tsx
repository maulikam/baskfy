"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { ArrowUpRight, X } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useEffect, useId, useState } from "react";

import { Money, MoneyDelta } from "@/components/portfolio/amounts";
import { CombinedChart } from "@/components/portfolio/combined-chart";
import { ReturnValue } from "@/components/portfolio/return-value";
import { SourceBadge } from "@/components/portfolio/source-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { EMPTY_CELL, formatFraction, formatNumber, formatTradeDate } from "@/lib/format";
import { loadInspector, type InspectorData } from "@/lib/portfolio/inspector";
import {
  MONITORING_NOTE,
  describeBrokers,
  fromFigure,
  toneFor,
  type PortfolioRow,
} from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * §6.5's right-side inspector drawer — *"~40% width, no page navigation"*.
 *
 * The no-navigation rule is the point of the whole control. Comparing four portfolios means
 * opening four drawers; a route change per row would discard the table's scroll position, its
 * active grouping tab and the reader's place in a list they are working down. So this is a Radix
 * dialog over the same page — `next/navigation` is never called, and the full detail page (§7) is
 * offered as a link at the bottom for the reader who does want to leave.
 *
 * On a phone 40% of the width is nothing, so it becomes a bottom sheet, the same responsive
 * treatment `screens/peek-drawer` already uses.
 *
 * ## What the three tabs may and may not show
 *
 * **Performance** shows the portfolio's own NAV series and, where the payload has one, its
 * benchmark — and, on a subscribed portfolio, the publisher's model figure as a *second, labelled*
 * number beside the user's. §11 criterion 5 forbids one blended figure, so the two are rendered
 * by the same component with `is_model` visible on the model one; they are never added, averaged
 * or shown as a single "performance".
 *
 * **Holdings** shows quantity, average price where a purchase history exists, and both
 * contributions. An unknown average price is an em dash — a holding whose cost basis was never
 * imported (§5.3) has no purchase price, and inventing one from the current mark would turn every
 * position into a zero-profit position.
 *
 * **Activity** lists what happened. A corporate action is flagged as not a P&L event, because
 * §4.5 is explicit that a split or a bonus changes quantity and average price and produces no
 * profit, and a bare "+200 shares" row reads like a windfall.
 */

type TabKey = "PERFORMANCE" | "HOLDINGS" | "ACTIVITY";

const TABS: readonly { key: TabKey; label: string }[] = [
  { key: "PERFORMANCE", label: "Performance" },
  { key: "HOLDINGS", label: "Holdings" },
  { key: "ACTIVITY", label: "Activity" },
];

export interface InspectorDrawerProps {
  row: PortfolioRow | null;
  onClose: () => void;
  /** Injected in tests; the default reaches the API from the browser. */
  load?: (portfolioId: number) => Promise<InspectorData>;
}

export function InspectorDrawer({ row, onClose, load = loadInspector }: InspectorDrawerProps) {
  const baseId = useId();
  const portfolioId = row?.portfolio_id ?? null;
  const [tracked, setTracked] = useState<number | null>(portfolioId);
  const [tab, setTab] = useState<TabKey>("PERFORMANCE");
  const [data, setData] = useState<InspectorData | null>(null);
  const [loading, setLoading] = useState(portfolioId !== null);

  /* Reset on a *different* row, during render rather than in an effect. The effect below would
     have to call three setters synchronously to clear the previous portfolio's data, which
     renders the old numbers under the new name for one frame — the React docs' "adjusting state
     when a prop changes" case, and the same shape `screens/peek-drawer` already uses. */
  if (portfolioId !== tracked) {
    setTracked(portfolioId);
    setData(null);
    setTab("PERFORMANCE");
    setLoading(portfolioId !== null);
  }

  useEffect(() => {
    if (portfolioId === null) return;
    let live = true;
    void load(portfolioId).then(
      (result) => {
        if (!live) return;
        setData(result);
        setLoading(false);
      },
      () => {
        if (live) setLoading(false);
      },
    );
    return () => {
      live = false;
    };
  }, [portfolioId, load]);

  if (row === null) return null;

  const excluded = !row.counts_toward_total;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-foreground/25",
            "data-[state=open]:animate-in data-[state=open]:fade-in-0",
            "data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
            "motion-safe:duration-200 motion-reduce:animate-none",
          )}
        />
        <DialogPrimitive.Content
          data-testid="inspector-drawer"
          className={cn(
            "fixed z-50 flex flex-col gap-4 overflow-y-auto overscroll-contain border-border bg-card p-5 shadow-lg outline-none",
            "inset-x-0 bottom-0 max-h-[88vh] rounded-t-[26px] border-t",
            "min-[900px]:inset-y-0 min-[900px]:right-0 min-[900px]:left-auto min-[900px]:max-h-none",
            "min-[900px]:w-[40vw] min-[900px]:min-w-[420px] min-[900px]:max-w-none",
            "min-[900px]:rounded-l-[26px] min-[900px]:rounded-t-none min-[900px]:border-l min-[900px]:border-t-0",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0",
            "min-[900px]:data-[state=open]:slide-in-from-right-4",
            "motion-safe:duration-200 motion-reduce:animate-none",
          )}
        >
          <div
            aria-hidden="true"
            className="mx-auto h-1 w-10 shrink-0 rounded-full bg-muted-foreground/30 min-[900px]:hidden"
          />

          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-1.5">
              <DialogTitle className="text-lg font-semibold">{row.name}</DialogTitle>
              <DialogDescription className="sr-only">
                Details for {row.name}. This panel opens over the list; the list is still there
                behind it.
              </DialogDescription>
              <div className="flex flex-wrap items-center gap-2">
                <SourceBadge row={row} />
                <span className="text-xs text-muted-foreground">{describeBrokers(row)}</span>
              </div>
            </div>
            <DialogPrimitive.Close asChild>
              <Button
                variant="ghost"
                size="icon"
                className="size-8 shrink-0"
                aria-label="Close details"
              >
                <X aria-hidden="true" className="size-4" />
              </Button>
            </DialogPrimitive.Close>
          </div>

          {excluded ? (
            <p
              data-testid="drawer-monitoring-note"
              className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
            >
              {row.excluded_note ?? MONITORING_NOTE}
            </p>
          ) : null}

          <dl className="grid grid-cols-2 gap-3">
            <div className="rounded-lg border border-border/70 p-3">
              <dt className="text-xs text-muted-foreground">Value</dt>
              <dd className="mt-0.5 text-xl font-semibold tabular-nums">
                <Money value={row.value} />
              </dd>
            </div>
            <div className="rounded-lg border border-border/70 p-3">
              <dt className="text-xs text-muted-foreground">{row.todays_pnl.label}</dt>
              <dd
                className={cn("mt-0.5 text-xl font-semibold tabular-nums", toneFor(row.todays_pnl.amount))}
              >
                <MoneyDelta value={row.todays_pnl.amount ?? null} />
              </dd>
            </div>
          </dl>

          <div className="flex flex-wrap items-start gap-6">
            <ReturnValue
              entry={fromFigure(row.headline_return)}
              showReason
              data-testid="drawer-headline-return"
            />
            {row.model_return ? (
              <ReturnValue
                entry={fromFigure(row.model_return)}
                showReason
                data-testid="drawer-model-return"
              />
            ) : null}
          </div>

          <div role="tablist" aria-label="Portfolio details" className="flex gap-1 border-b border-border">
            {TABS.map((entry) => (
              <button
                key={entry.key}
                type="button"
                role="tab"
                id={`${baseId}-tab-${entry.key}`}
                aria-selected={tab === entry.key}
                aria-controls={`${baseId}-panel-${entry.key}`}
                data-testid={`drawer-tab-${entry.key}`}
                onClick={() => setTab(entry.key)}
                className={cn(
                  "-mb-px border-b-2 px-3 py-2 text-sm transition-colors",
                  tab === entry.key
                    ? "border-accent font-medium text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                {entry.label}
              </button>
            ))}
          </div>

          <div
            role="tabpanel"
            id={`${baseId}-panel-${tab}`}
            aria-labelledby={`${baseId}-tab-${tab}`}
            tabIndex={0}
            data-testid="drawer-panel"
            className="min-h-24"
          >
            {loading ? (
              <p className="py-6 text-sm text-muted-foreground">Loading…</p>
            ) : tab === "PERFORMANCE" ? (
              data?.nav ? (
                <CombinedChart chart={data.nav} />
              ) : (
                <p className="py-6 text-sm text-muted-foreground">
                  {data?.failures.nav ??
                    "No end-of-day valuations have been recorded for this portfolio yet."}
                </p>
              )
            ) : tab === "HOLDINGS" ? (
              <HoldingsTable data={data} />
            ) : (
              <ActivityList data={data} />
            )}
          </div>

          <Button variant="secondary" size="sm" asChild className="mt-auto self-start rounded-full px-5">
            <Link
              href={`/portfolio/${row.portfolio_id}` as Route}
              data-testid="drawer-full-page"
            >
              Open the full page
              <ArrowUpRight aria-hidden="true" />
            </Link>
          </Button>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </Dialog>
  );
}

function HoldingsTable({ data }: { data: InspectorData | null }) {
  const holdings = data?.detail?.holdings ?? [];
  if (data?.detail === null || data?.detail === undefined) {
    return (
      <p className="py-6 text-sm text-muted-foreground">
        {data?.failures.detail ?? "Holdings have not loaded."}
      </p>
    );
  }
  if (holdings.length === 0) {
    return (
      <p className="py-6 text-sm text-muted-foreground">
        Nothing is allocated to this portfolio yet.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm" data-testid="drawer-holdings">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            <th scope="col" className="py-1.5 pr-3 font-normal">
              Stock
            </th>
            <th scope="col" className="py-1.5 pr-3 text-right font-normal">
              Qty
            </th>
            <th scope="col" className="py-1.5 pr-3 text-right font-normal">
              Avg
            </th>
            <th scope="col" className="py-1.5 pr-3 text-right font-normal">
              Value
            </th>
            <th scope="col" className="py-1.5 pr-3 text-right font-normal">
              Weight
            </th>
            <th scope="col" className="py-1.5 text-left font-normal">
              Broker
            </th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((holding) => (
            <tr
              key={`${holding.instrument.instrument_id}-${holding.broker.broker_account_id}`}
              className="border-b border-border/50 last:border-0"
            >
              <td className="py-1.5 pr-3">
                <span className="font-medium">{holding.instrument.symbol}</span>
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">
                {formatNumber(holding.quantity, { decimals: 0 })}
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">
                {holding.avg_price === null || holding.avg_price === undefined
                  ? EMPTY_CELL
                  : formatNumber(holding.avg_price, { decimals: 2 })}
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">
                <Money value={holding.value ?? null} />
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">
                {holding.weight === null || holding.weight === undefined
                  ? EMPTY_CELL
                  : formatFraction(holding.weight)}
              </td>
              <td className="py-1.5 text-muted-foreground">{holding.broker.label}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted-foreground">
        An em dash under Avg means no purchase price was ever recorded for that holding.
      </p>
    </div>
  );
}

function ActivityList({ data }: { data: InspectorData | null }) {
  const items = data?.activity;
  if (items === null || items === undefined) {
    return (
      <p className="py-6 text-sm text-muted-foreground">
        {data?.failures.activity ?? "Activity has not loaded."}
      </p>
    );
  }
  if (items.length === 0) {
    return <p className="py-6 text-sm text-muted-foreground">Nothing has happened here yet.</p>;
  }
  return (
    <ul className="divide-y divide-border/60" data-testid="drawer-activity">
      {items.map((item, index) => (
        <li key={`${item.on}-${item.kind}-${index}`} className="flex items-baseline justify-between gap-3 py-2">
          <div className="min-w-0">
            <p className="text-sm">{item.description}</p>
            <p className="text-xs text-muted-foreground">
              {formatTradeDate(item.on)}
              {item.is_pnl_event === false ? " · no profit or loss from this" : ""}
            </p>
          </div>
          <span className={cn("shrink-0 text-sm tabular-nums", toneFor(item.amount))}>
            <MoneyDelta value={item.amount ?? null} />
          </span>
        </li>
      ))}
    </ul>
  );
}
