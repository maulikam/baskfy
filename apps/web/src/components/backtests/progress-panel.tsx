"use client";

import { useEffect, useState } from "react";

import { type ProgressFrame, subscribeToProgress } from "@/lib/backtests/queries";

/**
 * docs/08 §Routes marks `/backtests/[id]` "client + polling/SSE", and PROMPTS.md Prompt 15 §4
 * asks for "progress events streamed to the client over SSE".
 *
 * Both are here and they do different jobs. The **stream** moves this bar; the **poll** (in
 * `useBacktest`) is what fetches the finished result and what keeps the page correct if the
 * stream never connects — behind a proxy that buffers `text/event-stream`, say. So a browser with
 * no working stream sees a bar that sits at its last known percentage and a page that still turns
 * into results within one poll interval, rather than an error.
 */
export interface ProgressPanelProps {
  publicId: string;
  status: string;
  /** Called when the stream reports a terminal state, so the page can refetch immediately. */
  onFinished: () => void;
}

const STAGE_LABELS: Record<string, string> = {
  queued: "Waiting for a worker",
  screening: "Running the screen on each rebalance date",
  simulating: "Walking the trading calendar",
  fragility: "Re-running under four perturbations",
  running: "Running",
  done: "Finished",
  failed: "Failed",
};

export function ProgressPanel({ publicId, status, onFinished }: ProgressPanelProps) {
  const [frame, setFrame] = useState<ProgressFrame | null>(null);

  useEffect(() => {
    if (status === "done" || status === "failed") return undefined;
    const stop = subscribeToProgress(publicId, (next) => {
      setFrame(next);
      if (next.status === "done" || next.status === "failed") onFinished();
    });
    return stop;
  }, [publicId, status, onFinished]);

  const percent = frame?.percent ?? 0;
  const stage = frame?.stage ?? status;

  return (
    <section
      className="space-y-3 rounded-lg border border-border bg-card p-4"
      aria-labelledby="backtest-progress"
    >
      <h2 id="backtest-progress" className="text-sm font-semibold">
        {STAGE_LABELS[stage] ?? stage}
      </h2>
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-labelledby="backtest-progress"
        className="h-2 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-500 motion-reduce:transition-none"
          style={{ width: `${percent}%` }}
        />
      </div>
      <p className="text-sm text-muted-foreground">
        {frame === null
          ? "Queued. This page updates itself — nothing to click."
          : `${percent}%${frame.as_of ? ` · ${frame.as_of}` : ""}${
              frame.total > 1 ? ` · ${frame.completed} of ${frame.total}` : ""
            }`}
      </p>
      {frame?.detail ? <p className="text-sm text-destructive">{frame.detail}</p> : null}
    </section>
  );
}
