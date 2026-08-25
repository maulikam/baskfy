"use client";

import type { FactorOut, ScreenDefinition, ScreenRunResponse, UniverseOut } from "@baskfy/api-client";

import { Skeleton } from "@/components/ui/skeleton";
import { buildStorySentence, computeStoryStats } from "@/lib/screens/story";
import { cn } from "@/lib/utils";

export interface StoryStripProps {
  definition: ScreenDefinition;
  result: ScreenRunResponse | undefined;
  universes: readonly UniverseOut[];
  factors: readonly FactorOut[];
  isPending?: boolean;
  className?: string | undefined;
}

function Tile({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string | undefined;
}) {
  return (
    <div className="vaaya-stat min-w-0 px-4 py-3">
      <p className="vaaya-eyebrow text-[10px]">{label}</p>
      <p className="mt-1 truncate text-sm font-medium tabular-nums" title={hint ?? value}>
        {value}
      </p>
    </div>
  );
}

/** Zero-click insight strip between title and table (§1.2). */
export function StoryStrip({
  definition,
  result,
  universes,
  factors,
  isPending = false,
  className,
}: StoryStripProps) {
  const sentence = buildStorySentence({
    definition,
    resultCount: result?.result_count,
    universes,
    factors,
  });
  const stats = computeStoryStats(result);

  return (
    <section
      aria-label="Screen story"
      data-testid="story-strip"
      className={cn("space-y-4", className)}
    >
      <p className="vaaya-eyebrow">What this ranks</p>
      {isPending ? (
        <Skeleton className="h-6 w-full max-w-3xl rounded-full" />
      ) : (
        <p
          className="vaaya-display max-w-3xl text-lg leading-snug text-foreground sm:text-xl"
          data-testid="story-sentence"
        >
          {sentence}
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {isPending ? (
          <>
            <Skeleton className="h-14" />
            <Skeleton className="h-14" />
            <Skeleton className="h-14" />
            <Skeleton className="h-14" />
          </>
        ) : (
          <>
            <Tile
              label="Top pick today"
              value={
                stats.topPickSymbol
                  ? stats.topPickScore
                    ? `${stats.topPickSymbol} · ${stats.topPickScore}`
                    : stats.topPickSymbol
                  : "—"
              }
            />
            <Tile label="Best 1-yr return" value={stats.bestReturnText ?? "—"} />
            <Tile label="Median bumpiness" value={stats.medianVolText ?? "—"} />
            <Tile label="Results as of" value={stats.asOfText} />
          </>
        )}
      </div>
    </section>
  );
}
