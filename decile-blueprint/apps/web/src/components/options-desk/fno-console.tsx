"use client";

import type { ReactNode } from "react";

import { Notice, Row, Table, Td, Th } from "@/components/desk/ui";
import { Badge } from "@/components/ui/badge";

import {
  d,
  fetchFno,
  hhmm,
  inr,
  signTone,
  type FnoAction,
  type FnoFuture,
  type FnoPlan,
  type FnoSpread,
  type FnoStructure,
  type LastMark,
} from "./api";
import { ConfirmAction } from "./confirm-action";
import { useDeskView } from "./use-desk-view";

function statusVariant(status: string): "positive" | "warning" | "negative" {
  if (status === "OPEN" || status === "CLOSED") return "positive";
  if (["ISSUED", "CONFIRMED", "FILLING", "EXITING"].includes(status)) return "warning";
  return "negative";
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3" aria-label={title}>
      <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">{title}</h2>
      {children}
    </section>
  );
}

export function Card({ children }: { children: ReactNode }) {
  return (
    <article className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4 text-sm">
      {children}
    </article>
  );
}

export function Muted({ children }: { children: ReactNode }) {
  return <p className="text-sm leading-relaxed text-muted-foreground">{children}</p>;
}

function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-sm">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="tabular-nums">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function Paper({ on }: { on: boolean }) {
  return on ? <Badge variant="warning">PAPER</Badge> : null;
}

function settle(m: LastMark | null): string {
  return m ? ` · settle ${d(m.trade_date)}: ${d(m.mark_points)} (${inr(m.pnl_inr)})` : "";
}

function planFacts(p: FnoPlan): [string, ReactNode][] {
  const out: [string, ReactNode][] = [];
  if (p.group === "F3") {
    out.push(["Direction", `${d(p.direction)} · level ${d(p.level)} · ${p.symbol} ${d(p.index_last)} at 09:20`]);
    out.push([
      "Credit",
      `${d(p.credit)} pts (priced 09:20) · width ${d(p.width)} pts · target at a mark of ${d(p.decay_target)} · cut at ${d(p.loss_cut)}`,
    ]);
    if (p.rule) out.push(["Rule", p.rule]);
  } else if (p.group === "F1") {
    out.push([
      "Credit",
      <span key="c" className="inline-flex flex-wrap items-center gap-2">
        {`${d(p.credit)} pts (priced 09:20)${p.live_credit !== null ? ` · now ${d(p.live_credit)}` : ""} · scan ${d(p.scan_credit)}`}
        {p.credit_drift_pct !== null && (
          <Badge variant={p.credit_drift_amber ? "warning" : "positive"}>drift {d(p.credit_drift_pct)} %</Badge>
        )}
      </span>,
    ]);
    out.push(["Width", `${d(p.width)} pts · profit take at ${d(p.profit_take)} · loss close at ${d(p.loss_close)}`]);
  } else {
    out.push(["Entry", `${d(p.entry_reference)} (the ask at 09:20) · stop ${d(p.stop)} · GTT trigger ${d(p.gtt_trigger)}`]);
    out.push(["Roll / time exit", `${d(p.roll_date)} / ${d(p.time_exit_date)}`]);
  }
  out.push([
    "Max loss",
    `${inr(p.max_loss_inr)}${p.max_loss_r !== null ? ` = ${d(p.max_loss_r)} R of the ${inr(p.risk_budget_inr)} budget` : ""}`,
  ]);
  out.push([
    "Lots",
    `${p.lots} × ${p.lot_size}${p.lots_sized !== null && p.lots_sized !== p.lots ? ` (sized ${p.lots_sized})` : ""}${p.lots_at_ceiling !== null ? ` · ${p.lots_at_ceiling} at the ceiling` : ""}`,
  ]);
  out.push(["Margin", `basket ${inr(p.margin_required_inr)}${p.margin_message ? ` · ${p.margin_message}` : ""}`]);
  out.push(["Costs", `${inr(p.cost_inr)}${p.cost_share_pct !== null ? ` · ${p.cost_share_pct} of the credit` : ""}`]);
  out.push(["Entry sequence", `${p.entry_sequence} (longs first)`]);
  out.push(["Hard exit", d(p.hard_exit_date)]);
  return out;
}

function PlanCard({ p, onDone }: { p: FnoPlan; onDone: () => void }) {
  return (
    <Card>
      <h3 className="flex flex-wrap items-center gap-2 font-medium">
        {p.sleeve} · {p.symbol} · {p.structure}
        <Badge variant={statusVariant(p.status)}>{p.status}</Badge>
        <Paper on={p.paper} />
      </h3>
      {p.reason && (
        <p>
          <b>{p.status}</b>: {p.reason}
        </p>
      )}
      <Table
        caption={`Legs of ${p.plan_id}`}
        head={
          <>
            <Th>#</Th>
            <Th>Leg</Th>
            <Th>Contract</Th>
            <Th>Side</Th>
            <Th align="right">Qty</Th>
            <Th align="right">Bid</Th>
            <Th align="right">Ask</Th>
            <Th align="right">Spread %</Th>
            <Th align="right">OI</Th>
            <Th align="right">Fill</Th>
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
            <Td align="right">{d(lg.bid)}</Td>
            <Td align="right">{d(lg.ask)}</Td>
            <Td align="right">{d(lg.spread_pct)}</Td>
            <Td align="right">
              {lg.oi ?? "—"}
              {!lg.live && <span className="text-muted-foreground"> (09:20)</span>}
            </Td>
            <Td align="right">{lg.filled ? `${lg.filled} @ ${d(lg.avg_price)}` : "—"}</Td>
          </Row>
        ))}
      </Table>
      <Facts items={planFacts(p)} />
      {p.confirmable && (
        <ConfirmAction
          post={{ path: "/fno/execute", plan_id: p.plan_id }}
          label={p.paper ? "Confirm (paper)" : "Confirm"}
          sentence={p.sentence}
          expiresMs={p.expires_epoch_ms}
          danger={!p.paper}
          onDone={onDone}
        />
      )}
    </Card>
  );
}

function StructureCard({ s }: { s: FnoStructure }) {
  const sigmas = Object.entries(s.short_sigma)
    .map(([role, sg]) => ` · ${role.replaceAll("_", " ").toLowerCase()} ${d(sg)} σ`)
    .join("");
  return (
    <Card>
      <h3 className="flex flex-wrap items-center gap-2 font-medium">
        {s.sleeve} · {s.symbol} · {s.lots} × {s.lot_size} <Paper on={s.simulated} />
      </h3>
      <p className="tabular-nums">
        Credit {d(s.entry_credit)} · cost to close now {d(s.live_close_cost)} · P&amp;L{" "}
        <span className={signTone(s.live_pnl_inr)}>{inr(s.live_pnl_inr)}</span>
        {settle(s.last_mark)}
      </p>
      <p className="tabular-nums">
        To the profit take ({d(s.profit_take)}): {d(s.to_profit_take)} · to the loss close ({d(s.loss_close)}):{" "}
        {d(s.to_loss_close)} · {s.symbol} {d(s.spot)}
        {sigmas}
      </p>
      <Muted>Next: {s.next_rule}</Muted>
    </Card>
  );
}

function SpreadCard({ s }: { s: FnoSpread }) {
  return (
    <Card>
      <h3 className="flex flex-wrap items-center gap-2 font-medium">
        {s.sleeve} · {s.symbol} · {s.direction} · {s.lots} × {s.lot_size}
        {s.added && " · added"} <Paper on={s.simulated} />
      </h3>
      <p className="tabular-nums">
        Level {d(s.level)} · {s.symbol} now {d(s.spot)} · credit {d(s.entry_credit)} · mark now {d(s.mark)} · P&amp;L{" "}
        <span className={signTone(s.live_pnl_inr)}>{inr(s.live_pnl_inr)}</span> · max loss {inr(s.max_loss_inr)}
        {settle(s.last_mark)}
      </p>
      <p className="tabular-nums">
        Target at {d(s.decay_target)} · cut at {d(s.loss_cut)} · expiry {s.expiry}
        {s.legs.map(
          (lg) =>
            ` · ${(lg.role ?? "").replaceAll("_", " ").toLowerCase()} ${d(lg.symbol)} × ${d(lg.quantity ?? null)} @ ${d(lg.avg_price)}${lg.mid !== null ? ` (mid ${d(lg.mid)})` : ""}`,
        )}
      </p>
      <Muted>Next: {s.next_rule}</Muted>
    </Card>
  );
}

function FutureCard({ f }: { f: FnoFuture }) {
  return (
    <Card>
      <h3 className="flex flex-wrap items-center gap-2 font-medium">
        {f.symbol} · {f.contract} · {f.lots} × {f.lot_size} <Paper on={f.simulated} />
      </h3>
      <p className="tabular-nums">
        Entry {d(f.entry_price)} · last {d(f.last)} · P&amp;L{" "}
        <span className={signTone(f.live_pnl_inr)}>{inr(f.live_pnl_inr)}</span> · rolls {d(f.rolls)}
      </p>
      {f.gtt_resting ? (
        <p className="tabular-nums">
          GTT <code>{f.gtt_id}</code> · trigger {d(f.gtt_trigger)} · trail stop {d(f.trail_stop)}
        </p>
      ) : (
        <Badge variant="negative" className="self-start">
          NO RESTING GTT — the monitor exits this future at its next check (NAKED_FUTURE)
        </Badge>
      )}
      <Muted>Next: {f.next_rule}</Muted>
    </Card>
  );
}

function ActionRow({ a, onDone }: { a: FnoAction; onDone: () => void }) {
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-2 font-medium">
        {a.kind} · {d(a.symbol)} · {d(a.code)}
        <Badge variant={statusVariant(a.status)}>{a.status}</Badge>
        {a.awaits_click && a.status === "ISSUED" && <Badge variant="warning">WAITS FOR YOUR CLICK</Badge>}
      </div>
      <Muted>
        Plan {a.plan_id}
        {a.parent_plan_id ? ` · under ${a.parent_plan_id}` : ""}
        {a.message || a.reason ? ` · ${a.message ?? a.reason}` : ""}
      </Muted>
      {a.confirmable && (
        <ConfirmAction
          post={{ path: "/fno/execute", plan_id: a.plan_id }}
          label={a.kind === "EXIT" ? "Send the exit (paper)" : "Add (paper)"}
          sentence={a.sentence}
          expiresMs={a.expires_epoch_ms}
          onDone={onDone}
        />
      )}
    </Card>
  );
}

export function FnoConsole() {
  const { view, error, readAt, refresh } = useDeskView(fetchFno);
  const done = () => void refresh();

  if (!view) {
    return error ? <Notice>The desk did not answer: {error}</Notice> : <Muted>Reading the desk…</Muted>;
  }
  const plans = view.plans;
  const structures = view.structures ?? [];
  const spreads = view.spreads ?? [];
  const futures = view.futures ?? [];
  const actions = view.actions ?? [];
  const violations = view.violations ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center gap-2">
        {Object.entries(view.modes ?? {}).map(([sleeve, mode]) => (
          <Badge key={sleeve} variant={mode === "LIVE" ? "negative" : "warning"}>
            {sleeve} {mode}
          </Badge>
        ))}
        <span className="text-xs text-muted-foreground">
          Live from the desk · read {readAt ? hhmm(readAt.toISOString()) : "—"} · refreshes every 30 s
        </span>
      </div>
      {!view.ok && <Notice>Unavailable: {view.error}</Notice>}
      {error && <Notice>The last refresh failed: {error}. Showing the previous read.</Notice>}
      {view.live_note && <Muted>{view.live_note}</Muted>}
      <Muted>
        Every leg goes through the desk&apos;s gateway; a PAPER sleeve fills by walking the live depth (simulated).
        Nothing here moves anything except a confirm, and each confirm is a single POST to the desk with the
        plan&apos;s own id.
      </Muted>

      <Section title="Plans today">
        {plans.length === 0 && <Muted>No FO plan has been issued today.</Muted>}
        {plans.map((p) => (
          <PlanCard key={p.plan_id} p={p} onDone={done} />
        ))}
      </Section>

      <Section title="Open structures">
        {structures.length === 0 && <Muted>No condor is open.</Muted>}
        {structures.map((s) => (
          <StructureCard key={s.id} s={s} />
        ))}
      </Section>

      <Section title="Open spreads (F3)">
        <Muted>
          Auto-exit{" "}
          {view.f3_auto_exit
            ? "ON — the monitor sends a level break, the cut, the target and the hard exit itself (BASKFY_FNO_F3_AUTO_EXIT)"
            : "OFF — the monitor raises the exit and waits for your click below (BASKFY_FNO_F3_AUTO_EXIT)"}
          . An add is always a click.
        </Muted>
        {spreads.length === 0 && <Muted>No spread is open.</Muted>}
        {spreads.map((s) => (
          <SpreadCard key={s.id} s={s} />
        ))}
      </Section>

      <Section title="Open futures">
        {futures.length === 0 && <Muted>No future is open.</Muted>}
        {futures.map((f) => (
          <FutureCard key={f.id} f={f} />
        ))}
      </Section>

      <Section title="Exits and rolls">
        {actions.length === 0 && <Muted>None today.</Muted>}
        {actions.map((a) => (
          <ActionRow key={a.plan_id} a={a} onDone={done} />
        ))}
        {violations.length > 0 && (
          <div className="flex flex-col gap-1">
            <h3 className="font-medium">Paper-checklist violations</h3>
            <ul className="list-disc pl-5 text-sm">
              {violations.map((x, i) => (
                <li key={`${x.position_id}-${i}`}>
                  <b>{x.code}</b> · {x.symbol}: {x.message}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Section>
    </div>
  );
}
