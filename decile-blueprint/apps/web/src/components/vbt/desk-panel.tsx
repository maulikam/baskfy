"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import { Row, Table, Td, Th } from "@/components/desk/ui";
import { useIsStaff } from "@/components/shell/staff-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DeskError } from "@/lib/desk-actions/client";
import {
  SCAN_IN_FLIGHT,
  confirmVbtLine,
  fetchVbtDesk,
  fetchVbtScanRun,
  rescanVbt,
  type VbtDeskLine,
  type VbtDeskView,
} from "@/lib/vbt/desk";

const QUERY_KEY = ["desk", "vbt", "data"] as const;
const REFETCH_MS = 30_000;

function errorText(error: unknown): string {
  if (error instanceof DeskError) return `${error.status} — ${error.message}`;
  return error instanceof Error ? error.message : String(error);
}

function countdown(expiresAt: string | null, now: number): string {
  if (!expiresAt) return "no expiry";
  const left = Math.trunc((Date.parse(expiresAt) - now) / 1000);
  if (left <= 0) return "expired — rebuild the plan";
  return `${Math.floor(left / 60)}m ${left % 60}s left`;
}

function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

function StatusBar({ v, onRefresh }: { v: VbtDeskView; onRefresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [runId, setRunId] = useState<number | null>(null);

  const run = useQuery({
    queryKey: ["desk", "vbt", "scan", runId],
    queryFn: () => fetchVbtScanRun(runId ?? 0),
    enabled: runId !== null,
    refetchInterval: (query) =>
      query.state.data && !SCAN_IN_FLIGHT.includes(query.state.data.status) ? false : 5_000,
  });
  const runStatus = run.data?.status ?? null;
  useEffect(() => {
    if (runStatus && !SCAN_IN_FLIGHT.includes(runStatus)) onRefresh();
  }, [runStatus, onRefresh]);

  async function rescan() {
    setBusy(true);
    setMessage(null);
    try {
      const out = await rescanVbt();
      if (out.accepted) {
        setRunId(out.run_id);
        setMessage("queued — the worker picks it up within a minute");
      } else {
        setMessage(
          `${out.reason}${out.retry_after_seconds ? ` (${out.retry_after_seconds}s)` : ""}`,
        );
      }
    } catch (error) {
      setMessage(`the request failed: ${errorText(error)}`);
    } finally {
      setBusy(false);
    }
  }

  const scan = run.data
    ? { status: run.data.status, session_date: run.data.session_date, detail: run.data.detail, error: run.data.error }
    : v.scan;

  return (
    <div
      className="space-y-2 rounded-xl border border-border/70 bg-card p-4 text-sm"
      data-testid="vbt-desk-status"
    >
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span>
          <b>DRY_RUN</b>=<code>{String(v.desk_dry_run)}</code>
        </span>
        <span>
          <b>BASKFY_VBT_EXECUTION_ENABLED</b>=<code>{String(v.execution_enabled)}</code>
        </span>
        <span>
          <b>DRY_RUN sessions</b>: {v.dry_run_sessions}
          {v.dry_run_sessions_required
            ? ` of ${v.dry_run_sessions_required}`
            : " rehearsed (no paper gate)"}
        </span>
        {v.breadth ? (
          <span className="inline-flex items-center gap-1.5">
            <b>gate</b>
            <Badge variant={v.breadth.gate === "OPEN" ? "positive" : "warning"}>{v.breadth.gate}</Badge>
            {v.breadth.pct_above_dma === null ? "—" : `${Number(v.breadth.pct_above_dma).toFixed(2)}%`} of{" "}
            {v.breadth.measured_count} names above their 200-day average
          </span>
        ) : null}
      </p>
      <p className="text-muted-foreground">
        This sleeve is end-of-day. The plan is built after the close and rebuilt before the open;
        nothing here fires on its own, and there is no auto-confirm for it.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy || (runStatus !== null && SCAN_IN_FLIGHT.includes(runStatus))}
          onClick={() => void rescan()}
          title="Detect the latest published session again. Reads bars, writes signals and breadth, places nothing."
        >
          Re-detect the last session
        </Button>
        <span className="text-muted-foreground" role="status" aria-live="polite">
          {message ? `${message} · ` : ""}
          {scan ? (
            <>
              last: <Badge variant={scan.status === "DONE" ? "positive" : scan.status === "FAILED" ? "negative" : "warning"}>{scan.status}</Badge>{" "}
              {scan.session_date ?? ""}
              {scan.status === "DONE" && scan.detail?.signals !== undefined
                ? ` · ${scan.detail.signals} signals${scan.detail.scan_hits !== undefined ? ` of ${scan.detail.scan_hits} scan hits` : ""}`
                : ""}
              {scan.status === "FAILED" && scan.error ? ` · ${scan.error}` : ""}
              {scan.status === "QUEUED" ? " · waiting for the worker's sweep (up to a minute)" : ""}
            </>
          ) : (
            "never run on this desk"
          )}
        </span>
      </div>
    </div>
  );
}

function ConfirmCell({
  line,
  simulated,
  expired,
  onDone,
}: {
  line: VbtDeskLine;
  simulated: boolean;
  expired: boolean;
  onDone: () => void;
}) {
  const [armed, setArmed] = useState(false);
  const [sent, setSent] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);

  async function send() {
    if (sent) return;
    setSent(true);
    try {
      const out = await confirmVbtLine(line.plan_id, line.id);
      setOutcome(`${out.status}${out.reason ? ` — ${out.reason}` : ""}`);
    } catch (error) {
      setOutcome(`not confirmed: ${errorText(error)}. Reload before trying again.`);
    } finally {
      setArmed(false);
      onDone();
    }
  }

  if (!line.confirmable || expired) {
    return (
      <span className="text-muted-foreground">
        {line.expired || expired ? "plan expired" : line.state.toLowerCase()}
        {outcome ? <span className="block text-xs">{outcome}</span> : null}
      </span>
    );
  }
  return (
    <div className="flex flex-col gap-1 whitespace-normal">
      {!armed ? (
        <Button
          type="button"
          size="sm"
          variant={simulated ? "outline" : "destructive"}
          disabled={sent}
          onClick={() => setArmed(true)}
          title={line.label}
        >
          Confirm
        </Button>
      ) : (
        <span className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs">
            {simulated ? "Simulate" : "Send live"}: {line.label} · {line.quantity} {line.symbol}?
          </span>
          <Button
            type="button"
            size="sm"
            variant={simulated ? "secondary" : "destructive"}
            disabled={sent}
            onClick={() => void send()}
          >
            {simulated ? "Send simulated" : "Send live order"}
          </Button>
          <Button type="button" size="sm" variant="ghost" disabled={sent} onClick={() => setArmed(false)}>
            Cancel
          </Button>
        </span>
      )}
      <span className="text-xs text-muted-foreground" aria-live="polite">
        {outcome ?? ""}
      </span>
    </div>
  );
}

function PlanSection({ v, onDone }: { v: VbtDeskView; onDone: () => void }) {
  const now = useNow();
  const plan = v.plan;
  const planExpired = plan?.expires_at ? Date.parse(plan.expires_at) <= now : false;
  return (
    <section aria-label="Desk plan" className="space-y-3">
      <h3 className="text-sm font-medium">
        Plan{" "}
        {plan ? (
          <span className="font-normal text-muted-foreground">
            {plan.source.toLowerCase()} · session {plan.session_date} · sleeve ₹
            {Number(plan.sleeve_equity_inr ?? 0).toLocaleString("en-IN")} ·{" "}
            {countdown(plan.expires_at, now)}
          </span>
        ) : null}
      </h3>
      {!plan ? (
        <p className="text-sm text-muted-foreground">
          No plan yet. The evening job builds one after the detector runs.
        </p>
      ) : (
        <>
          <Table
            caption="The desk's plan: one confirm per line"
            head={
              <>
                <Th>Line</Th>
                <Th>Symbol</Th>
                <Th align="right">Qty</Th>
                <Th align="right">Limit</Th>
                <Th align="right">Stop</Th>
                <Th align="right">Value</Th>
                <Th>Note</Th>
                <Th>State</Th>
                <Th> </Th>
              </>
            }
          >
            {v.lines.map((line) => (
              <Row key={line.id}>
                <Td>
                  <b>{line.label}</b>
                </Td>
                <Td>{line.symbol}</Td>
                <Td align="right">{line.quantity}</Td>
                <Td align="right">{line.limit_text}</Td>
                <Td align="right">{line.stop_text}</Td>
                <Td align="right">{line.value_text}</Td>
                <Td tone="text-muted-foreground">{line.note ?? ""}</Td>
                <Td>{line.state}</Td>
                <td className="py-2.5 pl-4 pr-3">
                  <ConfirmCell line={line} simulated={v.dry_run} expired={planExpired} onDone={onDone} />
                </td>
              </Row>
            ))}
            {v.lines.length === 0 ? (
              <Row>
                <td colSpan={9} className="py-2.5 pl-4 text-muted-foreground">
                  Nothing to confirm on this plan.
                </td>
              </Row>
            ) : null}
          </Table>

          <h3 className="text-sm font-medium">
            Passed over <span className="font-normal text-muted-foreground">{v.skips.length}</span>
          </h3>
          <Table
            caption="Names the plan passed over, and why"
            head={
              <>
                <Th>Symbol</Th>
                <Th>Reason</Th>
                <Th>Detail</Th>
              </>
            }
          >
            {v.skips.map((skip, index) => (
              <Row key={`${skip.symbol}-${index}`}>
                <Td>{skip.symbol}</Td>
                <Td>{skip.reason}</Td>
                <Td tone="text-muted-foreground">{skip.detail ?? ""}</Td>
              </Row>
            ))}
            {v.skips.length === 0 ? (
              <Row>
                <td colSpan={3} className="py-2.5 pl-4 text-muted-foreground">
                  Nothing was passed over.
                </td>
              </Row>
            ) : null}
          </Table>
        </>
      )}
    </section>
  );
}

function WorkingSection({ v }: { v: VbtDeskView }) {
  return (
    <section aria-label="Working orders" className="space-y-3">
      <h3 className="text-sm font-medium">
        Working orders{" "}
        <span className="font-normal text-muted-foreground">
          a limit works three sessions, then it is cancelled
        </span>
      </h3>
      <Table
        caption="Resting limit orders"
        head={
          <>
            <Th>Symbol</Th>
            <Th align="right">Limit</Th>
            <Th align="right">Qty</Th>
            <Th align="right">Filled</Th>
            <Th>Sessions</Th>
            <Th>State</Th>
            <Th>Broker id</Th>
          </>
        }
      >
        {v.working.map((order) => (
          <Row key={order.id}>
            <Td>{order.symbol}</Td>
            <Td align="right">{order.limit_text}</Td>
            <Td align="right">{order.quantity}</Td>
            <Td align="right">{order.filled_quantity}</Td>
            <Td>
              {order.sessions_text}
              {order.expires_tonight ? (
                <Badge variant="warning" className="ml-1.5">
                  cancels tonight
                </Badge>
              ) : null}
            </Td>
            <Td>{order.state}</Td>
            <Td tone="text-muted-foreground">{order.broker_order_id ?? "—"}</Td>
          </Row>
        ))}
        {v.working.length === 0 ? (
          <Row>
            <td colSpan={7} className="py-2.5 pl-4 text-muted-foreground">
              No limit is resting.
            </td>
          </Row>
        ) : null}
      </Table>
    </section>
  );
}

function BookSection({ v }: { v: VbtDeskView }) {
  return (
    <section aria-label="Desk positions" className="space-y-3">
      <h3 className="text-sm font-medium">
        Positions <span className="font-normal text-muted-foreground">what this strategy bought, and nothing else</span>
      </h3>
      <p className="text-sm text-muted-foreground">Exit rule: {v.exit_rule}</p>
      <Table
        caption="Open volume-breakout positions"
        head={
          <>
            <Th>Symbol</Th>
            <Th>Entered</Th>
            <Th align="right">Entry</Th>
            <Th align="right">Qty</Th>
            <Th align="right">Initial stop</Th>
            <Th align="right">Stop</Th>
            <Th align="right">₹ at risk</Th>
            <Th>GTT</Th>
            <Th>Tomorrow</Th>
          </>
        }
      >
        {v.book.map((position) => (
          <Row key={position.id}>
            <Td>
              {position.symbol}
              {position.simulated ? (
                <Badge variant="warning" className="ml-1.5">
                  simulated
                </Badge>
              ) : null}
            </Td>
            <Td>{position.entry_date ?? "—"}</Td>
            <Td align="right">{position.entry_text}</Td>
            <Td align="right">{position.quantity_open}</Td>
            <Td align="right">{position.initial_stop_text}</Td>
            <Td align="right">{position.stop_text}</Td>
            <Td align="right">{position.risk_text}</Td>
            <Td>
              {position.naked ? <Badge variant="negative">naked</Badge> : String(position.gtt_id ?? "")}
            </Td>
            <Td tone={position.sells_tomorrow ? "" : "text-muted-foreground"}>
              {position.sells_tomorrow ? "sells at the open" : "hold"}
            </Td>
          </Row>
        ))}
        {v.book.length === 0 ? (
          <Row>
            <td colSpan={9} className="py-2.5 pl-4 text-muted-foreground">
              No positions.
            </td>
          </Row>
        ) : null}
      </Table>
    </section>
  );
}

/**
 * The desk console for VBT, staff only. Reads the desk view; every action is the desk's own
 * same-origin route. A line is confirmed one at a time, on two clicks, with the desk's plan_id.
 */
export function VbtDeskPanel() {
  const isStaff = useIsStaff();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: fetchVbtDesk,
    enabled: isStaff,
    refetchInterval: REFETCH_MS,
  });
  const refresh = useCallback(
    () => void queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
    [queryClient],
  );

  if (!isStaff) return null;
  const v = query.data;

  return (
    <section aria-label="Desk console" className="space-y-4" data-testid="vbt-desk-panel">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Desk console
        </h2>
        {v ? (
          <Badge variant={v.dry_run ? "warning" : "negative"}>{v.dry_run ? "SIMULATED" : "LIVE"}</Badge>
        ) : null}
      </div>
      {query.isError ? (
        <p className="text-sm text-negative" role="status">
          The desk did not answer: {errorText(query.error)}
        </p>
      ) : !v ? (
        <p className="text-sm text-muted-foreground">Reading the desk…</p>
      ) : (
        <>
          <StatusBar v={v} onRefresh={refresh} />
          {!v.available ? (
            <p className="text-sm text-negative" role="status">
              Unavailable: {v.reason || "this desk has no vb_ tables"}
            </p>
          ) : (
            <>
              <PlanSection v={v} onDone={refresh} />
              <WorkingSection v={v} />
              <BookSection v={v} />
            </>
          )}
        </>
      )}
    </section>
  );
}
