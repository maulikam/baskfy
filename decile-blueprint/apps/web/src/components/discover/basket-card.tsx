import Link from "next/link";
import type { Route } from "next";

import { CompareToggle } from "@/components/discover/compare-toggle";
import { MetricStat } from "@/components/discover/metric-stat";
import { ReturnBasis } from "@/components/discover/return-basis";
import { SaveButton } from "@/components/discover/save-button";
import { StrategyMark } from "@/components/discover/strategy-mark";
import { capitalMetrics, returnMetrics, riskMetrics } from "@/lib/discover/metrics";
import type { ExploreBasketCard } from "@/lib/explore/fetch";
import { cn } from "@/lib/utils";

/**
 * A basket card that answers the four questions the brief asks of it.
 *
 * 1. **What does it do?** — a drawn strategy mark, the tag line, and one sentence of thesis.
 * 2. **How has it performed?** — the headline return, with its window named and its unit present.
 * 3. **What can go wrong?** — volatility, *before* the return rather than after it, and the
 *    return convention available at the number rather than in the page footer.
 * 4. **What can I do next?** — View analysis as a filled primary, plus Save and Compare.
 *
 * **The whole card is no longer one link.** It used to be: a single `<Link>` wrapped everything,
 * which is why there was no room for a prominent action and no way to save without opening the
 * basket first. The name is the link and the primary action is a button; Save and Compare are
 * their own controls. Nesting them inside a card-wide anchor would have been invalid HTML and
 * would have made every Save click a navigation.
 *
 * **Risk before return** is a deliberate inversion of the old order (minimum → return → risk).
 * A reader who has already read a return has formed an opinion before they know what it cost.
 */
export function DiscoverBasketCard({
  basket,
  /** Shown under the title when a filter produced this card — always filter language. */
  matchNote,
  savedInitially = false,
  showActions = true,
  className,
}: {
  basket: ExploreBasketCard;
  matchNote?: string;
  savedInitially?: boolean;
  showActions?: boolean;
  className?: string;
}) {
  const risk = riskMetrics(basket.metrics);
  const performance = returnMetrics(basket.metrics);
  const capital = capitalMetrics(basket);

  const thesis =
    basket.description_md?.replace(/[#*_`]/g, "").replace(/\s+/g, " ").trim() ||
    "No one-line summary has been written for this basket yet.";

  const descriptors = [
    basket.categories[0] ? titleCase(basket.categories[0]) : null,
    `${titleCase(basket.rebalance_frequency)} rebalance`,
    basket.manager.name,
  ].filter(Boolean) as string[];

  return (
    <article
      className={cn(
        "flex h-full flex-col gap-3 rounded-lg border border-border/70 bg-card p-4 transition-colors duration-150",
        "hover:border-muted-foreground/40",
        className,
      )}
      data-testid="discover-basket-card"
      data-slug={basket.slug}
    >
      <div className="flex items-start gap-3">
        <StrategyMark categories={basket.categories} />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold leading-snug">
            <Link
              href={`/basket/${basket.slug}` as Route}
              className="underline-offset-2 hover:underline"
            >
              {basket.name}
            </Link>
          </h3>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">
            {descriptors.join(" · ")}
          </p>
        </div>
        {basket.access !== "FREE" ? (
          <span className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {basket.access.toLowerCase()}
          </span>
        ) : null}
      </div>

      <p className="line-clamp-2 text-xs leading-relaxed text-muted-foreground">{thesis}</p>

      {matchNote ? (
        <p
          className="rounded-md border border-border/70 bg-muted/40 px-2.5 py-1.5 text-xs leading-relaxed"
          data-testid="match-note"
        >
          {matchNote}
        </p>
      ) : null}

      {/* Risk first. A reader who reads the return first has an opinion before they know its cost. */}
      <div className="grid grid-cols-3 gap-3 border-t border-border/60 pt-3">
        {[...risk, ...performance, ...capital].map((metric) => (
          <MetricStat key={metric.key} metric={metric} />
        ))}
      </div>

      <ReturnBasis metrics={basket.metrics} />

      {showActions ? (
        <div className="mt-auto flex flex-wrap items-center gap-2 pt-1">
          <Link
            href={`/basket/${basket.slug}` as Route}
            data-testid="view-analysis"
            className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-accent-foreground transition-opacity hover:opacity-90"
          >
            View analysis
          </Link>
          <CompareToggle slug={basket.slug} name={basket.name} />
          <SaveButton slug={basket.slug} name={basket.name} initiallySaved={savedInitially} />
        </div>
      ) : null}
    </article>
  );
}

function titleCase(value: string): string {
  return value
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase())
    .replace(/\B\w+/g, (word) => word.toLowerCase());
}
