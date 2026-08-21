"use client";

import { Fragment, useState, useTransition } from "react";
import type { PipelineRunDetailOut } from "@baskfy/api-client";

import { rerunPipeline, type AdminActionResult } from "@/app/actions/admin";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/data/empty-state";
import { cn } from "@/lib/utils";

/**
 * docs/09 §Observability: "`pipeline_run_step` is the operator UI; expose it at `/admin/pipeline`
 * behind staff auth." This is that UI.
 *
 * One row per run, expandable to the ten step rows docs/03 requires each run to write —
 * `status`, `duration_ms`, `rows_in`, `rows_out` and the `error` payload. The step payload is
 * rendered raw, as JSON, on purpose: at 3am the useful thing is the traceback and the failing
 * assertion list verbatim, not a summary of them that has decided in advance what matters.
 *
 * Colour is never the sole carrier of meaning (docs/11 §Accessibility): every status badge shows
 * its word, and the words are the ones in the database.
 */

const STATUS_STYLE: Record<string, string> = {
  succeeded: "bg-positive/10 text-positive",
  failed: "bg-negative/10 text-negative",
  running: "bg-accent text-accent-foreground",
  aborted: "bg-muted text-muted-foreground",
  skipped: "bg-muted text-muted-foreground",
};

function Status({ value }: { value: string }) {
  return (
    <span
      className={cn(
        "inline-flex rounded px-1.5 py-0.5 font-mono text-xs",
        STATUS_STYLE[value] ?? "bg-muted text-muted-foreground",
      )}
    >
      {value}
    </span>
  );
}

function seconds(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** docs/09 §Observability: "publish latency (EOD close → data live)". 15:30 IST is the close. */
function latency(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const hours = Math.floor(value / 3600);
  const minutes = Math.round((value % 3600) / 60);
  return `${hours}h ${String(minutes).padStart(2, "0")}m`;
}

export function PipelineRuns({ runs }: { runs: PipelineRunDetailOut[] }) {
  const [open, setOpen] = useState<number | null>(runs[0]?.id ?? null);
  const [result, setResult] = useState<AdminActionResult | null>(null);
  const [pending, startTransition] = useTransition();

  if (runs.length === 0) {
    return (
      <EmptyState
        title="No pipeline runs yet"
        reason="docs/03's chain writes a run row for every trade date it attempts. Nothing has run against this database."
      />
    );
  }

  return (
    <div className="space-y-4">
      {result ? (
        <p
          role="status"
          className={cn(
            "rounded border px-3 py-2 text-sm",
            result.ok
              ? "border-positive/30 bg-positive/10 text-positive"
              : "border-negative/30 bg-negative/10 text-negative",
          )}
        >
          {result.message}
        </p>
      ) : null}

      <div className="overflow-x-auto rounded border border-border">
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">
            Nightly pipeline run history, newest first, with per-step detail
          </caption>
          <thead>
            <tr className="border-b border-border bg-muted/40 text-left">
              <th scope="col" className="px-3 py-2 font-medium">Trade date</th>
              <th scope="col" className="px-3 py-2 font-medium">Status</th>
              <th scope="col" className="px-3 py-2 font-medium">data_version</th>
              <th scope="col" className="px-3 py-2 font-medium">Publish latency</th>
              <th scope="col" className="px-3 py-2 font-medium">Steps</th>
              <th scope="col" className="px-3 py-2 font-medium">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => {
              const failed = run.steps.filter((step) => step.status === "failed");
              const expanded = open === run.id;
              return (
                // A Fragment with a key, not a bare `<>`: each run renders two sibling rows and
                // React needs the key on the thing that repeats.
                <Fragment key={run.id}>
                  <tr className="border-b border-border last:border-0">
                    <td className="px-3 py-2 font-mono">{run.trade_date}</td>
                    <td className="px-3 py-2"><Status value={run.status} /></td>
                    <td className="px-3 py-2 font-mono tabular-nums">{run.data_version ?? "—"}</td>
                    <td className="px-3 py-2 tabular-nums">{latency(run.publish_latency_seconds)}</td>
                    <td className="px-3 py-2">
                      {run.steps.length}
                      {failed.length > 0 ? (
                        <span className="ml-2 text-negative">
                          {failed.length} failed ({failed.map((s) => s.step).join(", ")})
                        </span>
                      ) : null}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-expanded={expanded}
                        onClick={() => setOpen(expanded ? null : run.id)}
                      >
                        {expanded ? "Hide steps" : "Show steps"}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="ml-2"
                        disabled={pending}
                        onClick={() =>
                          startTransition(async () => {
                            setResult(await rerunPipeline(run.trade_date));
                          })
                        }
                      >
                        Re-run
                      </Button>
                    </td>
                  </tr>
                  {expanded ? (
                    <tr className="border-b border-border last:border-0">
                      <td colSpan={6} className="bg-muted/20 px-3 py-3">
                        <table className="w-full border-collapse text-xs">
                          <caption className="sr-only">Steps of run {run.id}</caption>
                          <thead>
                            <tr className="text-left text-muted-foreground">
                              <th scope="col" className="py-1 font-medium">Step</th>
                              <th scope="col" className="py-1 font-medium">Status</th>
                              <th scope="col" className="py-1 font-medium">Duration</th>
                              <th scope="col" className="py-1 font-medium">rows in</th>
                              <th scope="col" className="py-1 font-medium">rows out</th>
                            </tr>
                          </thead>
                          <tbody>
                            {run.steps.map((step) => (
                              <tr key={step.step} className="align-top">
                                <td className="py-1 font-mono">{step.step}</td>
                                <td className="py-1"><Status value={step.status} /></td>
                                <td className="py-1 tabular-nums">{seconds(step.duration_ms)}</td>
                                <td className="py-1 tabular-nums">{step.rows_in ?? "—"}</td>
                                <td className="py-1 tabular-nums">{step.rows_out ?? "—"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        {run.steps.some((step) => step.detail) ? (
                          <details className="mt-3">
                            <summary className="cursor-pointer text-xs text-muted-foreground">
                              Step payloads (verbatim — tracebacks, gate assertions, notes)
                            </summary>
                            <pre className="mt-2 max-h-96 overflow-auto rounded bg-background p-2 text-[11px] leading-relaxed">
                              {JSON.stringify(
                                Object.fromEntries(
                                  run.steps
                                    .filter((step) => step.detail)
                                    .map((step) => [step.step, step.detail]),
                                ),
                                null,
                                2,
                              )}
                            </pre>
                          </details>
                        ) : null}
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
