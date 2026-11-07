"use client";

import { Notice, Row, Table, Td, Th } from "@/components/desk/ui";
import { Badge } from "@/components/ui/badge";

import { d, fetchNifty, hhmm, type NiftyPlan, type NiftyPosition } from "./api";
import { ConfirmAction } from "./confirm-action";
import { Card, Muted, Section } from "./fno-console";
import { useDeskView } from "./use-desk-view";

function expiresMs(iso: string): number | null {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

function PlanCard({ p, onDone }: { p: NiftyPlan; onDone: () => void }) {
  return (
    <Card>
      <h3 className="flex flex-wrap items-center gap-2 font-medium">
        {p.sleeve} · {p.structure}
        <Badge variant={p.status === "ISSUED" ? "warning" : "neutral"}>{p.status}</Badge>
        <span className="text-muted-foreground">({p.session_state})</span>
        {p.paper && <Badge variant="warning">PAPER</Badge>}
      </h3>
      <Table
        caption={`Legs of ${p.plan_id}`}
        head={
          <>
            <Th>#</Th>
            <Th>Leg</Th>
            <Th>Contract</Th>
            <Th>Side</Th>
            <Th align="right">Qty</Th>
          </>
        }
      >
        {p.legs.map((lg) => (
          <Row key={lg.seq}>
            <Td>{lg.seq}</Td>
            <Td>{lg.role}</Td>
            <Td>{lg.symbol}</Td>
            <Td>{lg.side}</Td>
            <Td align="right">{lg.quantity}</Td>
          </Row>
        ))}
      </Table>
      <Muted>Lapses {hhmm(p.expires_at)}</Muted>
      {p.confirmable && (
        <ConfirmAction
          post={{ path: "/nifty-options/execute", plan_id: p.plan_id }}
          label={p.paper ? "Confirm — simulated" : "Confirm"}
          sentence={p.sentence}
          expiresMs={expiresMs(p.expires_at)}
          danger={!p.paper}
          onDone={onDone}
        />
      )}
    </Card>
  );
}

function PositionCard({ pos, onDone }: { pos: NiftyPosition; onDone: () => void }) {
  return (
    <Card>
      <h3 className="font-medium tabular-nums">
        {pos.sleeve} · entry {pos.entry_points} · mark {d(pos.last_mark_points)}
      </h3>
      <Muted>
        Hard exit {hhmm(pos.hard_exit_at)}
        {pos.exit_plan_id ? ` · exiting (${pos.exit_plan_id})` : ""}
      </Muted>
      {!pos.exit_plan_id && (
        <ConfirmAction
          post={{ path: "/nifty-options/close", session_id: pos.session_id }}
          label="Close now"
          sentence="Close this position now, shorts first?"
          expiresMs={null}
          danger
          onDone={onDone}
        />
      )}
    </Card>
  );
}

export function NiftyConsole() {
  const { view, error, readAt, refresh } = useDeskView(fetchNifty);
  const done = () => void refresh();

  if (!view) {
    return error ? <Notice>The desk did not answer: {error}</Notice> : <Muted>Reading the desk…</Muted>;
  }
  return (
    <div className="flex flex-col gap-6">
      <span className="text-xs text-muted-foreground">
        From the desk · read {readAt ? hhmm(readAt.toISOString()) : "—"} · refreshes every 30 s
      </span>
      {!view.ok && <Notice>Unavailable: {view.error}</Notice>}
      {error && <Notice>The last refresh failed: {error}. Showing the previous read.</Notice>}

      <Section title="Plans today">
        {view.plans.length === 0 && <Muted>No plan has been issued today.</Muted>}
        {view.plans.map((p) => (
          <PlanCard key={p.plan_id} p={p} onDone={done} />
        ))}
      </Section>

      <Section title="Open positions">
        {view.positions.length === 0 && <Muted>Nothing open.</Muted>}
        {view.positions.map((pos) => (
          <PositionCard key={pos.session_id} pos={pos} onDone={done} />
        ))}
      </Section>
    </div>
  );
}
