"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";

import { useIsStaff } from "@/components/shell/staff-context";
import { executeDeskBuy, reconcileFills } from "@/lib/opportunities/desk-buy";
import { cn } from "@/lib/utils";

import { ConfirmAction, type ActionResult } from "./desk-actions";
import {
  countdown,
  cutoffSweep,
  fetchSwingDesk,
  inr,
  istStamp,
  istTime,
  num,
  pctText,
  px,
  rearmGtt,
  type SwCatalyst,
  type SwingDeskView,
  type SwLine,
  type SwOutcome,
  type SwPlan,
} from "./desk-data";

/**
 * The desk's swing console (`templates/swing.html`) inside the web `/swing` page. Staff only.
 * Reads `GET /desk/swing/data`; every action is one same-origin desk POST with confirm=true
 * behind a confirm dialog — execute (plan_id + line_id the desk issued), re-arm, reconcile, cutoff.
 */
export function SwingDeskConsole() {
  const isStaff = useIsStaff();
  if (!isStaff) return null;
  return <Console />;
}

function outcomeResult(o: Pick<SwOutcome, "status" | "reason" | "simulated" | "position_id"> & { gtt?: SwOutcome["gtt"] }): ActionResult {
  const bad = o.status === "BLOCKED" || o.status === "REJECTED" || o.status === "EXPIRED";
  const parts = [o.status, o.reason];
  if (o.position_id) parts.push(`position #${o.position_id}`);
  if (o.gtt?.status) parts.push(`GTT ${o.gtt.status}`);
  if (o.simulated) parts.push("(simulated)");
  return { tone: bad ? "bad" : "ok", text: parts.filter(Boolean).join(" · "), retryable: o.status === "BLOCKED" };
}

function H2({ children }: { children: ReactNode }) {
  return <h2 className="text-lg font-medium">{children}</h2>;
}

function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("overflow-x-auto rounded-xl border border-border/70 bg-card p-4", className)}>{children}</div>;
}

function Banner({ tone, tag, children }: { tone: "warn" | "bad" | "info"; tag: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "flex gap-3 rounded-lg border px-4 py-3 text-sm",
        tone === "bad" ? "border-negative/50" : tone === "warn" ? "border-warning/50" : "border-border",
      )}
    >
      <span
        className={cn(
          "h-fit rounded border px-1.5 py-0.5 text-[11px] uppercase tracking-wide",
          tone === "bad" ? "border-negative/50 text-negative" : tone === "warn" ? "border-warning/50 text-warning" : "text-muted-foreground",
        )}
      >
        {tag}
      </span>
      <div className="min-w-0 space-y-1">{children}</div>
    </div>
  );
}

function Th({ children }: { children?: ReactNode }) {
  return <th className="whitespace-nowrap py-2 pr-3 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">{children}</th>;
}

function Td({ children, className, colSpan }: { children?: ReactNode; className?: string; colSpan?: number }) {
  return (
    <td colSpan={colSpan} className={cn("py-2 pr-3 align-top tabular-nums", className)}>
      {children}
    </td>
  );
}

function Catalyst({ c }: { c: SwCatalyst | null | undefined }) {
  if (!c) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {c.url ? (
        <a
          href={c.url}
          target="_blank"
          rel="noopener noreferrer"
          className="underline underline-offset-2"
          title={`NSE filing${c.published_at ? `, ${istStamp(c.published_at)}` : ""} — opens on nseindia.com`}
        >
          {c.headline || "NSE filing"}
        </a>
      ) : null}
      {c.earnings_date ? (
        <span className="rounded border border-warning/50 px-1 text-[11px] text-warning">earnings {c.earnings_date}</span>
      ) : null}
    </span>
  );
}

function Countdown({ expiresAt, now }: { expiresAt: string | null | undefined; now: number }) {
  const c = countdown(expiresAt, now);
  return <span className={cn("tabular-nums", c.expired && "text-negative")}>{c.text}</span>;
}

function Console() {
  const [view, setView] = useState<SwingDeskView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async () => {
    try {
      setView(await fetchSwingDesk());
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not reach the desk.");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchSwingDesk().then(
      (v) => {
        if (!cancelled) setView(v);
      },
      (e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Could not reach the desk.");
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);

  const pollMs = view?.poll_ms ?? 0;
  const scanning = view?.status.scan.in_flight ?? false;
  const opensIn = view?.window_opens_in_ms ?? null;
  useEffect(() => {
    const every = pollMs > 0 ? pollMs : scanning ? 5000 : 0;
    if (every > 0) {
      const t = window.setInterval(() => void load(), every);
      return () => window.clearInterval(t);
    }
    if (opensIn !== null) {
      const t = window.setTimeout(() => void load(), opensIn + 1000);
      return () => window.clearTimeout(t);
    }
    return undefined;
  }, [pollMs, scanning, opensIn, load]);

  const refresh = useCallback(() => {
    window.setTimeout(() => void load(), 1500);
  }, [load]);

  if (!view) {
    return (
      <section aria-label="Swing desk" className="space-y-2">
        <H2>Desk — triggers, plan and positions</H2>
        <p className="text-sm text-muted-foreground">{error ? `The desk could not be read: ${error}` : "Reading the desk…"}</p>
      </section>
    );
  }

  const s = view.status;
  const simulated = s.simulated;
  return (
    <section aria-label="Swing desk" className="space-y-6" data-testid="swing-desk">
      <div className="flex flex-wrap items-center gap-3">
        <H2>Desk — triggers, plan and positions</H2>
        <span
          className={cn(
            "rounded border px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
            simulated ? "border-warning/60 text-warning" : "border-negative/60 text-negative",
          )}
        >
          {simulated ? "Simulated" : "Live"}
        </span>
        {error ? <span className="text-xs text-negative">refresh failed: {error}</span> : null}
      </div>

      <Banner tone={simulated ? "warn" : "bad"} tag="status">
        <p>
          <b>DRY_RUN</b>=<code>{String(s.dry_run)}</code> · <b>BASKFY_SWING_EXECUTION_ENABLED</b>=
          <code>{String(s.execution_enabled)}</code> · <b>monitor</b>: {s.monitor.label} · <b>Kite token</b>:{" "}
          <span className={cn(s.token.expired && "text-negative")}>{s.token.label}</span>
        </p>
        <p className="text-muted-foreground">
          {s.mode_label ? `${s.mode_label}. ` : ""}Session {view.today}
          {s.session.recorded ? ` (${s.session.mode ?? "—"})` : " (no sw_session row yet)"}: signals {s.session.signals} ·
          confirms {s.session.confirms} · fills {s.session.fills} · manage actions {s.session.manage_actions} · monitor ran{" "}
          {s.session.monitor_ran ? "yes" : "no"}.
        </p>
        <p className="text-muted-foreground">
          <b>scan</b>:{" "}
          <span className={cn(s.scan.provisional && "text-warning")}>
            {s.scan.label || `nightly${s.scan.date ? ` (${s.scan.date})` : ""}`}
          </span>
          {s.scan.run_label ? (
            <span className={cn(s.scan.run?.status === "FAILED" && "text-negative")}> · {s.scan.run_label}</span>
          ) : null}
          {s.scan.in_flight ? " · scanning…" : ""}
        </p>
      </Banner>

      {!view.available ? (
        <Banner tone="bad" tag="unavailable">
          <p>The swing positions could not be read: {view.reason}.</p>
        </Banner>
      ) : (
        <>
          {view.sleeve ? (
            <p className="text-sm text-muted-foreground">
              Allocation {inr(view.sleeve.capital_inr)} · risk per trade {pctText(view.sleeve.risk_per_trade_pct)} · position
              cap {pctText(view.sleeve.max_position_pct)} · rung {view.sleeve.exposure_level} · up to{" "}
              {view.sleeve.max_open_positions} positions
              {view.sleeve.first_live_sessions_left > 0
                ? halfRiskLeft(view.sleeve.first_live_sessions_left)
                : ""}
              {!view.sleeve.configured ? <span className="text-negative"> · no sw_config row — schema defaults shown</span> : null}
              {num(view.sleeve.capital_inr) === 0 ? <span className="text-negative"> · allocation is zero: nothing will be sized</span> : null}
            </p>
          ) : null}

          <Triggers view={view} now={now} simulated={simulated} onDone={refresh} />
          <PlanSection view={view} now={now} simulated={simulated} onDone={refresh} />
          <Book view={view} simulated={simulated} onDone={refresh} />
        </>
      )}
    </section>
  );
}

function LineConfirm({ line, now, simulated, onDone }: { line: SwLine; now: number; simulated: boolean; onDone: () => void }) {
  const left = countdown(line.plan_expires_at, now);
  if (!line.confirmable) {
    return <span className="text-xs text-muted-foreground">{line.why_not}</span>;
  }
  return (
    <ConfirmAction
      label={left.expired ? "expired" : "Confirm"}
      title={line.label}
      confirmLabel={`${simulated ? "Send (simulated)" : "Send live order"} · ${line.symbol}`}
      danger={!simulated}
      disabled={left.expired}
      disabledReason="Plan expired"
      run={async () => {
        return outcomeResult(await executeDeskBuy(line.plan_id, line.id));
      }}
      onDone={onDone}
    >
      <p className="font-medium text-foreground">{line.label}</p>
      <p>
        {line.kind} · qty {line.quantity.toLocaleString("en-IN")} · trigger {px(line.trigger)} · stop {px(line.stop)} · risk{" "}
        {inr(line.risk_inr)} · value {inr(line.position_value)}
      </p>
      <p>
        Plan <code>{line.plan_id.slice(0, 8)}</code> · expires in <Countdown expiresAt={line.plan_expires_at} now={now} />
      </p>
      <p>
        One line, sent once through the desk&rsquo;s gateway (guards, risk, rate limit, journal). A buy arms its GTT stop in the
        same request. {simulated ? "The desk is SIMULATED — nothing reaches the broker." : "The desk is LIVE — this sends a real order."}
      </p>
    </ConfirmAction>
  );
}

function Triggers({ view, now, simulated, onDone }: { view: SwingDeskView; now: number; simulated: boolean; onDone: () => void }) {
  return (
    <section aria-label="Triggers" className="space-y-2">
      <h3 className="font-medium">Triggers — today, newest first</h3>
      <Card>
        <table className="w-full min-w-[60rem] border-collapse text-sm">
          <thead>
            <tr className="border-b border-border/70">
              <Th>Time</Th>
              <Th>Symbol</Th>
              <Th>Setup</Th>
              <Th>What happened</Th>
              <Th>Entry</Th>
              <Th>Stop</Th>
              <Th>Line</Th>
              <Th>Plan</Th>
              <Th>Catalyst</Th>
              <Th>Action</Th>
            </tr>
          </thead>
          <tbody>
            {view.triggers.length === 0 ? (
              <tr>
                <Td colSpan={10} className="text-muted-foreground">
                  No signals today
                  {view.status.monitor.state === "not enabled"
                    ? " — the opening-range monitor is not enabled, so the only entries are the plan's waiting buys below"
                    : ""}
                  .
                </Td>
              </tr>
            ) : (
              view.triggers.map((t) => (
                <tr key={t.id} className="border-b border-border/40">
                  <Td>{istTime(t.raised_at, true)}</Td>
                  <Td className="font-medium">
                    {t.focus ? <span className="mr-1 text-warning">★</span> : null}
                    {t.symbol}
                  </Td>
                  <Td>{t.setup}</Td>
                  <Td className={cn("whitespace-normal", t.locked && "text-negative")}>
                    <span className="mr-1 rounded border border-border px-1 text-[11px] uppercase">{t.state.replaceAll("_", " ").toLowerCase()}</span>
                    {t.text}
                  </Td>
                  <Td>{px(t.entry)}</Td>
                  <Td>{px(t.stop)}</Td>
                  <Td className="whitespace-normal">
                    {t.line ? (
                      <>
                        <b>{t.line.label}</b>
                        <span className="block text-xs text-muted-foreground">
                          risk {inr(t.line.risk_inr)} · {inr(t.line.position_value)} · {pctText(t.line.pct_of_sleeve)} of allocation · cap{" "}
                          {pctText(t.line.cap_pct)}
                        </span>
                      </>
                    ) : t.skip ? (
                      <>
                        <span className="text-negative">skipped — {t.skip.reason}</span>
                        {t.skip.detail ? <span className="block text-xs text-muted-foreground">{t.skip.detail}</span> : null}
                      </>
                    ) : (
                      <span className="text-muted-foreground">{t.locked ? "no line — a locked name cannot be bought" : "no line"}</span>
                    )}
                  </Td>
                  <Td>
                    {t.line ? (
                      <>
                        <code>{t.line.plan_id.slice(0, 8)}</code> <Countdown expiresAt={t.line.plan_expires_at} now={now} />
                      </>
                    ) : t.skip ? (
                      <code>{t.skip.plan_id.slice(0, 8)}</code>
                    ) : (
                      "—"
                    )}
                  </Td>
                  <Td className="whitespace-normal text-xs text-muted-foreground">
                    <Catalyst c={t.catalyst} />
                  </Td>
                  <Td>
                    {t.line ? (
                      <LineConfirm line={t.line} now={now} simulated={simulated} onDone={onDone} />
                    ) : t.locked ? (
                      <span className="text-xs text-muted-foreground">locked</span>
                    ) : (
                      "—"
                    )}
                  </Td>
                </tr>
              ))
            )}
          </tbody>
        </table>
        <p className="mt-3 text-xs text-muted-foreground">
          Each Confirm sends one line to the desk, which runs the guards, the risk checks, the rate limit and the journal, then
          places the buy and arms its GTT stop in the same request. A locked-circuit signal has no button. Every line is prefixed
          SWING so it cannot be mistaken for the weekly plan.
        </p>
      </Card>
    </section>
  );
}

function PlanPanel({ plan, title, now, simulated, onDone }: { plan: SwPlan; title: string; now: number; simulated: boolean; onDone: () => void }) {
  return (
    <Card className="space-y-3">
      <p className="text-sm">
        <b>{title}</b> — plan <code>{plan.plan_id}</code> · for {plan.as_of ?? "—"} · built {istStamp(plan.built_at)} · gate{" "}
        <b>{plan.gate}</b> · rung {plan.exposure_level} · risk {inr(plan.total_risk_inr)} · new exposure{" "}
        {inr(plan.total_new_exposure_inr)} · expires in <Countdown expiresAt={plan.expires_at} now={now} />
        {plan.expired ? <span className="text-negative"> expired</span> : null}
      </p>

      <table className="w-full min-w-[48rem] border-collapse text-sm" aria-label={`${title} — exits`}>
        <thead>
          <tr className="border-b border-border/70">
            <Th>Exit</Th>
            <Th>Symbol</Th>
            <Th>Qty</Th>
            <Th>Stop</Th>
            <Th>Note</Th>
            <Th>State</Th>
            <Th>Action</Th>
          </tr>
        </thead>
        <tbody>
          {plan.exits.length === 0 ? (
            <tr>
              <Td colSpan={7} className="text-muted-foreground">No exits — nothing to sell or raise.</Td>
            </tr>
          ) : (
            plan.exits.map((ln) => (
              <tr key={ln.id} className="border-b border-border/40">
                <Td className="font-medium">{ln.label}</Td>
                <Td>{ln.symbol}</Td>
                <Td>{ln.quantity.toLocaleString("en-IN")}</Td>
                <Td>{px(ln.stop)}</Td>
                <Td className="whitespace-normal text-xs text-muted-foreground">
                  {ln.note ?? ""} <Catalyst c={ln.catalyst} />
                </Td>
                <Td>{ln.state}</Td>
                <Td>
                  <LineConfirm line={ln} now={now} simulated={simulated} onDone={onDone} />
                </Td>
              </tr>
            ))
          )}
        </tbody>
      </table>

      <table className="w-full min-w-[60rem] border-collapse text-sm" aria-label={`${title} — buys waiting for a signal`}>
        <thead>
          <tr className="border-b border-border/70">
            <Th>Waiting buy</Th>
            <Th>Symbol</Th>
            <Th>Setup</Th>
            <Th>Qty</Th>
            <Th>Trigger</Th>
            <Th>Stop</Th>
            <Th>Risk</Th>
            <Th>Value</Th>
            <Th>State</Th>
            <Th>Action</Th>
          </tr>
        </thead>
        <tbody>
          {plan.pending.map((ln) => (
            <tr key={ln.id} className="border-b border-border/40">
              <Td className="font-medium">{ln.label}</Td>
              <Td>{ln.symbol}</Td>
              <Td>{ln.setup ?? "—"}</Td>
              <Td className="text-muted-foreground">—</Td>
              <Td>{px(ln.trigger)}</Td>
              <Td className="text-muted-foreground">range</Td>
              <Td className="text-muted-foreground">—</Td>
              <Td className="whitespace-normal text-xs text-muted-foreground">
                {ln.note ?? ""} <Catalyst c={ln.catalyst} />
              </Td>
              <Td>{ln.state}</Td>
              <Td className="text-xs text-muted-foreground">{ln.why_not}</Td>
            </tr>
          ))}
          {plan.buys.map((ln) => (
            <tr key={ln.id} className="border-b border-border/40">
              <Td className="font-medium">{ln.label}</Td>
              <Td>{ln.symbol}</Td>
              <Td>{ln.setup ?? "—"}</Td>
              <Td>{ln.quantity.toLocaleString("en-IN")}</Td>
              <Td>{px(ln.trigger)}</Td>
              <Td>{px(ln.stop)}</Td>
              <Td>{inr(ln.risk_inr)}</Td>
              <Td>
                {inr(ln.position_value)} <span className="text-muted-foreground">({pctText(ln.pct_of_sleeve)})</span>
                {ln.catalyst ? (
                  <span className="block text-xs text-muted-foreground">
                    <Catalyst c={ln.catalyst} />
                  </span>
                ) : null}
              </Td>
              <Td>{ln.state}</Td>
              <Td>
                <LineConfirm line={ln} now={now} simulated={simulated} onDone={onDone} />
              </Td>
            </tr>
          ))}
          {plan.buys.length === 0 && plan.pending.length === 0 ? (
            <tr>
              <Td colSpan={10} className="text-muted-foreground">No buys waiting.</Td>
            </tr>
          ) : null}
        </tbody>
      </table>

      {plan.skips.length > 0 ? (
        <div className="space-y-1 text-sm">
          <p className="text-muted-foreground">
            <b>Skipped</b>, with reasons:
          </p>
          <ul className="grid gap-1">
            {plan.skips.map((k) => (
              <li key={k.id}>
                <b>{k.symbol}</b> <span className="text-negative">{k.reason}</span>
                {k.detail ? <span className="text-muted-foreground"> — {k.detail}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

function PlanSection({ view, now, simulated, onDone }: { view: SwingDeskView; now: number; simulated: boolean; onDone: () => void }) {
  const sent = view.sent_lines ?? [];
  const { morning, preview } = view.plans;
  return (
    <section aria-label="Plan" className="space-y-3">
      <h3 className="font-medium">Plan — exits first, then the buys waiting for a signal, then the skips</h3>
      {view.sleeve?.first_live_header ? (
        <Banner tone="warn" tag="start small">
          <p>
            {view.sleeve.first_live_header} — every new entry is sized at half the configured risk until the countdown ends (SELL
            and RAISE lines are never halved).
          </p>
        </Banner>
      ) : null}
      {sent.length > 0 ? (
        <Banner tone="info" tag="resting">
          <p>
            {sent.length} live buy{sent.length === 1 ? "" : "s"} accepted and not yet complete (
            {sent.map((l) => `${l.symbol} x${l.quantity} ${l.journal_ref ?? ""}`.trim()).join(", ")}).
          </p>
          <div className="flex flex-wrap gap-3">
            <ConfirmAction
              label="Reconcile fills"
              title="Reconcile today's sent swing buys against the broker"
              confirmLabel="Reconcile"
              run={async () => {
                const r = (await reconcileFills()) as { count?: number };
                return { tone: "ok", text: `reconciled ${r.count ?? 0} line(s)` };
              }}
              onDone={onDone}
            >
              <p>Reads each sent buy&rsquo;s order status from the broker and applies fills. Places nothing.</p>
            </ConfirmAction>
            <ConfirmAction
              label="10:45 sweep — cancel remainders"
              title="Cancel the unfilled remainder of today's sent swing buys"
              confirmLabel="Cancel remainders"
              danger
              run={async () => {
                const r = await cutoffSweep();
                return {
                  tone: r.cancel_failed > 0 ? "bad" : "ok",
                  text: `reconciled ${r.reconciled} · cancelled ${r.cancelled} · cancel failed ${r.cancel_failed} · slots freed ${r.slots_freed}`,
                };
              }}
              onDone={onDone}
            >
              <p>Reconciles every sent buy, then cancels whatever is still open at the broker.</p>
            </ConfirmAction>
          </div>
        </Banner>
      ) : null}
      {morning ? (
        <PlanPanel plan={morning} title="Morning plan" now={now} simulated={simulated} onDone={onDone} />
      ) : (
        <Card>
          <p className="text-sm text-muted-foreground">
            No morning plan for {view.today} yet — the 09:09 premarket job builds it (source=MORNING).
          </p>
        </Card>
      )}
      {preview ? (
        <details open={!morning}>
          <summary className="cursor-pointer text-sm text-muted-foreground">
            EOD preview built {istStamp(preview.built_at)} for {preview.as_of ?? "—"} — {preview.line_count} line(s),{" "}
            {preview.skips.length} skip(s){preview.expired ? ", expired" : ""}
          </summary>
          <div className="mt-2">
            <PlanPanel plan={preview} title="EOD preview" now={now} simulated={simulated} onDone={onDone} />
          </div>
        </details>
      ) : null}
      <p className="text-xs text-muted-foreground">
        A waiting buy confirms as a LIMIT buy at its trigger; a live break arrives as a trigger above. Confirm on a SELL or a
        RAISE GTT goes through the same endpoint. Every line is confirmed on its own.
      </p>
    </section>
  );
}

function halfRiskLeft(n: number): string {
  return ` · ${n} half-risk live session${n === 1 ? "" : "s"} left`;
}

function Book({ view, simulated, onDone }: { view: SwingDeskView; simulated: boolean; onDone: () => void }) {
  const { positions, naked_count: naked, manage } = view.book;
  return (
    <section aria-label="Positions" className="space-y-3">
      <h3 className="font-medium">
        Book — {positions.length} open
        {naked > 0 ? <span className="text-negative">, {naked} without a stop</span> : null}
      </h3>
      <p className="text-xs text-muted-foreground">Exit rule: {view.exit_rule}</p>
      <Card>
        <table className="w-full min-w-[64rem] border-collapse text-sm" aria-label="Open swing positions">
          <thead>
            <tr className="border-b border-border/70">
              <Th>Symbol</Th>
              <Th>Setup</Th>
              <Th>Entered</Th>
              <Th>Avg</Th>
              <Th>Open / entered</Th>
              <Th>Stop</Th>
              <Th>₹ at risk</Th>
              <Th>GTT</Th>
              <Th>Trail</Th>
              <Th>Partial</Th>
              <Th>Mode</Th>
              <Th>Action</Th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <Td colSpan={12} className="text-muted-foreground">No open swing positions.</Td>
              </tr>
            ) : (
              positions.map((p) => {
                const isNaked = p.naked ?? p.gtt_id === null;
                const avg = num(p.entry_avg);
                const stop = num(p.stop);
                return (
                  <tr key={p.id} className={cn("border-b border-border/40", isNaked && "bg-negative/5")}>
                    <Td className="font-medium">{p.symbol}</Td>
                    <Td>{p.setup}</Td>
                    <Td>{p.entry_date ?? "—"}</Td>
                    <Td>{px(p.entry_avg)}</Td>
                    <Td>
                      {p.quantity_open.toLocaleString("en-IN")} / {p.quantity_entered.toLocaleString("en-IN")}
                    </Td>
                    <Td>
                      {px(p.stop)} <span className="text-muted-foreground">(initial {px(p.initial_stop)})</span>
                    </Td>
                    <Td>{avg !== null && stop !== null ? px((avg - stop) * p.quantity_open) : "—"}</Td>
                    <Td>
                      {isNaked ? (
                        <b className="text-negative">naked</b>
                      ) : (
                        <>
                          #{p.gtt_id} <span className="text-muted-foreground">@ {px(p.gtt_trigger)}</span>
                        </>
                      )}
                    </Td>
                    <Td>{p.trail}</Td>
                    <Td>{p.partial_done ? `done${p.partial_date ? ` ${p.partial_date}` : ""}` : "—"}</Td>
                    <Td>{p.simulated ? "Simulated" : "Real"}</Td>
                    <Td>
                      {isNaked ? (
                        <ConfirmAction
                          label="Re-arm GTT"
                          title={`Re-arm the GTT stop for ${p.symbol}`}
                          confirmLabel={`${simulated ? "Arm (simulated)" : "Arm live GTT"} · ${p.symbol}`}
                          danger={!simulated}
                          run={async () => outcomeResult(await rearmGtt(p.id))}
                          onDone={onDone}
                        >
                          <p>
                            Places a GTT stop — not a buy — for {p.quantity_open.toLocaleString("en-IN")} {p.symbol} at the stop in
                            force, {px(p.stop)}.
                          </p>
                        </ConfirmAction>
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          armed{p.gtt_armed_at ? ` ${istStamp(p.gtt_armed_at)}` : ""}
                        </span>
                      )}
                    </Td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
        <p className="mt-3 text-xs text-muted-foreground">
          A position without a resting GTT is the one state the method forbids. Re-arm GTT places a stop, not a buy, for the open
          quantity at the stop in force.
        </p>
      </Card>

      <h4 className="text-sm font-medium">
        Last {manage.length} manage action{manage.length === 1 ? "" : "s"}
      </h4>
      <Card>
        {manage.length > 0 ? (
          <ul className="grid gap-1.5 text-sm">
            {manage.map((m) => (
              <li key={m.id}>
                <b>{m.label}</b>{" "}
                <span className="text-muted-foreground">
                  — {m.state} · plan {m.plan_source} for {m.as_of ?? "—"}
                  {m.note ? ` · ${m.note}` : ""}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">Nothing managed yet — the evening job proposes exits once there is a position.</p>
        )}
      </Card>
    </section>
  );
}
