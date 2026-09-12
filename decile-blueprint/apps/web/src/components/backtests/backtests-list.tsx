"use client";

import type { BacktestSummaryOut, ScreenOut } from "@baskfy/api-client";
import { Plus, Trash2 } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ConfigForm } from "@/components/backtests/config-form";
import { EmptyState } from "@/components/data/empty-state";
import { ErrorState } from "@/components/data/error-state";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { PAGES } from "@/lib/vocabulary";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { UNKNOWN } from "@/lib/backtests/metrics";
import { useBacktests, useDeleteBacktest, useQueueBacktest } from "@/lib/backtests/queries";
import { formatPercent, formatTradeDate } from "@/lib/format";

/**
 * `/backtests` — docs/08 §Routes marks it "client + polling/SSE", and docs/08 §Backtests opens
 * with "Config form (screen, date range, rebalance frequency, top N, weighting, costs) → queued
 * job with progress".
 *
 * The list polls while any run is queued or running (`useBacktests`), so a run started here shows
 * its result without a reload. The per-user concurrency cap is one (Prompt 15 §4), so the "Run
 * backtest" affordance is disabled while one is in flight rather than letting the user collect a
 * 429 — the constraint is real, so the interface should say so before the request, not after.
 *
 * Delete confirms, surfaces `remove.isError`, and the table scrolls horizontally on a narrow
 * viewport (AUDIT 4.3).
 */
export interface BacktestsListProps {
  initial: BacktestSummaryOut[] | null;
  screens: readonly ScreenOut[];
  /** docs/01 §2.13 — the first date the service holds data for. */
  earliest: string;
  latest: string;
  error: unknown;
  /** AFH 5.9: open the new-run form with this screen selected. */
  initialScreenId?: string | null;
}

function backtestHref(publicId: string): Route {
  return `/build/backtests/${publicId}` as Route;
}

export function BacktestsList({
  initial,
  screens,
  earliest,
  latest,
  error,
  initialScreenId = null,
}: BacktestsListProps) {
  const router = useRouter();
  const backtests = useBacktests(initial ?? undefined);
  const queue = useQueueBacktest();
  const remove = useDeleteBacktest();
  const [showForm, setShowForm] = useState(Boolean(initialScreenId));

  if (error) return <ErrorState error={error} onRetry={() => router.refresh()} />;

  const rows = backtests.data ?? [];
  const running = rows.some((row) => row.status === "queued" || row.status === "running");

  function confirmDelete(publicId: string): void {
    if (
      !window.confirm(
        "Delete this backtest? The run and its results will be removed from this account.",
      )
    ) {
      return;
    }
    void remove.mutateAsync(publicId).catch(() => {
      /* `remove.isError` renders below; the promise rejection must not go unhandled. */
    });
  }

  return (
    <div className="space-y-8">
      <SectionTabs section="build" />
      <PageHeader
        title={PAGES["/build/backtests"].title}
        blurb={PAGES["/build/backtests"].blurb}
        actions={
          <Button
            variant={showForm ? "outline" : "primary"}
            size="sm"
            onClick={() => setShowForm((open) => !open)}
          >
            <Plus aria-hidden="true" className="size-4" />
            {showForm ? "Close" : "New run"}
          </Button>
        }
        meta="Nothing here knows anything the market did not know at the time — costs and whole shares included."
      />

      {running ? (
        <p className="rounded-xl border border-border/70 bg-muted/50 p-3.5 text-sm text-muted-foreground">
          One run at a time. The one below has to finish before another can start.
        </p>
      ) : null}

      {showForm ? (
        <ConfigForm
          screens={screens}
          submitting={queue.isPending}
          earliest={earliest}
          latest={latest}
          initialScreenId={initialScreenId}
          onSubmit={(config) => {
            void queue.mutateAsync(config).then((accepted) => {
              setShowForm(false);
              router.push(backtestHref(accepted.public_id));
            });
          }}
        />
      ) : null}

      {queue.isError ? <ErrorState error={queue.error} /> : null}
      {remove.isError ? <ErrorState error={remove.error} /> : null}

      {backtests.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No backtests yet"
          reason="A backtest runs one of your saved screens over a historical window. Nothing has been queued on this account."
          action={{ label: "Configure one", onClick: () => setShowForm(true) }}
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[40rem] text-sm">
            <caption className="sr-only">Your backtests</caption>
            <thead className="bg-muted/50 text-left text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">Screen</th>
                <th scope="col" className="px-3 py-2 font-medium">Window</th>
                <th scope="col" className="px-3 py-2 font-medium">Status</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">CAGR</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Max DD</th>
                <th scope="col" className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.public_id} className="border-t border-border">
                  <td className="px-3 py-2">
                    <Link
                      href={backtestHref(row.public_id)}
                      className="font-medium underline-offset-4 hover:underline"
                    >
                      {row.screen_name ?? row.screen_public_id ?? "Inline definition"}
                    </Link>
                    <span className="block text-xs text-muted-foreground">
                      top {row.top_n} · {row.rebalance_frequency} · {row.weighting}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {formatTradeDate(row.start)} → {formatTradeDate(row.end)}
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={row.status} error={row.error ?? null} />
                  </td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums">
                    {row.cagr === null || row.cagr === undefined
                      ? UNKNOWN
                      : formatPercent(row.cagr * 100, 2)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums">
                    {row.max_drawdown === null || row.max_drawdown === undefined
                      ? UNKNOWN
                      : formatPercent(row.max_drawdown * 100, 2)}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Delete backtest ${row.public_id}`}
                      onClick={() => confirmDelete(row.public_id)}
                    >
                      <Trash2 aria-hidden="true" className="size-4" />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

const STATUS_STYLES: Record<string, string> = {
  queued: "bg-muted text-muted-foreground",
  running: "bg-accent/15 text-accent",
  done: "bg-positive/15 text-positive",
  failed: "bg-negative/15 text-negative",
};

function StatusBadge({ status, error }: { status: string; error: string | null }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-xs ${STATUS_STYLES[status] ?? "bg-muted"}`}
      title={error ?? undefined}
    >
      {status}
    </span>
  );
}
