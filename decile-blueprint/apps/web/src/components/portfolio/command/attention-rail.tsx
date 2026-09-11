"use client";

import type { Route } from "next";
import { useState } from "react";
import { CheckCircle2, ChevronRight, CircleAlert, Clock, Info } from "lucide-react";

import type { HealthProblem } from "@/lib/portfolio/command-center";
import { cn } from "@/lib/utils";

/**
 * "Needs attention" — the right-hand rail.
 *
 * Brief: each alert must say *what changed, why it matters, which portfolio is affected, the
 * suggested next step, and when it was detected*; and *"Never phrase analytical alerts as direct
 * investment advice."*
 *
 * That second constraint is not a style note in this product — it is the regulatory posture.
 * Baskfy is not a SEBI-registered adviser (the disclaimer at the foot of every page says so), so
 * "reduce your position in X" is a sentence it must never write. The wording here is therefore
 * always about the DATA and never about the trade: what is inconsistent, what is unmeasured, what
 * is unreconciled. `attention-rail.test.tsx` scans the rendered copy for advice vocabulary, which
 * is a cheaper guard than remembering.
 *
 * Priority is by impact, and it is a word as well as a colour: "Critical", "Review today",
 * "Informational". A reader who cannot separate red from amber still gets the ordering, and so
 * does the screen reader.
 */

export type AttentionLevel = "critical" | "review" | "info";

export interface AttentionItem {
  readonly id: string;
  readonly level: AttentionLevel;
  /** What changed. Names its subject. */
  readonly headline: string;
  /** Why it matters, in terms of what the screen can no longer tell you. */
  readonly detail: string;
  /** The next step — an action on the data, never on a position. */
  readonly action?: { readonly label: string; readonly href: Route } | undefined;
  /** When it was detected. */
  readonly since?: string | undefined;
}

const LEVEL = {
  critical: { word: "Critical", Icon: CircleAlert, className: "text-negative", rank: 0 },
  review: { word: "Review today", Icon: Clock, className: "text-warning", rank: 1 },
  info: { word: "Informational", Icon: Info, className: "text-muted-foreground", rank: 2 },
} as const;

/** Health problems are attention items — one source, rendered in two places. */
export function itemsFromProblems(problems: readonly HealthProblem[]): AttentionItem[] {
  return problems.map((p) => ({
    id: p.id,
    level: p.severity === "action-required" ? "critical" : "review",
    headline: p.headline,
    detail: p.detail,
    since: p.since,
    action: p.href ? { label: "Open", href: p.href as Route } : undefined,
  }));
}

function Item({ item }: { item: AttentionItem }) {
  const level = LEVEL[item.level];
  const { Icon } = level;
  return (
    <li className="px-4 py-3">
      <div className="flex items-start gap-2">
        <Icon aria-hidden="true" className={cn("mt-0.5 size-4 shrink-0", level.className)} />
        <div className="min-w-0">
          <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
            {level.word}
            {item.since ? <span className="ml-1.5 normal-case">· {item.since}</span> : null}
          </p>
          <p className="mt-0.5 text-sm font-medium leading-snug">{item.headline}</p>
          <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{item.detail}</p>
          {item.action ? (
            <a
              href={item.action.href}
              className="mt-1 inline-flex items-center gap-0.5 text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
            >
              {item.action.label}
              <ChevronRight aria-hidden="true" className="size-3" />
            </a>
          ) : null}
        </div>
      </div>
    </li>
  );
}

export function AttentionRail({
  items,
  className,
}: {
  items: readonly AttentionItem[];
  className?: string | undefined;
}) {
  const [open, setOpen] = useState(true);
  const ordered = [...items].sort((a, b) => LEVEL[a.level].rank - LEVEL[b.level].rank);
  const critical = ordered.filter((i) => i.level === "critical").length;

  return (
    <aside
      aria-label="Needs attention"
      data-testid="attention-rail"
      className={cn("rounded-xl border border-border bg-card", className)}
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          Needs attention
          {ordered.length > 0 ? (
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 text-xs tabular-nums",
                critical > 0
                  ? "bg-negative-muted text-negative"
                  : "bg-muted text-muted-foreground",
              )}
            >
              {ordered.length}
            </span>
          ) : null}
        </h2>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          {open ? "Hide" : "Show"}
        </button>
      </div>

      {open ? (
        ordered.length === 0 ? (
          <div className="flex items-start gap-2 px-4 py-4">
            <CheckCircle2 aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-positive" />
            <div>
              <p className="text-sm font-medium">No action required</p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Every holding is filed, every broker has synced, and nothing is waiting on an
                answer from you.
              </p>
            </div>
          </div>
        ) : (
          <ul className="divide-y divide-border/60">
            {ordered.map((item) => (
              <Item key={item.id} item={item} />
            ))}
          </ul>
        )
      ) : null}
    </aside>
  );
}
