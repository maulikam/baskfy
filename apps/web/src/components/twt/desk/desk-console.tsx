"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Notice, Row, Table, Td, Th } from "@/components/desk/ui";
import { useIsStaff } from "@/components/shell/staff-context";
import { Button } from "@/components/ui/button";
import { DeskError } from "@/lib/desk-actions/client";
import {
  confirmTwtLine,
  fetchTwtDesk,
  fetchTwtScanRun,
  haltTwtSleeve,
  inr,
  outcomeLine,
  queueTwtDeskScan,
  rearmTwtStop,
  reconcileTwtStops,
  riskInr,
  sweepTwtNaked,
  type TwtDeskLine,
  type TwtDeskView,
} from "@/lib/twt/desk-ops";
import { cn } from "@/lib/utils";

import { ConfirmAction } from "./confirm-action";

const POLL_MS = 30_000;

function pctText(v: string | null | undefined): string {
  const n = Number(v);
  return v === null || v === undefined || !Number.isFinite(n) ? "—" : `${n.toFixed(2)}%`;
}

/** The desk's `/twt` console (staff only): exits, entries, skips, the book, and the sleeve's actions. */
export function TwtDeskConsole() {
  const isStaff = useIsStaff();
  const [view, setView] = useState<TwtDeskView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async () => {
    try {
      setView(await fetchTwtDesk());
      setError(null);
    } catch (e) {
      setError(e instanceof DeskError ? `${e.status}: ${e.message}` : "The desk could not be reached.");
    }
    setNow(Date.now());
  }, []);

  useEffect(() => {
    if (!isStaff) return undefined;
    const first = window.setTimeout(() => void load(), 0);
    const id = window.setInterval(() => {
      if (!document.hidden) void load();
    }, POLL_MS);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(id);
    };
  }, [isStaff, load]);

  if (!isStaff) return null;

  if (!view) {
    return (
      <section aria-labelledby="twt-desk-h" className="space-y-3" data-testid="twt-desk">
        <h2 id="twt-desk-h" className="text-lg font-semibold">Desk</h2>
        <p className="text-sm text-muted-foreground">{error ?? "Reading the desk…"}</p>
      </section>
    );
  }

  const expiresAt = view.plan ? Date.parse(view.plan.expires_at) : Number.NaN;
  const planLive = !view.expired && Number.isFinite(expiresAt) && now < expiresAt;
  const live = !view.dry_run;
  const sells = view.lines.filter((l) => l.kind === "SELL_AT_OPEN");

  const confirmCell = (line: TwtDeskLine) =>
    line.confirmable && planLive ? (
      <ConfirmAction
        label="Confirm"
        danger={live}
        testId={`twt-confirm-${line.id}`}
        title={`${live ? "LIVE" : "Simulated"} · ${line.label} · ${line.symbol}`}
        body={
          <>
            <p>
              {line.quantity} {line.symbol}
              {line.value_inr ? ` · ${inr(line.value_inr)}` : ""}
              {line.stop_price ? ` · stop ${inr(line.stop_price)}` : ""}
            </p>
            <p>
              Plan {line.plan_id.slice(0, 8)} · this sends exactly this one line through the sleeve&apos;s
              gateway{live ? ", to the broker." : " (simulated — nothing reaches the broker)."}
            </p>
          </>
        }
        confirmLabel={`${live ? "Send" : "Simulate"} ${line.label}`}
        run={async () => outcomeLine(await confirmTwtLine(line.plan_id, line.id))}
        onDone={() => void load()}
      />
    ) : (
      <span className="text-xs text-muted-foreground">
        {line.expired || !planLive ? "plan expired" : line.state.toLowerCase()}
      </span>
    );

  const lineTable = (rows: TwtDeskLine[], caption: string, ratchet: boolean) => (
    <Table
      caption={caption}
      head={
        <>
          <Th>Line</Th>
          <Th>Symbol</Th>
          <Th align="right">Qty</Th>
          {ratchet ? (
            <>
              <Th align="right">Old trigger</Th>
              <Th align="right">New trigger</Th>
              <Th align="right">High since</Th>
            </>
          ) : (
            <>
              <Th align="right">Value</Th>
              <Th align="right">Stop the fill gets</Th>
            </>
          )}
          <Th>Why</Th>
          <Th> </Th>
        </>
      }
    >
      {rows.map((line) => (
        <Row key={line.id}>
          <Td>{line.label}</Td>
          <Td><b>{line.symbol}</b></Td>
          <Td align="right">{line.quantity}</Td>
          {ratchet ? (
            <>
              <Td align="right">{inr(line.previous_trigger)}</Td>
              <Td align="right"><b>{inr(line.stop_price)}</b></Td>
              <Td align="right">{inr(line.high_since)}</Td>
            </>
          ) : (
            <>
              <Td align="right">{inr(line.value_inr)}</Td>
              <Td align="right">{inr(line.stop_price)}</Td>
            </>
          )}
          <Td tone="text-muted-foreground">{line.note ?? ""}</Td>
          <td className="py-2 pl-4 pr-3">{confirmCell(line)}</td>
        </Row>
      ))}
    </Table>
  );

  return (
    <section aria-labelledby="twt-desk-h" className="space-y-5" data-testid="twt-desk">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="twt-desk-h" className="text-lg font-semibold">Desk</h2>
        <span
          data-testid="twt-desk-mode"
          className={cn(
            "rounded-sm px-2 py-0.5 text-xs font-semibold",
            live ? "bg-negative text-background" : "bg-warning-muted",
          )}
        >
          {live ? "LIVE" : "SIMULATED"}
        </span>
        {error ? <span className="text-xs text-negative">{error}</span> : null}
      </div>

      <div className={cn("rounded-xl border p-3.5 text-sm leading-relaxed", live ? "border-negative/40" : "border-warning/35 bg-warning-muted")}>
        <p>
          DRY_RUN=<code>{String(view.desk_dry_run)}</code> · sleeve switch=<code>{String(view.execution_enabled)}</code>
          {view.plan ? ` · session ${view.plan.session_date} (${view.plan.source})` : ""}
          {view.breadth
            ? ` · gate ${view.breadth.gate}: ${pctText(view.breadth.pct_above_dma)} of ${view.breadth.measured_count} names above their 200-day average`
            : ""}
          {view.session
            ? ` · confirms ${view.session.confirms} · fills ${view.session.fills} · ratchets ${view.session.ratchets}`
            : ""}
          {` · half size left: ${view.first_live_entries_left ?? 0} entries`}
        </p>
        <p className="mt-1 text-muted-foreground">
          End-of-day sleeve: the plan reads the last completed session and expires {view.plan_ttl_minutes} minutes
          after it is built. Every line here is sent one at a time, on Confirm.
        </p>
        {view.countdown ? <p className="mt-1 font-semibold">{view.countdown}</p> : null}
      </div>

      {!view.available ? <Notice>The desk cannot read the TWT tables: {view.reason}</Notice> : null}

      {view.naked.length > 0 ? (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-negative/40 p-3.5 text-sm" data-testid="twt-desk-naked">
          <span>
            <b>{view.naked.length} open line(s) have no resting stop</b>: {view.naked.join(", ")}. Re-arm below, or run
            the sweep.
          </span>
          <ConfirmAction
            label="Run the 15:15 sweep"
            title="Re-arm every naked position"
            body={<p>Arms a stop for each position with none resting. Places no buy, cancels nothing.</p>}
            confirmLabel="Run the sweep"
            run={async () => {
              const r = await sweepTwtNaked();
              return `Re-armed ${r.rearmed} of ${r.naked_before}; ${r.naked} still naked.`;
            }}
            onDone={() => void load()}
          />
        </div>
      ) : null}

      <div className="space-y-2">
        <h3 className="font-semibold">Exits <span className="font-normal text-muted-foreground">— armed first, on purpose</span></h3>
        {view.exits.length || sells.length ? (
          <>
            {view.exits.length ? lineTable(view.exits, "Stops to arm or raise", true) : null}
            {sells.length ? lineTable(sells, "Sells at the open", false) : null}
          </>
        ) : (
          <p className="text-sm text-muted-foreground">No stop to arm and no stop to raise.</p>
        )}
      </div>

      <div className="space-y-2">
        <h3 className="font-semibold">Entries</h3>
        {view.entries.length ? (
          <>
            {lineTable(view.entries, "Entries", false)}
            <p className="text-xs text-muted-foreground">
              At most {view.max_new_entries_per_session} new entries in a session; a further confirm is BLOCKED
              (SESSION_CAP).
            </p>
          </>
        ) : (
          <p className="text-sm text-muted-foreground">No entries in this plan.</p>
        )}
      </div>

      {view.skips.length ? (
        <div className="space-y-2">
          <h3 className="font-semibold">Passed over</h3>
          <Table caption="Skipped names" head={<><Th>Symbol</Th><Th>Reason</Th><Th>Detail</Th></>}>
            {view.skips.map((s, i) => (
              <Row key={`${s.symbol}-${i}`}>
                <Td>{s.symbol}</Td>
                <Td><code>{s.reason}</code></Td>
                <Td tone="text-muted-foreground">{s.detail ?? ""}</Td>
              </Row>
            ))}
          </Table>
        </div>
      ) : null}

      <div className="space-y-2">
        <h3 className="font-semibold">Positions <span className="font-normal text-muted-foreground">— only what this strategy bought</span></h3>
        {view.exit_rule ? <p className="text-xs text-muted-foreground">Exit rule: {view.exit_rule}.{" "}<Link href="/trailing-stops" className="underline underline-offset-2">Trailing stops</Link></p> : null}
        {view.book.length ? (
          <Table
            caption="Strategy positions"
            head={
              <>
                <Th>Symbol</Th>
                <Th>Entered</Th>
                <Th align="right">Qty</Th>
                <Th align="right">Entry</Th>
                <Th align="right">Initial stop</Th>
                <Th align="right">Stop in force</Th>
                <Th align="right">₹ at risk</Th>
                <Th align="right">GTT</Th>
                <Th align="right">High since</Th>
                <Th>Next</Th>
                <Th> </Th>
              </>
            }
          >
            {view.book.map((p) => (
              <Row key={p.id}>
                <Td>
                  <b>{p.symbol}</b>
                  {p.half_size ? <span className="ml-1 text-xs text-muted-foreground">half size</span> : null}
                </Td>
                <Td>{p.entry_date}</Td>
                <Td align="right">{p.quantity_open}</Td>
                <Td align="right">{inr(p.entry_avg)}</Td>
                <Td align="right">{inr(p.initial_stop)}</Td>
                <Td align="right">{inr(p.stop_price)}</Td>
                <Td align="right">{riskInr(p)}</Td>
                <Td align="right" tone={p.gtt_id ? "" : "text-negative font-semibold"}>
                  {p.gtt_id ? inr(p.gtt_trigger) : "NAKED"}
                </Td>
                <Td align="right">{inr(p.high_since)}</Td>
                <Td tone="text-muted-foreground">
                  {p.exit_queued_for
                    ? `sell ${p.exit_queued_for} (${p.exit_reason_queued ?? "exit"})`
                    : p.partial_queued_for
                      ? `partial ${p.partial_quantity ?? ""} ${p.partial_queued_for}`
                      : p.next_trigger
                        ? `${inr(p.next_trigger)} for ${p.next_trigger_for ?? ""}`
                        : `trail ${p.trail ?? ""}`}
                </Td>
                <td className="py-2 pl-4 pr-3">
                  {p.gtt_id === null ? (
                    <ConfirmAction
                      label="Re-arm"
                      title={`Arm a stop for ${p.symbol}`}
                      body={<p>One GTT stop for {p.quantity_open} {p.symbol} at {inr(p.stop_price)}. Places no buy.</p>}
                      confirmLabel="Arm the stop"
                      run={async () => outcomeLine(await rearmTwtStop(p.id))}
                      onDone={() => void load()}
                    />
                  ) : null}
                </td>
              </Row>
            ))}
          </Table>
        ) : (
          <p className="text-sm text-muted-foreground">The strategy holds nothing.</p>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        Sleeve capital {inr(view.config.sleeve_capital_inr)} · equity {inr(view.money.equity_inr)} · cash available{" "}
        {inr(view.money.cash_available_inr)}. Sized against this sleeve&apos;s own money, never the account&apos;s.
      </p>

      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
        <DeskScan onDone={() => void load()} />
        <ConfirmAction
          label="Reconcile stops"
          title="Reconcile this strategy's stops with the broker"
          body={<p>Reads the broker&apos;s resting GTTs and attaches each to the position it protects. Places nothing.</p>}
          confirmLabel="Reconcile"
          run={async () => {
            const r = await reconcileTwtStops();
            return `Attached ${r.attached}; ${r.unmatched} unmatched.`;
          }}
          onDone={() => void load()}
        />
        <ConfirmAction
          label="Halt strategy"
          danger
          title="Halt the three-weeks-tight strategy"
          body={
            <p>
              Sets this sleeve&apos;s capital to zero and expires today&apos;s plans, so no new entry can be sent.
              Resting stops are left untouched.
            </p>
          }
          confirmLabel="Halt the strategy"
          run={async () => (await haltTwtSleeve()).message}
          onDone={() => void load()}
        />
      </div>
    </section>
  );
}

function DeskScan({ onDone }: { onDone: () => void }) {
  const [runId, setRunId] = useState<number | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (runId === null) return undefined;
    const id = window.setInterval(() => {
      fetchTwtScanRun(runId)
        .then((run) => {
          setStatus(`Scan ${run.status.toLowerCase()}${run.error ? `: ${run.error}` : ""}`);
          if (run.status !== "QUEUED" && run.status !== "RUNNING") {
            setRunId(null);
            onDone();
          }
        })
        .catch(() => setStatus("Could not read the scan's state."));
    }, 5_000);
    return () => window.clearInterval(id);
  }, [runId, onDone]);

  return (
    <span className="flex items-center gap-2">
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={pending || runId !== null}
        title="Queue the detector on the latest published prices. Moves no money."
        onClick={() => {
          setPending(true);
          queueTwtDeskScan()
            .then((r) => {
              setRunId(r.run_id);
              setStatus("Scan queued");
            })
            .catch((e: unknown) =>
              setStatus(e instanceof DeskError ? `${e.status}: ${e.message}` : "The desk could not be reached."),
            )
            .finally(() => setPending(false));
        }}
      >
        Desk scan
      </Button>
      <span role="status" aria-live="polite" className="text-xs text-muted-foreground">
        {status ?? ""}
      </span>
    </span>
  );
}
