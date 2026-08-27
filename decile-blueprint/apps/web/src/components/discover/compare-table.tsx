import Link from "next/link";
import type { Route } from "next";

import { StrategyMark } from "@/components/discover/strategy-mark";
import { buildComparison, differences, rowsBySection, windowNote } from "@/lib/discover/compare";
import { type HoldingSet, overlapMatrix, strongestOverlap } from "@/lib/discover/overlap";
import type { ExploreBasketCard } from "@/lib/explore/fetch";
import { cn } from "@/lib/utils";

/**
 * Two or three baskets, side by side, measured the same way.
 *
 * Three things this table does that a naive one would not.
 *
 * **One period for every column.** `commonWindow` finds the longest return window all the
 * selected baskets share and says, above the table, that it shortened and why. A young basket's
 * one-year number next to an old basket's five-year CAGR is not a comparison, it is a
 * flattering coincidence.
 *
 * **Blanks stay visible.** Most of what the brief asks to compare — drawdown, recovery, Sharpe,
 * turnover — is not computed anywhere in this product. Those rows render empty with the reason
 * rather than being dropped, because a dropped row reads as "these baskets are the same on this".
 *
 * **Overlap leads.** Whether two baskets hold the same stocks is the thing a table of returns
 * cannot tell you and the thing that decides whether buying both diversifies anything.
 */
export function CompareTable({
  baskets,
  holdings,
  className,
}: {
  baskets: readonly ExploreBasketCard[];
  /** One entry per basket, in the same order. `symbols: null` means "could not be read". */
  holdings?: readonly HoldingSet[];
  className?: string;
}) {
  const { window, rows } = buildComparison(baskets);
  const changed = differences(rows);
  const pairs = holdings ? overlapMatrix(holdings) : [];
  const lead = strongestOverlap(pairs);

  return (
    <div className={cn("space-y-6", className)} data-testid="compare-table">
      <p className="max-w-[80ch] text-sm leading-relaxed text-muted-foreground" data-testid="window-note">
        {windowNote(window, baskets)}
      </p>

      {holdings && holdings.length > 0 ? (
        <section
          className="rounded-lg border border-border/70 bg-muted/30 p-4"
          data-testid="overlap-panel"
        >
          <h2 className="text-sm font-semibold">What they hold in common</h2>
          <ul className="mt-2 space-y-1.5">
            {pairs.map((pair) => (
              <li
                key={`${pair.a.slug}-${pair.b.slug}`}
                className="text-xs leading-relaxed text-muted-foreground"
                data-testid="overlap-line"
                data-available={pair.available ? "true" : "false"}
              >
                {pair.summary}
              </li>
            ))}
          </ul>
          {lead && lead.jaccard >= 0.5 ? (
            <p className="mt-2 text-xs leading-relaxed" data-testid="overlap-warning">
              These are largely the same bet. Holding both concentrates the position rather than
              spreading it.
            </p>
          ) : null}
        </section>
      ) : null}

      {changed.length > 0 ? (
        <section data-testid="differences">
          <h2 className="text-sm font-semibold">What is different</h2>
          <ul className="mt-2 space-y-1">
            {changed.map((row) => (
              <li key={row.key} className="text-xs text-muted-foreground">
                <span className="text-foreground">{row.label}:</span>{" "}
                {row.cells
                  .map((cell, index) => `${baskets[index]?.name ?? cell.slug} ${cell.value}`)
                  .join(" · ")}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <div className="overflow-x-auto">
        <table className="w-full min-w-[36rem] border-collapse text-sm">
          <caption className="sr-only">
            {baskets.map((basket) => basket.name).join(", ")} compared over {window.label}
          </caption>
          <thead>
            <tr className="border-b border-border">
              <th scope="col" className="w-[16rem] py-3 pr-4 text-left align-bottom">
                <span className="eyebrow">Measure</span>
              </th>
              {baskets.map((basket) => (
                <th key={basket.slug} scope="col" className="py-3 pr-4 text-left align-bottom">
                  <span className="flex items-center gap-2">
                    <StrategyMark categories={basket.categories} size={28} />
                    <Link
                      href={`/basket/${basket.slug}` as Route}
                      className="font-semibold underline-offset-2 hover:underline"
                    >
                      {basket.name}
                    </Link>
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          {rowsBySection(rows).map(([section, sectionRows]) => (
            <tbody key={section} data-testid="compare-section" data-section={section}>
              <tr>
                <th
                  scope="colgroup"
                  colSpan={baskets.length + 1}
                  className="pt-5 pb-1 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                >
                  {section}
                </th>
              </tr>
              {sectionRows.map((row) => (
                <tr
                  key={row.key}
                  className="border-b border-border/50"
                  data-testid="compare-row"
                  data-row={row.key}
                  data-available={row.anyAvailable ? "true" : "false"}
                >
                  <th scope="row" className="py-2.5 pr-4 text-left align-top font-normal">
                    <details className="group">
                      <summary className="inline-flex cursor-help list-none items-center gap-1 text-muted-foreground decoration-dotted underline-offset-2 hover:underline marker:content-none [&::-webkit-details-marker]:hidden">
                        {row.label}
                        <span
                          aria-hidden="true"
                          className="text-[9px] opacity-60 group-open:opacity-100"
                        >
                          ⓘ
                        </span>
                      </summary>
                      <p className="mt-1 max-w-[40ch] text-xs leading-relaxed text-muted-foreground">
                        {row.explain}
                      </p>
                    </details>
                  </th>
                  {row.cells.map((cell) => (
                    <td
                      key={cell.slug}
                      className={cn(
                        "py-2.5 pr-4 align-top tabular-nums",
                        cell.absence && "text-muted-foreground",
                      )}
                      title={cell.absence?.note}
                    >
                      {cell.value}
                      {cell.absence?.kind === "not-computed" ? (
                        <span className="sr-only"> — not computed yet</span>
                      ) : null}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </div>
    </div>
  );
}
