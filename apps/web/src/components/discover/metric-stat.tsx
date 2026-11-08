import type { DisplayMetric } from "@/lib/discover/metrics";
import { cn } from "@/lib/utils";

/**
 * One figure, with its name, its unit, and the sentence that says what it means.
 *
 * The brief asks for an "explain this metric" interaction. This is it, built on `<details>`
 * rather than a popover component: it needs no JavaScript, it is keyboard-reachable and
 * screen-reader-announced for free, it works before hydration, and it survives inside a server
 * component. A bespoke popover would be more code and less accessible.
 *
 * When the figure is absent the explanation still opens, and the reason it is absent is shown
 * beneath it — "not computed yet" is a fact a reader is owed, not an empty cell to skip over.
 */
export function MetricStat({
  metric,
  className,
  align = "start",
}: {
  metric: DisplayMetric;
  className?: string;
  align?: "start" | "end";
}) {
  return (
    <div
      className={cn("min-w-0", align === "end" && "text-right", className)}
      data-testid="metric-stat"
      data-metric={metric.key}
      data-available={metric.absence ? "false" : "true"}
    >
      <details className="group">
        <summary
          className={cn(
            "eyebrow inline-flex cursor-help list-none items-center gap-1 decoration-dotted underline-offset-2 hover:underline",
            "marker:content-none [&::-webkit-details-marker]:hidden",
          )}
        >
          {metric.label}
          <span aria-hidden="true" className="text-[9px] opacity-60 group-open:opacity-100">
            ⓘ
          </span>
        </summary>
        <p className="mt-1 max-w-[42ch] text-left text-xs leading-relaxed text-muted-foreground">
          {metric.explain}
          {metric.absence ? (
            <>
              {" "}
              <span className="text-foreground/70">{metric.absence.note}</span>
            </>
          ) : null}
        </p>
      </details>
      <div
        className={cn(
          "mt-0.5 text-sm font-medium tabular-nums",
          metric.absence && "text-muted-foreground",
        )}
      >
        {metric.value}
      </div>
    </div>
  );
}
