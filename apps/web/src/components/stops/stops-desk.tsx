"use client";

import { useCallback, useEffect, useState } from "react";

import { Row, Stat, StatRow, Table, Td, Th } from "@/components/desk/ui";
import { useIsStaff } from "@/components/shell/staff-context";
import { Button } from "@/components/ui/button";
import { DeskError } from "@/lib/desk-actions/client";
import {
  armStops,
  fetchStopsReview,
  inr,
  num,
  planSecondsLeft,
  type ArmResult,
  type StopsReview,
} from "@/lib/rebalance/desk";
import { cn } from "@/lib/utils";

const BOX = "rounded-xl border p-3.5 text-sm";
const WARN = "border-warning/35 bg-warning-muted";
const BAD = "border-negative/40 bg-negative-muted";
const OK = "border-positive/40 bg-positive-muted";

function message(error: unknown): string {
  if (error instanceof DeskError) return `${error.status}: ${error.message}`;
  return error instanceof Error ? error.message : String(error);
}

export function StopsDesk() {
  const isStaff = useIsStaff();
  const [review, setReview] = useState<StopsReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [armed, setArmed] = useState<ArmResult | null>(null);
  const [armError, setArmError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(() => {
    setLoading(true);
    setLoadError(null);
    setConfirming(false);
    setReloadKey((k) => k + 1);
  }, []);

  // GET /stops builds a fresh review (and plan_id) from live prices; it places nothing.
  useEffect(() => {
    if (!isStaff) return;
    let live = true;
    fetchStopsReview()
      .then((r) => {
        if (!live) return;
        setReview(r);
        setNow(Date.now());
      })
      .catch((e: unknown) => {
        if (live) setLoadError(message(e));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [isStaff, reloadKey]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  if (!isStaff) {
    return <p className="text-sm text-muted-foreground">Stops is an operator page. Nothing to show here.</p>;
  }

  const plan = review?.plan ?? null;
  const dryRun = review?.dry_run ?? false;
  const secondsLeft = plan ? planSecondsLeft(plan.created_at, now) : 0;

  async function onArm() {
    if (!plan || sending) return;
    setSending(true);
    setArmError(null);
    try {
      // One POST, never retried; the plan is single use on the desk.
      setArmed(await armStops(plan.plan_id));
    } catch (e) {
      setArmError(message(e));
    } finally {
      setSending(false);
      setConfirming(false);
      load();
    }
  }

  return (
    <div className="flex flex-col gap-5">
      {armed ? (
        <div className={cn(BOX, OK)} role="status">
          <p className="font-semibold">{armed.armed}</p>
          <p className="mt-1 text-muted-foreground">
            Reloaded below from the broker, so this reflects what is actually live rather than what was requested.
          </p>
        </div>
      ) : null}
      {armError ? (
        <div className={cn(BOX, BAD)} role="status">
          <p className="font-semibold">Arming failed</p>
          <p className="mt-1 text-xs">{armError}</p>
          <p className="mt-1 text-muted-foreground">Check the Kite GTT list before arming again.</p>
        </div>
      ) : null}
      {loadError ? <p className={cn(BOX, WARN)}>Could not read the desk: {loadError}</p> : null}
      {loading && !review ? <p className="text-sm text-muted-foreground">Reading holdings and triggers…</p> : null}

      {review?.problem ? (
        <section className="flex flex-col gap-2 rounded-xl border border-border/70 bg-card p-4">
          <h2 className="font-semibold">Cannot review stops</h2>
          <p className="text-sm text-muted-foreground">{review.problem}</p>
          <div>
            <a href="/desk/login" className="inline-flex h-9 items-center rounded-md border border-input px-4 text-sm font-semibold hover:bg-muted">
              Log in to Kite
            </a>
          </div>
        </section>
      ) : plan && plan.count === 0 && plan.cancels.length === 0 ? (
        <p className={cn(BOX, OK)}>
          Nothing to arm. Every tradeable position already carries a stop covering its full quantity.
        </p>
      ) : plan && review ? (
        <>
          <p className={cn(BOX, dryRun ? WARN : BAD)}>
            <b>{dryRun ? "Dry run. " : "Live orders. "}</b>
            {dryRun
              ? "DRY_RUN is on, so confirming below simulates every trigger and places nothing."
              : `Confirming below places ${plan.count} real GTT triggers on your account.`}
          </p>

          <StatRow>
            <Stat
              label="Positions to cover"
              value={String(plan.count)}
              hint={`of ${plan.review.checked} tradeable positions. ${plan.review.active_gtts} trigger${plan.review.active_gtts === 1 ? "" : "s"} active now.`}
            />
            <Stat label="Value protected" value={inr(plan.value)} hint="Currently uncovered by any working stop." />
            <Stat
              label="Loss if every stop fires"
              value={inr(plan.at_risk)}
              tone="text-negative"
              hint="The distance from today's price to each trigger — the worst-case cost of the protection."
            />
            <Stat
              label="Stop band"
              value={`${review.band[0].toFixed(0)}–${review.band[1].toFixed(0)}%`}
              hint="Vol-scaled and clamped to this range by stop_from_vol()."
            />
          </StatRow>

          {plan.at_risk > review.daily_loss_cap ? (
            <p className={cn(BOX, WARN)}>
              <b>Exceeds the daily cap.</b> If every stop triggered on the same day the loss would be{" "}
              {inr(plan.at_risk)}, above the {inr(review.daily_loss_cap)} daily loss cap. A single bad session can take
              many of them, and the kill switch would halt trading part-way through.
            </p>
          ) : null}

          {plan.using_default_vol.length ? (
            <p className={cn(BOX, WARN)}>
              {plan.using_default_vol.length} of {plan.count} stops are sized from the default volatility (mid-band),
              because no scan volatility is available for them. Upload a weekly scan on Weekly rebalance first if you
              want each stop scaled to its own name.
            </p>
          ) : null}

          <section className="flex flex-col gap-3" aria-labelledby="proposed-stops">
            <h2 id="proposed-stops" className="text-lg font-semibold">
              Proposed stops
            </h2>
            <Table
              caption="Proposed stops"
              head={
                <>
                  <Th>Stock</Th>
                  <Th align="right">Qty</Th>
                  <Th align="right">Last price</Th>
                  <Th align="right">Trigger</Th>
                  <Th align="right">Below</Th>
                  <Th align="right">Value covered</Th>
                  <Th align="right">At risk</Th>
                  <Th>Sizing</Th>
                  <Th>Why</Th>
                </>
              }
            >
              {plan.rows.map((r) => (
                <Row key={r.symbol}>
                  <Td>
                    <span className="font-medium">{r.symbol}</span>
                  </Td>
                  <Td align="right">{num(r.qty)}</Td>
                  <Td align="right">{num(r.last_price)}</Td>
                  <Td align="right" tone="text-positive">
                    {num(r.trigger)}
                  </Td>
                  <Td align="right">−{r.drop_pct.toFixed(1)}%</Td>
                  <Td align="right">{inr(r.value)}</Td>
                  <Td align="right" tone="text-negative">
                    {inr(r.at_risk)}
                  </Td>
                  <Td tone="text-muted-foreground">{r.vol_source}</Td>
                  <Td tone="text-muted-foreground">{r.reason === "missing" ? "no stop" : "uncovered shares"}</Td>
                </Row>
              ))}
            </Table>
            <p className="text-xs text-muted-foreground">
              A partially covered position is proposed for its uncovered shares only, so arming cannot double up on
              quantity already protected. A stop that merely sits outside the band is left alone; over-coverage is
              cancelled below.
            </p>
          </section>

          {plan.cancels.length ? (
            <section className="flex flex-col gap-3" aria-labelledby="cancel-head">
              <h2 id="cancel-head" className="text-lg font-semibold">
                Triggers to cancel — {plan.cancels.length}
              </h2>
              <p className={cn(BOX, BAD)}>
                These run <b>before</b> anything is armed. A trigger covering more shares than you hold sells what you
                do not own when it fires, which is short delivery.
              </p>
              <Table
                caption="Triggers to cancel"
                head={
                  <>
                    <Th>Symbol</Th>
                    <Th>GTT id</Th>
                    <Th>Why</Th>
                    <Th>Detail</Th>
                  </>
                }
              >
                {plan.cancels.map((c) => (
                  <Row key={`${c.symbol}-${c.gtt_id}`}>
                    <Td>{c.symbol}</Td>
                    <Td>#{c.gtt_id}</Td>
                    <Td tone="text-negative">{c.reason}</Td>
                    <Td tone="text-muted-foreground">{c.detail}</Td>
                  </Row>
                ))}
              </Table>
            </section>
          ) : null}

          <section className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4" aria-labelledby="confirm-head">
            <h2 id="confirm-head" className="font-semibold">
              Confirm
            </h2>
            {plan.cancels.length ? (
              <p className="text-sm">
                <b>
                  {plan.cancels.length} existing trigger{plan.cancels.length === 1 ? "" : "s"} will be cancelled first
                </b>
                , then {plan.rows.length} armed.
              </p>
            ) : null}
            <p className="text-sm text-muted-foreground">
              Each trigger is a GTT sell at the price shown, valid until it fires or Zerodha expires it. Protected
              instruments are refused at the guard even if one reached this list. Plan <code>{plan.plan_id}</code> ·
              single use ·{" "}
              {secondsLeft > 0
                ? `expires in ${Math.floor(secondsLeft / 60)}m ${secondsLeft % 60}s`
                : "expired — reload the review"}
              .
            </p>
            {confirming ? (
              <div className={cn("flex flex-wrap items-center gap-3", BOX, dryRun ? WARN : BAD)}>
                <span>
                  {dryRun ? "Simulate" : "Arm"} {plan.count} stops
                  {plan.cancels.length ? ` after cancelling ${plan.cancels.length}` : ""}?
                </span>
                <Button
                  variant={dryRun ? "primary" : "destructive"}
                  disabled={sending || secondsLeft <= 0}
                  onClick={() => void onArm()}
                >
                  {sending ? "Sending…" : `Yes, ${dryRun ? "simulate" : "arm"} now`}
                </Button>
                <Button variant="outline" disabled={sending} onClick={() => setConfirming(false)}>
                  Cancel
                </Button>
              </div>
            ) : (
              <div className="flex flex-wrap gap-3">
                <Button
                  variant={dryRun ? "primary" : "destructive"}
                  disabled={secondsLeft <= 0 || sending}
                  onClick={() => setConfirming(true)}
                >
                  {dryRun ? "Simulate" : "Arm"} {plan.count} stops…
                </Button>
                <Button variant="outline" disabled={loading || sending} onClick={load}>
                  {loading ? "Reloading…" : "Reload review"}
                </Button>
              </div>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}
