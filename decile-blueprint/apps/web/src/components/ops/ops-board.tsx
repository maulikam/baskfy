"use client";

import Link from "next/link";
import type { Route } from "next";
import { useState } from "react";

import { Row, Table, Td, Th } from "@/components/desk/ui";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { useIsStaff } from "@/components/shell/staff-context";
import { errorText, postDesk } from "@/lib/desk-ops/client";

import { Banner, Card, ConfirmAction, Muted, StaffOnly, useDeskPage } from "./shared";
import { argv, failedSteps, type OpEntry, type OpsView } from "./types";

function statusVariant(status: string): "positive" | "negative" | "warning" | "neutral" {
  if (status === "ok") return "positive";
  if (status === "running" || status === "partial") return "warning";
  if (status === "failed" || status === "error" || status === "timeout") return "negative";
  return "neutral";
}

function OpCard({ entry, busy, onRun }: { entry: OpEntry; busy: boolean; onRun: (values: Record<string, string>) => Promise<void> }) {
  const { op } = entry;
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(op.params.filter((p) => p.kind === "date" && p.default).map((p) => [p.key, p.default ?? ""])),
  );
  const set = (key: string, value: string) => setValues((v) => ({ ...v, [key]: value }));
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-border/70 bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">{op.label}</h3>
        {entry.last ? <Badge variant={statusVariant(entry.last.status)}>{entry.last.status}</Badge> : null}
      </div>
      <Muted>{op.summary}</Muted>
      <div className="flex flex-wrap gap-1.5">
        {op.needs_kite ? <Badge variant="warning">needs login</Badge> : null}
        {op.writes ? <Badge variant="accent">writes</Badge> : <Badge>read only</Badge>}
        {op.long_running ? <Badge>slow</Badge> : null}
        {entry.last ? <Badge variant="outline">last {entry.last.started_at}</Badge> : null}
      </div>
      <code className="block overflow-x-auto rounded bg-muted px-2 py-1 text-xs">{entry.cli}</code>
      <div className="flex flex-wrap items-end gap-3">
        {op.params.map((p) =>
          p.kind === "flag" ? (
            <label key={p.key} className="flex items-center gap-1.5 text-xs">
              <input
                type="checkbox"
                checked={values[p.key] === "true"}
                onChange={(e) => set(p.key, e.target.checked ? "true" : "")}
              />
              {p.label}
            </label>
          ) : p.kind === "upload" ? (
            <label key={p.key} className="flex flex-col gap-1 text-xs">
              {p.label}
              <Select value={values[p.key] ?? ""} onChange={(e) => set(p.key, e.target.value)}>
                <option value="">none</option>
                {entry.scans.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </Select>
            </label>
          ) : p.kind === "date" ? (
            <label key={p.key} className="flex flex-col gap-1 text-xs">
              {p.label}
              <Input
                value={values[p.key] ?? ""}
                placeholder="YYYY-MM-DD"
                className="h-8 w-36"
                onChange={(e) => set(p.key, e.target.value)}
              />
            </label>
          ) : null,
        )}
        <ConfirmAction
          label={busy ? "Busy" : "Run"}
          confirmLabel="Run it"
          disabled={busy}
          variant="primary"
          warning={
            <>
              Run <b>{op.label}</b>
              {op.writes ? " — it writes to the desk's database" : ""}
              {op.needs_kite ? " and uses the broker session" : ""}. It never places an order.
            </>
          }
          onConfirm={() => onRun(Object.fromEntries(Object.entries(values).filter(([, v]) => v !== "")))}
        />
      </div>
    </div>
  );
}

export function OpsBoard() {
  const staff = useIsStaff();
  const { data, error, reload } = useDeskPage<OpsView>("/ops", (d) => (d.running ? 4000 : null));
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);

  if (!staff) return <StaffOnly what="Operations" />;
  if (error && !data) return <Banner tone="bad" tag="unavailable">{error}</Banner>;
  if (!data) return <Muted>Loading operations…</Muted>;

  const c = data.collection;
  const cTone = c.healthy ? "ok" : c.consecutive_failures > 1 || !c.ever_ran ? "bad" : "warn";
  const cTag = !c.ever_ran
    ? "never run"
    : c.healthy
      ? "collection healthy"
      : c.consecutive_failures > 1
        ? `${c.consecutive_failures} failures`
        : "last run failed";

  const run = async (name: string, values: Record<string, string>) => {
    try {
      const res = await postDesk("/ops/run", { op: name, ...values });
      setNotice({ ok: true, text: `Started ${res.message}.` });
    } catch (e) {
      setNotice({ ok: false, text: errorText(e) });
    }
    await reload();
  };

  return (
    <div className="flex flex-col gap-5">
      {data.running ? (
        <Banner tone="warn" tag="running">
          <b>{data.running.name}</b> started {data.running.started_at}. This page refreshes every few seconds;
          operations never run concurrently because they share the database and the broker session.
        </Banner>
      ) : null}
      {notice ? (
        <Banner tone={notice.ok ? "ok" : "bad"} tag={notice.ok ? "started" : "refused"}>
          {notice.text}
        </Banner>
      ) : null}

      <Banner tone={cTone} tag={cTag}>
        {c.message}
        {c.ran_at ? (
          <span className="text-muted-foreground">
            {" "}
            Last attempt {c.ran_at} ({c.trigger})
            {c.stale_days
              ? `; nothing collected for ${c.stale_days} day${c.stale_days === 1 ? "" : "s"}, which cannot be recovered`
              : ""}
            .
          </span>
        ) : null}
      </Banner>

      {data.daily_history.length ? (
        <div className="flex flex-col gap-2">
          <h2 className="text-base font-semibold">Daily collection history</h2>
          <Table
            caption="Daily collection history"
            head={
              <>
                <Th>Ran at</Th>
                <Th>Session</Th>
                <Th>Trigger</Th>
                <Th>Outcome</Th>
                <Th>Steps that failed</Th>
              </>
            }
          >
            {data.daily_history.map((r) => (
              <Row key={r.ran_at}>
                <Td>{r.ran_at}</Td>
                <Td>{r.session_date}</Td>
                <Td>{r.trigger}</Td>
                <Td>
                  <Badge variant={statusVariant(r.outcome)}>{r.outcome}</Badge>
                </Td>
                <Td>{failedSteps(r)}</Td>
              </Row>
            ))}
          </Table>
          <Muted>
            Scheduled runs appear here even though they never touch the buttons below. A run started from this page
            is also recorded in Run history, which tracks processes rather than collection outcomes.
          </Muted>
        </div>
      ) : null}

      <Banner tone="info" tag="how this works">
        Each button runs one <b>allowlisted</b> command with a fixed argument template — there is deliberately no
        free-form command field. Nothing here places an order. Every run is logged below with its exact command and
        full output.
      </Banner>

      {data.groups.map((group) => (
        <section key={group} className="flex flex-col gap-2">
          <h2 className="text-base font-semibold">{group}</h2>
          <div className="grid gap-3 md:grid-cols-2">
            {data.operations
              .filter((e) => e.op.group === group)
              .map((entry) => (
                <OpCard
                  key={entry.op.name}
                  entry={entry}
                  busy={Boolean(data.running)}
                  onRun={(values) => run(entry.op.name, values)}
                />
              ))}
          </div>
        </section>
      ))}

      <Card title="Run history">
        {data.history.length ? (
          <Table
            caption="Run history"
            head={
              <>
                <Th>Started</Th>
                <Th>Operation</Th>
                <Th>Command</Th>
                <Th>Status</Th>
                <Th align="right">Took</Th>
                <Th>Output</Th>
              </>
            }
          >
            {data.history.map((j) => (
              <Row key={j.id}>
                <Td>{j.started_at}</Td>
                <Td>{j.name}</Td>
                <Td>
                  <code className="text-xs">{argv(j)}</code>
                </Td>
                <Td>
                  <Badge variant={statusVariant(j.status)}>{j.status}</Badge>
                  {j.exit_code ? <span className="ml-1 text-xs text-muted-foreground">exit {j.exit_code}</span> : null}
                </Td>
                <Td align="right">{j.duration_s ? `${j.duration_s.toFixed(1)}s` : "—"}</Td>
                <Td>
                  <Link className="text-accent underline-offset-4 hover:underline" href={`/ops/job/${j.id}` as Route}>
                    view
                  </Link>
                </Td>
              </Row>
            ))}
          </Table>
        ) : (
          <Muted>Nothing has been run from here yet. Every run is recorded with its exact command, exit code and full output.</Muted>
        )}
      </Card>

      {data.history[0]?.output ? (
        <Card title={`Last output — ${data.history[0].name}`}>
          <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap text-xs">{data.history[0].output}</pre>
        </Card>
      ) : null}
    </div>
  );
}
