"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";

import { Button } from "@/components/ui/button";
import type {
  FilingsScan,
  FilingsScanActions,
  OverlapScope,
} from "@/lib/overlap/candidates";

/** How often a running scan's progress is read. NSE is read at 1 request a second, two a name. */
const POLL_MS = 4000;

function running(scan: FilingsScan | null): boolean {
  return scan?.state === "queued" || scan?.state === "running";
}

/** The line beside the button: what the last scan did, in this app's words. */
export function scanLine(scan: FilingsScan | null): string | null {
  if (scan === null || scan.state === "idle") return null;
  if (scan.state === "queued") return "Queued — starting shortly.";
  if (scan.state === "running") {
    return scan.total === null || scan.total === undefined
      ? "Reading the names on this page…"
      : `Reading filings: ${scan.done} of ${scan.total} names…`;
  }
  if (scan.state === "failed") return "The last scan failed; nothing on the page changed.";
  const names = scan.total ?? 0;
  const failed = scan.failed?.length ?? 0;
  return (
    `Filings read for ${names} ${names === 1 ? "name" : "names"}` +
    (failed ? ` (${failed} could not be read)` : "") +
    " — Laya tags the new headlines within a minute."
  );
}

/**
 * "Scan filings with Laya" — reads the exchange filings and result dates of every name the
 * table lists for the scope in view, then wakes Laya to tag the new headlines
 * (`POST /overlap/catalyst-scan`). The 09:17 feed covers only watched names and signals, so a
 * swing setup or a tight name shows dashes until this runs.
 *
 * Display context only: it fills the Results and Filing cells and never reaches a rank, a size
 * or an order. While a scan runs its progress is read every few seconds; when it finishes the
 * page is refreshed, so the cells fill from the same read as the rest of the table.
 */
export function FilingsScanButton({
  scope,
  actions,
}: {
  scope: OverlapScope;
  actions: FilingsScanActions;
}) {
  const router = useRouter();
  const [scan, setScan] = useState<FilingsScan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const wasRunning = useRef(false);

  // The last scan's state on load, so a scan started elsewhere (or before a reload) shows; then,
  // while one runs, its progress every few seconds. State is set only from the read's answer.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(
      () => {
        void actions.status().then((result) => {
          if (cancelled || !result.ok) return;
          const finished = wasRunning.current && result.scan.state === "done";
          wasRunning.current = running(result.scan);
          setScan(result.scan);
          if (finished) router.refresh();
          if (running(result.scan)) setTick((value) => value + 1);
        });
      },
      tick === 0 ? 0 : POLL_MS,
    );
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [actions, router, tick]);

  const busy = pending || running(scan);
  const line = error ?? scanLine(scan);

  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="overlap-filings-scan">
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={busy}
        onClick={() =>
          startTransition(async () => {
            setError(null);
            const result = await actions.start(scope);
            if (result.ok) {
              wasRunning.current = true;
              setScan(result.scan);
              setTick((value) => value + 1);
            } else setError(result.error);
          })
        }
      >
        {busy ? "Scanning filings…" : "Scan filings with Laya"}
      </Button>
      {line ? (
        <span
          className="text-xs text-muted-foreground"
          role="status"
          aria-live="polite"
          data-testid="overlap-filings-scan-status"
        >
          {line}
        </span>
      ) : null}
    </div>
  );
}
