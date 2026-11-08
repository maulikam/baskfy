"use client";

import { useState } from "react";
import { CheckCircle2, ChevronDown, CircleAlert, Clock, Layers, Users, Wallet } from "lucide-react";

import type { DataHealth, HealthProblem } from "@/lib/portfolio/command-center";
import { cn } from "@/lib/utils";

/**
 * The operational strip: is the data under this screen worth trusting right now?
 *
 * Brief: *"If there is a problem, show exactly what is affected rather than a generic warning."*
 * That single sentence is the whole design. A chip saying "1 issue" is worthless — it makes the
 * reader hunt. So the strip carries counts inline, and the status chip expands into the *named*
 * problems, each with what it affects and what it costs.
 *
 * WHY A STRIP AND NOT A BANNER
 * ----------------------------
 * Data health is a standing condition, not an event. A dismissible banner trains people to
 * dismiss it; a permanent thin line trains them to glance at it. It reads "live" far more often
 * than it reads anything else, and that is what makes the exceptions legible.
 *
 * Colour never carries the state alone: each status has its own icon and its own word.
 */

const STATUS = {
  live: {
    word: "Live",
    Icon: CheckCircle2,
    className: "border-positive/30 bg-positive-muted text-positive",
  },
  stale: {
    word: "Stale",
    Icon: Clock,
    className: "border-warning/40 bg-warning-muted text-warning",
  },
  "action-required": {
    word: "Action required",
    Icon: CircleAlert,
    className: "border-negative/30 bg-negative-muted text-negative",
  },
} as const;

function Fact({ icon: Icon, children }: { icon: typeof Layers; children: React.ReactNode }) {
  return (
    <span className="flex items-center gap-1.5 whitespace-nowrap">
      <Icon aria-hidden="true" className="size-3.5 shrink-0 opacity-60" />
      {children}
    </span>
  );
}

function ProblemRow({ problem }: { problem: HealthProblem }) {
  const severe = problem.severity === "action-required";
  return (
    <li className="flex gap-2 py-2">
      {severe ? (
        <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-negative" />
      ) : (
        <Clock aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
      )}
      <div className="min-w-0">
        <p className="text-sm font-medium">
          {problem.headline}
          {/* The severity is a word as well as a colour and an icon. */}
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            {severe ? "Action required" : "Stale"}
          </span>
        </p>
        <p className="text-xs leading-snug text-muted-foreground">{problem.detail}</p>
        {problem.href ? (
          <a
            href={problem.href}
            className="mt-0.5 inline-block text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
          >
            Open
          </a>
        ) : null}
      </div>
    </li>
  );
}

export function HealthStrip({ health }: { health: DataHealth }) {
  const [open, setOpen] = useState(false);
  const status = STATUS[health.status];
  const { Icon } = status;
  const hasProblems = health.problems.length > 0;

  return (
    <section
      aria-label="Data health"
      data-testid="health-strip"
      data-status={health.status}
      className="rounded-xl border border-border bg-card"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5 text-xs text-muted-foreground">
        <Fact icon={Layers}>
          <strong className="font-semibold text-foreground tabular-nums">
            {health.portfolioCount}
          </strong>{" "}
          portfolio{health.portfolioCount === 1 ? "" : "s"}
        </Fact>
        <Fact icon={Wallet}>
          <strong className="font-semibold text-foreground tabular-nums">
            {health.holdingsCount}
          </strong>{" "}
          holding{health.holdingsCount === 1 ? "" : "s"}
        </Fact>
        <Fact icon={Users}>
          <strong className="font-semibold text-foreground tabular-nums">
            {health.brokerCount}
          </strong>{" "}
          broker{health.brokerCount === 1 ? "" : "s"} connected
        </Fact>
        <Fact icon={Clock}>{health.pricesLabel}</Fact>
        <Fact icon={CheckCircle2}>{health.holdingsSyncedLabel}</Fact>

        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          disabled={!hasProblems}
          aria-expanded={hasProblems ? open : undefined}
          data-testid="health-status-chip"
          className={cn(
            "ml-auto flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors duration-150",
            status.className,
            hasProblems ? "cursor-pointer hover:opacity-80" : "cursor-default",
          )}
        >
          <Icon aria-hidden="true" className="size-3.5" />
          {status.word}
          {hasProblems ? (
            <>
              <span className="tabular-nums">· {health.problems.length}</span>
              <ChevronDown
                aria-hidden="true"
                className={cn("size-3.5 transition-transform duration-150", open && "rotate-180")}
              />
            </>
          ) : null}
        </button>
      </div>

      {open && hasProblems ? (
        <div className="border-t border-border px-4 py-1" data-testid="health-problems">
          <ul className="divide-y divide-border/60">
            {health.problems.map((problem) => (
              <ProblemRow key={problem.id} problem={problem} />
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
