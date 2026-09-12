"use client";

import type { StatusOut } from "@baskfy/api-client";
import { useQuery } from "@tanstack/react-query";
import { CircleAlert, Database } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { browserApi } from "@/lib/api/browser";
import { ApiError } from "@/lib/api/errors";
import { formatTradeDate } from "@/lib/format";
import { isMarketOpen } from "@/lib/market/session";
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
 *
 * WIDENED 9 Sep 2026, from 9.5rem. The widest content is no longer a bare date: an open market
 * appends "· market open" (see `behindToday` below). The number is sized to the longest string
 * the pill can now hold — `Data: 8 Sep 2026 · market open` — because a fixed box that its own
 * content overflows is worse than the reflow it was written to prevent, and every state still
 * reserves the same box so the criterion holds.
 */
const PILL_BOX = "h-7 w-[15rem]";

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

  // Three states, not two (M76). A run that is still going is neither published nor failed, and
  // reading `degraded` alone told Maulik "the last pipeline run did not publish" while the night
  // was mid-flight. `running` wins over `degraded`: the failure it would otherwise report is the
  // PREVIOUS run, and blaming a finished failure while its replacement is working is the more
  // misleading of the two. The nightly takes about an hour, so this shows for a long time.
  const running = data.pipeline_running === true;
  const degraded = data.degraded && !running;

  // "WHY DOES IT SAY YESTERDAY?" — asked three times in a week, and the pill's wording was the
  // reason (9 Sep 2026).
  //
  // `as_of` is the last COMPLETED session, and during an open market that is necessarily
  // yesterday: today's daily bar does not exist until today ends. The pill was right and read
  // as "this whole product is a day stale", which is false — the portfolio's marks and the
  // swing book's setups are live from Kite quotes while this shows 8 Sep.
  //
  // So when the market is open the pill says so. It deliberately does NOT claim "everything
  // here is live": baskets, factors and market health are end-of-day by design (house rules 5
  // and 7), and a blanket claim would be the same over-reach in the other direction. The
  // tooltip carries the distinction.
  const marketOpen = isMarketOpen();
  const behindToday = marketOpen && !running && !degraded;

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
          {running ? (
            <span data-testid="pipeline-running" className="text-muted-foreground">
              {" "}
              · updating…
            </span>
          ) : null}
          {behindToday ? (
            <span data-testid="market-open" className="text-muted-foreground">
              {" "}
              · market open
            </span>
          ) : null}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {running
          ? `A pipeline run is in progress. Until it publishes you are seeing the ${formatTradeDate(data.as_of)} trading session.`
          : degraded
            ? `The last pipeline run did not publish. You are seeing the ${formatTradeDate(data.as_of)} trading session.`
            : behindToday
              ? `Published for the ${formatTradeDate(data.as_of)} trading session — the last one that has closed. Today's publishes after the market closes. Portfolio marks are close, plus live overlay when a Kite session exists; the swing desk's setups are live from Kite; baskets, factors and market health are end-of-day by design.`
              : `Published for the ${formatTradeDate(data.as_of)} trading session.`}
      </TooltipContent>
    </Tooltip>
  );
}
