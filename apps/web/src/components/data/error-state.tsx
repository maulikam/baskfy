"use client";

import type { ProblemOut } from "@baskfy/api-client";
import { TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { problemOf } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

/**
 * Renders an RFC 9457 problem document — docs/07 §"Error catalogue".
 *
 * The API answers `application/problem+json` for every failure, with a `type` from a fixed
 * catalogue and a human `detail`. Showing that `detail` beats any message this component could
 * invent, because the server knows which of the eight things went wrong and the client does not.
 * A 402 additionally carries `upgrade_url`, so the paywall's call to action comes from the
 * response rather than from a hard-coded route.
 */
export interface ErrorStateProps {
  error: unknown;
  onRetry?: (() => void) | undefined;
  className?: string | undefined;
}

const FALLBACK_TITLE = "Something went wrong";
const FALLBACK_DETAIL =
  "The request did not complete. If it keeps happening, quote the request id from the response header.";

function upgradeUrl(problem: ProblemOut): string | null {
  const value = (problem as Record<string, unknown>).upgrade_url;
  return typeof value === "string" ? value : null;
}

export function ErrorState({ error, onRetry, className }: ErrorStateProps) {
  const problem = problemOf(error);
  const title = problem?.title ?? FALLBACK_TITLE;
  const detail = problem?.detail ?? FALLBACK_DETAIL;
  const upgrade = problem ? upgradeUrl(problem) : null;

  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-start gap-3 rounded-md border border-negative/40 bg-negative-muted/60 p-4",
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-negative" />
        <div className="space-y-1">
          <p className="text-sm font-medium text-foreground">{title}</p>
          <p className="text-sm text-muted-foreground">{detail}</p>
        </div>
      </div>
      <div className="flex gap-2">
        {upgrade ? (
          <Button variant="primary" size="sm" asChild>
            <a href={upgrade}>See plans</a>
          </Button>
        ) : null}
        {onRetry ? (
          <Button variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        ) : null}
      </div>
    </div>
  );
}
