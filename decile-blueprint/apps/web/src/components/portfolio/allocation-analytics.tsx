import { ArrowDownRight, ArrowUpRight, Layers, PieChart, Wallet } from "lucide-react";

import { allocationAnalytics, type AllocationSlice, type Spread } from "@/lib/portfolio/analytics";
import type { Unallocated } from "@/lib/portfolio/organize";
import type { PortfolioRow } from "@/lib/portfolio/overview";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * `/portfolio/portfolios`' analytics band — the shape of the book, above the grouping list.
 *
 * Maulik, 11 Sep 2026: *"this one needs more analytical stats and dashboard like feel where one
 * can analyse and view and understand the metrics ... more numbers and fun looking"*.
 *
 * WHAT IT SHOWS THAT `/portfolio` DOES NOT
 * ----------------------------------------
 * The overview page answers "what is each portfolio worth and what did it do". This answers the
 * question you can only ask once you have more than one: **how is the money spread?** One bar
 * showing every portfolio's share of net worth, the concentration read that follows from it, and
 * the two portfolios that actually moved the number today. Those are comparisons *between*
 * portfolios, which is why they live on the page about portfolios rather than beside any one.
 *
 * EVERY FIGURE IS LABELLED, AND ABSENT IS NOT ZERO
 * ------------------------------------------------
 * Criterion 3: a return carries the name of what it is. A portfolio with no close on record shows
 * a dash and is counted out of the weights, with a line saying how many were left out — because a
 * denominator quietly missing a portfolio produces a column that still totals 100% and is wrong
 * about every row in it.
 *
 * Read-only, like everything under `/portfolio`. Nothing here places an order.
 */

/**
 * The band's segments, walked in order, so a portfolio keeps its shade between the bar and the
 * table beside it.
 *
 * Opacity steps of the one accent rather than a rainbow, because `globals.css` says what this
 * palette is for in its own first comment: "positive/negative, rank emphasis, and one accent for
 * primary actions". Five invented hues would read as five categories with meanings; five weights
 * of one colour read as an ordering, which is what a share of net worth is.
 */
const BAND = [
  "bg-accent",
  "bg-accent/80",
  "bg-accent/60",
  "bg-accent/45",
  "bg-accent/30",
] as const;

const SPREAD_COPY: Readonly<Record<Spread, { title: string; blurb: string }>> = {
  concentrated: {
    title: "Concentrated",
    blurb: "More than half your holdings sit in one portfolio.",
  },
  balanced: {
    title: "Balanced",
    blurb: "Your largest portfolio is a quarter to a half of everything you hold.",
  },
  spread: {
    title: "Spread out",
    blurb: "No single portfolio is more than a quarter of everything you hold.",
  },
};

const NO_FIGURE = "—";

function colourFor(index: number): string {
  return BAND[index % BAND.length] ?? BAND[0];
}

function pct(value: string | null): string {
  return value === null ? NO_FIGURE : `${value}%`;
}

/** A KPI tile. Big number, small label, and a caption that says what the number *is*. */
function Tile({
  label,
  value,
  caption,
  tone = "neutral",
  icon,
}: {
  label: string;
  value: string;
  caption?: string | undefined;
  tone?: "neutral" | "up" | "down";
  icon?: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <p className="flex items-center gap-1.5 text-xs uppercase tracking-wide text-muted-foreground">
        {icon}
        {label}
      </p>
      <p
        className={cn(
          "mt-1 text-2xl font-semibold tabular-nums",
          tone === "up" && "text-positive",
          tone === "down" && "text-negative",
        )}
      >
        {value}
      </p>
      {caption ? <p className="mt-0.5 text-xs text-muted-foreground">{caption}</p> : null}
    </div>
  );
}

function MoverCard({
  title,
  slice,
  tone,
}: {
  title: string;
  slice: AllocationSlice;
  tone: "up" | "down";
}) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-border bg-card px-3 py-2">
      {tone === "up" ? (
        <ArrowUpRight aria-hidden="true" className="mt-0.5 size-4 text-positive" />
      ) : (
        <ArrowDownRight aria-hidden="true" className="mt-0.5 size-4 text-negative" />
      )}
      <div className="min-w-0">
        <p className="text-xs text-muted-foreground">{title}</p>
        <p className="truncate text-sm font-medium">{slice.name}</p>
        <p
          className={cn(
            "text-sm tabular-nums",
            tone === "up" ? "text-positive" : "text-negative",
          )}
        >
          {formatRupees(slice.todaysPnl)}
        </p>
      </div>
    </div>
  );
}

export function AllocationAnalytics({
  rows,
  unallocated,
}: {
  rows: readonly PortfolioRow[];
  unallocated: Unallocated | null;
}) {
  const analytics = allocationAnalytics(rows, unallocated);
  if (analytics.portfolioCount === 0 && analytics.unallocatedValue === null) return null;

  const spread = analytics.spread === null ? null : SPREAD_COPY[analytics.spread];
  const dayTone =
    analytics.todaysTotal === null || analytics.todaysTotal.startsWith("0")
      ? "neutral"
      : analytics.todaysTotal.startsWith("-")
        ? "down"
        : "up";

  return (
    <section aria-label="How your money is spread" className="space-y-4" data-testid="allocation-analytics">
      {/* ------------------------------------------------------------------ the numbers */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          label="Across all portfolios"
          value={formatRupees(analytics.totalValue)}
          caption={`${analytics.portfolioCount} portfolio${analytics.portfolioCount === 1 ? "" : "s"} · ${analytics.holdingsCount} holding${analytics.holdingsCount === 1 ? "" : "s"}`}
          icon={<PieChart aria-hidden="true" className="size-3.5" />}
        />
        <Tile
          label="Today"
          value={analytics.todaysTotal === null ? NO_FIGURE : formatRupees(analytics.todaysTotal)}
          caption="Sum of every portfolio's move"
          tone={dayTone}
        />
        <Tile
          label="Filed into a portfolio"
          value={formatRupees(analytics.allocatedValue)}
          caption={
            analytics.unallocatedPct === null
              ? undefined
              : `${pct(analytics.unallocatedPct)} still unallocated`
          }
          icon={<Layers aria-hidden="true" className="size-3.5" />}
        />
        <Tile
          label="Largest portfolio"
          value={pct(analytics.topShare)}
          caption={
            analytics.topThreeShare === null
              ? undefined
              : `Top three together: ${pct(analytics.topThreeShare)}`
          }
          icon={<Wallet aria-hidden="true" className="size-3.5" />}
        />
      </div>

      {/* --------------------------------------------------------------------- the bar */}
      <div className="rounded-xl border border-border bg-card p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-sm font-semibold">How the money is spread</h3>
          {spread ? (
            <span className="text-xs text-muted-foreground">
              <span className="font-medium text-foreground">{spread.title}</span> — {spread.blurb}
            </span>
          ) : null}
        </div>

        <div
          className="mt-3 flex h-3 w-full overflow-hidden rounded-full bg-muted"
          role="img"
          aria-label="Each portfolio's share of the total"
          data-testid="allocation-bar"
        >
          {analytics.slices.map((slice, index) =>
            slice.weightPct === null ? null : (
              <div
                key={slice.portfolioId}
                className={cn(colourFor(index))}
                style={{ width: `${slice.weightPct}%` }}
                title={`${slice.name} — ${slice.weightPct}%`}
              />
            ),
          )}
          {analytics.unallocatedPct === null ? null : (
            <div
              className="bg-muted-foreground/30"
              style={{ width: `${analytics.unallocatedPct}%` }}
              title={`Unallocated — ${analytics.unallocatedPct}%`}
            />
          )}
        </div>

        {/* ----------------------------------------------------------------- the table */}
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[46rem] text-sm">
            <thead className="text-xs uppercase tracking-wide text-muted-foreground">
              <tr className="border-b border-border">
                <th scope="col" className="py-2 text-left font-medium">Portfolio</th>
                <th scope="col" className="py-2 text-right font-medium">Value</th>
                <th scope="col" className="py-2 text-right font-medium">Share</th>
                <th scope="col" className="py-2 text-right font-medium">Today</th>
                <th scope="col" className="py-2 text-right font-medium">Return</th>
                <th scope="col" className="py-2 text-right font-medium">Holdings</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/70">
              {analytics.slices.map((slice, index) => (
                <tr key={slice.portfolioId}>
                  <td className="py-2">
                    <span className="flex items-center gap-2">
                      <span
                        aria-hidden="true"
                        className={cn("size-2.5 shrink-0 rounded-sm", colourFor(index))}
                      />
                      <span className="truncate">{slice.name}</span>
                    </span>
                  </td>
                  <td className="py-2 text-right tabular-nums">
                    {slice.value === null ? (
                      <span className="text-muted-foreground" title="No close on record">
                        {NO_FIGURE}
                      </span>
                    ) : (
                      formatRupees(slice.value)
                    )}
                  </td>
                  <td className="py-2 text-right tabular-nums">{pct(slice.weightPct)}</td>
                  <td
                    className={cn(
                      "py-2 text-right tabular-nums",
                      slice.todaysPnl?.startsWith("-") && "text-negative",
                      slice.todaysPnl && !slice.todaysPnl.startsWith("-") &&
                        !slice.todaysPnl.startsWith("0") && "text-positive",
                    )}
                  >
                    {slice.todaysPnl === null ? NO_FIGURE : formatRupees(slice.todaysPnl)}
                  </td>
                  {/* Criterion 3: the label travels with the number, always. */}
                  <td className="py-2 text-right tabular-nums" title={slice.returnLabel ?? undefined}>
                    {slice.returnPct === null ? (
                      <span className="text-muted-foreground">{NO_FIGURE}</span>
                    ) : (
                      <>
                        {slice.returnPct}%
                        <span className="ml-1 text-xs text-muted-foreground">
                          {slice.returnLabel}
                        </span>
                      </>
                    )}
                  </td>
                  <td className="py-2 text-right tabular-nums">{slice.holdingsCount}</td>
                </tr>
              ))}
              {analytics.unallocatedValue === null ? null : (
                <tr className="text-muted-foreground">
                  <td className="py-2">
                    <span className="flex items-center gap-2">
                      <span aria-hidden="true" className="size-2.5 shrink-0 rounded-sm bg-muted-foreground/30" />
                      Unallocated
                    </span>
                  </td>
                  <td className="py-2 text-right tabular-nums">
                    {formatRupees(analytics.unallocatedValue)}
                  </td>
                  <td className="py-2 text-right tabular-nums">{pct(analytics.unallocatedPct)}</td>
                  <td className="py-2 text-right">{NO_FIGURE}</td>
                  <td className="py-2 text-right">{NO_FIGURE}</td>
                  <td className="py-2 text-right tabular-nums">
                    {unallocated?.holdings_count ?? 0}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {analytics.unpricedCount > 0 ? (
          <p className="mt-2 text-xs text-warning" data-testid="analytics-unpriced">
            {analytics.unpricedCount} portfolio
            {analytics.unpricedCount === 1 ? " has" : "s have"} no close on record and
            {analytics.unpricedCount === 1 ? " is" : " are"} not in the shares above.
          </p>
        ) : null}
      </div>

      {/* ------------------------------------------------------------------- the movers */}
      {analytics.bestToday && analytics.worstToday ? (
        <div className="grid gap-3 sm:grid-cols-2" data-testid="analytics-movers">
          <MoverCard title="Moved up most today" slice={analytics.bestToday} tone="up" />
          <MoverCard title="Moved down most today" slice={analytics.worstToday} tone="down" />
        </div>
      ) : null}
    </section>
  );
}
