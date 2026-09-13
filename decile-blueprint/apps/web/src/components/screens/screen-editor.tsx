"use client";

import type { ScreenOut, StatusOut } from "@baskfy/api-client";
import { CalendarClock, Columns3, Copy, Loader2, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { parseAsString, useQueryState } from "nuqs";
import { useCallback, useMemo, useRef, useState } from "react";

import { ErrorState } from "@/components/data/error-state";
import { ApplyFiltersPill } from "@/components/screens/apply-filters-pill";
import { ExportButton } from "@/components/screens/export-button";
import { FilterChipBar } from "@/components/screens/filter-chip-bar";
import { PortfolioFit } from "@/components/screens/portfolio-fit";
import { PresetsMenu } from "@/components/screens/presets-menu";
import { ResultsPanel } from "@/components/screens/results-panel";
import type { ColumnMeta } from "@/components/screens/result-columns";
import { ScreenIdentity } from "@/components/screens/screen-identity";
import { ShareButton } from "@/components/screens/share-button";
import { StoryStrip } from "@/components/screens/story-strip";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatTradeDate } from "@/lib/format";
import { countDefinitionChanges } from "@/lib/screens/change-count";
import { defaultDefinition } from "@/lib/screens/defaults";
import {
  useColumns,
  useDuplicateScreen,
  useDeleteScreen,
  useFactors,
  usePreview,
  useSaveScreen,
  useScreen,
  useTradingDays,
  useUniverses,
} from "@/lib/screens/queries";
import { buildStorySentence } from "@/lib/screens/story";
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

  const [draftName, setDraftName] = useState(screen.name);
  const [pendingDelete, setPendingDelete] = useState(false);
  // Reset the draft when the saved name or the screen itself changes. Adjusting state during
  // render against the last-seen source avoids the extra render a synchronising effect costs.
  const [draftSource, setDraftSource] = useState({ name: screen.name, publicId: screen.public_id });
  if (draftSource.name !== screen.name || draftSource.publicId !== screen.public_id) {
    setDraftSource({ name: screen.name, publicId: screen.public_id });
    setDraftName(screen.name);
  }
  const factors = useFactors();
  const columns = useColumns();
  const universes = useUniverses();
  const save = useSaveScreen();
  const duplicate = useDuplicateScreen();
  const remove = useDeleteScreen();
  const [demoDismissed, setDemoDismissed] = useState(false);

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
  const nameDirty = draftName.trim() !== screen.name && draftName.trim().length > 0;
  const persistCount = changeCount + (nameDirty ? 1 : 0);

  const previousWorking = useRef(working);
  const patch = useCallback(
    (partial: Partial<typeof working>) => {
      previousWorking.current = working;
      void setRaw(encodeState(saved, { ...working, ...partial }));
    },
    [saved, working, setRaw],
  );
  const undoLastFilter = useCallback(() => {
    void setRaw(encodeState(saved, previousWorking.current));
  }, [saved, setRaw]);

  const reset = useCallback(() => {
    setDraftName(screen.name);
    void setRaw(encodeState(saved, { ...defaultDefinition(), index: working.index, sort_by: working.sort_by }));
  }, [saved, working.index, working.sort_by, setRaw, screen.name]);

  const apply = useCallback(() => {
    const name = draftName.trim() || screen.name;
    const afterSave = (publicId: string) => {
      void setRaw(null);
      setDraftName(name);
      if (publicId !== screen.public_id) {
        router.push(`/build/${publicId}` as never);
        return;
      }
      router.refresh();
    };
    if (screen.editable) {
      save.mutate(
        { publicId: screen.public_id, name: draftName.trim() || screen.name, definition: working },
        { onSuccess: (savedScreen) => afterSave(savedScreen.public_id) },
      );
      return;
    }
    if (!screen.is_example) return;
    duplicate.mutate(screen.public_id, {
      onSuccess: (copy) => {
        save.mutate(
          {
            publicId: copy.public_id,
            name: draftName.trim() || copy.name,
            definition: working,
          },
          { onSuccess: (savedScreen) => afterSave(savedScreen.public_id) },
        );
      },
    });
  }, [save, duplicate, screen, working, draftName, setRaw, router]);

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

  const storySentence = useMemo(
    () =>
      buildStorySentence({
        definition: settled,
        resultCount: preview.data?.result_count,
        universes: universes.data ?? [],
        factors: factors.data ?? [],
      }),
    [settled, preview.data?.result_count, universes.data, factors.data],
  );

  const historical = working.historical_date;
  const readOnly = !screen.editable;
  const showDemoBanner = readOnly && screen.is_example && !demoDismissed;

  const filterProps = {
    definition: working,
    patch,
    factors: factors.data ?? [],
    universes: universes.data ?? [],
    operands,
    tradingDays: tradingDays.data?.dates ?? [],
    dataStartDate: status?.data_start_date ?? null,
    latestDate: status?.as_of ?? null,
    onReset: reset,
    disabled: readOnly && !screen.is_example,
  };

  return (
    <div className="vaaya-surface vaaya-shell flex min-h-0 flex-col gap-6 rounded-[28px] px-1 py-2 sm:px-2">
      <header className="flex flex-wrap items-start gap-4 pt-1">
        <div className="min-w-0 space-y-2">
          <ScreenIdentity
            name={draftName}
            onNameChange={setDraftName}
            canEdit={screen.editable || screen.is_example}
            canDelete={screen.editable}
            onDelete={() => setPendingDelete(true)}
          />
          <p className="max-w-2xl text-sm font-light text-muted-foreground">
            {screen.is_example
              ? "A template. Change the name or filters and Apply — that saves a copy you own, because the original is shared."
              : `Last updated ${formatTradeDate(screen.updated_at.slice(0, 10))}`}
          </p>
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          {readOnly ? (
            <Button
              variant="secondary"
              size="sm"
              className="rounded-full px-5"
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
              Duplicate & make it yours
            </Button>
          ) : null}

          <PresetsMenu
            definition={working}
            patch={patch}
            disabled={readOnly && !screen.is_example}
          />

          <Button
            variant="outline"
            size="icon"
            asChild
            aria-label="Edit columns"
            className="vaaya-pill size-9 rounded-full border-border bg-card shadow-none"
          >
            <Link href={`/build/${screen.public_id}/columns`}>
              <Columns3 aria-hidden="true" />
            </Link>
          </Button>

          <ShareButton
            screenName={screen.name}
            storySentence={storySentence}
            asOf={preview.data?.as_of ?? null}
            rows={preview.data?.rows ?? []}
          />

          <ExportButton
            publicId={screen.public_id}
            screenName={screen.name}
            asOf={preview.data?.as_of ?? null}
            iconOnly
          />
        </div>
      </header>

      {showDemoBanner ? (
        <div
          role="status"
          data-testid="demo-banner"
          className="vaaya-card flex items-start gap-3 px-4 py-3 text-sm font-light text-muted-foreground"
        >
          <p className="flex-1">
            This is a demo screen — look, poke, sort. Duplicate it to make it yours.
          </p>
          <Button
            variant="ghost"
            size="icon"
            className="size-7 shrink-0"
            aria-label="Dismiss demo banner"
            onClick={() => setDemoDismissed(true)}
          >
            <X aria-hidden="true" className="size-3.5" />
          </Button>
        </div>
      ) : null}

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

      {!screen.editable && !screen.is_example ? (
        <p className="rounded-md border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
          This screen is read-only. Your edits preview live but cannot be saved — duplicate it to
          keep them.
        </p>
      ) : null}

      {save.error ? <ErrorState error={save.error} onRetry={() => save.reset()} /> : null}

      <StoryStrip
        definition={settled}
        result={preview.data}
        universes={universes.data ?? []}
        factors={factors.data ?? []}
        isPending={preview.isPending && !preview.data}
      />

      <div className="flex min-w-0 flex-col gap-5">
        <FilterChipBar {...filterProps} />
        <ResultsPanel
          result={preview.data}
          columnMeta={columnMeta}
          sortingFactorUnit={sortingFactorUnit}
          isPending={preview.isPending}
          isFetching={preview.isFetching}
          error={preview.error}
          onRetry={() => void preview.refetch()}
          onLoosenFilters={reset}
          onUndoLastFilter={undoLastFilter}
          screenName={screen.name}
          screenPublicId={screen.public_id}
          definition={settled}
        />
        <PortfolioFit
          definition={settled}
          asOf={preview.data?.as_of}
          dataVersion={preview.data?.data_version}
        />
      </div>

      {(screen.editable || screen.is_example) && persistCount > 0 ? (
        <ApplyFiltersPill
          changeCount={persistCount}
          saving={save.isPending || duplicate.isPending}
          onApply={apply}
          onReset={reset}
        />
      ) : null}

      <Dialog open={pendingDelete} onOpenChange={setPendingDelete}>
        <DialogContent className="max-w-md">
          <DialogTitle className="text-base font-semibold">Delete “{screen.name}”?</DialogTitle>
          <DialogDescription className="mt-2 text-sm text-muted-foreground">
            This removes the screen and its run history. It cannot be undone.
          </DialogDescription>
          <div className="mt-4 flex gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={remove.isPending}
              data-testid="confirm-delete"
              onClick={() => {
                void remove.mutateAsync(screen.public_id).then(() => {
                  setPendingDelete(false);
                  router.push("/build");
                });
              }}
            >
              Delete screen
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setPendingDelete(false)}>
              Keep it
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
