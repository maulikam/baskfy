"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, CircleAlert, Columns3, X } from "lucide-react";

import { MetricValue, Panel } from "@/components/portfolio/detail/primitives";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatTradeDate } from "@/lib/format";
import type { Metric } from "@/lib/portfolio/command-center";
import {
  HOLDING_COLUMNS,
  applyFilter,
  availableFilters,
  sortHoldings,
  summariseSelection,
  type HoldingColumnId,
  type HoldingFilterId,
  type HoldingRow,
  type SortDirection,
} from "@/lib/portfolio/detail-tabs";
import { cn } from "@/lib/utils";

/**
 * The holdings table: sixteen columns, of which every one is a figure the payload actually sent.
 *
 * Brief: an institutional-quality data table, sortable, with column selection, a sticky header, a
 * sticky first column, quick filters, and a selection toolbar. All of that is here. What is NOT
 * here is a column for anything `DetailHoldingOut` does not carry — no exchange, no instrument
 * type, no free-against-pledged quantity, no per-row price age — because a column of dashes is
 * worse than no column: it implies the number exists and is merely missing today.
 * {@link BLOCKED_COLUMNS} is rendered underneath instead, naming what each would take.
 *
 * ## The average-price cell is the whole design of this table in miniature
 *
 * A null average price has four possible causes, and `history_source` says which. A zero would be
 * the actively harmful rendering: ₹0.00 in that column is indistinguishable from a genuinely free
 * share and makes the position read as pure profit. A bare dash is only half a fix. So the cell
 * carries a short reason in words, the whole sentence on `title` and to a screen reader, and the
 * footnote under the table prints every distinct sentence present, because a tooltip is invisible
 * to anyone not holding a mouse.
 *
 * ## Selection places no order and never can
 *
 * The toolbar adds the selected rows up, states their share of the book, and copies their
 * symbols. There is no path from it to a broker. Baskfy's first non-negotiable is that orders
 * fire only from a confirmed plan, and a "sell these" button on a portfolio screen would be the
 * beginning of a second path to one.
 *
 * ## Below `md` the table is not attempted at all
 *
 * A sixteen-column grid on a 390px screen is a sideways scroll that hides the columns a person
 * opened the screen for. The same rows render as a stacked list, and {@link RowFigures} is shared
 * by both so the phone cannot quietly drift into showing less. PC1 learned this the same way.
 */

const SHORT_REASON: Readonly<Record<HoldingColumnId, string>> = {
  security: "unnamed",
  broker: "no broker",
  quantity: "no quantity",
  avgPrice: "no purchase price",
  price: "not priced",
  marketValue: "not priced",
  weight: "not priced",
  targetWeight: "no target",
  drift: "no target",
  todaysPnl: "no previous close",
  todaysPct: "no previous close",
  unrealisedPnl: "no purchase price",
  unrealisedPct: "no purchase price",
  contribution: "portfolio cost unknown",
  heldFor: "no purchase date",
  pricedOn: "never valued",
};

const RUPEE_COLUMNS = new Set<HoldingColumnId>([
  "avgPrice",
  "price",
  "marketValue",
  "todaysPnl",
  "unrealisedPnl",
]);
const PERCENT_COLUMNS = new Set<HoldingColumnId>([
  "weight",
  "targetWeight",
  "todaysPct",
  "unrealisedPct",
  "contribution",
]);
const SIGNED_COLUMNS = new Set<HoldingColumnId>([
  "drift",
  "todaysPnl",
  "todaysPct",
  "unrealisedPnl",
  "unrealisedPct",
  "contribution",
]);

function cellFor(row: HoldingRow, column: HoldingColumnId): Metric {
  switch (column) {
    case "quantity":
      return row.quantity;
    case "avgPrice":
      return row.avgPrice;
    case "price":
      return row.price;
    case "marketValue":
      return row.marketValue;
    case "weight":
      return row.weight;
    case "targetWeight":
      return row.targetWeight;
    case "drift":
      return row.drift;
    case "todaysPnl":
      return row.todaysPnl;
    case "todaysPct":
      return row.todaysPct;
    case "unrealisedPnl":
      return row.unrealisedPnl;
    case "unrealisedPct":
      return row.unrealisedPct;
    case "contribution":
      return row.contribution;
    case "heldFor":
      return row.heldFor;
    case "pricedOn":
      return row.pricedOn;
    case "broker":
      return row.broker;
    default:
      return row.marketValue;
  }
}

function Cell({ row, column }: { row: HoldingRow; column: HoldingColumnId }) {
  const metric = cellFor(row, column);
  if (column === "pricedOn" && metric.value !== null) {
    return <span className="tabular-nums text-xs text-muted-foreground">{formatTradeDate(metric.value)}</span>;
  }
  if (column === "broker") {
    return (
      <MetricValue
        metric={metric}
        kind="text"
        compact={metric.value === null}
        short={SHORT_REASON.broker}
        className="text-xs text-muted-foreground"
      />
    );
  }
  if (column === "heldFor") {
    /* The span answers "how long"; the date under it answers "since when". The brief asks for
       both and they belong in one column, because the second is what the first is measured
       from and a reader comparing two columns of dates has been given a puzzle. */
    return (
      <span className="block">
        <MetricValue
          metric={metric}
          kind="text"
          compact={metric.value === null}
          short={SHORT_REASON.heldFor}
          className="text-xs"
        />
        {metric.since ? (
          <span className="block text-[0.6875rem] tabular-nums text-muted-foreground">
            from {formatTradeDate(metric.since)}
          </span>
        ) : null}
      </span>
    );
  }
  return (
    <MetricValue
      metric={metric}
      kind={
        RUPEE_COLUMNS.has(column)
          ? "rupees"
          : PERCENT_COLUMNS.has(column)
            ? "percent"
            : column === "drift"
              ? "points"
              : "count"
      }
      signed={SIGNED_COLUMNS.has(column)}
      compact={metric.value === null}
      short={SHORT_REASON[column]}
      className="text-sm"
    />
  );
}

/** One renderer for the phone list and the expanded desktop row, so the two cannot diverge. */
function RowFigures({ row, columns }: { row: HoldingRow; columns: readonly HoldingColumnId[] }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-3">
      {columns
        .filter((column) => column !== "security")
        .map((column) => {
          const definition = HOLDING_COLUMNS.find((candidate) => candidate.id === column);
          return (
            <div key={column} className="flex min-w-0 justify-between gap-2">
              <dt className="truncate text-muted-foreground" title={definition?.definition}>
                {definition?.label ?? column}
              </dt>
              <dd className="shrink-0 text-right">
                <Cell row={row} column={column} />
              </dd>
            </div>
          );
        })}
    </dl>
  );
}

function FlagChips({ row }: { row: HoldingRow }) {
  if (row.flags.length === 0) return null;
  return (
    <span className="mt-0.5 flex flex-wrap gap-1">
      {row.flags.map((flag) => (
        <Badge
          key={flag.id}
          variant={flag.severity === "critical" ? "negative" : "warning"}
          title={flag.detail}
          data-testid={`holding-flag-${flag.id}`}
        >
          <CircleAlert aria-hidden="true" className="size-3" />
          {flag.short}
        </Badge>
      ))}
    </span>
  );
}

function SecurityCell({ row }: { row: HoldingRow }) {
  return (
    <span className="block min-w-0">
      <span className="block truncate text-sm font-medium">{row.symbol}</span>
      <span className="block truncate text-xs text-muted-foreground">{row.name}</span>
      <FlagChips row={row} />
    </span>
  );
}

export interface HoldingsTabProps {
  rows: readonly HoldingRow[];
  basketBacked: boolean;
  /**
   * "Valued at close of 10 Sep 2026", or the sentence for a portfolio never valued.
   *
   * One valuation date covers every row, so it belongs above the table rather than repeated down
   * a column. Without it a reader has no way to know whether a price is this morning's or last
   * week's, and the payload has no per-row answer to give them.
   */
  pricedOnLine?: string;
  /** Non-null when the holdings read itself failed, which is not the same as an empty portfolio. */
  unavailableReason?: string | null;
}

export function HoldingsTab({
  rows,
  basketBacked,
  pricedOnLine,
  unavailableReason = null,
}: HoldingsTabProps) {
  const [sortKey, setSortKey] = useState<HoldingColumnId>("marketValue");
  const [direction, setDirection] = useState<SortDirection>(-1);
  const [filter, setFilter] = useState<HoldingFilterId>("all");
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [pickerOpen, setPickerOpen] = useState(false);
  const picker = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  /* Default-on columns are the readable first view; the rest are opt-in. Sixteen columns at once
     is a spreadsheet, and the point of a picker is that the reader chooses which six matter. */
  const [shown, setShown] = useState<ReadonlySet<HoldingColumnId>>(
    () => new Set(HOLDING_COLUMNS.filter((column) => column.defaultOn).map((column) => column.id)),
  );

  const columns = useMemo(
    () =>
      HOLDING_COLUMNS.filter((column) => {
        if (column.basketOnly && !basketBacked) return false;
        return column.fixed || shown.has(column.id);
      }),
    [basketBacked, shown],
  );

  const columnIds = useMemo(() => columns.map((column) => column.id), [columns]);

  const filters = useMemo(() => availableFilters(rows, { basketBacked }), [rows, basketBacked]);
  const filtered = useMemo(() => applyFilter(rows, filter), [rows, filter]);
  const ordered = useMemo(
    () => sortHoldings(filtered, sortKey, direction),
    [filtered, sortKey, direction],
  );

  const selectedRows = useMemo(
    () => ordered.filter((row) => selected.has(row.key)),
    [ordered, selected],
  );
  const selectionSummary = useMemo(() => summariseSelection(selectedRows), [selectedRows]);

  const footnotes = useMemo(() => {
    const seen = new Map<string, string>();
    for (const row of rows) for (const flag of row.flags) seen.set(flag.detail, flag.detail);
    return [...seen.values()];
  }, [rows]);

  /*
   * Computed from the current key rather than inside a `setSortKey` updater.
   *
   * The updater form looked tidier and was wrong: React invokes a state updater twice under
   * StrictMode to surface exactly this, and a `setDirection` inside one fires twice, flipping the
   * direction back to where it started. The header would then have looked like a dead control in
   * development and worked in production, which is the worst pair of behaviours to debug.
   */
  const toggleSort = useCallback(
    (column: HoldingColumnId) => {
      if (column === sortKey) {
        setDirection((value) => (value === 1 ? -1 : 1));
        return;
      }
      setSortKey(column);
      /* Text ascends, figures descend: nobody opens a table of values wanting the smallest. */
      setDirection(column === "security" || column === "broker" || column === "pricedOn" ? 1 : -1);
    },
    [sortKey],
  );

  const toggleRow = useCallback((key: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  /* A menu that only closes by pressing the button that opened it is a menu that stays open.
     Escape and a click outside are what a reader will try, and both work. */
  useEffect(() => {
    if (!pickerOpen) return undefined;
    function onPointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && picker.current?.contains(target) === false) {
        setPickerOpen(false);
      }
    }
    function onEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setPickerOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onEscape);
    };
  }, [pickerOpen]);

  const allShown = ordered.length > 0 && ordered.every((row) => selected.has(row.key));
  const toggleAll = useCallback(() => {
    setSelected((current) => {
      const everyShown = ordered.every((row) => current.has(row.key));
      if (everyShown) return new Set<string>();
      return new Set(ordered.map((row) => row.key));
    });
  }, [ordered]);

  if (unavailableReason !== null) {
    return (
      <Panel title="What this portfolio holds" testId="holdings-unavailable">
        <p className="px-4 py-6 text-sm text-muted-foreground">{unavailableReason}</p>
      </Panel>
    );
  }

  if (rows.length === 0) {
    return (
      <>
        <Panel
          title="What this portfolio holds"
          blurb="Nothing is filed into this portfolio yet."
          testId="holdings-empty"
        >
          <p className="max-w-[70ch] px-4 py-6 text-sm text-muted-foreground">
            Allocating a holding here records which portfolio it belongs to, and nothing else. It
            moves no shares and places no order.
          </p>
        </Panel>
      </>
    );
  }

  return (
    <>
      <Panel
        title="What this portfolio holds"
        blurb={`${rows.length} holding${rows.length === 1 ? "" : "s"}. ${pricedOnLine ?? "Priced at the latest close on record."} Every figure here is one the payload sent; nothing on this table is estimated.`}
        testId="holdings-panel"
        actions={
          <div className="relative" ref={picker}>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPickerOpen((open) => !open)}
              aria-expanded={pickerOpen}
              data-testid="holdings-column-picker-toggle"
            >
              <Columns3 aria-hidden="true" />
              Columns
              <ChevronDown aria-hidden="true" className={cn("transition-transform duration-150", pickerOpen && "rotate-180")} />
            </Button>
            {pickerOpen ? (
              <div
                data-testid="holdings-column-picker"
                className="absolute right-0 z-20 mt-1 w-64 rounded-lg border border-border bg-popover p-2 shadow-md"
              >
                <p className="px-1 pb-1 text-xs text-muted-foreground">
                  The security column always stays.
                </p>
                <ul className="max-h-72 overflow-y-auto">
                  {HOLDING_COLUMNS.filter((column) => !column.fixed).map((column) => {
                    const disabled = column.basketOnly && !basketBacked;
                    const on = shown.has(column.id) && !disabled;
                    return (
                      <li key={column.id}>
                        <button
                          type="button"
                          disabled={disabled}
                          title={
                            disabled
                              ? "This column exists only for a portfolio that tracks a published model."
                              : column.definition
                          }
                          onClick={() =>
                            setShown((current) => {
                              const next = new Set(current);
                              if (next.has(column.id)) next.delete(column.id);
                              else next.add(column.id);
                              return next;
                            })
                          }
                          className={cn(
                            "flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-muted",
                            disabled && "cursor-not-allowed opacity-50",
                          )}
                        >
                          <span
                            aria-hidden="true"
                            className={cn(
                              "grid size-4 shrink-0 place-items-center rounded border",
                              on ? "border-brand bg-brand text-brand-foreground" : "border-border",
                            )}
                          >
                            {on ? <Check className="size-3" /> : null}
                          </span>
                          <span className="min-w-0 flex-1 truncate">{column.label}</span>
                          <span className="sr-only">{on ? "shown" : "hidden"}</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ) : null}
          </div>
        }
      >
        {/* Quick filters. One that cannot be asked of this portfolio is disabled beside its
            reason rather than hidden: a control that vanishes teaches nothing. */}
        <div
          role="group"
          aria-label="Quick filters"
          data-testid="holdings-filters"
          className="flex flex-wrap gap-1.5 border-b border-border px-4 py-2.5"
        >
          {filters.map(({ filter: item, count, unavailable }) => {
            const active = filter === item.id;
            return (
              <button
                key={item.id}
                type="button"
                disabled={unavailable !== null}
                aria-pressed={active}
                title={unavailable ?? item.definition}
                onClick={() => setFilter(item.id)}
                data-testid={`holdings-filter-${item.id}`}
                className={cn(
                  "flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors duration-150",
                  active
                    ? "border-brand bg-brand-muted text-brand-strong"
                    : "border-border text-muted-foreground hover:text-foreground",
                  unavailable !== null && "cursor-not-allowed opacity-60",
                )}
              >
                {item.label}
                <span className="tabular-nums opacity-70">
                  {unavailable === null ? count : 0}
                </span>
              </button>
            );
          })}
        </div>

        {/* The reason a disabled filter is disabled, in words, on the surface itself. */}
        {filters
          .filter((offered) => offered.unavailable !== null)
          .slice(0, 1)
          .map((offered) => (
            <p
              key={offered.filter.id}
              data-testid="holdings-filter-unavailable"
              className="border-b border-border px-4 py-2 text-xs text-muted-foreground"
            >
              {offered.unavailable}
            </p>
          ))}

        {selectedRows.length > 0 ? (
          <SelectionToolbar
            rows={selectedRows}
            summary={selectionSummary}
            onClear={() => setSelected(new Set())}
          />
        ) : null}

        {/* Desktop: the table. `max-h` plus `sticky` is what makes the header stay put while the
            rows scroll under it; the first column sticks sideways for the same reason. */}
        <div className="hidden max-h-[36rem] overflow-auto md:block">
          <table className="w-full min-w-[68rem] border-separate border-spacing-0 text-sm">
            <caption className="sr-only">
              Every holding in this portfolio. Each column can be sorted, and a cell with no figure
              names the reason in place of it.
            </caption>
            <thead>
              <tr>
                <th
                  scope="col"
                  className="sticky left-0 top-0 z-30 w-10 border-b border-border bg-card px-3 py-2 text-left"
                >
                  <input
                    type="checkbox"
                    checked={allShown}
                    onChange={toggleAll}
                    aria-label={allShown ? "Clear the selection" : "Select every row shown"}
                    data-testid="holdings-select-all"
                    className="size-3.5 accent-[var(--brand)]"
                  />
                </th>
                {columns.map((column) => {
                  const active = sortKey === column.id;
                  return (
                    <th
                      key={column.id}
                      scope="col"
                      aria-sort={active ? (direction === 1 ? "ascending" : "descending") : "none"}
                      className={cn(
                        "sticky top-0 z-20 whitespace-nowrap border-b border-border bg-card px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground",
                        column.align === "right" ? "text-right" : "text-left",
                        column.fixed && "left-10 z-30",
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => toggleSort(column.id)}
                        title={column.definition}
                        data-testid={`holdings-sort-${column.id}`}
                        className="inline-flex items-center gap-1 hover:text-foreground"
                      >
                        {column.label}
                        <span aria-hidden="true" className={cn("text-[0.7em]", !active && "opacity-0")}>
                          {direction === 1 ? "▲" : "▼"}
                        </span>
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {ordered.map((row) => {
                const checked = selected.has(row.key);
                return (
                  <tr
                    key={row.key}
                    data-testid={`holding-row-${row.symbol}`}
                    data-selected={checked}
                    className={cn(
                      "transition-colors duration-150 hover:bg-muted/40",
                      checked && "bg-brand-muted/40",
                    )}
                  >
                    <td className="sticky left-0 z-10 border-b border-border/60 bg-card px-3 py-2 align-top">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleRow(row.key)}
                        aria-label={`Select ${row.symbol}`}
                        data-testid={`holdings-select-${row.symbol}`}
                        className="size-3.5 accent-[var(--brand)]"
                      />
                    </td>
                    {columns.map((column) => (
                      <td
                        key={column.id}
                        className={cn(
                          "border-b border-border/60 px-3 py-2 align-top",
                          column.align === "right" ? "text-right" : "text-left",
                          column.fixed && "sticky left-10 z-10 bg-card",
                        )}
                      >
                        {column.id === "security" ? (
                          <SecurityCell row={row} />
                        ) : (
                          <Cell row={row} column={column.id} />
                        )}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Phone: the same rows, stacked, sharing RowFigures with nothing dropped. */}
        <ul className="divide-y divide-border md:hidden" data-testid="holdings-cards">
          {ordered.map((row) => {
            const open = expanded === row.key;
            const checked = selected.has(row.key);
            return (
              <li key={row.key} className="px-4 py-3">
                <div className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleRow(row.key)}
                    aria-label={`Select ${row.symbol}`}
                    className="mt-1 size-3.5 shrink-0 accent-[var(--brand)]"
                  />
                  <div className="min-w-0 flex-1">
                    <SecurityCell row={row} />
                  </div>
                  <span className="shrink-0 text-right text-sm font-semibold">
                    <Cell row={row} column="marketValue" />
                  </span>
                </div>
                <button
                  type="button"
                  aria-expanded={open}
                  onClick={() => setExpanded(open ? null : row.key)}
                  className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
                >
                  {open ? "Fewer figures" : "All figures"}
                  <ChevronDown
                    aria-hidden="true"
                    className={cn("size-3 transition-transform duration-150", open && "rotate-180")}
                  />
                </button>
                {open ? (
                  <div className="mt-2" data-testid={`holding-card-detail-${row.symbol}`}>
                    <RowFigures row={row} columns={columnIds} />
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>

        {ordered.length === 0 ? (
          <p data-testid="holdings-filter-empty" className="px-4 py-6 text-sm text-muted-foreground">
            No holding in this portfolio matches that filter. Every holding is still here; only
            this view is narrowed.
          </p>
        ) : null}

        {footnotes.length > 0 ? (
          <div
            data-testid="holdings-footnote"
            className="space-y-1 border-t border-border px-4 py-3 text-xs text-muted-foreground"
          >
            <p className="font-medium text-foreground">
              Where a figure is missing above, this is why. None of them is a zero.
            </p>
            <ul className="list-disc space-y-0.5 pl-5">
              {footnotes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </Panel>
    </>
  );
}

/**
 * What the selected rows come to, and three things a reader can do with them.
 *
 * None of the three reaches a broker, and there is no fourth. The summary sums exactly and names
 * the rows it had to leave out rather than counting an unvalued holding as zero.
 */
function SelectionToolbar({
  rows,
  summary,
  onClear,
}: {
  rows: readonly HoldingRow[];
  summary: ReturnType<typeof summariseSelection>;
  onClear: () => void;
}) {
  const [copied, setCopied] = useState<"idle" | "done" | "unsupported">("idle");

  const copy = useCallback(() => {
    const text = rows.map((row) => row.symbol).join(", ");
    const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
    if (clipboard === undefined) {
      setCopied("unsupported");
      return;
    }
    clipboard.writeText(text).then(
      () => setCopied("done"),
      () => setCopied("unsupported"),
    );
  }, [rows]);

  return (
    <div
      role="group"
      aria-label="Selected holdings"
      data-testid="holdings-selection-toolbar"
      className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-border bg-muted/40 px-4 py-2.5"
    >
      <p className="text-xs font-semibold">
        <span className="tabular-nums">{summary.count}</span> selected
      </p>
      {(
        [
          { label: "Value", metric: summary.marketValue, kind: "rupees" as const, signed: false },
          { label: "Share", metric: summary.weight, kind: "percent" as const, signed: false },
          { label: "Today", metric: summary.todaysPnl, kind: "rupees" as const, signed: true },
          { label: "Unrealised", metric: summary.unrealisedPnl, kind: "rupees" as const, signed: true },
        ] satisfies { label: string; metric: Metric; kind: "rupees" | "percent"; signed: boolean }[]
      ).map((entry) => (
        <p key={entry.label} className="flex items-baseline gap-1.5 text-xs">
          <span className="text-muted-foreground">{entry.label}</span>
          <MetricValue
            metric={entry.metric}
            kind={entry.kind}
            signed={entry.signed}
            compact={entry.metric.value === null}
            short="nothing to add"
            className="font-semibold"
          />
        </p>
      ))}
      {summary.excluded > 0 ? (
        <p className="text-xs text-muted-foreground" data-testid="holdings-selection-excluded">
          <span className="tabular-nums">{summary.excluded}</span> of them could not be valued and
          are in none of those sums.
        </p>
      ) : null}
      <div className="ml-auto flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={copy} data-testid="holdings-copy-symbols">
          {copied === "done" ? "Symbols copied" : "Copy symbols"}
        </Button>
        <Button variant="ghost" size="sm" onClick={onClear} data-testid="holdings-clear-selection">
          <X aria-hidden="true" />
          Clear
        </Button>
      </div>
      {copied === "unsupported" ? (
        <p role="status" className="w-full text-xs text-muted-foreground">
          This browser would not let the page write to the clipboard. The symbols are{" "}
          {rows.map((row) => row.symbol).join(", ")}.
        </p>
      ) : null}
      <p className="w-full text-xs text-muted-foreground">
        Selecting rows adds them up. It places no order and sends nothing to a broker.
      </p>
    </div>
  );
}
