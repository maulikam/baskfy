"use client";

import { type Freshness, freshnessLabel, freshnessTone } from "@/lib/indices/api";
import { cn } from "@/lib/utils";

const TONE = {
  positive: "bg-positive-muted text-positive",
  warning: "bg-warning-muted text-warning",
  negative: "bg-negative-muted text-negative",
  neutral: "bg-muted text-muted-foreground",
} as const;

export function FreshnessChip({
  freshness,
  fetchFailed = false,
  className,
}: {
  freshness: Freshness | null | undefined;
  fetchFailed?: boolean;
  className?: string;
}) {
  const tone = freshnessTone(freshness, fetchFailed);
  return (
    <span
      data-testid="indices-freshness"
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium tabular-nums",
        TONE[tone],
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-1.5 rounded-full bg-current",
          tone === "positive" && "motion-safe:animate-pulse",
        )}
      />
      {freshnessLabel(freshness, fetchFailed)}
    </span>
  );
}
