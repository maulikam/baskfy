import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { PipelineRuns } from "@/components/admin/pipeline-runs";
import { serverApi } from "@/lib/api/server";

/**
 * `/admin/pipeline` — the path docs/09 §Observability names by hand:
 *
 *     "`pipeline_run_step` is the operator UI; expose it at `/admin/pipeline` behind staff auth."
 *
 * Run history, per-step detail, and the re-run button PROMPTS.md Prompt 17 §4 asks for.
 */
export const metadata: Metadata = {
  title: "Pipeline runs",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function AdminPipelinePage() {
  const api = await serverApi();
  const runs = await api.GET("/api/v1/admin/pipeline/runs", {
    params: { query: { limit: 60 } },
  });
  if (!runs.data && runs.response.status === 404) notFound();

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-xl font-medium">Pipeline runs</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          docs/03&rsquo;s ten steps, newest first. A failed run does <strong>not</strong> bump{" "}
          <code className="font-mono">data_version</code> — the site keeps serving the previous
          snapshot with a staleness banner, which is docs/11 §Reliability&rsquo;s graceful
          degradation. Re-running is safe: every step upserts.
        </p>
      </header>
      <PipelineRuns runs={runs.data?.data ?? []} />
    </div>
  );
}
