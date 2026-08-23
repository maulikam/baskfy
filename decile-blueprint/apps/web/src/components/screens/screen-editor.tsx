"use client";

import type { ScreenOut, StatusOut } from "@baskfy/api-client";
import { CalendarClock, Columns3, Copy, Loader2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { parseAsString, useQueryState } from "nuqs";
import { useCallback, useMemo } from "react";

import { ErrorState } from "@/components/data/error-state";
import { ApplyFiltersPill } from "@/components/screens/apply-filters-pill";
import { ExportButton } from "@/components/screens/export-button";
import { FilterForm } from "@/components/screens/filter-form";
import { ResultsPanel } from "@/components/screens/results-panel";
import type { ColumnMeta } from "@/components/screens/result-columns";
import { Button } from "@/components/ui/button";
import { formatTradeDate } from "@/lib/format";
import { countDefinitionChanges } from "@/lib/screens/change-count";
import { defaultDefinition } from "@/lib/screens/defaults";
import {
  useColumns,
  useDuplicateScreen,
  useFactors,
  usePreview,
  useSaveScreen,
  useScreen,
  useTradingDays,
  useUniverses,
} from "@/lib/screens/queries";
import { PREVIEW_DEBOUNCE_MS, useDebounced } from "@/lib/screens/use-debounced";
import { STATE_PARAM, decodeState, definitionsEqual, encodeState, parseDefinition } from "@/lib/screens/url-state";
import { CUSTOM_FILTER_OPERAND_KEYS, OPERAND_LABELS } from "@/lib/screens/operands";

export interface ScreenEditorProps {
  screen: ScreenOut;
  status: StatusOut | null;
}

export function ScreenEditor({ screen: initial, status }: ScreenEditorProps) {
  const router = useRouter();
  const { data: screen } = useScreen(initial.public_id, initial);
  const saved = useMemo(() => parseDefinition(screen.definition), [screen.definition]);
  const [raw, setRaw] = useQueryState(
    STATE_PARAM,
    parseAsString.withOptions({ history: "push", throttleMs: PREVIEW_DEBOUNCE_MS }),
  );
  const working = useMemo(() => decodeState(saved, raw), [saved, raw]);
  const settled = useDebounced(working);

  const savedName = screen.name;
  const factors = useFactors();
  const columns = useColumns();
  const universes = useUniverses();
  const save = useSaveScreen();
  const duplicate = useDuplicateScreen();

  const tradingDays = useTradingDays(status?.data_start_date ?? null, status?.as_of ?? null);

  const preview = usePreview({
    definition: settled,
    columns: screen.columns,
    enabled: factors.isSuccess && universes.isSuccess,
  });

  const dirty = !definitionsEqual(saved, working);
  const changeCount = useMemo(
    () => (dirty ? countDefinitionChanges(saved, working) : 0),
    [dirty, saved, working],
  );

  const patch = useCallback(
    (partial: Partial<typeof working>) => {
      void setRaw(encodeState(saved, { ...working, ...partial }));
    },
    [saved, working, setRaw],
  );

  const reset = useCallback(() => {
    void setRaw(encodeState(saved, { ...defaultDefinition(), index: working.index, sort_by: working.sort_by }));
  }, [saved, working.index, working.sort_by, setRaw]);

  const apply = useCallback(() => {
    save.mutate(
      { publicId: screen.public_id, definition: working },
      {
        onSuccess: () => {
          void setRaw(null);
          router.refresh();
        },
      },
    );
  }, [save, screen.public_id, working, setRaw, router]);

  const columnMeta = useMemo<ReadonlyMap<string, ColumnMeta>>(
    () => new Map((columns.data ?? []).map((column) => [column.key, column])),
    [columns.data],
  );

  const sortingFactorUnit =
    factors.data?.find((factor) => factor.key === settled.sort_by)?.unit ?? "ratio";

  const operands = useMemo(
    () =>
      CUSTOM_FILTER_OPERAND_KEYS.map((key) => ({
        key,
        label:
          columnMeta.get(key)?.label ??
          factors.data?.find((factor) => factor.key === key)?.label ??
          OPERAND_LABELS[key] ??
          key,
      })),
    [columnMeta, factors.data],
  );

  const historical = working.historical_date;
  const readOnly = !screen.editable;

  return (
    <div className="flex min-h-0 flex-col gap-4">
      <header className="flex flex-wrap items-center gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold tracking-tight">{savedName}</h1>
          <p className="text-xs text-muted-foreground">
            {screen.is_example
              ? "A read-only template. Duplicate it to make changes you can save."
              : `Last updated ${formatTradeDate(screen.updated_at.slice(0, 10))}`}
          </p>
        </div>

        <div className="ml-auto flex items-center gap-1.5">
          {readOnly ? (
            <Button
              variant="primary"
              size="sm"
              disabled={duplicate.isPending}
              onClick={() =>
                duplicate.mutate(screen.public_id, {
                  onSuccess: (copy) => router.push(`/build/${copy.public_id}` as never),
                })
              }
            >
              {duplicate.isPending ? (
                <Loader2 aria-hidden="true" className="animate-spin" />
              ) : (
                <Copy aria-hidden="true" />
              )}
              Duplicate &amp; make it yours
            </Button>
          ) : null}

          <Button variant="outline" size="icon" asChild aria-label="Edit columns">
            <Link href={`/build/${screen.public_id}/columns`}>
              <Columns3 aria-hidden="true" />
            </Link>
          </Button>

          <ExportButton
            publicId={screen.public_id}
            screenName={screen.name}
            asOf={preview.data?.as_of ?? null}
            iconOnly
          />
        </div>
      </header>

      {historical ? (
        <div
          role="status"
          data-testid="historical-banner"
          className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning-muted px-3 py-2 text-sm text-warning"
        >
          <CalendarClock aria-hidden="true" className="size-4 shrink-0" />
          <span>
            Viewing this screen as it stood on {formatTradeDate(historical)}. Index membership and
            every factor are point-in-time, so this is what the screen would have shown that day —
            not today&rsquo;s names re-scored.
          </span>
        </div>
      ) : null}

      {readOnly ? (
        <p className="rounded-md border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
          Example screens are read-only. Your edits preview live but cannot be saved — duplicate the
          screen to keep them.
        </p>
      ) : null}

      {save.error ? <ErrorState error={save.error} onRetry={() => save.reset()} /> : null}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)] lg:items-start">
        <FilterForm
          definition={working}
          patch={patch}
          factors={factors.data ?? []}
          universes={universes.data ?? []}
          operands={operands}
          tradingDays={tradingDays.data?.dates ?? []}
          dataStartDate={status?.data_start_date ?? null}
          latestDate={status?.as_of ?? null}
          onReset={reset}
          disabled={readOnly}
        />

        <ResultsPanel
          result={preview.data}
          columnMeta={columnMeta}
          sortingFactorUnit={sortingFactorUnit}
          isPending={preview.isPending}
          isFetching={preview.isFetching}
          error={preview.error}
          onRetry={() => void preview.refetch()}
          onLoosenFilters={reset}
          screenName={screen.name}
        />
      </div>

      {readOnly ? (
        <Button data-testid="apply-filters" disabled className="sr-only" tabIndex={-1}>
          Apply
        </Button>
      ) : null}

      {!readOnly && dirty ? (
        <ApplyFiltersPill
          changeCount={changeCount}
          saving={save.isPending}
          onApply={apply}
          onReset={reset}
        />
      ) : null}
    </div>
  );
}
