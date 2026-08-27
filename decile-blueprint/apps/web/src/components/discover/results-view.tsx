import Link from "next/link";
import type { Route } from "next";

import { CompareToggle } from "@/components/discover/compare-toggle";
import { DiscoverBasketCard } from "@/components/discover/basket-card";
import { SaveButton } from "@/components/discover/save-button";
import { StrategyMark } from "@/components/discover/strategy-mark";
import {
  capitalMetrics,
  returnMetrics,
  riskMetrics,
} from "@/lib/discover/metrics";
import type { ExploreBasketCard } from "@/lib/explore/fetch";
import { cn } from "@/lib/utils";

export type ResultsMode = "cards" | "table";

export function isResultsMode(value: string | undefined): value is ResultsMode {
  return value === "cards" || value === "table";
}

/**
 * The same baskets, as cards or as a table.
 *
 * The brief's reasoning, and it is right: cards are for discovery — you are skimming, and the
 * strategy mark plus a sentence tells you whether to stop. A table is for deciding — you are
 * comparing six things on one number and want them in a column, aligned, with no decoration
 * between them. Neither is better; they are different tasks, so both exist and the reader picks.
 *
 * The table is the same data through the same formatters, so a figure cannot read one way in one
 * view and another way in the other.
 */
export function ResultsView({
  baskets,
  mode,
  className,
}: {
  baskets: readonly ExploreBasketCard[];
  mode: ResultsMode;
  className?: string;
}) {
  if (mode === "table") {
    return (
      <div className={cn("overflow-x-auto", className)} data-testid="results-table">
        <table className="w-full min-w-[44rem] border-collapse text-sm">
          <caption className="sr-only">Baskets, with risk before return</caption>
          <thead>
            <tr className="border-b border-border text-left">
              <th scope="col" className="py-2 pr-4 font-medium">
                Basket
              </th>
              {/* Risk first here too, so the two views cannot teach different habits. */}
              <th scope="col" className="py-2 pr-4 font-medium">
                Volatility
              </th>
              <th scope="col" className="py-2 pr-4 font-medium">
                Return
              </th>
              <th scope="col" className="py-2 pr-4 font-medium">
                Minimum
              </th>
              <th scope="col" className="py-2 pr-4 font-medium">
                Rebalance
              </th>
              <th scope="col" className="py-2 font-medium">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {baskets.map((basket) => {
              const [risk] = riskMetrics(basket.metrics);
              const [performance] = returnMetrics(basket.metrics);
              const [capital] = capitalMetrics(basket);
              return (
                <tr
                  key={basket.slug}
                  className="border-b border-border/50"
                  data-testid="results-row"
                  data-slug={basket.slug}
                >
                  <th scope="row" className="py-2.5 pr-4 text-left font-normal">
                    <span className="flex items-center gap-2">
                      <StrategyMark categories={basket.categories} size={26} />
                      <Link
                        href={`/basket/${basket.slug}` as Route}
                        className="font-medium underline-offset-2 hover:underline"
                      >
                        {basket.name}
                      </Link>
                    </span>
                  </th>
                  <td className="py-2.5 pr-4 tabular-nums">{risk?.value}</td>
                  <td className="py-2.5 pr-4 tabular-nums">
                    {performance?.value}
                    <span className="ml-1 text-xs text-muted-foreground">
                      {performance?.label}
                    </span>
                  </td>
                  <td className="py-2.5 pr-4 tabular-nums">{capital?.value}</td>
                  <td className="py-2.5 pr-4">{basket.rebalance_frequency.toLowerCase()}</td>
                  <td className="py-2.5">
                    <span className="flex flex-wrap gap-1.5">
                      <CompareToggle slug={basket.slug} name={basket.name} />
                      <SaveButton slug={basket.slug} name={basket.name} />
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <ul
      className={cn("grid gap-4 sm:grid-cols-2 2xl:grid-cols-3", className)}
      aria-label="Basket catalog"
      data-testid="results-cards"
    >
      {baskets.map((basket) => (
        <li key={basket.slug} className="h-full">
          <DiscoverBasketCard basket={basket} />
        </li>
      ))}
    </ul>
  );
}

/** Cards | Table. A pair of links, so the choice is in the URL and survives a reload. */
export function ResultsModeToggle({
  mode,
  hrefFor,
}: {
  mode: ResultsMode;
  hrefFor: (mode: ResultsMode) => string;
}) {
  return (
    <nav aria-label="Result layout" data-testid="results-mode-toggle">
      <ul className="inline-flex rounded-lg border border-border/70 bg-muted/40 p-1">
        {(["cards", "table"] as const).map((option) => (
          <li key={option}>
            <Link
              href={hrefFor(option) as Route}
              aria-current={mode === option ? "true" : undefined}
              data-active={mode === option ? "true" : "false"}
              className={cn(
                "block rounded-md px-3 py-1 text-xs font-medium capitalize transition-colors",
                mode === option
                  ? "marker-control"
                  : "text-muted-foreground hover:bg-background hover:text-foreground",
              )}
            >
              {option}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
