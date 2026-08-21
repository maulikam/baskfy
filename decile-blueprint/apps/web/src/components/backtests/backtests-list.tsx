"use client";

import type { BacktestSummaryOut, ScreenOut } from "@decile/api-client";
import { Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ConfigForm } from "@/components/backtests/config-form";
import { Disclaimer } from "@/components/data/disclaimer";
import { EmptyState } from "@/components/data/empty-state";
import { ErrorState } from "@/components/data/error-state";
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
 */
export interface BacktestsListProps {
  initial: BacktestSummaryOut[] | null;
  screens: readonly ScreenOut[];
  /** docs/01 §2.13 — the first date the service holds data for. */
  earliest: string;
  latest: string;
  error: unknown;
}

export function BacktestsList({
  initial,
  screens,
  earliest,
  latest,
  error,
}: BacktestsListProps) {
  const router = useRouter();
  const backtests = useBacktests(initial ?? undefined);
  const queue = useQueueBacktest();
  const remove = useDeleteBacktest();
  const [showForm, setShowForm] = useState(false);

  if (error) return <ErrorState error={error} onRetry={() => router.refresh()} />;

  const rows = backtests.data ?? [];
  const running = rows.some((row) => row.status === "queued" || row.status === "running");

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Backtests</h1>
          <p className="text-sm text-muted-foreground">
            Run a saved screen over history, point in time, with costs and whole shares.
          </p>
        </div>
        <Button
          variant={showForm ? "secondary" : "primary"}
          size="sm"
          className="ml-auto"
          onClick={() => setShowForm((open) => !open)}
        >
          <Plus aria-hidden="true" className="size-4" />
          {showForm ? "Close" : "New backtest"}
        </Button>
      </header>

      {running ? (
        <p className="rounded-md border border-border bg-muted/40 p-3 text-sm text-muted-foreground">
          One backtest at a time per account. The one below finishes before another can start.
        </p>
      ) : null}

      {showForm ? (
        <ConfigForm
          screens={screens}
          submitting={queue.isPending}
          earliest={earliest}
          latest={latest}
          onSubmit={(config) => {
            void queue.mutateAsync(config).then((accepted) => {
              setShowForm(false);
              router.push(`/backtests/${accepted.public_id}` as never);
            });
          }}
        />
      ) : null}

      {queue.isError ? <ErrorState error={queue.error} /> : null}

      {backtests.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No backtests yet"
          reason="A backtest runs one of your saved screens over a historical window. Nothing has been queued on this account."
          action={{ label: "Configure one", onClick: () => setShowForm(true) }}
        />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
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
                      href={`/backtests/${row.public_id}` as never}
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
                      onClick={() => void remove.mutateAsync(row.public_id)}
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

      <Disclaimer variant="block" />
    </div>
  );
}

const STATUS_STYLES: Record<string, string> = {
  queued: "bg-muted text-muted-foreground",
  running: "bg-accent/15 text-accent",
  done: "bg-positive/15 text-positive",
  failed: "bg-destructive/15 text-destructive",
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
