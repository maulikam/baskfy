"use client";

import { useState } from "react";

import { Money, MoneyDelta } from "@/components/portfolio/amounts";
import { ReturnValue } from "@/components/portfolio/return-value";
import { SourceBadge } from "@/components/portfolio/source-badge";
import { Badge } from "@/components/ui/badge";
import { EMPTY_CELL } from "@/lib/format";
import {
  GROUPING_TABS,
  MONITORING_NOTE,
  describeBrokers,
  fromFigure,
  rowsForTab,
  tabExcludedFromTotals,
  toneFor,
  totalOfRows,
  type GroupingTab,
  type Overview,
  type PortfolioRow,
} from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * §6.5 — the portfolio table, its grouping tabs, and the row that opens the inspector.
 *
 * ## The Monitoring views tab is muted, and that is load-bearing
 *
 * §4.1: a monitoring view overlaps other portfolios and is excluded from every total; §11
 * criterion 2 says it *"never affects any total"*. Two things follow, and both are here:
 *
 * 1. **The tab has no sum.** Every other tab shows what its rows add up to. This one shows §4.1's
 *    sentence in the same place, so the reader's eye lands on an explanation exactly where it
 *    would otherwise land on a number. A subtotal under overlapping rows would be a number that
 *    double-counts, printed by the product itself.
 * 2. **The rows are visually quieter.** Muted text and a dashed left border, not just a badge —
 *    §4.1 asks for "visually distinct (muted styling) from capital portfolios in every list", and
 *    a reader skimming a list of ten reads weight before they read words.
 *
 * ## Return is a labelled column, never a bare percentage
 *
 * The header says "Return" but the cell says what *this row's* return is — "TWR since you
 * subscribed", "Since grouped" — because §5.2's whole point is that the metric differs by source
 * and one column header cannot be true for every row under it. {@link ReturnValue} carries the
 * start date in `title` (§11 criterion 3).
 *
 * ## Clicking a row
 *
 * The name is a real `<button>`. A `<tr>` with an `onClick` is unreachable by keyboard and
 * `jsx-a11y` rejects giving it an interactive role; a button in the first cell is the same click
 * target for a mouse — the row's hover styling follows it via `group-hover` — and is the only
 * version that works with Tab and Enter. It calls `onSelect`; it does not navigate (§6.5).
 */

const STATUS_TONE: Readonly<Record<string, "neutral" | "accent" | "warning" | "positive">> = {
  "On target": "positive",
  "Rebalance due": "accent",
  "Pending reconciliation": "warning",
  Synced: "neutral",
};

export interface PortfolioTableProps {
  overview: Overview;
  onSelect: (row: PortfolioRow) => void;
  /** Lifted so the header's Export writes exactly the rows on screen. */
  onRowsChange?: (rows: readonly PortfolioRow[]) => void;
}

export function PortfolioTable({ overview, onSelect, onRowsChange }: PortfolioTableProps) {
  const [tab, setTab] = useState<GroupingTab>("ALL");
  const rows = rowsForTab(overview, tab);
  const excludedTab = tabExcludedFromTotals(rows);
  const total = totalOfRows(rows);
  const note = overview.monitoring_excluded_note ?? MONITORING_NOTE;

  function pick(next: GroupingTab) {
    setTab(next);
    onRowsChange?.(rowsForTab(overview, next));
  }

  return (
    <section aria-label="Portfolios" className="space-y-3">
      <div role="tablist" aria-label="Group portfolios by" className="flex flex-wrap gap-1">
        {GROUPING_TABS.map((entry) => {
          const count = rowsForTab(overview, entry.key).length;
          const muted = entry.key === "MONITORING";
          return (
            <button
              key={entry.key}
              type="button"
              role="tab"
              aria-selected={tab === entry.key}
              data-testid={`grouping-tab-${entry.key}`}
              data-muted={muted ? "true" : "false"}
              onClick={() => pick(entry.key)}
              className={cn(
                "rounded-md border px-3 py-1.5 text-sm transition-colors",
                tab === entry.key
                  ? "border-accent bg-accent-muted text-accent"
                  : "border-border/70 text-muted-foreground hover:text-foreground",
                muted && tab !== entry.key && "border-dashed text-muted-foreground/70",
              )}
            >
              {entry.label}
              <span className="ml-1.5 text-xs tabular-nums opacity-70">{count}</span>
            </button>
          );
        })}
      </div>

      {excludedTab ? (
        <p data-testid="monitoring-note" className="text-xs text-muted-foreground">
          {note}
        </p>
      ) : null}

      {rows.length === 0 ? (
        <p
          data-testid="table-empty"
          className="rounded-xl border border-dashed border-border bg-card/50 px-6 py-10 text-center text-sm text-muted-foreground"
        >
          Nothing in this group yet.
        </p>
      ) : (
        <>
          {/* Desktop: the compact table §6.5 specifies. */}
          <div className="hidden overflow-x-auto rounded-xl border border-border/70 md:block">
            <table className="w-full text-sm" data-testid="portfolio-table">
              <caption className="sr-only">
                Your portfolios, with what each is worth and how it has done.
              </caption>
              <thead>
                <tr className="border-b border-border bg-muted/30 text-left text-xs text-muted-foreground">
                  <th scope="col" className="px-3 py-2 font-normal">
                    Name
                  </th>
                  <th scope="col" className="px-3 py-2 font-normal">
                    Source
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-normal">
                    Value
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-normal">
                    Today
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-normal">
                    Return
                  </th>
                  <th scope="col" className="px-3 py-2 font-normal">
                    Brokers
                  </th>
                  <th scope="col" className="px-3 py-2 font-normal">
                    Status
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const muted = !row.counts_toward_total;
                  return (
                    <tr
                      key={row.portfolio_id}
                      data-testid="portfolio-row"
                      data-counts={row.counts_toward_total ? "true" : "false"}
                      className={cn(
                        "group border-b border-border/60 last:border-0 hover:bg-muted/30",
                        muted && "border-l-2 border-l-muted-foreground/30 text-muted-foreground",
                      )}
                    >
                      <th scope="row" className="px-3 py-2 text-left font-normal">
                        <button
                          type="button"
                          data-testid="portfolio-row-open"
                          onClick={() => onSelect(row)}
                          className={cn(
                            "text-left font-medium underline-offset-4 hover:underline",
                            muted ? "text-muted-foreground" : "text-foreground",
                          )}
                        >
                          {row.name}
                        </button>
                        {muted ? (
                          <span className="mt-0.5 block text-[11px] leading-snug text-muted-foreground">
                            {row.excluded_note ?? note}
                          </span>
                        ) : null}
                      </th>
                      <td className="px-3 py-2">
                        <SourceBadge row={row} />
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        <Money value={row.value} />
                      </td>
                      <td
                        className={cn("px-3 py-2 text-right tabular-nums", toneFor(row.todays_pnl.amount))}
                        title={row.todays_pnl.label}
                      >
                        <MoneyDelta value={row.todays_pnl.amount ?? null} />
                      </td>
                      <td className="px-3 py-2 text-right">
                        <ReturnValue
                          entry={fromFigure(row.headline_return)}
                          className="items-end"
                        />
                      </td>
                      <td className="px-3 py-2 text-muted-foreground">{describeBrokers(row)}</td>
                      <td className="px-3 py-2">
                        <Badge variant={STATUS_TONE[row.status] ?? "neutral"}>{row.status}</Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr className="border-t border-border bg-muted/20">
                  <td colSpan={2} className="px-3 py-2 text-xs text-muted-foreground">
                    {excludedTab ? note : `${rows.length} in this group`}
                  </td>
                  <td className="px-3 py-2 text-right text-sm font-medium tabular-nums" data-testid="table-total">
                    {excludedTab || total === null ? (
                      EMPTY_CELL
                    ) : (
                      <Money value={total} />
                    )}
                  </td>
                  <td colSpan={4} />
                </tr>
              </tfoot>
            </table>
          </div>

          {/* Mobile: the same rows as cards. */}
          <ul className="space-y-2 md:hidden" data-testid="portfolio-cards">
            {rows.map((row) => {
              const muted = !row.counts_toward_total;
              return (
                <li key={row.portfolio_id}>
                  <button
                    type="button"
                    onClick={() => onSelect(row)}
                    data-testid="portfolio-card-open"
                    className={cn(
                      "w-full space-y-2 rounded-xl border p-3 text-left",
                      muted
                        ? "border-dashed border-border bg-card/50 text-muted-foreground"
                        : "border-border/70 bg-card",
                    )}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className="font-medium">{row.name}</span>
                      <SourceBadge row={row} />
                    </div>
                    <div className="flex items-end justify-between gap-3">
                      <span className="text-lg font-semibold tabular-nums">
                        <Money value={row.value} />
                      </span>
                      <ReturnValue entry={fromFigure(row.headline_return)} className="items-end" />
                    </div>
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                      <span>{describeBrokers(row)}</span>
                      <span>{row.status}</span>
                    </div>
                    {muted ? (
                      <p className="text-[11px] leading-snug">{row.excluded_note ?? note}</p>
                    ) : null}
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
