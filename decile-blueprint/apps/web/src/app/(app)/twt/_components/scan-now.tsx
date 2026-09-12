"use client";

import { useRouter } from "next/navigation";
import { useActionState, useEffect } from "react";

import { Button } from "@/components/ui/button";
import type { TwtScanRun } from "@/lib/twt/fetch";
import type { TwtScanResult } from "@/lib/twt/write";
import { useTickingNow } from "@/lib/use-ticking-now";
import { cn } from "@/lib/utils";

import type { TwtScanFacts } from "../copy";
import { scanButtonLabel, scanRunLine } from "../copy";

/**
 * "Scan now" on `/twt` — the swing hub's SW15 control, same shape, same restraint.
 *
 * One button bound to the `scanNow` server action, the last run's state beside it, and a refresh
 * while a run is on its way. Polling is `router.refresh()` every few seconds while the last run
 * is queued or running — the same mechanism the portfolio's `LiveRefresh` uses, for the same
 * reason: this page is a server component and the tight names and open positions come from it, so
 * a refresh picks up the new rows and the new header without a second data path in the browser,
 * and without a bearer token ever reaching the tab. It stops the moment the run is done or
 * failed.
 *
 * **Nothing here can place.** The action posts to `/twt/scan`, which queues the detector and
 * answers 202, 409 (one already in flight) or 429 (one a minute) — and the button says which, in
 * words rather than in a status code. `../__tests__/read-only.test.tsx` is the census that keeps
 * it the only action under this tree. It sets no sleeve capital and flips no execution switch
 * either: both are Maulik's alone (`docs/twt/02` §3), and this path cannot reach either one.
 *
 * ACCESSIBILITY, THE FOUR THINGS THAT MATTER HERE
 * ----------------------------------------------
 *  - a real `<button type="submit">` inside a real `<form>`, so it works before hydration and
 *    answers to Enter and Space without a keydown handler;
 *  - the focus ring is the application's, from `globals.css`'s `:focus-visible` — not removed,
 *    not re-invented;
 *  - `disabled` while the action is pending or a run is already in flight, which is also the
 *    honest thing: a second press could only ever earn a 409;
 *  - one `role="status" aria-live="polite"` region, present in every state rather than mounted on
 *    success, so a screen reader hears the sentence change instead of hearing nothing.
 *
 * Colour is the semantic tokens only — `text-negative` for a run that did not finish,
 * `text-muted-foreground` otherwise — so the control is correct in both themes with no palette
 * of its own.
 */
const POLL_MS = 5_000;

type ScanAction = (
  previous: TwtScanResult | null,
  formData: FormData,
) => Promise<TwtScanResult>;

export function ScanNow({
  action,
  lastScan,
  session = null,
  intervalMs = POLL_MS,
}: {
  action: ScanAction;
  lastScan: (TwtScanRun & TwtScanFacts) | null;
  /**
   * The latest session this strategy has published. It is what the line falls back to when no
   * run exists — a page whose rows came from the nightly has been scanned, and saying nothing
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
    <form
      action={formAction}
      className="flex flex-wrap items-center gap-2"
      data-testid="twt-scan-now"
    >
      <Button
        type="submit"
        size="sm"
        variant="outline"
        disabled={pending || inFlight}
        title="Run the detector again on the latest published prices. It changes no money and places nothing."
      >
        {scanButtonLabel(pending, inFlight)}
      </Button>
      <span
        role="status"
        aria-live="polite"
        data-testid="twt-scan-status"
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
