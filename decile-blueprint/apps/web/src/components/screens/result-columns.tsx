"use client";

import type { ColumnDef } from "@tanstack/react-table";

import {
  EMPTY_CELL,
  formatCrore,
  formatFraction,
  formatNumber,
  formatPercent,
} from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Turns the API's `columns` array into TanStack column definitions.
 *
 * The response is deliberately dynamic — docs/07's `columns` is whatever the user saved — so the
 * table's shape is data, not code. What is *not* dynamic is how a value is rendered: that follows
 * the column's `unit` from `/meta/columns`, which comes from the factor registry, which docs/06
 * makes "the single source of truth for ... the column picker". One registry, one rendering rule
 * per unit, no per-column special cases to drift.
 *
 * docs/08 §"Design principles": "Numbers right-aligned, always."
 */
export type ResultRow = Record<string, unknown> & { rank: number };

export interface ColumnMeta {
  key: string;
  label: string;
  unit: string;
}

/** Widths tuned to the reference product's own columns; everything else gets a sensible default. */
const WIDTHS: Record<string, number> = {
  rank: 56,
  symbol: 110,
  name: 220,
  sorting_factor: 140,
  series: 74,
  marketcap_cr: 128,
};

const DEFAULT_WIDTH = 118;

/** `noUncheckedIndexedAccess` makes a record lookup `T | undefined`; the fallback is the point. */
function widthFor(key: string): number {
  return WIDTHS[key] ?? DEFAULT_WIDTH;
}

/** A cell that is not a number and not a string is a contract violation, not something to print. */
function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return EMPTY_CELL;
}

function renderValue(value: unknown, unit: string): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  switch (unit) {
    case "percent":
      return formatPercent(value as number);
    // docs/13 §2 finding 4: stored as a decimal fraction, shown as a percentage.
    case "fraction":
      return formatFraction(value as number);
    case "crore":
      return formatCrore(value as number);
    case "rupees":
      return formatNumber(value as number, { decimals: 0 });
    case "count":
      return formatNumber(value as number, { decimals: 0 });
    case "price":
    case "ratio":
      return formatNumber(value as number, { decimals: 2 });
    case "index":
      return formatNumber(value as number, { decimals: 4 });
    default:
      return asText(value);
  }
}

/** Signed units get the positive/negative colour — and the sign, which carries it in greyscale. */
const SIGNED_UNITS = new Set(["percent"]);

function isNumeric(unit: string): boolean {
  return unit !== "text";
}

export function buildColumns(
  columns: readonly string[],
  meta: ReadonlyMap<string, ColumnMeta>,
  sortingFactorLabel: string,
  sortingFactorUnit: string,
): Array<ColumnDef<ResultRow, unknown>> {
  const rank: ColumnDef<ResultRow, unknown> = {
    id: "rank",
    header: "#",
    accessorKey: "rank",
    size: widthFor("rank"),
    cell: (info) => (
      <span className="w-full text-right tnum text-muted-foreground">{String(info.getValue())}</span>
    ),
  };

  const rest = columns.map<ColumnDef<ResultRow, unknown>>((key) => {
    if (key === "symbol") {
      return {
        id: key,
        header: "Symbol",
        accessorKey: key,
        size: widthFor("symbol"),
        cell: (info) => <span className="font-medium">{asText(info.getValue())}</span>,
      };
    }
    if (key === "name") {
      return { id: key, header: "Name", accessorKey: key, size: widthFor("name") };
    }

    const entry = meta.get(key);
    const label = key === "sorting_factor" ? "Sorting Factor" : (entry?.label ?? key);
    const unit = key === "sorting_factor" ? sortingFactorUnit : (entry?.unit ?? "ratio");

    return {
      id: key,
      header: label,
      accessorKey: key,
      size: widthFor(key),
      cell: (info) => {
        const raw = info.getValue();
        const text = renderValue(raw, unit);
        const numeric = typeof raw === "number" ? raw : Number(raw);
        const signed = SIGNED_UNITS.has(unit) && Number.isFinite(numeric);
        return (
          <span
            title={key === "sorting_factor" ? sortingFactorLabel : undefined}
            className={cn(
              isNumeric(unit) && "w-full text-right tnum",
              signed && numeric > 0 && "text-positive",
              signed && numeric < 0 && "text-negative",
            )}
          >
            {text}
          </span>
        );
      },
    };
  });

  return [rank, ...rest];
}
