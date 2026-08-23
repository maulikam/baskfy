"use client";

import type { BacktestOut } from "@baskfy/api-client";
import { Download } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import dynamic from "next/dynamic";

import { AssumptionsPanel } from "@/components/backtests/assumptions-panel";
import { FragilityPanel } from "@/components/backtests/fragility-panel";
import { HoldingsPanel } from "@/components/backtests/holdings-panel";
import { MetricsTable } from "@/components/backtests/metrics-table";
import { ProgressPanel } from "@/components/backtests/progress-panel";
import { TradeLog } from "@/components/backtests/trade-log";
import { ErrorState } from "@/components/data/error-state";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type Artefact,
  exportLink,
  isTerminal,
  useBacktest,
  useHoldings,
  useTrades,
} from "@/lib/backtests/queries";

/*
 * The two charts are the only visx on this route (docs/02 locks visx for charts), and this page
 * spends most of its life showing a *progress* panel: a queued or running backtest has no curve
 * to draw, and a failed one never will. Loading the charting code with the rest of the page would
 * put it in the first-load budget for every one of those states. Prompt 16 deliverable 5 —
 * "dynamic import of charts"; `apps/web/scripts/bundle-budget.mjs` is what keeps it honest.
 *
 * `ssr: false` because a chart measures itself against the viewport; server-rendering one and
 * re-rendering it on hydration is a layout shift, which docs/08 budgets at CLS < 0.1.
 */
const EquityChart = dynamic(
  () => import("@/components/backtests/equity-chart").then((m) => m.EquityChart),
  { ssr: false, loading: () => <Skeleton className="h-64 w-full" /> },
);

const DrawdownChart = dynamic(
  () => import("@/components/backtests/drawdown-chart").then((m) => m.DrawdownChart),
  { ssr: false, loading: () => <Skeleton className="h-48 w-full" /> },
);

/**
 * `/backtests/[id]` — docs/08 §Backtests:
 *
 *     "queued job with progress → results page: equity curve vs benchmark, drawdown chart,
 *      metrics table, per-period holdings, trade log, and an 'assumptions' panel that states
 *      costs, slippage and the survivorship-bias handling in plain English."
 *
 * Every one of those is below, in that order. The page has three shapes — queued/running (the
 * progress panel), failed (the error the worker recorded, verbatim), and done — and it moves
 * between them without a reload, because `useBacktest` polls while the run is not terminal and
 * the SSE stream nudges it the moment the worker finishes.
 *
 * No `<Disclaimer/>` here: `AppShell` renders one into every page of the application frame, and a
 * second copy of the same regulatory sentence on the same screen reads as a bug rather than as
 * emphasis. The assumptions panel still carries the *run's* honesty disclaimer (past results do
 * not predict future results) — that is a different sentence, from the API.
 */
export interface BacktestResultProps {
  publicId: string;
  initial: BacktestOut | null;
  error: unknown;
}

export function BacktestResult({ publicId, initial, error }: BacktestResultProps) {
  const router = useRouter();
  const query = useBacktest(publicId, initial ?? undefined);
  const backtest = query.data;
  const done = backtest?.status === "done";
  const trades = useTrades(publicId, done);
  const holdings = useHoldings(publicId, null, done);
  const refetch = useCallback(() => void query.refetch(), [query]);

  if (error) return <ErrorState error={error} onRetry={() => router.refresh()} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={refetch} />;
  if (!backtest) return <Skeleton className="h-96 w-full" />;

  return (
    <div className="space-y-8">
      <Header backtest={backtest} />

      {!isTerminal(backtest.status) ? (
        <ProgressPanel
          publicId={publicId}
          status={backtest.status}
          onFinished={refetch}
        />
      ) : null}

      {backtest.status === "failed" ? (
        <section className="rounded-lg border border-destructive/40 bg-destructive/10 p-4">
          <h2 className="text-sm font-semibold">This backtest failed</h2>
          <p className="mt-2 font-mono text-sm">{backtest.error ?? "No reason was recorded."}</p>
        </section>
      ) : null}

      {done ? (
        <>
          <EquityChart
            points={backtest.equity_curve ?? []}
            benchmarkLabel={backtest.config.benchmark ?? "Benchmark"}
          />
          <DrawdownChart points={backtest.drawdown ?? []} />
          <MetricsTable backtest={backtest} />
          <FragilityPanel runs={backtest.fragility ?? []} />

          <section className="space-y-3" aria-labelledby="backtest-holdings">
            <h2 id="backtest-holdings" className="text-sm font-semibold">
              Per-rebalance holdings
            </h2>
            <HoldingsPanel
              holdings={holdings.data?.data ?? []}
              loading={holdings.isLoading}
            />
          </section>

          <section className="space-y-3" aria-labelledby="backtest-trades">
            <h2 id="backtest-trades" className="text-sm font-semibold">
              Trade log
            </h2>
            <TradeLog
              trades={trades.data?.data ?? []}
              loading={trades.isLoading}
              truncated={Boolean(trades.data?.next_cursor)}
            />
          </section>
        </>
      ) : null}

      <AssumptionsPanel
        assumptions={backtest.assumptions ?? []}
        disclaimer={backtest.disclaimer}
      />
    </div>
  );
}

function Header({ backtest }: { backtest: BacktestOut }) {
  return (
    <header className="flex flex-wrap items-start gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">
          {backtest.screen_name ?? "Backtest"}
        </h1>
        <p className="text-sm text-muted-foreground">
          {backtest.config.start} to {backtest.config.end} ·{" "}
          {backtest.config.rebalance?.frequency ?? "monthly"} ·{" "}
          top {backtest.config.selection?.top_n ?? 20}
          {backtest.config.selection?.hold_buffer
            ? ` + ${backtest.config.selection.hold_buffer} buffer`
            : ""}{" "}
          · {backtest.config.weighting ?? "equal"} weight
        </p>
        {backtest.metrics_hash ? (
          <p className="mt-1 font-mono text-xs text-muted-foreground">
            metrics hash {backtest.metrics_hash.slice(0, 16)}…
          </p>
        ) : null}
      </div>
      {backtest.status === "done" ? (
        <div className="ml-auto flex flex-wrap gap-2">
          <ExportButton publicId={backtest.public_id} artefact="trades" label="Trades CSV" />
          <ExportButton publicId={backtest.public_id} artefact="holdings" label="Holdings CSV" />
          <ExportButton publicId={backtest.public_id} artefact="equity" label="Equity CSV" />
        </div>
      ) : null}
    </header>
  );
}

/**
 * docs/07: `GET /backtests/{id}/export` → a **signed URL**. It is short-lived, so it is minted on
 * the click rather than rendered into the page where it would go stale while the user reads.
 */
function ExportButton({
  publicId,
  artefact,
  label,
}: {
  publicId: string;
  artefact: Artefact;
  label: string;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Button
      variant="secondary"
      size="sm"
      disabled={busy}
      onClick={() => {
        setBusy(true);
        void exportLink(publicId, artefact)
          .then((link) => {
            window.location.assign(link.url);
          })
          .finally(() => setBusy(false));
      }}
    >
      <Download aria-hidden="true" className="size-3.5" />
      {label}
    </Button>
  );
}
