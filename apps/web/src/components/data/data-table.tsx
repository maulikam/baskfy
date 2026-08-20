"use client";

import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type Row,
  type SortingState,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/**
 * The results grid — docs/08 §"Results panel":
 *
 *     "TanStack Table + TanStack Virtual; **sticky header**; repeat the header row every 16 rows
 *      for long scrolls (a genuinely good idea borrowed from the reference product — make it
 *      optional). Client-side re-sort on any visible column (does not re-run the screen; label it
 *      clearly)."
 *
 * and docs/08 §"Accessibility & quality bar": "the table supports arrow-key navigation."
 *
 * Why a `role="grid"` of divs and not a `<table>`
 * ----------------------------------------------
 * Virtualisation means only ~30 of 4,000 rows exist in the DOM, positioned by transform. A real
 * `<table>` cannot be positioned that way without breaking its own layout algorithm, and the
 * usual workaround (absolutely positioned `<tr>`) throws away the row/column semantics anyway.
 * The ARIA grid pattern keeps them explicitly: `aria-rowcount` states the true total, each row
 * carries its true `aria-rowindex`, and a roving tabindex gives one tab stop for the whole grid
 * with arrow keys inside it. That is what assistive technology expects from a data grid, and it
 * is what lets 4,000 rows scroll at 60fps (Prompt 8's second acceptance criterion).
 *
 * The repeated header rows are `aria-hidden`: they are a visual affordance for long scrolls, and
 * announcing the column names again every sixteen rows would be actively worse.
 */

export type Density = "comfortable" | "compact";

/** docs/08 §"Design principles": "default row height 34px with a comfortable/compact toggle." */
export const ROW_HEIGHT: Record<Density, number> = { comfortable: 34, compact: 28 };

/** docs/08: "repeat the header row every 16 rows". */
export const DEFAULT_REPEAT_HEADER_EVERY = 16;

/** Rows rendered above and below the viewport, to keep fast scrolls from showing blank space. */
const OVERSCAN = 8;

export interface DataTableProps<TRow> {
  data: readonly TRow[];
  columns: ReadonlyArray<ColumnDef<TRow, unknown>>;
  /** Accessible name for the grid. Required — an unnamed grid is unnavigable. */
  label: string;
  density?: Density;
  /** 0 disables the repeat. docs/08 asks for it to be optional. */
  repeatHeaderEvery?: number;
  loading?: boolean;
  /** How many skeleton rows to reserve while loading, so the swap shifts nothing. */
  loadingRows?: number;
  height?: number;
  onRowActivate?: ((row: TRow) => void) | undefined;
  className?: string | undefined;
}

type DisplayRow = { kind: "repeat-header" } | { kind: "data"; index: number };

function buildDisplayRows(rowCount: number, repeatEvery: number): DisplayRow[] {
  if (repeatEvery <= 0) {
    return Array.from({ length: rowCount }, (_, index) => ({ kind: "data", index }));
  }
  const out: DisplayRow[] = [];
  for (let index = 0; index < rowCount; index += 1) {
    if (index > 0 && index % repeatEvery === 0) out.push({ kind: "repeat-header" });
    out.push({ kind: "data", index });
  }
  return out;
}

/**
 * One virtualised row, memoised.
 *
 * Without the memo, a scroll frame re-renders every rendered row and every cell in it — around
 * 360 `flexRender` calls at this column count — even though only the two or three rows entering
 * and leaving the window have changed. Memoising on the values that actually vary (the row, its
 * offset, and whether it holds the focused cell) turns each frame into work proportional to what
 * moved, which is what keeps the 4,000-row scroll inside the frame budget.
 */
interface TableRowProps<TRow> {
  row: Row<TRow>;
  index: number;
  offset: number;
  height: number;
  /** The focused column index when this row holds the focused cell, otherwise `null`. */
  focusedColumn: number | null;
  clickable: boolean;
}

function TableRowInner<TRow>({
  row,
  index,
  offset,
  height,
  focusedColumn,
  clickable,
}: TableRowProps<TRow>) {
  return (
    <div
      role="row"
      aria-rowindex={index + 2}
      data-row-index={index}
      className={cn(
        "absolute left-0 top-0 flex w-full border-b border-border/60 text-sm hover:bg-muted/50",
        clickable && "cursor-pointer",
      )}
      style={{ height, transform: `translateY(${offset}px)` }}
    >
      {row.getVisibleCells().map((cell, columnIndex) => {
        const focused = columnIndex === focusedColumn;
        return (
          <div
            key={cell.id}
            role="gridcell"
            aria-colindex={columnIndex + 1}
            tabIndex={focused ? 0 : -1}
            ref={
              focused
                ? (node) => {
                    // Move focus with the roving tabindex, but only while focus is already inside
                    // the grid — otherwise scrolling would steal it from wherever the user is.
                    if (node && document.activeElement !== node) {
                      const grid = node.closest('[role="grid"]');
                      if (grid?.contains(document.activeElement)) node.focus();
                    }
                  }
                : undefined
            }
            style={{ width: cell.column.getSize() }}
            className={cn(
              "flex shrink-0 items-center overflow-hidden truncate px-2",
              focused && "ring-2 ring-ring ring-inset",
            )}
          >
            {flexRender(cell.column.columnDef.cell, cell.getContext())}
          </div>
        );
      })}
    </div>
  );
}

const TableRow = memo(TableRowInner) as typeof TableRowInner;

export function DataTable<TRow>({
  data,
  columns,
  label,
  density = "comfortable",
  repeatHeaderEvery = DEFAULT_REPEAT_HEADER_EVERY,
  loading = false,
  loadingRows = 12,
  height = 560,
  onRowActivate,
  className,
}: DataTableProps<TRow>) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [focus, setFocus] = useState<{ row: number; column: number }>({ row: 0, column: 0 });
  const scrollRef = useRef<HTMLDivElement>(null);

  const table = useReactTable({
    data: data as TRow[],
    columns: columns as Array<ColumnDef<TRow, unknown>>,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const rows = table.getRowModel().rows;
  const rowHeight = ROW_HEIGHT[density];
  const displayRows = useMemo(
    () => buildDisplayRows(rows.length, repeatHeaderEvery),
    [rows.length, repeatHeaderEvery],
  );

  const virtualizer = useVirtualizer({
    count: displayRows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeight,
    overscan: OVERSCAN,
  });

  const columnCount = table.getAllLeafColumns().length;

  /** Keep the focused cell inside the rendered window; a virtualised row cannot take focus. */
  const moveFocus = useCallback(
    (nextRow: number, nextColumn: number) => {
      const row = Math.max(0, Math.min(rows.length - 1, nextRow));
      const column = Math.max(0, Math.min(columnCount - 1, nextColumn));
      setFocus({ row, column });
      const displayIndex = displayRows.findIndex(
        (entry) => entry.kind === "data" && entry.index === row,
      );
      if (displayIndex >= 0) virtualizer.scrollToIndex(displayIndex, { align: "auto" });
    },
    [columnCount, displayRows, rows.length, virtualizer],
  );

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      const pageSize = Math.max(1, Math.floor(height / rowHeight) - 1);
      const handlers: Record<string, () => void> = {
        ArrowDown: () => moveFocus(focus.row + 1, focus.column),
        ArrowUp: () => moveFocus(focus.row - 1, focus.column),
        ArrowRight: () => moveFocus(focus.row, focus.column + 1),
        ArrowLeft: () => moveFocus(focus.row, focus.column - 1),
        PageDown: () => moveFocus(focus.row + pageSize, focus.column),
        PageUp: () => moveFocus(focus.row - pageSize, focus.column),
        Home: () => moveFocus(focus.row, 0),
        End: () => moveFocus(focus.row, columnCount - 1),
      };
      const handler = handlers[event.key];
      if (handler) {
        event.preventDefault();
        handler();
        return;
      }
      if (event.key === "Enter" && onRowActivate) {
        const row = rows[focus.row];
        if (row) {
          event.preventDefault();
          onRowActivate(row.original);
        }
      }
    },
    [columnCount, focus, height, moveFocus, onRowActivate, rowHeight, rows],
  );

  /**
   * Mouse activation is delegated from the grid rather than bound per row.
   *
   * With 4,000 rows and virtualisation that is 1 listener instead of 30 re-created on every
   * scroll frame — but the reason it is written this way is accessibility, not performance: a
   * `role="row"` with its own click handler is an interactive element that must also be focusable
   * and key-operable. Activation belongs to the grid, which is focusable, and which already
   * handles Enter through `onKeyDown`.
   */
  const onClick = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      if (!onRowActivate) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const rowElement = target.closest<HTMLElement>("[data-row-index]");
      const index = Number(rowElement?.dataset.rowIndex);
      const row = Number.isInteger(index) ? rows[index] : undefined;
      if (row) onRowActivate(row.original);
    },
    [onRowActivate, rows],
  );

  /** After a sort, the row under the focused index is a different row; go back to the top. */
  useEffect(() => {
    setFocus((current) => ({ row: 0, column: current.column }));
  }, [sorting]);

  /**
   * The repeated header (docs/08: "repeat the header row every 16 rows for long scrolls") is a
   * *picture* of the header, not another one.
   *
   * It carries no buttons and no roles. `aria-hidden` on a container that holds focusable sort
   * buttons is an actual defect — axe's `aria-hidden-focus` — because a keyboard user tabs into a
   * control the screen reader has been told does not exist. And a second `role="row"` with
   * `role="columnheader"` cells would tell assistive technology the table has 250 header rows.
   */
  const repeatHeaderRow = (
    <div aria-hidden="true" className="flex w-full">
      {table.getHeaderGroups()[0]?.headers.map((header) => (
        <div
          key={`repeat-${header.id}`}
          style={{ width: header.getSize() }}
          className="flex shrink-0 items-center border-b border-border bg-muted/60 px-2 py-1.5 text-xs font-medium text-muted-foreground"
        >
          <span className="truncate">
            {flexRender(header.column.columnDef.header, header.getContext())}
          </span>
        </div>
      ))}
    </div>
  );

  const headerRow = (
    <div role="row" className="flex w-full" aria-rowindex={1}>
      {table.getHeaderGroups()[0]?.headers.map((header) => {
        const sorted = header.column.getIsSorted();
        const sortable = header.column.getCanSort();
        return (
          <div
            key={header.id}
            role="columnheader"
            aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none"}
            style={{ width: header.getSize() }}
            className="shrink-0 border-b border-border bg-muted/60 px-2 text-xs font-medium text-muted-foreground"
          >
            <button
              type="button"
              disabled={!sortable}
              onClick={header.column.getToggleSortingHandler()}
              className={cn(
                "flex h-full w-full items-center gap-1 py-1.5 text-left",
                sortable ? "hover:text-foreground" : "cursor-default",
              )}
            >
              <span className="truncate">
                {flexRender(header.column.columnDef.header, header.getContext())}
              </span>
              {sortable ? (
                <span aria-hidden="true" className="shrink-0">
                  {sorted === "asc" ? (
                    <ArrowUp className="size-3" />
                  ) : sorted === "desc" ? (
                    <ArrowDown className="size-3" />
                  ) : (
                    <ChevronsUpDown className="size-3 opacity-40" />
                  )}
                </span>
              ) : null}
            </button>
          </div>
        );
      })}
    </div>
  );

  if (loading) {
    return (
      /*
       * The structure mirrors the loaded branch exactly — outer bordered box, inner element
       * carrying `height` — so the two occupy the same rectangle to the pixel. Putting `height`
       * on the outer div instead would make the skeleton two pixels shorter than the table (the
       * border), which is a real, measurable shift on every load.
       */
      <div
        data-slot="data-table"
        data-loading="true"
        className={cn("overflow-hidden rounded-md border border-border bg-card", className)}
      >
        <div style={{ height }} className="overflow-hidden">
          {headerRow}
          <div className="divide-y divide-border">
            {Array.from({ length: loadingRows }, (_, index) => (
              <div key={index} className="flex items-center px-2" style={{ height: rowHeight }}>
                <Skeleton className="h-3.5 w-full" />
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      data-slot="data-table"
      data-loading="false"
      className={cn("overflow-hidden rounded-md border border-border bg-card", className)}
    >
      <div
        ref={scrollRef}
        role="grid"
        aria-label={label}
        aria-rowcount={rows.length + 1}
        aria-colcount={columnCount}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onClick={onClick}
        style={{ height }}
        className="relative overflow-auto outline-hidden"
      >
        <div className="sticky top-0 z-10">{headerRow}</div>

        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const entry = displayRows[virtualRow.index];
            if (!entry) return null;

            if (entry.kind === "repeat-header") {
              return (
                <div
                  key={`repeat-${virtualRow.index}`}
                  className="absolute left-0 top-0 w-full"
                  style={{ height: rowHeight, transform: `translateY(${virtualRow.start}px)` }}
                >
                  {repeatHeaderRow}
                </div>
              );
            }

            const row = rows[entry.index];
            if (!row) return null;

            return (
              <TableRow
                key={row.id}
                row={row}
                index={entry.index}
                offset={virtualRow.start}
                height={rowHeight}
                focusedColumn={entry.index === focus.row ? focus.column : null}
                clickable={onRowActivate !== undefined}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}
