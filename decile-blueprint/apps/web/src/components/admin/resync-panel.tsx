"use client";

import { useCallback, useEffect, useRef, useState, useTransition } from "react";
import type { ResyncFindingOut, ResyncOutcomeOut, ResyncPlanOut } from "@baskfy/api-client";

import { inspectResync, startResync } from "@/app/actions/admin";
import { Button } from "@/components/ui/button";
import { formatDateTimeIST } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Leaf 3.1: "give a button to resync so it resyncs everything if any data is pending, so I don't
 * have to come to this machine."
 *
 * Three states, and all three are rendered honestly:
 *
 *   nothing pending   the common answer, and a real one. Says what it checked and when.
 *   pending           every gap listed with the number behind it and the remedy that will run.
 *   partial           what the last repair fixed, what it could not, and what it never got to.
 *
 * The fourth state is the one this deliberately refuses to have: a spinner that resolves to
 * silence. `POST /admin/resync` answers 202, so after pressing, the panel polls the inspection
 * until the repair's own report lands — and if it does not land inside the budget it says the
 * repair is still running rather than pretending it finished.
 *
 * `pending: false` is **not** the same as "everything was checked". A host that cannot reach NSE
 * returns unresolved notes, and those get their own block above the all-clear rather than being
 * folded into it.
 */

const KIND_LABEL: Record<string, string> = {
  missing_run: "no published run",
  thin_bars: "bars missing",
  wrong_holiday: "wrongly marked a holiday",
  kite_session: "broker session",
};

/** How long to keep watching after pressing, and how often. A bhavcopy day is seconds; thirty of
 *  them is a couple of minutes, so three minutes covers a full press with room to spare. */
const POLL_MS = 5_000;
const WATCH_BUDGET_MS = 180_000;

type Phase = "idle" | "checking" | "starting" | "watching" | "still-running";

function stamp(outcome: ResyncOutcomeOut | null | undefined): string {
  return outcome ? outcome.completed_at : "";
}

function Lines({ title, tone, items }: { title: string; tone: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="space-y-1">
      <p className={cn("text-xs font-medium uppercase tracking-wide", tone)}>
        {title} · {items.length}
      </p>
      <ul className="space-y-1">
        {items.map((line) => (
          <li key={line} className="text-sm text-muted-foreground">
            {line}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Finding({ finding }: { finding: ResyncFindingOut }) {
  return (
    <li className="rounded border border-border/60 px-3 py-2">
      <p className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono text-xs">{finding.trade_date ?? "this host"}</span>
        <span className="rounded bg-negative/10 px-1.5 py-0.5 font-mono text-xs text-negative">
          {KIND_LABEL[finding.kind] ?? finding.kind}
        </span>
        {typeof finding.observed_bars === "number" &&
        typeof finding.expected_bars === "number" ? (
          <span className="font-mono text-xs text-muted-foreground">
            {finding.observed_bars.toLocaleString("en-IN")} of ~
            {finding.expected_bars.toLocaleString("en-IN")} bars
          </span>
        ) : null}
      </p>
      <p className="mt-1 text-sm">{finding.summary}</p>
      <p className="mt-1 text-xs text-muted-foreground">Will run: {finding.remedy}</p>
    </li>
  );
}

function LastResync({ outcome }: { outcome: ResyncOutcomeOut }) {
  // "Complete" means every gap it found was closed — not that every question was answerable.
  // Anything it could not check is rendered on its own line, right beside the verdict.
  const verdict = outcome.complete
    ? "Closed everything it found."
    : "Did NOT close everything — the lines below say what is left.";
  return (
    <div className="space-y-3 rounded border border-border/60 bg-muted/30 px-3 py-3">
      <p className="text-sm">
        <span className="font-medium">Last resync</span> ·{" "}
        <span className="font-mono text-xs">{formatDateTimeIST(outcome.completed_at)}</span>
        {outcome.actor ? <> · {outcome.actor}</> : null}
      </p>
      <p className={cn("text-sm", outcome.complete ? "text-positive" : "text-negative")}>
        {verdict}
      </p>
      <Lines title="Repaired" tone="text-positive" items={outcome.repaired} />
      <Lines title="Could not fix" tone="text-negative" items={outcome.failed} />
      <Lines title="Not attempted yet" tone="text-muted-foreground" items={outcome.deferred} />
      <Lines title="Still pending" tone="text-negative" items={outcome.still_pending} />
      <Lines title="Could not check" tone="text-muted-foreground" items={outcome.unresolved} />
    </div>
  );
}

export function ResyncPanel({ initial }: { initial: ResyncPlanOut | null }) {
  const [plan, setPlan] = useState<ResyncPlanOut | null>(initial);
  const [phase, setPhase] = useState<Phase>("idle");
  const [message, setMessage] = useState<string>("");
  const [failed, setFailed] = useState(false);
  const [, startTransition] = useTransition();
  const watching = useRef(false);

  // Stop the watch when the operator navigates away, so a page they have left does not keep
  // asking the API a question nobody is reading the answer to.
  useEffect(() => () => {
    watching.current = false;
  }, []);

  const check = useCallback(async (): Promise<ResyncPlanOut | null> => {
    const result = await inspectResync();
    setFailed(!result.ok);
    if (!result.ok) {
      setMessage(result.message);
      return null;
    }
    setPlan(result.plan);
    return result.plan;
  }, []);

  const onCheck = () =>
    startTransition(async () => {
      setPhase("checking");
      setMessage("");
      await check();
      setPhase("idle");
    });

  const onResync = () =>
    startTransition(async () => {
      const before = stamp(plan?.last_resync);
      setPhase("starting");
      const started = await startResync();
      setFailed(!started.ok);
      setMessage(started.message);
      if (!started.ok) {
        setPhase("idle");
        return;
      }

      // Poll the inspection until the repair writes its own report. Watching for a *new*
      // `completed_at` rather than for an empty plan, because "it finished and could not fix
      // two of them" is an outcome the operator must see, not a reason to keep spinning.
      setPhase("watching");
      watching.current = true;
      const deadline = Date.now() + WATCH_BUDGET_MS;
      while (watching.current && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        const fresh = await check();
        if (fresh && stamp(fresh.last_resync) !== before) {
          watching.current = false;
          setPhase("idle");
          setMessage("");
          return;
        }
      }
      watching.current = false;
      setPhase("still-running");
    });

  const busy = phase !== "idle" && phase !== "still-running";
  const findings = plan?.findings ?? [];
  const unresolved = plan?.unresolved ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={onResync} disabled={busy}>
          {findings.length > 0
            ? `Resync ${findings.length} pending item${findings.length === 1 ? "" : "s"}`
            : "Resync anyway"}
        </Button>
        <Button variant="outline" onClick={onCheck} disabled={busy}>
          Check again
        </Button>
        {/* One live region rather than three conditional spans: a screen reader has to be told
            that a five-second poll is in progress, and only an announced region does that. */}
        <span role="status" aria-live="polite" className="text-sm text-muted-foreground">
          {phase === "checking" ? "Checking…" : null}
          {phase === "starting" ? "Queueing the repair…" : null}
          {phase === "watching" ? "Repairing — watching for the report…" : null}
        </span>
      </div>

      {message ? (
        <p
          role="status"
          className={cn(
            "rounded border px-3 py-2 text-sm",
            failed
              ? "border-negative/30 bg-negative/10 text-negative"
              : "border-border bg-muted/40",
          )}
        >
          {message}
        </p>
      ) : null}

      {phase === "still-running" ? (
        <p role="status" className="rounded border border-border bg-muted/40 px-3 py-2 text-sm">
          The repair is still running after three minutes — a wide backfill takes that long.
          Nothing has been lost; press <strong>Check again</strong> in a few minutes to read its
          report.
        </p>
      ) : null}

      {plan === null ? (
        <p className="text-sm text-muted-foreground">
          The data check has not run yet. Press <strong>Check again</strong>.
        </p>
      ) : (
        <>
          {findings.length === 0 ? (
            <p
              className={cn(
                "rounded border px-3 py-2 text-sm",
                unresolved.length === 0
                  ? "border-positive/30 bg-positive/10 text-positive"
                  : "border-border bg-muted/40",
              )}
            >
              {unresolved.length === 0
                ? "Nothing is pending."
                : "Nothing pending in what could be checked — see below for what could not."}{" "}
              <span className="text-muted-foreground">
                {plan.trading_days_checked} trading day
                {plan.trading_days_checked === 1 ? "" : "s"} checked, {plan.window_start} to{" "}
                {plan.window_end}.
              </span>
            </p>
          ) : (
            <div className="space-y-2">
              <p className="text-sm">
                {findings.length} pending item{findings.length === 1 ? "" : "s"} across{" "}
                {plan.trading_days_checked} trading day
                {plan.trading_days_checked === 1 ? "" : "s"} ({plan.window_start} to{" "}
                {plan.window_end}).
              </p>
              <ul className="space-y-2">
                {findings.map((finding) => (
                  <Finding
                    key={`${finding.kind}-${finding.trade_date ?? "host"}`}
                    finding={finding}
                  />
                ))}
              </ul>
            </div>
          )}

          <Lines
            title="Could not be checked"
            tone="text-muted-foreground"
            items={unresolved}
          />

          {plan.last_resync ? <LastResync outcome={plan.last_resync} /> : null}
        </>
      )}
    </div>
  );
}
