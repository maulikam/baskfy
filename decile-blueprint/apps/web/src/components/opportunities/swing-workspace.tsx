"use client";

import { RefreshCw, Square } from "lucide-react";
import { useState } from "react";

import { AskPanel } from "@/components/opportunities/ask-panel";
import { ErrorPanel, ProgressBar } from "@/components/opportunities/states";
import { SwingFilters } from "@/components/opportunities/swing-filters";
import { SwingResultView } from "@/components/opportunities/swing-results";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { toProblem, useOpportunitiesMeta, useSwingScan } from "@/lib/opportunities/hooks";
import type { OpportunitiesMeta, SwingScanRequest } from "@/lib/opportunities/types";
import { progressPercent } from "@/lib/opportunities/view";

function SwingScanner({ meta }: { meta: OpportunitiesMeta }) {
  const [filters, setFilters] = useState<SwingScanRequest>(meta.swing_defaults);
  const { state, run, cancel } = useSwingScan();
  const running = state.phase === "running";
  const percent = progressPercent(state.progress);

  return (
    <div className="space-y-5">
      <SwingFilters meta={meta} value={filters} onChange={setFilters} />
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" disabled={running} onClick={() => void run(filters)}>
          <RefreshCw className={running ? "animate-spin" : undefined} aria-hidden />
          {running ? "Scanning…" : "Scan"}
        </Button>
        {running ? (
          <Button variant="outline" onClick={cancel}>
            <Square aria-hidden />
            Cancel
          </Button>
        ) : null}
        <Button variant="ghost" disabled={running} onClick={() => setFilters(meta.swing_defaults)}>
          Reset filters
        </Button>
        {state.phase === "cancelled" ? <span className="text-xs text-muted-foreground">Scan cancelled.</span> : null}
      </div>
      {running ? (
        <ProgressBar
          percent={percent}
          label={
            state.progress
              ? `${state.progress.stage} — ${state.progress.done.toLocaleString("en-IN")} of ${state.progress.total.toLocaleString("en-IN")}`
              : "Starting scan…"
          }
        />
      ) : null}
      {state.error ? <ErrorPanel problem={state.error} onRetry={() => void run(filters)} /> : null}
      {state.result ? (
        <SwingResultView response={state.result} horizonMax={filters.horizon_max} />
      ) : state.phase === "idle" ? (
        <p className="text-sm text-muted-foreground">
          Set the filters and press Scan. Results are computed from the last completed session.
        </p>
      ) : null}
    </div>
  );
}

export function SwingWorkspace() {
  const meta = useOpportunitiesMeta();
  if (meta.isPending) {
    return (
      <div className="space-y-3" aria-busy="true">
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-9 w-32" />
      </div>
    );
  }
  if (meta.isError) {
    return <ErrorPanel problem={toProblem(meta.error)} onRetry={() => void meta.refetch()} />;
  }
  return (
    <div className="space-y-8">
      <SwingScanner meta={meta.data} />
      <AskPanel />
    </div>
  );
}
