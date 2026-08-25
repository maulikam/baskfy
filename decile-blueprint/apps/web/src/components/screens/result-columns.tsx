"use client";

import type { ColumnDef, Table } from "@tanstack/react-table";

import {
  BumpinessDots,
  RankBadge,
  ReturnChip,
  ScoreBar,
  scoreBarScale,
} from "@/components/screens/cell-encodings";
import {
  columnDisplayLabel,
  columnDisplayTooltip,
  visibleResultColumns,
} from "@/lib/screens/column-display";
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
 * Display names and cell encodings follow the screen-page redesign brief (§1.3, §2.1–§2.2).
 */
export type ResultRow = Record<string, unknown> & { rank: number };

export interface ColumnMeta {
  key: string;
  label: string;
  unit: string;
}

const WIDTHS: Record<string, number> = {
  rank: 56,
  symbol: 200,
  name: 220,
  sorting_factor: 160,
  ret_12m: 112,
  vol_12m: 112,
  close_raw: 96,
  series: 74,
  marketcap_cr: 128,
};

const DEFAULT_WIDTH = 118;

function widthFor(key: string): number {
  return WIDTHS[key] ?? DEFAULT_WIDTH;
}

function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return EMPTY_CELL;
}

/**
 * A cell's text, from the row's own value for that column and nothing else.
 *
 * There is deliberately no `close_raw ?? close` here. House rule 6 makes `close` the adjusted
 * series and `close_raw` the exchange print, so filling a column headed "Price" from `close`
 * would put an adjusted number under a label that promises the print — a misstatement on a page
 * about money, and a silent one. A missing print stays an em dash; a column that is nothing but
 * em dashes is a column that should not have been rendered, which is `buildColumns`'s problem.
 */
function renderValue(value: unknown, unit: string): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  switch (unit) {
    case "percent":
      return formatPercent(value as number);
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

const SIGNED_UNITS = new Set(["percent"]);

function isNumeric(unit: string): boolean {
  return unit !== "text";
}

function headerLabel(
  key: string,
  meta: ReadonlyMap<string, ColumnMeta>,
  sortingFactorLabel: string,
): string {
  if (key === "sorting_factor") {
    return columnDisplayLabel("sorting_factor", "Score");
  }
  const entry = meta.get(key);
  const technical = entry?.label ?? key;
  return columnDisplayLabel(
    key,
    technical === sortingFactorLabel
      ? columnDisplayLabel("avg_sharpe_12_6_3_1", technical)
      : technical,
  );
}

/** The columns rendered as a score bar, and so the ones that need a scale. */
const SCORE_COLUMNS: ReadonlySet<string> = new Set([
  "sorting_factor",
  "avg_sharpe_12_6_3_1",
  "sharpe_12m",
]);

/**
 * One score scale per row model per column, rather than one per cell.
 *
 * The scale is a property of the whole result set — the largest score paints the full track — but
 * it is needed inside a renderer that runs once per visible row. TanStack rebuilds the row model
 * object only when the data or the sort changes, so keying a `WeakMap` on it makes the pass over
 * 271 rows happen once per such change instead of 271 times per scroll frame, and lets the entry
 * be collected with the model it describes.
 */
const SCORE_SCALES = new WeakMap<object, Map<string, number>>();

function scaleForColumn(table: Table<ResultRow>, columnId: string): number {
  const model = table.getRowModel();
  let byColumn = SCORE_SCALES.get(model);
  if (!byColumn) {
    byColumn = new Map<string, number>();
    SCORE_SCALES.set(model, byColumn);
  }
  const cached = byColumn.get(columnId);
  if (cached !== undefined) return cached;
  const scale = scoreBarScale(model.rows.map((row) => row.getValue(columnId)));
  byColumn.set(columnId, scale);
  return scale;
}

function renderEncodedCell(
  key: string,
  raw: unknown,
  unit: string,
  text: string,
  scoreScale: number | undefined,
): React.ReactNode {
  const numeric = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(numeric)) {
    return <span className="w-full text-right tabular-nums text-muted-foreground">{text}</span>;
  }

  if (SCORE_COLUMNS.has(key)) {
    return (
      <div className="w-full min-w-0">
        <ScoreBar value={numeric} scale={scoreScale} />
      </div>
    );
  }

  if (key === "ret_12m" || unit === "percent") {
    return (
      <span className="flex w-full justify-end">
        <ReturnChip text={text} value={numeric} />
      </span>
    );
  }

  if (key === "vol_12m" || (key.startsWith("vol_") && unit === "fraction")) {
    return (
      <span className="flex w-full items-center justify-end gap-2 tabular-nums">
        <BumpinessDots value={numeric} />
        <span className="text-xs text-muted-foreground">{text}</span>
      </span>
    );
  }

  const signed = SIGNED_UNITS.has(unit);
  return (
    <span
      className={cn(
        isNumeric(unit) && "w-full text-right tabular-nums",
        signed && numeric > 0 && "text-positive",
        signed && numeric < 0 && "text-negative",
      )}
    >
      {text}
    </span>
  );
}

/**
 * @param rows The rows the table is about to render. They decide nothing about *how* a cell looks
 *   — only which columns are worth a header. A column the API declares and then fills with NULL
 *   in every row (`close_raw` on the seeded screens, 271 of 271) renders a column of em dashes,
 *   and the dead-column policy in `lib/screens/column-display` is what drops it. Passing the rows
 *   is what lets that policy see them; the policy itself is not this file's to write.
 */
export function buildColumns(
  columns: readonly string[],
  meta: ReadonlyMap<string, ColumnMeta>,
  sortingFactorLabel: string,
  sortingFactorUnit: string,
  rows?: readonly ResultRow[],
): Array<ColumnDef<ResultRow, unknown>> {
  const filtered = visibleResultColumns(columns, rows);

  const rank: ColumnDef<ResultRow, unknown> = {
    id: "rank",
    header: "Rank",
    accessorKey: "rank",
    size: widthFor("rank"),
    enableSorting: false,
    cell: (info) => {
      const value = Number(info.getValue());
      return (
        <span className="flex w-full justify-end">
          <RankBadge rank={value} />
        </span>
      );
    },
  };

  const rest: Array<ColumnDef<ResultRow, unknown>> = [];

  for (const key of filtered) {
    if (key === "name") continue;

    if (key === "symbol") {
      rest.push({
        id: "symbol",
        header: "Stock",
        accessorFn: (row) => row.symbol,
        size: widthFor("symbol"),
        meta: { grow: true },
        cell: (info) => {
          const row = info.row.original;
          const symbol = asText(row.symbol);
          const name = asText(row.name);
          return (
            <div className="min-w-0">
              <div className="truncate font-medium">{symbol}</div>
              {name && name !== symbol ? (
                <div className="truncate text-xs text-muted-foreground">{name}</div>
              ) : null}
            </div>
          );
        },
      });
      continue;
    }

    const entry = meta.get(key);
    const label = headerLabel(key, meta, sortingFactorLabel);
    const unit = key === "sorting_factor" ? sortingFactorUnit : (entry?.unit ?? "ratio");
    const tooltip = columnDisplayTooltip(key);

    const column: ColumnDef<ResultRow, unknown> = {
      id: key,
      header: label,
      accessorKey: key,
      size: widthFor(key),
      cell: (info) => {
        const raw = info.getValue();
        const text = renderValue(raw, unit);
        const scoreScale = SCORE_COLUMNS.has(key)
          ? scaleForColumn(info.table, key)
          : undefined;
        return renderEncodedCell(key, raw, unit, text, scoreScale);
      },
    };
    if (tooltip) {
      column.meta = { headerTooltip: tooltip };
    }
    rest.push(column);
  }

  return [rank, ...rest];
}