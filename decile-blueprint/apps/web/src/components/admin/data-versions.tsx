import type { DataVersionOut } from "@baskfy/api-client";

import { formatDateTimeIST } from "@/lib/format";

/**
 * PROMPTS.md Prompt 17 §4's "data_version history".
 *
 * There is no separate table: `data_version` lives on `pipeline_run` and is set only by docs/03's
 * step 10, on a run whose quality gate passed. So "the history of data_version" and "the runs that
 * published" are the same list, which is what stops the two disagreeing.
 *
 * There is deliberately **no roll-back button.** `data_version` is the gate's output, and a UI
 * that can move it backwards is a UI that can move it forwards over data the gate rejected.
 * `docs/runbooks/bad-data-published.md` has the procedure, and it starts by asking whether you
 * should.
 */
export function DataVersions({ rows, current }: { rows: DataVersionOut[]; current: number }) {
  if (rows.length === 0) {
    return (
      <p className="rounded border border-dashed border-border px-4 py-6 text-sm text-muted-foreground">
        Nothing has published. Until a run passes docs/03&rsquo;s step 9 gate, every analytics
        endpoint answers <code className="font-mono">503 pipeline-degraded</code>.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded border border-border">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Published data versions, newest first</caption>
        <thead>
          <tr className="border-b border-border bg-muted/40 text-left">
            <th scope="col" className="px-3 py-2 font-medium">data_version</th>
            <th scope="col" className="px-3 py-2 font-medium">Trade date</th>
            <th scope="col" className="px-3 py-2 font-medium">Published</th>
            <th scope="col" className="px-3 py-2 font-medium">Latency from 15:30 IST</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.data_version} className="border-b border-border last:border-0">
              <td className="px-3 py-2 font-mono tabular-nums">
                {row.data_version}
                {row.data_version === current ? (
                  <span className="ml-2 rounded bg-accent px-1.5 py-0.5 text-xs text-accent-foreground">
                    current
                  </span>
                ) : null}
              </td>
              <td className="px-3 py-2 font-mono">{row.trade_date}</td>
              <td className="px-3 py-2">
                {row.published_at ? formatDateTimeIST(row.published_at) : "—"}
              </td>
              <td className="px-3 py-2 tabular-nums">
                {row.publish_latency_seconds === null || row.publish_latency_seconds === undefined
                  ? "—"
                  : `${(row.publish_latency_seconds / 3600).toFixed(2)} h`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
