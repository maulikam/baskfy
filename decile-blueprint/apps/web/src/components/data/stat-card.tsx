import type * as React from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { describeChange, direction, directionGlyph } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * docs/08 §"Instrument factsheet":
 *
 *     "**Metric cards with medians** — value, sparkline, and a subdued `Median: x` line. The
 *      median is the stock's own history; add a tooltip saying so."
 *
 * The tooltip is not decoration: a median with no stated population is a number nobody can act
 * on, and "the stock's own history" is a materially different claim from "the universe".
 *
 * `loading` renders a skeleton of the *same box*, so the swap causes no layout shift — Prompt 8's
 * fourth acceptance criterion.
 */
export interface StatCardProps {
  label: string;
  value: string;
  /** Signed magnitude behind `value`, used for the colour and the glyph. */
  changeValue?: number | null | undefined;
  changeLabel?: string | undefined;
  median?: string | undefined;
  medianExplanation?: string;
  sparkline?: React.ReactNode;
  loading?: boolean;
  className?: string | undefined;
}

const DEFAULT_MEDIAN_EXPLANATION =
  "The median of this instrument's own history, not of the universe.";

export function StatCard({
  label,
  value,
  changeValue,
  changeLabel,
  median,
  medianExplanation = DEFAULT_MEDIAN_EXPLANATION,
  sparkline,
  loading = false,
  className,
}: StatCardProps) {
  const dir = direction(changeValue);

  return (
    <div
      className={cn(
        "flex min-h-24 flex-col justify-between gap-2 rounded-md border border-border bg-card p-3",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-medium text-muted-foreground">{label}</p>
        {sparkline ? <div className="shrink-0">{sparkline}</div> : null}
      </div>

      {loading ? (
        <Skeleton className="h-7 w-24" />
      ) : (
        <div className="flex items-baseline gap-2">
          <p className="text-xl font-semibold leading-7 tnum">{value}</p>
          {changeValue !== undefined && changeValue !== null ? (
            <p
              className={cn(
                "text-xs font-medium tnum",
                dir === "up" && "text-positive",
                dir === "down" && "text-negative",
                dir === "flat" && "text-muted-foreground",
              )}
            >
              <span aria-hidden="true">{directionGlyph(changeValue)} </span>
              {changeLabel ?? ""}
              <span className="sr-only">{describeChange(changeValue)}</span>
            </p>
          ) : null}
        </div>
      )}

      {median !== undefined ? (
        loading ? (
          <Skeleton className="h-4 w-20" />
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>
              <p className="w-fit cursor-help text-xs text-muted-foreground underline decoration-dotted underline-offset-2 tnum">
                Median: {median}
              </p>
            </TooltipTrigger>
            <TooltipContent>{medianExplanation}</TooltipContent>
          </Tooltip>
        )
      ) : null}
    </div>
  );
}
