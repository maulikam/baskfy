"use client";

import { useMemo, useState } from "react";

import { Field, Notice, PanelHeading } from "@/components/portfolio/manage/panel-chrome";
import { TransferPreview } from "@/components/portfolio/manage/transfer-preview";
import { WriteFailure, useWrite } from "@/components/portfolio/manage/write-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { VIEWS_NOTICE } from "@/lib/portfolio/command-center";
import {
  actionById,
  previewAssignment,
  successSentence,
  targetFromRow,
  toTransferRequest,
  type ManageOutcome,
  type TransferRequest,
} from "@/lib/portfolio/manage";
import {
  describeAllocations,
  describeHolding,
  parseHoldingKeyId,
  rowKeyIds,
  type AggregatedHolding,
  type PortfolioDraft,
} from "@/lib/portfolio/organize";
import type { PortfolioRow } from "@/lib/portfolio/overview";

/**
 * Monitoring views — made and edited **without reallocating ownership**, and saying so.
 *
 * WHY THIS IS NOT THE ASSIGN PANEL WITH A DIFFERENT DROPDOWN
 * ----------------------------------------------------------
 * §4.1: a lens answers *"which names"*, never *"how many"*. The API says the same thing in the
 * `POST /portfolio/{id}/holdings` docstring — a quantity sent for a `MONITORING` view is accepted
 * and ignored. So a quantity box on this panel would be a control that changes nothing: the user
 * types 40, the server files the name, and the next page load says all 320. That is the dead
 * control this leaf's gate exists to keep off the screen, so the picker here has no quantity box
 * at all. It picks names.
 *
 * The second difference is what has to be *said*. Assigning moves ownership and the user knows
 * it — they chose "Assign". Making a view moves nothing, and that is genuinely surprising the
 * first time: the holdings stay in whichever capital portfolio they were in, the view enters no
 * total, and the same share may appear in five views at once. None of that is inferable from a
 * form full of ticked checkboxes, so the notice is rendered on every path through this panel —
 * new view and existing view alike — in the brief's own words.
 */

/**
 * A name list, not a share list. No quantity box, because a view has nowhere to put a quantity.
 *
 * Exported so the create form uses the same control when the kind being created is a view; two
 * pickers with two ideas of what a view holds is how the two screens start disagreeing.
 */
export function NamePicker({
  rows,
  selected,
  onChange,
}: {
  rows: readonly AggregatedHolding[];
  selected: ReadonlySet<string>;
  onChange: (next: Set<string>) => void;
}) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const visible = useMemo(
    () =>
      needle === ""
        ? rows
        : rows.filter((row) =>
            `${row.instrument.symbol} ${row.instrument.name}`.toLowerCase().includes(needle),
          ),
    [rows, needle],
  );

  function toggle(row: AggregatedHolding, on: boolean): void {
    const next = new Set(selected);
    for (const id of rowKeyIds(row)) {
      if (on) next.add(id);
      else next.delete(id);
    }
    onChange(next);
  }

  return (
    <section
      aria-label="Names to watch"
      data-testid="view-name-picker"
      className="space-y-3 rounded-xl border border-border bg-card p-3"
    >
      <Field label="Find a stock">
        {(id) => (
          <Input
            id={id}
            type="search"
            value={query}
            placeholder="Symbol or name"
            onChange={(event) => setQuery(event.target.value)}
          />
        )}
      </Field>

      {visible.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-3 py-5 text-center text-xs text-muted-foreground">
          {rows.length === 0
            ? "You hold nothing yet, so there is nothing for a view to watch."
            : "No holding matches that."}
        </p>
      ) : (
        <ul className="max-h-72 divide-y divide-border/70 overflow-y-auto">
          {visible.map((row) => {
            const ids = rowKeyIds(row);
            const on = ids.length > 0 && ids.every((id) => selected.has(id));
            return (
              <li key={row.instrument.instrument_id} className="flex items-start gap-3 py-2">
                <input
                  type="checkbox"
                  checked={on}
                  onChange={(event) => toggle(row, event.target.checked)}
                  aria-label={`Watch ${row.instrument.name} in this view`}
                  className="mt-1 size-4"
                />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{describeHolding(row)}</p>
                  {/* Where it is now — and it STAYS there. The caption is the shipped one, so the
                      drawer and the organize screen describe an allocation the same way. */}
                  <p className="text-xs text-muted-foreground">
                    {row.instrument.symbol}
                    {describeAllocations(row)} — and it stays there.
                  </p>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export interface ViewBuilderProps {
  rows: readonly AggregatedHolding[];
  /** Existing monitoring views. Empty is a normal first-run state, not an error. */
  views: readonly PortfolioRow[];
  onCreate?: ((draft: PortfolioDraft) => Promise<ManageOutcome>) | undefined;
  onTransfer?: ((request: TransferRequest) => Promise<ManageOutcome>) | undefined;
  onSaved: (message: string) => void;
}

type Mode = "NEW" | "EXISTING";

export function ViewBuilder({ rows, views, onCreate, onTransfer, onSaved }: ViewBuilderProps) {
  const action = actionById("watch");
  const write = useWrite();
  const lenses = useMemo(() => views.filter((row) => row.kind === "MONITORING"), [views]);

  const [mode, setMode] = useState<Mode>(lenses.length === 0 ? "NEW" : "EXISTING");
  const [name, setName] = useState("");
  const [viewId, setViewId] = useState<number | null>(lenses[0]?.portfolio_id ?? null);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  const chosenView = lenses.find((row) => row.portfolio_id === viewId) ?? null;

  /* A view being created does not exist yet, so there is no id to preview against. It is given
     the name the user is typing and a placeholder id, purely so the preview can show what the
     view will watch; nothing is sent with it — `createDraft` builds the real body. */
  const destination = useMemo(
    () =>
      mode === "NEW"
        ? {
            portfolio_id: -1,
            name: name.trim() === "" ? "This view" : name.trim(),
            kind: "MONITORING" as const,
          }
        : chosenView === null
          ? null
          : targetFromRow(chosenView),
    [mode, name, chosenView],
  );

  const preview = useMemo(
    () => previewAssignment({ intent: "WATCH", rows, selected, destination }),
    [rows, selected, destination],
  );

  const nameIsBlank = name.trim() === "";
  const canSave =
    selected.size > 0 &&
    (mode === "NEW" ? !nameIsBlank && onCreate !== undefined : chosenView !== null && onTransfer !== undefined);

  async function save(): Promise<void> {
    if (selected.size === 0) return;
    if (mode === "NEW") {
      const keys = [...selected]
        .map(parseHoldingKeyId)
        .filter((key): key is NonNullable<typeof key> => key !== null);
      const draft: PortfolioDraft = {
        start: "HOLDINGS",
        kind: "MONITORING",
        name: name.trim(),
        /* No benchmark for a lens: it is not measured against an index, it is a way of looking at
           holdings that are already measured inside the portfolios that own them. */
        benchmark: "",
        keys,
        sourceId: null,
        targetPortfolioId: null,
      };
      await write.run(
        actionById("create"),
        onCreate === undefined ? undefined : () => onCreate(draft),
        () => {
          const saved = name.trim();
          setSelected(new Set());
          setName("");
          onSaved(`${saved} was created as a monitoring view. ${VIEWS_NOTICE}`);
        },
      );
      return;
    }
    const request = toTransferRequest(preview);
    if (request === null || chosenView === null) return;
    await write.run(
      action,
      onTransfer === undefined ? undefined : () => onTransfer(request),
      () => {
        setSelected(new Set());
        onSaved(successSentence(action, chosenView.name));
      },
    );
  }

  return (
    <div className="space-y-4" data-testid="view-builder" data-mode={mode}>
      <PanelHeading title="Monitoring view">{action.blurb}</PanelHeading>

      {/* Stated at the top, before a single checkbox — not under the save button, where it would
          be read after the decision it is meant to inform. */}
      <Notice testId="view-ownership-notice">
        A monitoring view changes no ownership. Every holding stays in whichever capital portfolio
        — or in Unallocated — it is in now, and the same share may appear in as many views as you
        like. {VIEWS_NOTICE}
      </Notice>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="View">
          {(id) => (
            <Select
              id={id}
              value={mode === "NEW" ? "NEW" : String(viewId ?? "")}
              onChange={(event) => {
                write.clearFailure();
                if (event.target.value === "NEW") {
                  setMode("NEW");
                  return;
                }
                setMode("EXISTING");
                setViewId(Number(event.target.value));
              }}
            >
              <option value="NEW">Create a new view</option>
              {lenses.map((row) => (
                <option key={row.portfolio_id} value={String(row.portfolio_id)}>
                  {row.name}
                </option>
              ))}
            </Select>
          )}
        </Field>

        {mode === "NEW" ? (
          <Field label="Name" hint="What this lens is for — “Dividend payers”, “Watching to trim”.">
            {(id) => (
              <Input
                id={id}
                value={name}
                onChange={(event) => {
                  setName(event.target.value);
                  write.clearFailure();
                }}
                placeholder="Name this view"
              />
            )}
          </Field>
        ) : (
          <div className="space-y-1.5" data-testid="view-currently-watching">
            {/* Not a `Field`: there is no control here, and a `<label for>` pointing at a
                paragraph is a label with nothing to label. */}
            <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
              Currently watching
            </p>
            <p className="text-sm">
              {chosenView === null
                ? "Choose a view."
                : `${chosenView.holdings_count} name${chosenView.holdings_count === 1 ? "" : "s"}. Adding to it takes nothing away.`}
            </p>
          </div>
        )}
      </div>

      <NamePicker
        rows={rows}
        selected={selected}
        onChange={(next) => {
          setSelected(next);
          write.clearFailure();
        }}
      />

      <TransferPreview preview={preview} />

      <WriteFailure failure={write.failure} />

      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <Button
          type="button"
          variant="primary"
          size="sm"
          disabled={!canSave || write.saving}
          onClick={() => void save()}
        >
          {write.saving ? "Saving…" : mode === "NEW" ? "Create this view" : "Add to this view"}
        </Button>
        {canSave ? null : (
          <span className="text-xs text-muted-foreground" data-testid="view-blocked-reason">
            {selected.size === 0
              ? "Tick at least one name for this view to watch."
              : mode === "NEW" && nameIsBlank
                ? "Give the view a name first."
                : mode === "EXISTING" && chosenView === null
                  ? "Choose which view these names go into."
                  : "This page has not passed a save handler for views yet, so nothing would be written. Your selection stays on screen."}
          </span>
        )}
      </div>
    </div>
  );
}
