"use client";

import type { ScreenOut, StatusOut } from "@baskfy/api-client";
import { CalendarClock, Loader2, Save } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { parseAsString, useQueryState } from "nuqs";
import { useCallback, useMemo } from "react";

import { Disclaimer } from "@/components/data/disclaimer";
import { ErrorState } from "@/components/data/error-state";
import { ExportButton } from "@/components/screens/export-button";
import { FilterForm } from "@/components/screens/filter-form";
import { ResultsPanel } from "@/components/screens/results-panel";
import type { ColumnMeta } from "@/components/screens/result-columns";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatTradeDate } from "@/lib/format";
import { defaultDefinition } from "@/lib/screens/defaults";
import {
  useColumns,
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

/**
 * The screen editor — docs/08 §"Screen editor" and §"Results panel".
 *
 * Three pieces of state, and it matters which is which:
 *
 * * **The saved definition** (`screen.definition`) — what the server holds.
 * * **The working definition** — the saved one plus whatever the URL carries. It lives in the URL
 *   rather than in React state, which is what makes docs/08's "shareable, back-button-correct"
 *   true: the browser's history *is* the undo stack, and a copied address bar reproduces the form
 *   exactly (`src/lib/screens/url-state.ts`).
 * * **The debounced definition** — the working one, 400 ms after it stops changing. This is what
 *   the preview runs on, and what TanStack Query keys its cache by.
 *
 * "Unsaved changes" is therefore not a flag anyone has to remember to set: it is
 * `saved !== working`, computed.
 */
export interface ScreenEditorProps {
  screen: ScreenOut;
  status: StatusOut | null;
}

export function ScreenEditor({ screen: initial, status }: ScreenEditorProps) {
  const router = useRouter();
  // The live copy, seeded from the server render — see `useScreen` for why the prop is not enough.
  const { data: screen } = useScreen(initial.public_id, initial);
  const saved = useMemo(() => parseDefinition(screen.definition), [screen.definition]);
  /*
   * `history: "push"` is what makes docs/08's "back-button-correct" true: each change becomes a
   * history entry, so Back undoes it rather than leaving the editor. `throttleMs` keeps that from
   * becoming one entry per keystroke — it is the same 400 ms the preview debounces by, so the URL
   * and the results settle together.
   */
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

  const tradingDays = useTradingDays(status?.data_start_date ?? null, status?.as_of ?? null);

  const preview = usePreview({
    definition: settled,
    columns: screen.columns,
    enabled: factors.isSuccess && universes.isSuccess,
  });

  const dirty = !definitionsEqual(saved, working);

  const patch = useCallback(
    (partial: Partial<typeof working>) => {
      void setRaw(encodeState(saved, { ...working, ...partial }));
    },
    [saved, working, setRaw],
  );

  const reset = useCallback(() => {
    // Reset to the *defaults*, not to the saved screen: docs/08 lists "Reset to defaults" and
    // "Duplicate screen" as separate actions, and a user who wants the saved state can discard.
    void setRaw(encodeState(saved, { ...defaultDefinition(), index: working.index, sort_by: working.sort_by }));
  }, [saved, working.index, working.sort_by, setRaw]);

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

  return (
    <div className="flex min-h-0 flex-col gap-4">
      <header className="flex flex-wrap items-center gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold tracking-tight">{savedName}</h1>
          <p className="text-xs text-muted-foreground">
            {screen.is_example
              ? "A read-only example. Duplicate it to make changes you can save."
              : `Last updated ${formatTradeDate(screen.updated_at.slice(0, 10))}`}
          </p>
        </div>
        {dirty ? (
          <Badge variant="warning" data-testid="unsaved-badge">
            Unsaved changes
          </Badge>
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link href={`/screens/${screen.public_id}/columns`}>Edit Columns</Link>
          </Button>
          <ExportButton
            publicId={screen.public_id}
            screenName={screen.name}
            asOf={preview.data?.as_of ?? null}
          />
          <Button
            variant="primary"
            size="sm"
            disabled={!dirty || !screen.editable || save.isPending}
            data-testid="apply-filters"
            onClick={() =>
              save.mutate(
                { publicId: screen.public_id, definition: working },
                {
                  onSuccess: () => {
                    // Clear the URL diff — it is now the saved state — and invalidate the RSC
                    // payload so a reload reads the new definition rather than the cached one.
                    void setRaw(null);
                    router.refresh();
                  },
                },
              )
            }
          >
            {save.isPending ? (
              <Loader2 aria-hidden="true" className="animate-spin" />
            ) : (
              <Save aria-hidden="true" />
            )}
            Update &amp; Apply Filters
          </Button>
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

      {screen.editable ? null : (
        <p className="rounded-md border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
          Example screens are read-only. Your edits preview live but cannot be saved — duplicate the
          screen from the list to keep them.
        </p>
      )}

      {save.error ? <ErrorState error={save.error} onRetry={() => save.reset()} /> : null}

      <div className="grid min-h-0 gap-6 lg:grid-cols-[380px_minmax(0,1fr)]">
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
          className="lg:max-h-[calc(100dvh-16rem)]"
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
        />
      </div>

      <Disclaimer />
    </div>
  );
}
