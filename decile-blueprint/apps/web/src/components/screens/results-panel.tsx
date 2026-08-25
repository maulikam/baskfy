"use client";

import type { ScreenRunResponse } from "@baskfy/api-client";
import { useEffect, useMemo, useState } from "react";

import { DataTable, type Density } from "@/components/data/data-table";
import { EmptyState } from "@/components/data/empty-state";
import { ErrorState } from "@/components/data/error-state";
import { PeekDrawer } from "@/components/screens/peek-drawer";
import { ResultCards } from "@/components/screens/result-cards";
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
import { columnDisplayLabel, suppressedResultColumns } from "@/lib/screens/column-display";
import { cn } from "@/lib/utils";

const LOADING_LINES = [
  "Crunching tickers…",
  "Doing the math so you don't have to…",
  "Sorting the survivors…",
  "Checking who earned their spot…",
] as const;

const SORT_NOTE =
  "Sorting a column re-orders these rows in the browser. It does not re-run the screen, so the ranks stay as the server computed them.";

export interface ResultsPanelProps {
  result: ScreenRunResponse | undefined;
  columnMeta: ReadonlyMap<string, ColumnMeta>;
  sortingFactorUnit: string;
  isPending: boolean;
  isFetching: boolean;
  error: unknown;
  onRetry: () => void;
  onLoosenFilters: (() => void) | undefined;
  /**
   * Step the last filter back. The control always renders (`data-testid="undo-last-filter"`)
   * so the empty state has a second affordance; without this callback it is disabled rather
   * than silently resetting everything.
   */
  onUndoLastFilter?: (() => void) | undefined;
  actions?: React.ReactNode;
  className?: string;
  screenName?: string;
  /**
   * A fixed name count. Leaving it undefined is what turns the sizing controls on, so the editor
   * omits it and only the compact card previews pin it.
   */
  topN?: number;
  /** Needed to save the basket; absent for an unsaved definition, which has nothing to link to. */
  screenPublicId?: string;
}

const TABLE_HEIGHT = 560;

function LoadingOneLiner() {
  const [index, setIndex] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => {
      setIndex((i) => (i + 1) % LOADING_LINES.length);
    }, 2200);
    return () => window.clearInterval(id);
  }, []);
  return (
    <p
      className="text-sm text-muted-foreground motion-safe:animate-in motion-safe:fade-in"
      data-testid="loading-one-liner"
      key={index}
    >
      {LOADING_LINES[index]}
    </p>
  );
}

function suppressedColumnsNote(
  keys: readonly string[],
  meta: ReadonlyMap<string, ColumnMeta>,
): string {
  const names = keys.map((key) => columnDisplayLabel(key, meta.get(key)?.label ?? key));
  const lead = names[0];
  if (lead === undefined) return "";
  if (names.length === 1) {
    return `${lead} is hidden — every row came back empty.`;
  }
  const last = names[names.length - 1] ?? lead;
  return `${names.slice(0, -1).join(", ")} and ${last} are hidden — every row came back empty.`;
}

function ScreenResultsTable({
  rows,
  columns,
  count,
  density,
  loading,
  onRowActivate,
  contentKey,
  className,
}: {
  rows: readonly ResultRow[];
  columns: ReturnType<typeof buildColumns>;
  count: number;
  density: Density;
  loading: boolean;
  onRowActivate: (row: ResultRow) => void;
  contentKey?: string | undefined;
  className?: string;
}) {
  return (
    <DataTable
      data={rows}
      columns={columns}
      label={`Screen results, ${count} rows`}
      density={density}
      loading={loading}
      height={TABLE_HEIGHT}
      repeatHeaderEvery={0}
      rowHeight={density === "compact" ? 36 : 52}
      sortNote={SORT_NOTE}
      contentKey={contentKey}
      onRowActivate={onRowActivate}
      className={className}
    />
  );
}

export function ResultsPanel({
  result,
  columnMeta,
  sortingFactorUnit,
  isPending,
  isFetching,
  error,
  onRetry,
  onLoosenFilters,
  onUndoLastFilter,
  actions,
  className,
  screenName = "Screen basket",
  topN,
  screenPublicId,
}: ResultsPanelProps) {
  const [density, setDensity] = useState<Density>("comfortable");
  const [peeked, setPeeked] = useState<ResultRow | null>(null);

  const rows = useMemo<readonly ResultRow[]>(
    () => result?.rows ?? [],
    [result?.rows],
  );

  const columns = useMemo(
    () =>
      buildColumns(
        result?.columns ?? [],
        columnMeta,
        result?.sorting_factor.label ?? "",
        sortingFactorUnit,
        rows,
      ),
    [result?.columns, result?.sorting_factor.label, columnMeta, sortingFactorUnit, rows],
  );

  const suppressed = useMemo(
    () => (result ? suppressedResultColumns(result.columns, rows) : []),
    [result, rows],
  );

  const tableContentKey =
    result === undefined ? undefined : `${result.as_of}-${result.result_count}-${rows.length}`;

  if (error && !result) {
    return <ErrorState error={error} onRetry={onRetry} className={className} />;
  }

  const count = result?.result_count ?? 0;
  const asOf = formatTradeDate(result?.as_of ?? null);

  return (
    <div className={cn("flex min-w-0 flex-col gap-4", className)}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <p className="text-sm font-light tabular-nums text-muted-foreground" data-testid="result-count">
          {isPending ? "…" : `${count} matches`}
        </p>
        <p className="text-sm font-light text-muted-foreground" data-testid="as-of">
          {isPending ? null : `fresh as of ${asOf}`}
        </p>
        {/* Keep the seeded factor string for e2e / power users; visually quieter. */}
        <p className="sr-only" data-testid="sorting-factor">
          Ranked by {result?.sorting_factor.label ?? "—"}
        </p>
        {isFetching && !isPending ? (
          <Badge variant="accent" data-testid="updating-pill">
            Updating…
          </Badge>
        ) : null}
        <div className="ml-auto flex items-center gap-3">
          <div className="hidden items-center gap-2 min-[700px]:flex">
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

      {isPending ? <LoadingOneLiner /> : null}

      {!isPending && suppressed.length > 0 ? (
        <p
          className="text-xs text-muted-foreground"
          data-testid="suppressed-columns"
          aria-live="polite"
        >
          {suppressedColumnsNote(suppressed, columnMeta)}
        </p>
      ) : null}

      {!isPending && result && result.result_count === 0 ? (
        <div className="flex flex-col items-center gap-2">
          <EmptyState
            title="Nothing survived your filters"
            reason="Ruthless. Loosen one and try again — or reset to the defaults."
            action={
              onLoosenFilters
                ? { label: "Reset filters to defaults", onClick: onLoosenFilters }
                : undefined
            }
            className="w-full"
          />
          <Button
            variant="ghost"
            size="sm"
            data-testid="undo-last-filter"
            onClick={onUndoLastFilter}
            disabled={!onUndoLastFilter}
          >
            Undo last filter
          </Button>
        </div>
      ) : result && !isPending ? (
        <>
          <div className="hidden min-[700px]:block">
            <ScreenBasketView
              result={result}
              screenName={screenName}
              screenPublicId={screenPublicId}
              topN={topN}
              table={
                <ScreenResultsTable
                  rows={rows}
                  columns={columns}
                  count={result.result_count}
                  density={density}
                  loading={false}
                  contentKey={tableContentKey}
                  onRowActivate={setPeeked}
                />
              }
            />
          </div>
          <div className="min-[700px]:hidden">
            <ResultCards rows={rows} onActivate={setPeeked} />
          </div>
        </>
      ) : (
        <ScreenResultsTable
          rows={rows}
          columns={columns}
          count={result?.result_count ?? 0}
          density={density}
          loading={isPending}
          contentKey={tableContentKey}
          onRowActivate={setPeeked}
        />
      )}

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

export { Button as ResultsAction };
