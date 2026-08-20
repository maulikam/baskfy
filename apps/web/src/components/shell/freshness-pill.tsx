"use client";

import type { StatusOut } from "@decile/api-client";
import { useQuery } from "@tanstack/react-query";
import { CircleAlert, Database } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { browserApi } from "@/lib/api/browser";
import { ApiError } from "@/lib/api/errors";
import { formatTradeDate } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * docs/08 §"App shell": "data-freshness pill (`Data: 19 Aug 2026`)".
 *
 * Reads `GET /meta/status`, which docs/07 defines as `{ as_of, data_version, last_pipeline_run }`.
 * The `degraded` flag is the one docs/11 §Reliability asks for — "if the pipeline fails, serve the
 * last good `data_version` with a banner" — so a failed nightly run turns the pill amber and says
 * which date is actually being served rather than quietly showing a stale one as if it were fresh.
 *
 * The skeleton is the pill's exact size, so the shell does not reflow when the query settles.
 */
export const STATUS_QUERY_KEY = ["meta", "status"] as const;

/** The publish step runs once a night (docs/03 §Schedule); polling every five minutes is plenty. */
const REFETCH_INTERVAL_MS = 5 * 60_000;

/**
 * A fixed width, shared by the skeleton, the error state and the loaded pill.
 *
 * Prompt 8's fourth acceptance criterion is "No layout shift ... on data load". The pill sits to
 * the left of the theme toggle and the user menu, so a pill that grows from "Data: unavailable"
 * to "Data: 19 Aug 2026" pushes both of them sideways — a measurable CLS on every page load. The
 * widest content it can hold is a full date, so every state reserves that.
 */
const PILL_BOX = "h-7 w-[9.5rem]";

async function fetchStatus(): Promise<StatusOut> {
  const { data, error } = await browserApi().GET("/api/v1/meta/status");
  if (!data) throw ApiError.from(error, "Could not read the data freshness status.");
  return data;
}

export function FreshnessPill({ className }: { className?: string }) {
  const { data, isPending, isError } = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: fetchStatus,
    refetchInterval: REFETCH_INTERVAL_MS,
  });

  if (isPending) return <Skeleton className={cn(PILL_BOX, "rounded-full", className)} />;

  if (isError || !data) {
    return (
      <span
        className={cn(
          PILL_BOX,
          "inline-flex items-center gap-1.5 rounded-full border border-border px-2.5 text-xs text-muted-foreground",
          className,
        )}
      >
        <CircleAlert aria-hidden="true" className="size-3.5" />
        Data: unavailable
      </span>
    );
  }

  const degraded = data.degraded;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            PILL_BOX,
            "inline-flex cursor-help items-center gap-1.5 rounded-full border px-2.5 text-xs tnum",
            degraded
              ? "border-warning/40 bg-warning-muted text-warning"
              : "border-border text-muted-foreground",
            className,
          )}
        >
          {degraded ? (
            <CircleAlert aria-hidden="true" className="size-3.5" />
          ) : (
            <Database aria-hidden="true" className="size-3.5" />
          )}
          Data: {formatTradeDate(data.as_of)}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {degraded
          ? `The last pipeline run did not publish. You are seeing data version ${data.data_version}, from ${formatTradeDate(data.as_of)}.`
          : `Data version ${data.data_version}, published for the ${formatTradeDate(data.as_of)} trading session.`}
      </TooltipContent>
    </Tooltip>
  );
}
