"use client";

import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { useIsStaff } from "@/components/shell/staff-context";

import { Banner, Card, Muted, StaffOnly, useDeskPage } from "./shared";
import { argv, type OpJob } from "./types";

export function OpsJobView({ jobId }: { jobId: string }) {
  const staff = useIsStaff();
  const { data, error } = useDeskPage<{ j: OpJob }>(`/ops/job/${encodeURIComponent(jobId)}`, (d) =>
    d.j.finished_at ? null : 4000,
  );
  if (!staff) return <StaffOnly what="Operations" />;
  if (error && !data) return <Banner tone="bad" tag="unavailable">{error}</Banner>;
  if (!data) return <Muted>Loading job…</Muted>;
  const j = data.j;
  const kv: [string, ReactNode][] = [
    ["Status", <Badge key="s">{j.status}</Badge>],
    ["Operation", j.name],
    ["Command", <code key="c" className="text-xs">{argv(j)}</code>],
    ["Started", j.started_at],
    ["Finished", j.finished_at ?? "still running"],
    ["Duration", j.duration_s ? `${j.duration_s.toFixed(2)}s` : "—"],
    ["Exit code", j.exit_code ?? "—"],
  ];
  return (
    <Card>
      <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-2 text-sm">
        {kv.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="min-w-0 break-words">{v}</dd>
          </div>
        ))}
      </dl>
      {j.output ? (
        <pre className="mt-4 max-h-[40rem] overflow-auto whitespace-pre-wrap rounded bg-muted p-3 text-xs">{j.output}</pre>
      ) : (
        <Muted className="mt-4">No output captured yet.</Muted>
      )}
    </Card>
  );
}
