"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { LayoutGrid, Rows3, Search } from "lucide-react";
import { parseAsString, parseAsStringLiteral, useQueryState } from "nuqs";
import { useDeferredValue, useMemo } from "react";

import type { IndexRowOut } from "@decile/api-client";

import { DataTable } from "@/components/data/data-table";
import { Sparkline } from "@/components/data/sparkline";
import { Input } from "@/components/ui/input";
import { EMPTY_CELL, formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * docs/01 §7's indices dashboard, in the shape docs/08 §Dashboard asks for:
 *
 *     "Virtualised card grid or dense table (user toggle), sortable by %chg / PE / PB / DivYield,
 *      search box, sector grouping, and a sparkline per index (30-day). Handle `-` for indices
 *      with no fundamentals."
 *
 * **Everything here is client-side.** The rows arrive once from the server component and are
 * never refetched: sorting, searching and the view toggle are all in-memory over the same array.
 * That is Prompt 11's second acceptance criterion, and it is also the only way a 145-row table
 * can re-sort inside a frame — a round trip per header click would be perceptible and pointless,
 * since the server has nothing to add.
 *
 * Sector grouping is the one thing docs/08 asks for that is missing: there is no sector column in
 * docs/04, on `index_def` or anywhere else, so there is nothing to group by. `docs/11a` §2.
 */
export interface IndexDashboardProps {
  rows: readonly IndexRowOut[];
  asOf: string;
}

const VIEWS = ["table", "cards"] as const;

const TABLE_HEIGHT = 560;

function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const numeric = typeof value === "string" ? Number(value) : value;
  return Number.isNaN(numeric) ? null : numeric;
}

/** Sorts nulls last in both directions: "no P/E" is not "the lowest P/E". */
function nullsLast(a: unknown, b: unknown): number {
  const left = toNumber(a as string | number | null);
  const right = toNumber(b as string | number | null);
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return left - right;
}

function level(value: string | number | null | undefined): string {
  return formatNumber(value, { decimals: 2 });
}

function ratio(value: string | number | null | undefined): string {
  return formatNumber(value, { decimals: 2 });
}

function sparklineValues(row: IndexRowOut): number[] {
  // `sparkline` has a server-side default, so it is optional on the wire; an index registered
  // today has no history and `Sparkline` draws its "not enough history" rule for it.
  return (row.sparkline ?? [])
    .map((value) => Number(value))
    .filter((value) => !Number.isNaN(value));
}

function matches(row: IndexRowOut, needle: string): boolean {
  if (!needle) return true;
  const query = needle.trim().toLowerCase();
  return row.name.toLowerCase().includes(query) || row.slug.includes(query);
}

function Change({ value }: { value: string | number | null | undefined }) {
  const numeric = toNumber(value);
  return (
    <span
      className={cn(
        "tnum",
        numeric !== null && numeric > 0 && "text-positive",
        numeric !== null && numeric < 0 && "text-negative",
      )}
    >
      {numeric === null ? EMPTY_CELL : formatPercent(numeric)}
    </span>
  );
}

function useColumns(): ColumnDef<IndexRowOut, unknown>[] {
  return useMemo(
    () => [
      {
        id: "name",
        accessorFn: (row) => row.name,
        header: "Index",
        size: 260,
        cell: ({ row }) => <span className="truncate font-medium">{row.original.name}</span>,
      },
      {
        id: "sparkline",
        header: "30 days",
        size: 110,
        enableSorting: false,
        cell: ({ row }) => (
          <Sparkline
            values={sparklineValues(row.original)}
            label={`${row.original.name} 30-day level`}
          />
        ),
      },
      {
        id: "change_pct",
        accessorFn: (row) => row.change_pct,
        header: "% Chg",
        size: 96,
        sortingFn: (a, b, id) => nullsLast(a.getValue(id), b.getValue(id)),
        cell: ({ row }) => <Change value={row.original.change_pct} />,
      },
      {
        id: "level",
        accessorFn: (row) => row.level,
        header: "Level",
        size: 118,
        sortingFn: (a, b, id) => nullsLast(a.getValue(id), b.getValue(id)),
        cell: ({ row }) => <span className="tnum">{level(row.original.level)}</span>,
      },
      {
        id: "change_abs",
        accessorFn: (row) => row.change_abs,
        header: "Chg",
        size: 100,
        sortingFn: (a, b, id) => nullsLast(a.getValue(id), b.getValue(id)),
        cell: ({ row }) => <span className="tnum">{level(row.original.change_abs)}</span>,
      },
      {
        id: "pe",
        accessorFn: (row) => row.pe,
        header: "PE",
        size: 84,
        sortingFn: (a, b, id) => nullsLast(a.getValue(id), b.getValue(id)),
        cell: ({ row }) => <span className="tnum">{ratio(row.original.pe)}</span>,
      },
      {
        id: "pb",
        accessorFn: (row) => row.pb,
        header: "PB",
        size: 84,
        sortingFn: (a, b, id) => nullsLast(a.getValue(id), b.getValue(id)),
        cell: ({ row }) => <span className="tnum">{ratio(row.original.pb)}</span>,
      },
      {
        id: "div_yield",
        accessorFn: (row) => row.div_yield,
        header: "Div Yield",
        size: 104,
        sortingFn: (a, b, id) => nullsLast(a.getValue(id), b.getValue(id)),
        cell: ({ row }) => <span className="tnum">{ratio(row.original.div_yield)}</span>,
      },
    ],
    [],
  );
}

function IndexCard({ row }: { row: IndexRowOut }) {
  return (
    <li className="flex flex-col gap-2 rounded-md border border-border bg-card p-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium leading-tight">{row.name}</p>
        <Sparkline values={sparklineValues(row)} label={`${row.name} 30-day level`} />
      </div>
      <div className="flex items-baseline gap-2">
        <p className="text-lg font-semibold tnum">{level(row.level)}</p>
        <Change value={row.change_pct} />
      </div>
      <dl className="flex gap-4 text-xs text-muted-foreground">
        <div className="flex gap-1">
          <dt>PE</dt>
          <dd className="tnum text-foreground">{ratio(row.pe)}</dd>
        </div>
        <div className="flex gap-1">
          <dt>PB</dt>
          <dd className="tnum text-foreground">{ratio(row.pb)}</dd>
        </div>
        <div className="flex gap-1">
          <dt>Div</dt>
          <dd className="tnum text-foreground">{ratio(row.div_yield)}</dd>
        </div>
      </dl>
    </li>
  );
}

export function IndexDashboard({ rows, asOf }: IndexDashboardProps) {
  const [view, setView] = useQueryState(
    "view",
    parseAsStringLiteral(VIEWS).withDefault("table").withOptions({ history: "replace" }),
  );
  const [query, setQuery] = useQueryState(
    "q",
    parseAsString.withDefault("").withOptions({ history: "replace", throttleMs: 200 }),
  );
  /* The list re-filters on a deferred copy of the query, so typing stays responsive at 145 rows
     even when the render behind it is the expensive part. */
  const deferred = useDeferredValue(query);
  const visible = useMemo(() => rows.filter((row) => matches(row, deferred)), [rows, deferred]);
  const columns = useColumns();

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full max-w-xs">
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            type="search"
            value={query}
            onChange={(event) => void setQuery(event.target.value)}
            placeholder="Search indices"
            aria-label="Search indices"
            className="pl-8"
          />
        </div>

        <div className="flex items-center gap-2">
          <p aria-live="polite" className="text-xs text-muted-foreground tnum">
            {visible.length} of {rows.length} indices · {asOf}
          </p>
          <div role="group" aria-label="View" className="flex rounded-md border border-border p-0.5">
            <button
              type="button"
              aria-pressed={view === "table"}
              onClick={() => void setView("table")}
              className={cn(
                "flex items-center gap-1.5 rounded-sm px-2 py-1 text-xs",
                view === "table" ? "bg-muted font-medium" : "text-muted-foreground",
              )}
            >
              <Rows3 aria-hidden="true" className="size-3.5" />
              Table
            </button>
            <button
              type="button"
              aria-pressed={view === "cards"}
              onClick={() => void setView("cards")}
              className={cn(
                "flex items-center gap-1.5 rounded-sm px-2 py-1 text-xs",
                view === "cards" ? "bg-muted font-medium" : "text-muted-foreground",
              )}
            >
              <LayoutGrid aria-hidden="true" className="size-3.5" />
              Cards
            </button>
          </div>
        </div>
      </div>

      {view === "table" ? (
        <DataTable
          data={visible}
          columns={columns}
          label="Indices, sortable by any column"
          height={TABLE_HEIGHT}
          repeatHeaderEvery={0}
        />
      ) : (
        <ul
          aria-label="Indices"
          className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
        >
          {visible.map((row) => (
            <IndexCard key={row.slug} row={row} />
          ))}
        </ul>
      )}

      {visible.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">
          No index matches “{query}”.
        </p>
      ) : null}
    </div>
  );
}
