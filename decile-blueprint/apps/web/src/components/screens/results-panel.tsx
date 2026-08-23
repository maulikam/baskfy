"use client";

import type { ScreenRunResponse } from "@baskfy/api-client";
import { useMemo, useState } from "react";

import { DataTable, type Density } from "@/components/data/data-table";
import { EmptyState } from "@/components/data/empty-state";
import { ErrorState } from "@/components/data/error-state";
import { PeekDrawer } from "@/components/screens/peek-drawer";
import {
  buildColumns,
  type ColumnMeta,
  type ResultRow,
} from "@/components/screens/result-columns";
import { ScreenBasketView } from "@/components/screens/screen-basket-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { formatTradeDate } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * docs/08 §"Results panel", top to bottom:
 *
 *     "Header strip: `N results` · `Results are shown for <date>` ·
 *      `Sorting Factor Column's Value = <FACTOR>` · `Edit Columns` · `Export`."
 *
 *     "Client-side re-sort on any visible column (does not re-run the screen; label it clearly)."
 *
 *     "Loading: skeleton rows, never a spinner over stale data. Stale-while-revalidate with a
 *      subtle 'updating' pill."
 *
 * The three strings in the header strip are the reference product's own wording. They are not
 * decoration: "Results are shown for 14 Aug 2026" is how a user finds out their weekend date was
 * snapped backwards (docs/06 §step 1), and the sorting-factor line is how they find out which of
 * 64 factors that unlabelled column of numbers actually is.
 */
export interface ResultsPanelProps {
  result: ScreenRunResponse | undefined;
  columnMeta: ReadonlyMap<string, ColumnMeta>;
  sortingFactorUnit: string;
  isPending: boolean;
  isFetching: boolean;
  error: unknown;
  onRetry: () => void;
  /** docs/08's "one-click 'loosen this filter' affordance" for the empty state. */
  onLoosenFilters: (() => void) | undefined;
  actions?: React.ReactNode;
  className?: string;
  /** Basket view title — defaults to "Screen basket". */
  screenName?: string;
  /** Top-N for materialization (screen setting / default 20). */
  topN?: number;
}

const TABLE_HEIGHT = 560;

export function ResultsPanel({
  result,
  columnMeta,
  sortingFactorUnit,
  isPending,
  isFetching,
  error,
  onRetry,
  onLoosenFilters,
  actions,
  className,
  screenName = "Screen basket",
  topN = 20,
}: ResultsPanelProps) {
  const [density, setDensity] = useState<Density>("comfortable");
  const [peeked, setPeeked] = useState<ResultRow | null>(null);

  const columns = useMemo(
    () =>
      buildColumns(
        result?.columns ?? [],
        columnMeta,
        result?.sorting_factor.label ?? "",
        sortingFactorUnit,
      ),
    [result?.columns, result?.sorting_factor.label, columnMeta, sortingFactorUnit],
  );

  const rows = useMemo<readonly ResultRow[]>(
    () => result?.rows ?? [],
    [result?.rows],
  );

  if (error && !result) {
    return <ErrorState error={error} onRetry={onRetry} className={className} />;
  }

  return (
    <div className={cn("flex min-w-0 flex-col gap-3", className)}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <p className="text-sm font-medium tnum" data-testid="result-count">
          {isPending ? "…" : `${result?.result_count ?? 0} results`}
        </p>
        <p className="text-sm text-muted-foreground" data-testid="as-of">
          Results are shown for {formatTradeDate(result?.as_of ?? null)}
        </p>
        <p className="text-sm text-muted-foreground" data-testid="sorting-factor">
          Ranked by {result?.sorting_factor.label ?? "—"}
        </p>
        {isFetching && !isPending ? (
          <Badge variant="accent" data-testid="updating-pill">
            Updating…
          </Badge>
        ) : null}
        <div className="ml-auto flex items-center gap-3">
          <div className="flex items-center gap-2">
            <Switch
              id="results-density"
              checked={density === "compact"}
              onCheckedChange={(checked) => setDensity(checked ? "compact" : "comfortable")}
            />
            <Label htmlFor="results-density" className="text-xs">
              Compact
            </Label>
          </div>
          {actions}
        </div>
      </div>

      {!isPending && result && result.result_count === 0 ? (
        <EmptyState
          title="0 results"
          reason={emptyReason(result)}
          action={
            onLoosenFilters
              ? { label: "Reset the filters to their defaults", onClick: onLoosenFilters }
              : undefined
          }
        />
      ) : result && !isPending ? (
        <ScreenBasketView
          result={result}
          screenName={screenName}
          topN={topN}
          table={
            <DataTable
              data={rows}
              columns={columns}
              label={`Screen results, ${result.result_count} rows`}
              density={density}
              loading={false}
              height={TABLE_HEIGHT}
              repeatHeaderEvery={0}
              onRowActivate={setPeeked}
            />
          }
        />
      ) : (
        <DataTable
          data={rows}
          columns={columns}
          label={`Screen results, ${result?.result_count ?? 0} rows`}
          density={density}
          loading={isPending}
          height={TABLE_HEIGHT}
          repeatHeaderEvery={0}
          onRowActivate={setPeeked}
        />
      )}

      <p className="text-xs text-muted-foreground">
        Sorting a column re-orders these rows in the browser. It does not re-run the screen, so the
        ranks stay as the server computed them.
      </p>

      {error ? <ErrorState error={error} onRetry={onRetry} /> : null}

      <PeekDrawer
        row={peeked}
        columns={result?.columns ?? []}
        meta={columnMeta}
        sortingFactorLabel={result?.sorting_factor.label ?? ""}
        onClose={() => setPeeked(null)}
      />
    </div>
  );
}

/**
 * docs/08: "Empty state must explain *why*: '0 results — the 1-year filters exclude instruments
 * listed after 19 Aug 2025'".
 *
 * The date is computed from the run's own `as_of` rather than hard-coded, because that sentence is
 * only true relative to the date the screen actually ran for.
 */
function emptyReason(result: ScreenRunResponse): string {
  const asOf = new Date(`${result.as_of}T00:00:00Z`);
  const yearBefore = new Date(asOf);
  yearBefore.setUTCFullYear(yearBefore.getUTCFullYear() - 1);
  const cutoff = formatTradeDate(yearBefore.toISOString().slice(0, 10));
  return (
    `No instrument in this universe passed every filter for ${formatTradeDate(result.as_of)}. ` +
    `Any 1-year filter excludes instruments listed after ${cutoff}, because they have no ` +
    "1-year history to measure — and NULLs never satisfy a predicate."
  );
}

export { Button as ResultsAction };
