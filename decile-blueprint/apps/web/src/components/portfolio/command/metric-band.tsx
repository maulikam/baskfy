"use client";

import { CircleAlert, Info } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { ExecutiveSnapshot, Metric } from "@/lib/portfolio/command-center";
import { formatNumber } from "@/lib/format";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * The executive snapshot — one connected band, deliberately not a grid of cards.
 *
 * Brief: *"Create a compact, connected metric band — not four oversized disconnected cards"* and
 * *"Do not place every metric inside an isolated card."* So this is a single bordered surface
 * divided by hairlines, which reads as one instrument rather than eleven unrelated tiles. The
 * division is `divide-x` on a wrapping flex row: the rules disappear where a row wraps, which is
 * what keeps it looking like a band at every width instead of a broken grid.
 *
 * TWO RULES CARRIED IN THE MARKUP
 * -------------------------------
 * **Never a bare dash.** A metric with no value renders its *reason* — visibly, in place of the
 * figure, with a marker. `Metric` makes this structural: the type cannot hold a missing value
 * without also holding the explanation, so there is no path through this component that produces
 * an unexplained "—". That is the brief's own hard rule and it is the one most easily lost.
 *
 * **Never colour alone.** A gain is green *and* carries "▲"; a loss is red *and* carries "▼". A
 * reader who cannot separate the two hues still gets the direction, and so does a screenshot
 * printed in grey.
 */

const NOT_AVAILABLE_MARK = "Not available";

/** Tabular numerals everywhere, so a column of figures aligns on its decimal point. */
const FIGURE = "tabular-nums tracking-tight";

/** A percentage that is ALREADY a percentage: signed, two decimals, no scaling. */
function formatPercent(value: string | null): string {
  return formatNumber(value, { decimals: 2, signed: true, suffix: "%" });
}

function toneOf(value: string | null): "up" | "down" | "flat" {
  if (value === null) return "flat";
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

/** The direction glyph. Present so direction survives without colour (brief: never colour alone). */
function DirectionMark({ tone }: { tone: "up" | "down" | "flat" }) {
  if (tone === "flat") return null;
  return (
    <span aria-hidden="true" className="mr-0.5 text-[0.8em]">
      {tone === "up" ? "▲" : "▼"}
    </span>
  );
}

export function MetricCell({
  metric,
  emphasis = "normal",
  signed = false,
  percent = false,
}: {
  metric: Metric;
  emphasis?: "hero" | "normal";
  /** Colour and a glyph by sign — only for figures where direction is the point. */
  signed?: boolean;
  /** Render as a percentage rather than rupees. */
  percent?: boolean;
}) {
  const tone = signed ? toneOf(metric.value) : "flat";
  const unavailable = metric.value === null;

  return (
    <div className={cn("min-w-0 px-4 py-3", emphasis === "hero" && "md:px-5 md:py-4")}>
      <Tooltip>
        <TooltipTrigger asChild>
          <p className="flex cursor-help items-center gap-1 text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
            {metric.label}
            <Info aria-hidden="true" className="size-3 opacity-60" />
          </p>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs text-xs leading-relaxed">
          {metric.definition}
          {metric.since ? (
            <span className="mt-1 block text-muted-foreground">Measured from {metric.since}.</span>
          ) : null}
        </TooltipContent>
      </Tooltip>

      {unavailable ? (
        /* The brief's rule, made visible: the REASON stands where the figure would. It is not a
           dash with a tooltip — a reader should not have to hover to find out why a number is
           missing from their own portfolio. */
        <div className="mt-1">
          <p
            className={cn(
              "flex items-center gap-1 font-medium text-muted-foreground",
              emphasis === "hero" ? "text-base" : "text-sm",
            )}
          >
            <CircleAlert aria-hidden="true" className="size-3.5 shrink-0 text-warning" />
            {NOT_AVAILABLE_MARK}
          </p>
          <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{metric.unavailable}</p>
        </div>
      ) : (
        <p
          className={cn(
            "mt-1 font-semibold",
            FIGURE,
            emphasis === "hero" ? "text-2xl md:text-3xl" : "text-base",
            tone === "up" && "text-positive",
            tone === "down" && "text-negative",
          )}
        >
          <DirectionMark tone={tone} />
          {/* THIS CELL FORMATS; IT DOES NOT SCALE — and the difference is the whole bug.
              `Metric.value` for a rate is a PERCENTAGE, converted once where the metric is built
              (`asPercent` in `command-center.ts`, on scaled integers). The API stores fractions:
              `pct = money / base`, so a 1.99% day arrives as `0.019900`. Whoever converts must be
              the pure layer, because this cell is shared — PC1's band, PC2's chart read-out and
              its headline strip all render through it, and PC2's figures come from `percentOf`
              already in percent. A `× 100` here showed PC2's 10% as 1,000%.

              The original defect was the other direction: this read `${metric.value}%` while the
              band handed it the raw fraction, so the executive snapshot would have shown
              "0.0199%" for a 1.99% day and "-0.082%" for an 8.2% fall. Forty-five tests were
              green over it because the fixtures used "1.99" and "18.7" — values that type-check
              and that the server never sends. House rule 2 in its least obvious form. */}
          {percent ? formatPercent(metric.value) : formatRupees(metric.value)}
          {metric.pct ? (
            <span className="ml-1.5 text-sm font-medium opacity-80">
              {formatPercent(metric.pct)}
            </span>
          ) : null}
        </p>
      )}
    </div>
  );
}

export function MetricBand({ snapshot }: { snapshot: ExecutiveSnapshot }) {
  return (
    <section
      aria-label="Executive snapshot"
      data-testid="metric-band"
      className="overflow-hidden rounded-xl border border-border bg-card"
    >
      {/* The two figures a person looks at first, given the most room. Everything else is one
          rank down — the band has a hierarchy rather than eleven equal boxes. */}
      <div className="flex flex-wrap divide-x divide-border border-b border-border">
        <div className="min-w-[15rem] flex-1">
          <MetricCell metric={snapshot.netWorth} emphasis="hero" />
        </div>
        <div className="min-w-[15rem] flex-1">
          <MetricCell metric={snapshot.todaysPnl} emphasis="hero" signed />
        </div>
      </div>

      <div className="flex flex-wrap divide-x divide-border">
        {[
          { m: snapshot.invested },
          { m: snapshot.cash },
          { m: snapshot.unallocated },
          { m: snapshot.unrealisedPnl, signed: true },
          { m: snapshot.realisedPnl, signed: true },
          { m: snapshot.xirr, percent: true, signed: true },
          { m: snapshot.twr, percent: true, signed: true },
          { m: snapshot.drawdown, percent: true, signed: true },
          { m: snapshot.peak },
        ].map(({ m, signed, percent }) => (
          <div key={m.label} className="min-w-[9.5rem] flex-1">
            <MetricCell metric={m} signed={signed ?? false} percent={percent ?? false} />
          </div>
        ))}
      </div>
    </section>
  );
}
