"use client";

import { ArrowDownRight, ArrowUpRight, CircleAlert, Minus, NotebookPen } from "lucide-react";
import { useState } from "react";

import { Input } from "@/components/ui/input";
import type { Metric } from "@/lib/portfolio/command-center";
import type { ComparisonRow, NameList, PreviewSide, RebalancePreview } from "@/lib/portfolio/rebalance-preview";
import { cn } from "@/lib/utils";

/**
 * Current weight against target weight, name by name — the one comparison this payload supports
 * in full, and therefore the one the drawer is built around.
 *
 * Both sides are fractions of the portfolio that the server computed: `DetailHoldingOut.weight`
 * is a position's value over the book's, `TargetWeightOut.weight` is the screen's equal split.
 * Comparing them needs no price, no quantity and no assumption, which is exactly why this is the
 * part that gets the space. Everything the brief asks for that WOULD need a price is named on the
 * Impact step instead of being estimated here.
 *
 * ONE ROW RENDERER, TWO LAYOUTS
 * -----------------------------
 * PC1 learned this the expensive way: a ten-column grid inside `overflow-x-auto` is a sideways
 * scroll that hides the columns the reader opened the drawer for. So there is a single `<li>`
 * renderer. Below `md` it stacks and every figure carries its own visible label; at `md` and up
 * the same markup lays out on one line under a header row, and the labels become screen-reader
 * only rather than disappearing. A phone cannot drift into showing less than a desktop, because
 * there is nothing separate for it to drift from.
 *
 * NEVER A BARE DASH, AND NEVER COLOUR ALONE
 * -----------------------------------------
 * Every figure is a `Metric`, so a missing one carries its reason and renders it in place of the
 * number. Direction is an arrow and a word as well as a hue.
 */

const SIDE = {
  entry: { word: "Entry", className: "text-positive", Icon: ArrowUpRight },
  exit: { word: "Exit", className: "text-negative", Icon: ArrowDownRight },
  "inside-window": { word: "Band", className: "text-warning", Icon: Minus },
  hold: { word: "Hold", className: "text-muted-foreground", Icon: Minus },
} as const satisfies Record<PreviewSide, { word: string; className: string; Icon: typeof Minus }>;

const FIGURE = "tabular-nums tracking-tight";

function signOf(value: string | null): "up" | "down" | "flat" {
  if (value === null) return "flat";
  if (value.startsWith("-")) return "down";
  return Number(value) === 0 ? "flat" : "up";
}

/**
 * One weight. A percentage, or the reason there is not one — never a dash, on either layout.
 *
 * The label is in the DOM at every width. It is visible while the row is stacked and screen-reader
 * only once the header row above it carries the same words, so a person listening to the row
 * hears "Target weight, 5.00 percent" rather than a naked number.
 */
function WeightCell({
  metric,
  signed = false,
  className,
}: {
  metric: Metric;
  signed?: boolean;
  className?: string;
}) {
  const tone = signed ? signOf(metric.value) : "flat";
  return (
    <div className={cn("min-w-0", className)}>
      <span className="mr-1.5 text-[0.6875rem] uppercase tracking-wide text-muted-foreground md:sr-only">
        {metric.label}
      </span>
      {metric.value === null ? (
        <span className="inline-flex items-baseline gap-1 text-xs leading-snug text-muted-foreground">
          <CircleAlert aria-hidden="true" className="size-3 shrink-0 self-center text-warning" />
          {metric.unavailable}
        </span>
      ) : (
        <span
          className={cn(
            "text-sm font-semibold",
            FIGURE,
            tone === "up" && "text-positive",
            tone === "down" && "text-negative",
          )}
          title={metric.definition}
        >
          {signed && tone === "up" ? "+" : ""}
          {metric.value}%
        </span>
      )}
    </div>
  );
}

function NoteField({
  row,
  note,
  onNote,
}: {
  row: ComparisonRow;
  note: string;
  onNote: (instrumentId: number, note: string) => void;
}) {
  const [open, setOpen] = useState(note !== "");
  const id = `rebalance-note-${row.instrumentId}`;
  return (
    <div className="mt-1.5 md:col-span-full">
      {open ? (
        <label className="flex items-center gap-2 text-[0.6875rem] uppercase tracking-wide text-muted-foreground">
          <NotebookPen aria-hidden="true" className="size-3.5 shrink-0" />
          <span className="sr-only">Your note on {row.symbol}</span>
          <Input
            id={id}
            value={note}
            maxLength={120}
            data-testid={`note-${row.symbol}`}
            placeholder={`Anything to carry with ${row.symbol} into the plan`}
            className="h-7 text-xs normal-case"
            onChange={(event) => onNote(row.instrumentId, event.target.value)}
          />
        </label>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          data-testid={`add-note-${row.symbol}`}
          className="inline-flex items-center gap-1 text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          <NotebookPen aria-hidden="true" className="size-3" />
          Note
        </button>
      )}
    </div>
  );
}

export function ComparisonRowItem({
  row,
  note,
  onToggleExclude,
  onNote,
}: {
  row: ComparisonRow;
  note: string;
  onToggleExclude: (instrumentId: number) => void;
  onNote: (instrumentId: number, note: string) => void;
}) {
  const side = SIDE[row.side];
  const { Icon } = side;
  return (
    <li
      data-testid={`row-${row.symbol}`}
      data-excluded={row.excluded ? "true" : "false"}
      className={cn(
        "px-3 py-2.5 md:grid md:grid-cols-[minmax(0,1fr)_6.5rem_6.5rem_6.5rem_7rem] md:items-baseline md:gap-3",
        row.excluded && "bg-muted/40",
      )}
    >
      <div className="min-w-0">
        <p className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-sm font-semibold">{row.symbol}</span>
          <span className={cn("inline-flex items-center gap-0.5 text-[0.6875rem] font-medium uppercase tracking-wide", side.className)}>
            <Icon aria-hidden="true" className="size-3" />
            {side.word}
          </span>
          {row.excluded ? (
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
              Excluded
            </span>
          ) : null}
          <span className="min-w-0 truncate text-xs text-muted-foreground">{row.name}</span>
        </p>
        <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{row.ruleLine}</p>
        {row.brokers.length > 0 ? (
          <p className="mt-0.5 text-[0.6875rem] text-muted-foreground">
            Held at {row.brokers.join(", ")}
          </p>
        ) : null}
      </div>

      <WeightCell metric={row.current} className="mt-1.5 md:mt-0 md:text-right" />
      <WeightCell metric={row.target} className="mt-0.5 md:mt-0 md:text-right" />
      <WeightCell metric={row.delta} signed className="mt-0.5 md:mt-0 md:text-right" />

      <div className="mt-1.5 md:mt-0 md:text-right">
        {row.excludable ? (
          <button
            type="button"
            aria-pressed={row.excluded}
            data-testid={`exclude-${row.symbol}`}
            onClick={() => onToggleExclude(row.instrumentId)}
            className={cn(
              "rounded-md border px-2 py-1 text-xs font-medium transition-colors duration-150",
              row.excluded
                ? "border-brand/40 bg-brand-muted text-brand-strong hover:brightness-95"
                : "border-border text-muted-foreground hover:border-foreground/50 hover:text-foreground",
            )}
          >
            {row.excluded ? `Put ${row.symbol} back` : `Exclude ${row.symbol}`}
          </button>
        ) : (
          <span className="text-[0.6875rem] text-muted-foreground">No action to exclude</span>
        )}
        {row.excludable ? <NoteField row={row} note={note} onNote={onNote} /> : null}
      </div>
    </li>
  );
}

/** The four lists the API returns, each named, counted and carrying its own rule. */
export function ListSummary({ lists }: { lists: readonly NameList[] }) {
  return (
    <ul
      data-testid="rebalance-lists"
      className="grid gap-px overflow-hidden rounded-xl border border-border bg-border sm:grid-cols-2 xl:grid-cols-4"
    >
      {lists.map((list) => {
        const side = SIDE[list.key];
        const { Icon } = side;
        return (
          <li key={list.key} data-testid={`list-${list.key}`} className="bg-card px-3 py-2.5">
            <p className="flex items-center gap-1.5">
              <Icon aria-hidden="true" className={cn("size-3.5", side.className)} />
              <span className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
                {list.title}
              </span>
              <span className={cn("ml-auto text-lg font-semibold", FIGURE)}>{list.count}</span>
            </p>
            <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
              {list.count === 0 ? list.emptyMessage : list.rule}
            </p>
          </li>
        );
      })}
    </ul>
  );
}

export function WeightComparison({
  preview,
  notes,
  onToggleExclude,
  onNote,
}: {
  preview: RebalancePreview;
  notes: ReadonlyMap<number, string>;
  onToggleExclude: (instrumentId: number) => void;
  onNote: (instrumentId: number, note: string) => void;
}) {
  return (
    <section aria-label="Current weight against target weight" data-testid="weight-comparison">
      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <div className="hidden border-b border-border px-3 py-2 text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground md:grid md:grid-cols-[minmax(0,1fr)_6.5rem_6.5rem_6.5rem_7rem] md:gap-3">
          <span>Name</span>
          <span className="text-right">Current</span>
          <span className="text-right">Target</span>
          <span className="text-right">Change</span>
          <span className="text-right">In the plan</span>
        </div>
        <ul className="divide-y divide-border/60">
          {preview.rows.map((row) => (
            <ComparisonRowItem
              key={row.instrumentId}
              row={row}
              note={notes.get(row.instrumentId) ?? ""}
              onToggleExclude={onToggleExclude}
              onNote={onNote}
            />
          ))}
        </ul>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        Weights only. A quantity is not shown anywhere in this drawer, and is not derived from
        weight, value and price — see what the Impact step lists as produced elsewhere.
      </p>
    </section>
  );
}
