"use client";

import { useRouter } from "next/navigation";
import { useActionState, useEffect } from "react";

import { Button } from "@/components/ui/button";
import type { SwingScanRun } from "@/lib/swing/fetch";
import type { SwingFormResult } from "@/lib/swing/write";
import { useTickingNow } from "@/lib/use-ticking-now";
import { cn } from "@/lib/utils";

import { scanLine, scanRunLine } from "../copy";

/**
 * "Scan now" — SW15. One button bound to the `scanNow` server action, the last run's state
 * beside it, and a refresh while a run is on its way.
 *
 * Polling is `router.refresh()` every few seconds while `last_scan` is QUEUED or RUNNING — the
 * same mechanism the portfolio's `LiveRefresh` uses, for the same reason: the page is a server
 * component and the rows come from it, so a refresh picks up the new rows and the new header
 * without a second data path in the browser. It stops the moment the run is DONE or FAILED.
 *
 * Nothing here can place: the action posts to `/swing/scan`, which queues the detectors and
 * answers 202, 409 (one already in flight) or 429 (one a minute) — and the button says which.
 */
const POLL_MS = 5_000;

type ScanAction = (
  previous: SwingFormResult | null,
  formData: FormData,
) => Promise<SwingFormResult>;

export function ScanNow({
  action,
  lastScan,
  session = null,
  intervalMs = POLL_MS,
}: {
  action: ScanAction;
  lastScan: SwingScanRun | null;
  /**
   * The latest session the book has published. It is what the line falls back to when no run
   * exists — a book whose setups came from the nightly has been scanned, and saying nothing
   * there was the gap of 12 Sep 2026.
   */
  session?: string | null;
  intervalMs?: number;
}) {
  const router = useRouter();
  const now = useTickingNow();
  const [result, formAction, pending] = useActionState(action, null);
  const inFlight = lastScan?.status === "QUEUED" || lastScan?.status === "RUNNING";

  useEffect(() => {
    if (!inFlight) return undefined;
    const id = window.setInterval(() => {
      if (!document.hidden) router.refresh();
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [inFlight, intervalMs, router]);

  return (
    <form action={formAction} className="flex flex-wrap items-center gap-2" data-testid="scan-now">
      <Button
        type="submit"
        size="sm"
        variant="outline"
        disabled={pending || inFlight}
        title="Run the detectors now. During the session the bar is built from live quotes and every row is labelled provisional. Moves no money."
      >
        {pending ? "Queuing…" : inFlight ? "Scanning…" : "Scan now"}
      </Button>
      <span
        role="status"
        aria-live="polite"
        className={cn(
          "text-xs",
          result?.ok === false || lastScan?.status === "FAILED"
            ? "text-negative"
            : "text-muted-foreground",
        )}
      >
        {result === null
          ? scanRunLine(lastScan, { now, session })
          : result.ok
            ? result.message
            : result.error}
      </span>
    </form>
  );
}

/** The header's line: "provisional — scanned 13:42 IST from live quotes". */
export function ScanLabel({
  provisional,
  scannedAt,
}: {
  provisional: boolean;
  scannedAt: string | null;
}) {
  const line = scanLine(provisional, scannedAt);
  if (!line) return null;
  return (
    <span
      className={cn("text-sm", provisional ? "text-warning" : "text-muted-foreground")}
      data-testid="scan-label"
      title={
        provisional
          ? "Detected on today's bar so far — open, high, low, the last price and the volume so far from Kite. A base that is tight now can be wide by the close; the nightly scan replaces these rows."
          : "Re-detected on demand from published bars."
      }
    >
      {line}
    </span>
  );
}
