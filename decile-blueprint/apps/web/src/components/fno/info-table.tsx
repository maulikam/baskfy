"use client";

import { useMemo, useState } from "react";

import type { FnoInfoRow } from "@/lib/fno/types";
import { percent, price, ratio, signed, volPct } from "@/lib/fno/view";
import { cn } from "@/lib/utils";

/**
 * `docs/fno/04` §5's Stock F&O table (`05` §3). **No column is named or coloured as a signal**: no
 * green or red anywhere, no candidate column, no rank; the default order is futures turnover (the
 * liquidity the pack filters on), not any number below. Sorting is the reader's, by any column.
 *
 * Phone (`05` §3): the table collapses to symbol · IV ÷ RV · OI chg · ban, and a tap opens the row.
 */

type Key =
  | "turnover"
  | "symbol"
  | "lot_size"
  | "days"
  | "settle"
  | "basis"
  | "oi"
  | "iv"
  | "rv"
  | "ivrv"
  | "ivpct"
  | "ban";

type BanFilter = "all" | "banned" | "not_banned";

const COLUMNS: readonly { key: Key; label: string; numeric: boolean }[] = [
  { key: "symbol", label: "Symbol", numeric: false },
  { key: "lot_size", label: "Lot", numeric: true },
  { key: "days", label: "Days to monthly", numeric: true },
  { key: "settle", label: "Futures settle", numeric: true },
  { key: "basis", label: "Basis (a.y.)", numeric: true },
  { key: "oi", label: "OI chg (5 sessions)", numeric: true },
  { key: "iv", label: "IV", numeric: true },
  { key: "rv", label: "RV20", numeric: true },
  { key: "ivrv", label: "IV ÷ RV20", numeric: true },
  { key: "ivpct", label: "1-year IV percentile", numeric: true },
  { key: "ban", label: "Ban", numeric: false },
];

function num(value: string | number | null): number | null {
  if (value === null) return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function sortValue(row: FnoInfoRow, key: Key): number | string | null {
  switch (key) {
    case "turnover":
      return num(row.fut_turnover_20d);
    case "symbol":
      return row.symbol;
    case "lot_size":
      return row.lot_size;
    case "days":
      return row.days_to_near_monthly;
    case "settle":
      return num(row.fut_settle);
    case "basis":
      return num(row.basis_ann);
    case "oi":
      return num(row.oi_change_5d_pct);
    case "iv":
      return num(row.iv);
    case "rv":
      return num(row.rv20);
    case "ivrv":
      return num(row.iv_rv);
    case "ivpct":
      return num(row.iv_pct_1y);
    case "ban":
      return row.in_ban ? 1 : 0;
  }
}

/** Sort a copy; nulls last whatever the direction. */
export function sortRows(
  rows: readonly FnoInfoRow[],
  key: Key,
  descending: boolean,
): FnoInfoRow[] {
  return [...rows].sort((a, b) => {
    const x = sortValue(a, key);
    const y = sortValue(b, key);
    if (x === null && y === null) return a.symbol.localeCompare(b.symbol);
    if (x === null) return 1;
    if (y === null) return -1;
    const cmp =
      typeof x === "string" ? x.localeCompare(String(y)) : x - Number(y);
    return descending ? -cmp : cmp;
  });
}

export function filterRows(
  rows: readonly FnoInfoRow[],
  ban: BanFilter,
): FnoInfoRow[] {
  if (ban === "banned") return rows.filter((r) => r.in_ban);
  if (ban === "not_banned") return rows.filter((r) => !r.in_ban);
  return [...rows];
}

function cell(row: FnoInfoRow, key: Key): string {
  switch (key) {
    case "turnover":
      return price(row.fut_turnover_20d);
    case "symbol":
      return row.symbol;
    case "lot_size":
      return row.lot_size === null ? "—" : row.lot_size.toLocaleString("en-IN");
    case "days":
      return row.days_to_near_monthly === null
        ? "—"
        : String(row.days_to_near_monthly);
    case "settle":
      return price(row.fut_settle);
    case "basis":
      return volPct(row.basis_ann);
    case "oi":
      return row.oi_change_5d_pct === null
        ? "—"
        : `${signed(row.oi_change_5d_pct)}%`;
    case "iv":
      return volPct(row.iv);
    case "rv":
      return volPct(row.rv20);
    case "ivrv":
      return ratio(row.iv_rv);
    case "ivpct":
      return row.iv_pct_1y === null
        ? `— (${row.iv_sessions_1y} sessions)`
        : percent(row.iv_pct_1y, 0);
    case "ban":
      return row.in_ban ? "In ban" : "";
  }
}

export function InfoTable({ rows }: { rows: readonly FnoInfoRow[] }) {
  const [sort, setSort] = useState<{ key: Key; descending: boolean }>({
    key: "turnover",
    descending: true,
  });
  const [ban, setBan] = useState<BanFilter>("all");
  const shown = useMemo(
    () => sortRows(filterRows(rows, ban), sort.key, sort.descending),
    [rows, ban, sort],
  );

  function toggle(key: Key) {
    setSort((current) =>
      current.key === key
        ? { key, descending: !current.descending }
        : { key, descending: key !== "symbol" },
    );
  }

  return (
    <div className="space-y-3" data-testid="fno-info">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2">
          <span className="text-muted-foreground">Ban list</span>
          <select
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={ban}
            onChange={(event) => setBan(event.target.value as BanFilter)}
            data-testid="fno-ban-filter"
          >
            <option value="all">All underlyings</option>
            <option value="banned">In the ban period only</option>
            <option value="not_banned">Outside the ban period</option>
          </select>
        </label>
        <span className="text-xs text-muted-foreground">
          {shown.length} of {rows.length} · sorted by{" "}
          {sort.key === "turnover"
            ? "futures turnover"
            : COLUMNS.find((c) => c.key === sort.key)?.label}
        </span>
      </div>

      {/* sm and up: the whole table. */}
      <div className="hidden overflow-x-auto sm:block">
        <table
          className="w-full min-w-[56rem] text-sm"
          data-testid="fno-info-table"
        >
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              {COLUMNS.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  className={cn(
                    "py-2 font-normal",
                    column.numeric && "text-right",
                  )}
                  aria-sort={
                    sort.key === column.key
                      ? sort.descending
                        ? "descending"
                        : "ascending"
                      : "none"
                  }
                >
                  <button
                    type="button"
                    className="hover:text-foreground"
                    onClick={() => toggle(column.key)}
                  >
                    {column.label}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row) => (
              <tr
                key={row.symbol}
                className="border-b border-border/50"
                data-symbol={row.symbol}
              >
                {COLUMNS.map((column) => (
                  <td
                    key={column.key}
                    className={cn(
                      "py-1.5 tabular-nums",
                      column.numeric && "text-right",
                    )}
                  >
                    {cell(row, column.key)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Phone: symbol · IV ÷ RV · OI chg · ban; a tap opens the row. */}
      <div
        aria-hidden="true"
        className="grid grid-cols-[1fr_auto_auto_auto] gap-3 text-xs text-muted-foreground sm:hidden"
      >
        <span>Symbol</span>
        <span>IV ÷ RV</span>
        <span>OI chg</span>
        <span>Ban</span>
      </div>
      <ul
        className="divide-y divide-border/60 sm:hidden"
        data-testid="fno-info-phone"
      >
        {shown.map((row) => (
          <li key={row.symbol}>
            <details>
              <summary className="grid cursor-pointer grid-cols-[1fr_auto_auto_auto] gap-3 py-2 text-sm tabular-nums">
                <span className="font-medium">{row.symbol}</span>
                <span>{cell(row, "ivrv")}</span>
                <span>{cell(row, "oi")}</span>
                <span className="text-xs text-muted-foreground">
                  {cell(row, "ban")}
                </span>
              </summary>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 pb-3 text-xs">
                {COLUMNS.filter((c) => c.key !== "symbol").map((column) => (
                  <div key={column.key} className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">{column.label}</dt>
                    <dd className="tabular-nums">
                      {cell(row, column.key) || "no"}
                    </dd>
                  </div>
                ))}
              </dl>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}
