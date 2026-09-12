"use client";

import type { Route } from "next";
import { Fragment, useMemo, useState } from "react";
import { ChevronRight, CircleAlert, Info } from "lucide-react";
import Link from "next/link";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { AllocationSlice } from "@/lib/portfolio/analytics";
import { NOT_YET_MEASURED, percentOf, type CommandMode } from "@/lib/portfolio/command-center";
import type { PortfolioRow } from "@/lib/portfolio/overview";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * One sortable table in place of the repeating blocks the old screen drew.
 *
 * Brief: *"Replace the existing repetitive portfolio blocks with one powerful sortable table."*
 * The old page rendered a card per portfolio with the same six labels each time, which is a lot
 * of ink to compare four numbers. A table compares by construction: a column is a comparison.
 *
 * WHICH COLUMNS EXIST, AND WHY SOME DO NOT
 * ----------------------------------------
 * The brief lists eighteen. Eleven are real and are here. Seven — benchmark-relative return per
 * portfolio, drawdown per portfolio, volatility, risk contribution, concentration status,
 * rebalance drift — need per-portfolio return statistics or a stored target allocation, and
 * Baskfy has neither. They are **not** rendered as empty columns: a column of dashes is worse
 * than no column, because it implies the number exists and is merely missing today. What the
 * table does instead is say so once, underneath, with what each would take. The full survey is
 * `docs/PORTFOLIO-COMMAND-CENTER.md` §2.2.
 *
 * NEVER COLOUR ALONE
 * ------------------
 * The colour chip that identifies a portfolio is paired with its name in every place it appears,
 * and it also carries a stable letter, so the identifier survives greyscale and colour blindness.
 * Direction in the P&L column is a glyph as well as a hue.
 */

type SortKey = "name" | "value" | "share" | "today" | "holdings";

/** Shades of the one accent rather than a rainbow — an ordering, not a set of categories. */
const SHADES = [
  "bg-accent",
  "bg-accent/75",
  "bg-accent/55",
  "bg-accent/40",
  "bg-accent/25",
] as const;

const NO_FIGURE_REASON = "No closing price on record for this portfolio's holdings.";

function shade(index: number): string {
  return SHADES[index % SHADES.length] ?? SHADES[0];
}

/** The non-colour half of the identifier. Same input, same letter, every render. */
function initialOf(name: string): string {
  return name.trim().charAt(0).toUpperCase() || "•";
}

function compare(a: AllocationSlice, b: AllocationSlice, key: SortKey, dir: 1 | -1): number {
  const num = (v: string | null) => (v === null ? Number.NEGATIVE_INFINITY : Number(v));
  switch (key) {
    case "name":
      return dir * a.name.localeCompare(b.name);
    case "share":
      return dir * (num(a.weightPct) - num(b.weightPct));
    case "today":
      return dir * (num(a.todaysPnl) - num(b.todaysPnl));
    case "holdings":
      return dir * (a.holdingsCount - b.holdingsCount);
    default:
      return dir * (num(a.value) - num(b.value));
  }
}

function Money({ value, signed = false }: { value: string | null; signed?: boolean }) {
  if (value === null) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="cursor-help text-muted-foreground">Needs more data</span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs text-xs">{NO_FIGURE_REASON}</TooltipContent>
      </Tooltip>
    );
  }
  const n = Number(value);
  const tone = !signed || !Number.isFinite(n) || n === 0 ? "flat" : n > 0 ? "up" : "down";
  return (
    <span
      className={cn(
        "tabular-nums",
        tone === "up" && "text-positive",
        tone === "down" && "text-negative",
      )}
    >
      {tone !== "flat" ? (
        <span aria-hidden="true" className="mr-0.5 text-[0.8em]">
          {tone === "up" ? "▲" : "▼"}
        </span>
      ) : null}
      {formatRupees(value, { decimals: 0 })}
    </span>
  );
}

function Header({
  label,
  sortKey,
  active,
  dir,
  onSort,
  align = "right",
}: {
  label: string;
  sortKey?: SortKey;
  active: SortKey;
  dir: 1 | -1;
  onSort: (key: SortKey) => void;
  align?: "left" | "right";
}) {
  const isActive = sortKey !== undefined && sortKey === active;
  return (
    <th
      scope="col"
      aria-sort={isActive ? (dir === 1 ? "ascending" : "descending") : undefined}
      className={cn(
        "whitespace-nowrap py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground",
        align === "right" ? "text-right" : "text-left",
      )}
    >
      {sortKey ? (
        <button
          type="button"
          onClick={() => onSort(sortKey)}
          className="inline-flex items-center gap-1 hover:text-foreground"
        >
          {label}
          <span aria-hidden="true" className={cn("text-[0.7em]", !isActive && "opacity-0")}>
            {dir === 1 ? "▲" : "▼"}
          </span>
        </button>
      ) : (
        label
      )}
    </th>
  );
}

/**
 * What an expanded row reveals — one renderer, used by the desktop table AND the mobile list.
 *
 * Kept in one place deliberately: two copies of this drift, and the phone quietly becomes the
 * surface with less information on it, which is the opposite of what a small screen needs.
 */
function RowDetail({
  slice,
  row,
}: {
  slice: AllocationSlice;
  row: PortfolioRow | undefined;
}) {
  return (
    <>
        <dl className="grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <dt className="text-muted-foreground">Brokers</dt>
            <dd className="font-medium">
              {row?.brokers?.length
                ? row.brokers.map((b) => b.label).join(", ")
                : "No broker attached"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Benchmark</dt>
            <dd className="font-medium">
              {row?.benchmark_name ?? "None chosen"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Measured from</dt>
            <dd className="font-medium tabular-nums">
              {row?.started_on ?? "Unknown"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Cash</dt>
            <dd className="font-medium tabular-nums">
              {formatRupees(slice.cash, { decimals: 0 })}
            </dd>
          </div>
        </dl>
        <Link
          href={`/portfolio/${slice.portfolioId}` as Route}
          className="mt-2 inline-block text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
        >
          Open {slice.name}
        </Link>
    </>
  );
}

export function ComparisonTable({
  slices,
  rows,
  mode,
}: {
  slices: readonly AllocationSlice[];
  rows: readonly PortfolioRow[];
  mode: CommandMode;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("value");
  const [dir, setDir] = useState<1 | -1>(-1);
  const [expanded, setExpanded] = useState<number | null>(null);

  const byId = useMemo(() => new Map(rows.map((r) => [r.portfolio_id, r])), [rows]);
  const ordered = useMemo(
    () => [...slices].sort((a, b) => compare(a, b, sortKey, dir)),
    [slices, sortKey, dir],
  );

  function sortBy(key: SortKey): void {
    if (key === sortKey) {
      setDir((d) => (d === 1 ? -1 : 1));
      return;
    }
    setSortKey(key);
    setDir(key === "name" ? 1 : -1);
  }

  if (ordered.length === 0) return null;

  return (
    <section
      aria-label={mode === "capital" ? "Capital portfolios" : "Monitoring views"}
      data-testid="comparison-table"
      className="rounded-xl border border-border bg-card"
    >
      {/* Below `md` this table is not attempted at all — see `PortfolioCards`. A ten-column grid
          on a 390px screen is a sideways scroll that hides the columns a person came for. */}
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full min-w-[54rem] text-sm">
          <thead className="border-b border-border">
            <tr>
              <th scope="col" className="w-8" />
              <Header label={mode === "capital" ? "Portfolio" : "View"} sortKey="name" active={sortKey} dir={dir} onSort={sortBy} align="left" />
              <Header label="Type" active={sortKey} dir={dir} onSort={sortBy} align="left" />
              <Header label="Value" sortKey="value" active={sortKey} dir={dir} onSort={sortBy} />
              {mode === "capital" ? (
                <Header label="Share" sortKey="share" active={sortKey} dir={dir} onSort={sortBy} />
              ) : null}
              <Header label="Today" sortKey="today" active={sortKey} dir={dir} onSort={sortBy} />
              <Header label="Return" active={sortKey} dir={dir} onSort={sortBy} />
              <Header label="Cash" active={sortKey} dir={dir} onSort={sortBy} />
              <Header label="Holdings" sortKey="holdings" active={sortKey} dir={dir} onSort={sortBy} />
              <Header label="Status" active={sortKey} dir={dir} onSort={sortBy} align="left" />
            </tr>
          </thead>
          <tbody className="divide-y divide-border/70">
            {ordered.map((slice, index) => {
              const row = byId.get(slice.portfolioId);
              const open = expanded === slice.portfolioId;
              const cashShare = percentOf(slice.cash, slice.value);
              return (
                /* The keyed element is the Fragment, not the first `<tr>` inside it — a row and
                   its expansion panel are two siblings, and keying only the first leaves the
                   second unkeyed, which is how React loses expansion state on a re-sort. */
                <Fragment key={slice.portfolioId}>
                  <tr
                    data-testid={`portfolio-row-${slice.portfolioId}`}
                    className="transition-colors duration-150 hover:bg-muted/40"
                  >
                    <td className="py-2 pl-3">
                      <button
                        type="button"
                        aria-expanded={open}
                        aria-label={`${open ? "Collapse" : "Expand"} ${slice.name}`}
                        onClick={() => setExpanded(open ? null : slice.portfolioId)}
                        className="rounded p-0.5 text-muted-foreground hover:text-foreground"
                      >
                        <ChevronRight
                          aria-hidden="true"
                          className={cn(
                            "size-4 transition-transform duration-150",
                            open && "rotate-90",
                          )}
                        />
                      </button>
                    </td>

                    <td className="py-2 pr-3">
                      <span className="flex items-center gap-2">
                        {/* Colour AND a letter: the identifier survives greyscale. */}
                        <span
                          aria-hidden="true"
                          className={cn(
                            "grid size-5 shrink-0 place-items-center rounded text-[0.625rem] font-bold text-accent-foreground",
                            shade(index),
                          )}
                        >
                          {initialOf(slice.name)}
                        </span>
                        <Link
                          href={`/portfolio/${slice.portfolioId}` as Route}
                          className="truncate font-medium underline-offset-4 hover:underline"
                        >
                          {slice.name}
                        </Link>
                      </span>
                    </td>

                    <td className="py-2 pr-3 text-xs text-muted-foreground">
                      {row?.source_badge ?? "Type not recorded"}
                    </td>

                    <td className="py-2 pr-3 text-right">
                      <Money value={slice.value} />
                    </td>

                    {mode === "capital" ? (
                      <td className="py-2 pr-3 text-right tabular-nums">
                        {slice.weightPct === null ? (
                          <span className="text-muted-foreground">Needs more data</span>
                        ) : (
                          <span className="flex items-center justify-end gap-2">
                            {/* A bar as well as a number — shape reads faster than digits. */}
                            <span
                              aria-hidden="true"
                              className="h-1.5 w-10 overflow-hidden rounded-full bg-muted"
                            >
                              <span
                                className={cn("block h-full", shade(index))}
                                style={{ width: `${Math.min(100, Number(slice.weightPct))}%` }}
                              />
                            </span>
                            {slice.weightPct}%
                          </span>
                        )}
                      </td>
                    ) : null}

                    <td className="py-2 pr-3 text-right">
                      <Money value={slice.todaysPnl} signed />
                    </td>

                    <td className="py-2 pr-3 text-right tabular-nums">
                      {slice.returnPct === null ? (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="cursor-help text-muted-foreground">
                              Needs more data
                            </span>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-xs text-xs">
                            {row?.headline_return?.unavailable_reason ??
                              "Not enough history to measure a return yet."}
                          </TooltipContent>
                        </Tooltip>
                      ) : (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="cursor-help">
                              {/* Already a percentage — `allocationAnalytics` converts the API's
                                  fraction once, where the slice is built. This used to render
                                  `headline_return.value` verbatim, so a 12.5% return read
                                  "0.125%". */}
                              {slice.returnPct}%
                              <Info aria-hidden="true" className="ml-1 inline size-3 opacity-50" />
                            </span>
                          </TooltipTrigger>
                          {/* Criterion 3: a return never appears without saying which return. */}
                          <TooltipContent className="max-w-xs text-xs">
                            {slice.returnLabel}
                            {row?.headline_return?.since ? ` · since ${row.headline_return.since}` : ""}
                          </TooltipContent>
                        </Tooltip>
                      )}
                    </td>

                    <td className="py-2 pr-3 text-right tabular-nums text-muted-foreground">
                      {cashShare === null ? "0%" : `${cashShare}%`}
                    </td>

                    <td className="py-2 pr-3 text-right tabular-nums">{slice.holdingsCount}</td>

                    <td className="py-2 pr-3">
                      {row?.pending_reconciliation ? (
                        <span className="flex items-center gap-1 text-xs text-negative">
                          <CircleAlert aria-hidden="true" className="size-3.5" />
                          Not reconciled
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          {row?.status ?? "Synced"}
                        </span>
                      )}
                    </td>
                  </tr>

                  {open ? (
                    <tr key={`${slice.portfolioId}-detail`} data-testid={`portfolio-detail-${slice.portfolioId}`}>
                      <td />
                      <td colSpan={mode === "capital" ? 9 : 8} className="pb-3 pr-3">
                        <RowDetail slice={slice} row={row} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      <ul className="divide-y divide-border md:hidden" data-testid="portfolio-cards">
        {ordered.map((slice, index) => {
          const row = byId.get(slice.portfolioId);
          const open = expanded === slice.portfolioId;
          return (
            <li key={slice.portfolioId} className="px-4 py-3">
              <div className="flex items-start justify-between gap-3">
                <span className="flex min-w-0 items-center gap-2">
                  <span
                    aria-hidden="true"
                    className={cn(
                      "grid size-5 shrink-0 place-items-center rounded text-[0.625rem] font-bold text-accent-foreground",
                      shade(index),
                    )}
                  >
                    {initialOf(slice.name)}
                  </span>
                  <Link
                    href={`/portfolio/${slice.portfolioId}` as Route}
                    className="truncate text-sm font-medium underline-offset-4 hover:underline"
                  >
                    {slice.name}
                  </Link>
                </span>
                <span className="shrink-0 text-right text-sm font-semibold tabular-nums">
                  <Money value={slice.value} />
                </span>
              </div>

              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                <div className="flex justify-between gap-2">
                  <dt className="text-muted-foreground">Today</dt>
                  <dd>
                    <Money value={slice.todaysPnl} signed />
                  </dd>
                </div>
                {mode === "capital" ? (
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">Share</dt>
                    <dd className="tabular-nums">
                      {slice.weightPct === null ? "Needs more data" : `${slice.weightPct}%`}
                    </dd>
                  </div>
                ) : null}
                <div className="flex justify-between gap-2">
                  <dt className="text-muted-foreground">{slice.returnLabel}</dt>
                  <dd className="tabular-nums">
                    {slice.returnPct === null
                      ? (row?.headline_return?.unavailable_reason ??
                        "Not enough history yet")
                      : `${slice.returnPct}%`}
                  </dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-muted-foreground">Holdings</dt>
                  <dd className="tabular-nums">{slice.holdingsCount}</dd>
                </div>
              </dl>

              {row?.pending_reconciliation ? (
                <p className="mt-1.5 flex items-center gap-1 text-xs text-negative">
                  <CircleAlert aria-hidden="true" className="size-3.5" />
                  Not reconciled
                </p>
              ) : null}

              <button
                type="button"
                aria-expanded={open}
                onClick={() => setExpanded(open ? null : slice.portfolioId)}
                className="mt-2 inline-flex items-center gap-0.5 text-xs font-medium text-muted-foreground hover:text-foreground"
              >
                {open ? "Less" : "More"}
                <ChevronRight
                  aria-hidden="true"
                  className={cn("size-3 transition-transform duration-150", open && "rotate-90")}
                />
              </button>

              {open ? (
                <div className="mt-2" data-testid={`portfolio-card-detail-${slice.portfolioId}`}>
                  <RowDetail slice={slice} row={row} />
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>

      {/* Said once, plainly, instead of seven columns of dashes. */}
      <details className="border-t border-border px-4 py-2" data-testid="not-yet-measured">
        <summary className="cursor-pointer text-xs text-muted-foreground">
          Columns this table does not have yet, and what each needs
        </summary>
        <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
          {NOT_YET_MEASURED.map((item) => (
            <li key={item.name}>
              <span className="font-medium text-foreground">{item.name}</span> — {item.blockedBy}.
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}
