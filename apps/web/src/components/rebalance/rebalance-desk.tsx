"use client";

import type { Route } from "next";
import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { Row, Stat, StatRow, Table, Td, Th } from "@/components/desk/ui";
import { useIsStaff } from "@/components/shell/staff-context";
import { Button } from "@/components/ui/button";
import { DeskError } from "@/lib/desk-actions/client";
import {
  analyzeScan,
  executePlan,
  fetchDeskHome,
  inr,
  num,
  planSecondsLeft,
  type DeskHome,
  type ExecuteResult,
  type RebalancePlan,
} from "@/lib/rebalance/desk";
import { cn } from "@/lib/utils";

import { ExecutionReport } from "./report";

const BOX = "rounded-xl border p-3.5 text-sm";
const WARN = "border-warning/35 bg-warning-muted";
const BAD = "border-negative/40 bg-negative-muted";
const OK = "border-positive/40 bg-positive-muted";

function message(error: unknown): string {
  if (error instanceof DeskError) return `${error.status}: ${error.message}`;
  return error instanceof Error ? error.message : String(error);
}

function clock(at: string | undefined): string {
  return at ? at.slice(11, 16) : "";
}

function HealthBanners({ home }: { home: DeskHome }) {
  const { auto, collection, protection } = home;
  return (
    <>
      {auto?.running ? (
        <p className={cn(BOX, WARN)}>
          {auto.just_logged_in ? "Logged in. " : ""}Today&apos;s outstanding collection is running — started{" "}
          {clock(auto.running.started_at)}. Reload when it finishes.
        </p>
      ) : auto?.last && auto.last.status === "ok" ? (
        <p className={cn(BOX, OK)}>Today&apos;s collection ran at {clock(auto.last.started_at)} and landed.</p>
      ) : auto?.last ? (
        <p className={cn(BOX, BAD)}>
          The post-login run at {clock(auto.last.started_at)} ended <b>{auto.last.status}</b>. Today&apos;s data
          cannot be backfilled, so this is worth opening now on the desk&apos;s Operations page.
        </p>
      ) : auto?.just_logged_in ? (
        <p className={cn(BOX, OK)}>{auto.note || "Nothing outstanding — today is already collected."}</p>
      ) : null}

      {collection && !collection.healthy ? (
        !collection.ever_ran ? (
          <p className={cn(BOX, WARN)}>
            The daily collection has never run. A missed session cannot be backfilled: margins have no history and
            Kite&apos;s trade list is same-day only.
          </p>
        ) : (
          <p className={cn(BOX, collection.consecutive_failures > 1 ? BAD : WARN)}>
            <b>
              {collection.consecutive_failures > 1
                ? `${collection.consecutive_failures} runs failed. `
                : "Last run failed. "}
            </b>
            {collection.message}
            {collection.stale_days
              ? ` Nothing has been collected for ${collection.stale_days} day${collection.stale_days === 1 ? "" : "s"}, and those sessions cannot be recovered.`
              : collection.stale_days === null
                ? ""
                : " Today is still covered."}
            {collection.outcome === "auth" ? " Log in to Kite, then re-run the collection." : ""}
          </p>
        )
      ) : null}

      {protection && !protection.healthy ? (
        <p className={cn(BOX, BAD)}>
          <b>{(protection.counts.missing ?? 0) + (protection.counts.partial ?? 0)} unprotected.</b>{" "}
          {inr(protection.unprotected_value)} of {inr(protection.tradeable_value)} has no working stop (
          {protection.coverage_pct ?? "–"}% covered, {protection.active_gtts} active GTT
          {protection.active_gtts === 1 ? "" : "s"}). Largest:{" "}
          {protection.findings
            .slice(0, 4)
            .map((f) => `${f.symbol} ${inr(f.value)}`)
            .join(", ")}
          {protection.findings.length > 4 ? ` and ${protection.findings.length - 4} more` : ""}.{" "}
          <Link className="underline" href={"/stops" as Route}>
            Review and arm stops
          </Link>
          . Nothing is placed without an explicit confirmation.
        </p>
      ) : protection && protection.checked ? (
        <p className={cn(BOX, OK)}>
          Stops current: all {protection.checked} tradeable positions carry a stop covering their full quantity.
        </p>
      ) : null}
    </>
  );
}

function PlanTable({ plan }: { plan: RebalancePlan }) {
  return (
    <Table
      caption="Order plan — review every line"
      head={
        <>
          <Th>Symbol</Th>
          <Th align="right">Rank</Th>
          <Th align="right">Score</Th>
          <Th>Action</Th>
          <Th align="right">Now</Th>
          <Th align="right">Δ Order</Th>
          <Th align="right">Final</Th>
          <Th align="right">Ref ₹</Th>
          <Th align="right">Value ₹</Th>
          <Th align="right">Wt%</Th>
          <Th align="right">GTT stop</Th>
          <Th align="right">Pledged</Th>
        </>
      }
    >
      {plan.orders.map((o) => (
        <Row key={o.symbol}>
          <Td>
            <span className="font-medium">{o.symbol}</span>
            {o.note ? <span className="text-muted-foreground"> · {o.note}</span> : null}
          </Td>
          <Td align="right">{o.rank ?? "–"}</Td>
          <Td align="right">{o.score ?? "–"}</Td>
          <Td tone={o.delta > 0 ? "text-positive" : o.delta < 0 ? "text-negative" : ""}>{o.action}</Td>
          <Td align="right">{num(o.qty_now)}</Td>
          <Td align="right">
            {o.delta > 0 ? "+" : ""}
            {num(o.delta)}
          </Td>
          <Td align="right">{num(o.qty_final)}</Td>
          <Td align="right">{num(o.ref_price)}</Td>
          <Td align="right">{num(o.value)}</Td>
          <Td align="right">{o.weight ?? "–"}</Td>
          <Td align="right">{num(o.stop)}</Td>
          <Td align="right" tone={o.pledged ? "text-warning" : ""}>
            {num(o.pledged)}
          </Td>
        </Row>
      ))}
    </Table>
  );
}

function Ticket({
  plan,
  dryRun,
  secondsLeft,
  sending,
  onConfirm,
  onCancel,
}: {
  plan: RebalancePlan;
  dryRun: boolean;
  secondsLeft: number;
  sending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const lines = plan.orders.filter((o) => o.delta !== 0);
  const sells = lines.filter((o) => o.delta < 0).length;
  // Sells first, matching the order /execute places them in.
  const seq = [...lines].sort((a, b) => Number(a.delta > 0) - Number(b.delta > 0));
  const f = plan.funding;
  return (
    <section
      aria-labelledby="ticket-head"
      className={cn("flex flex-col gap-3 rounded-xl border-2 p-4", dryRun ? "border-warning" : "border-negative")}
    >
      <p className={cn("text-xs font-bold tracking-wide", dryRun ? "text-warning" : "text-negative")}>
        {dryRun ? "SIMULATION" : "LIVE — REAL MONEY"}
      </p>
      <h2 id="ticket-head" className="text-lg font-semibold">
        Contract note — confirm execution
      </h2>
      <p className="text-sm text-muted-foreground">
        {lines.length} orders — {sells} sell, {lines.length - sells} buy. Sells go first, then buys. Stops are armed
        afterwards from Stops, sized from fills.
      </p>
      <ul className="max-h-72 overflow-auto rounded-lg border border-border/70 text-sm">
        {seq.map((o) => (
          <li key={o.symbol} className="flex justify-between gap-2 border-b border-border/60 px-3 py-1.5 last:border-0">
            <span>
              {o.delta < 0 ? "SELL" : "BUY"} {o.symbol}
            </span>
            <b className="tabular-nums">
              {Math.abs(o.delta)} @ ~₹{num(o.ref_price)}
            </b>
          </li>
        ))}
      </ul>
      <p className="flex justify-between text-sm">
        <span>Net cash use</span>
        <b className="tabular-nums">{inr(plan.net_cash_use)}</b>
      </p>
      {f?.checked && (f.shortfall ?? 0) > 0 ? (
        <p className={cn(BOX, BAD)}>
          <b>Short {inr(f.shortfall)}.</b> This plan needs {inr(f.required)} of margin against {inr(f.available)}{" "}
          available. Orders go out largest first, so the last ones will be refused. Confirming will place the rest.
        </p>
      ) : f?.checked ? (
        <p className="text-sm text-muted-foreground">
          Funded — {inr(f.required)} required, {inr(f.available)} available.
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">Funding not verified{f?.error ? ` (${f.error})` : ""}.</p>
      )}
      <p className="text-xs text-muted-foreground">
        By confirming, orders are placed on your Zerodha account via Kite Connect, through the desk&apos;s gateway,
        guards and risk limits. Pledged shares sell directly; collateral margin reduces to the extent sold. Plan{" "}
        <code>{plan.plan_id}</code> · single use · expires in {Math.floor(secondsLeft / 60)}m {secondsLeft % 60}s.
      </p>
      <div className="flex flex-wrap gap-3">
        <Button variant="destructive" disabled={sending || secondsLeft <= 0} onClick={onConfirm}>
          {sending ? "Placing…" : dryRun ? "Confirm & simulate orders" : "Confirm & place orders"}
        </Button>
        <Button variant="outline" disabled={sending} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </section>
  );
}

export function RebalanceDesk() {
  const isStaff = useIsStaff();
  const [home, setHome] = useState<DeskHome | null>(null);
  const [homeError, setHomeError] = useState<string | null>(null);
  const [plan, setPlan] = useState<RebalancePlan | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [ticketOpen, setTicketOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<ExecuteResult | null>(null);
  const [execError, setExecError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const fileRef = useRef<HTMLInputElement>(null);
  const reportRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isStaff) return;
    let live = true;
    fetchDeskHome()
      .then((h) => {
        if (live) setHome(h);
      })
      .catch((e: unknown) => {
        if (live) setHomeError(message(e));
      });
    return () => {
      live = false;
    };
  }, [isStaff]);

  useEffect(() => {
    if (!plan) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [plan]);

  if (!isStaff) {
    return (
      <p className="text-sm text-muted-foreground">The weekly rebalance is an operator page. Nothing to show here.</p>
    );
  }

  const dryRun = home?.dry_run ?? false;
  const secondsLeft = plan ? planSecondsLeft(plan.created_at, now) : 0;
  const actionable = plan ? plan.orders.some((o) => o.delta !== 0) : false;

  async function onAnalyze(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setAnalyzing(true);
    setAnalyzeError(null);
    setPlan(null);
    setTicketOpen(false);
    setResult(null);
    setExecError(null);
    try {
      setPlan(await analyzeScan(file));
      setNow(Date.now());
    } catch (e) {
      setAnalyzeError(message(e));
    } finally {
      setAnalyzing(false);
    }
  }

  async function onConfirm() {
    if (!plan || sending) return;
    setSending(true);
    setTicketOpen(false);
    setExecError(null);
    try {
      // One POST, never retried: a lost answer is resolved from the order list, not by re-sending.
      setResult(await executePlan(plan.plan_id));
    } catch (e) {
      setExecError(message(e));
    } finally {
      setSending(false);
      setPlan(null);
      requestAnimationFrame(() => reportRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
  }

  return (
    <div className="flex flex-col gap-5">
      {homeError ? <p className={cn(BOX, WARN)}>Could not read the desk: {homeError}</p> : null}
      {home ? <HealthBanners home={home} /> : null}
      {home && !home.dry_run ? (
        <p className={cn(BOX, BAD)}>
          <b>Live orders.</b> DRY_RUN is off — confirming a plan here places real orders.
        </p>
      ) : home ? (
        <p className={cn(BOX, WARN)}>
          <b>Dry run.</b> DRY_RUN is on — confirming simulates every order and places nothing.
        </p>
      ) : null}

      <section className="grid gap-4 md:grid-cols-2">
        <div className="flex flex-col gap-2 rounded-xl border border-border/70 bg-card p-4">
          <h2 className="font-semibold">1 · Session</h2>
          <p className="text-sm text-muted-foreground">
            {home ? (home.authed ? "Kite session is live for today." : "Not logged in to Kite.") : "Checking…"} Token
            lasts the trading day.
          </p>
          <div>
            <a href="/desk/login" className="inline-flex h-9 items-center rounded-md border border-input px-4 text-sm font-semibold hover:bg-muted">
              {home?.authed ? "Log in to Kite again" : "Log in to Kite"}
            </a>
          </div>
        </div>
        <div className="flex flex-col gap-2 rounded-xl border border-border/70 bg-card p-4">
          <h2 className="font-semibold">2 · Upload weekly scan</h2>
          <form onSubmit={(e) => void onAnalyze(e)} className="flex flex-wrap items-center gap-3">
            <input
              ref={fileRef}
              type="file"
              name="scan"
              accept=".csv"
              required
              aria-label="Weekly scan CSV"
              className="text-sm"
            />
            <Button type="submit" variant="primary" disabled={analyzing}>
              {analyzing ? "Analysing…" : "Analyze & build plan"}
            </Button>
          </form>
          <p className="text-xs text-muted-foreground">
            Scores the universe, pulls live holdings, margins and LTP, returns the full order plan. Nothing is executed
            at this step.
          </p>
          {analyzeError ? <p className={cn(BOX, BAD)}>{analyzeError}</p> : null}
        </div>
      </section>

      {plan ? (
        <>
          <section className="flex flex-col gap-3" aria-labelledby="plan-summary">
            <h2 id="plan-summary" className="text-lg font-semibold">
              Plan summary
            </h2>
            <StatRow>
              <Stat label="Capital" value={inr(plan.capital)} />
              <Stat label="Holdings value" value={inr(plan.book_value)} />
              <Stat label="Cash" value={inr(plan.cash)} />
              <Stat label="Breadth > 20DMA" value={`${plan.breadth_above_20dma ?? "–"}%`} />
              <Stat label="Cash target" value={`${plan.cash_target_pct ?? "–"}%`} />
              <Stat label="Buys" value={inr(plan.buys_value)} />
              <Stat label="Sells" value={inr(plan.sells_value)} />
            </StatRow>
            {plan.pledged_sells_margin_note.length ? (
              <p className={cn(BOX, WARN)}>
                Pledged shares sell directly (Zerodha instant sale) — collateral margin will reduce:{" "}
                {plan.pledged_sells_margin_note.join(", ")}
              </p>
            ) : null}
            {(plan.cluster_warnings ?? []).map((c) => (
              <p key={c} className={cn(BOX, WARN)}>
                Cluster: {c}
              </p>
            ))}
            {(plan.scan?.warnings ?? []).map((w) => (
              <p key={w} className={cn(BOX, WARN)}>
                {w}
              </p>
            ))}
          </section>

          <section className="flex flex-col gap-3" aria-labelledby="plan-lines">
            <h2 id="plan-lines" className="text-lg font-semibold">
              Order plan — review every line
            </h2>
            <PlanTable plan={plan} />
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="destructive"
                disabled={!actionable || secondsLeft <= 0 || ticketOpen || sending}
                onClick={() => setTicketOpen(true)}
              >
                Execute plan…
              </Button>
              <span className="text-xs text-muted-foreground">
                {secondsLeft <= 0
                  ? "This plan is older than 30 minutes — prices are stale. Re-run Analyze."
                  : !actionable
                    ? "Nothing to trade in this plan."
                    : `Opens a contract-note confirmation. Plan expires in ${Math.floor(secondsLeft / 60)}m ${secondsLeft % 60}s.`}
              </span>
            </div>
          </section>

          {ticketOpen ? (
            <Ticket
              plan={plan}
              dryRun={dryRun}
              secondsLeft={secondsLeft}
              sending={sending}
              onConfirm={() => void onConfirm()}
              onCancel={() => setTicketOpen(false)}
            />
          ) : null}
        </>
      ) : null}

      <div ref={reportRef} tabIndex={-1}>
        {execError ? (
          <div className={cn(BOX, BAD)}>
            <p className="font-semibold">Not executed</p>
            <p className="mt-1">
              The desk refused the request, or it did not answer. If it refused (400/404/410), no order was placed.
              If there was no answer, check the Kite order list before re-running Analyze.
            </p>
            <p className="mt-1 text-xs text-muted-foreground">{execError}</p>
          </div>
        ) : null}
        {result ? <ExecutionReport result={result} /> : null}
      </div>
    </div>
  );
}
