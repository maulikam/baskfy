"use client";

import { useId } from "react";
import { CircleAlert, Info, Lock } from "lucide-react";

import { Label } from "@/components/ui/label";
import type { Metric } from "@/lib/portfolio/command-center";
import type { ManageAction } from "@/lib/portfolio/manage";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * The small pieces every panel in the manage drawer shares.
 *
 * They are here rather than repeated because two of them carry rules rather than styling:
 * {@link MetricLine} is the only way a rupee figure reaches this drawer, and {@link Unavailable}
 * is the only way an absent capability does. Having one of each is what makes "never a bare dash"
 * and "no dead control" properties of the drawer instead of things to remember per panel.
 */

export function PanelHeading({
  title,
  children,
}: {
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children ? (
        <p className="text-xs leading-relaxed text-muted-foreground">{children}</p>
      ) : null}
    </div>
  );
}

/** A labelled control. The label is a real `<label for>`, so the hit area is the words too. */
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string | undefined;
  children: (id: string) => React.ReactNode;
}) {
  const id = useId();
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children(id)}
      {hint ? <p className="text-xs leading-snug text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

/**
 * A rupee figure, or the reason there is not one.
 *
 * §6.2 rule 1, structurally: the type cannot hold a missing value without holding the
 * explanation, and this is the only renderer, so there is no path to an unexplained "—".
 */
export function MetricLine({
  metric,
  emphasis = false,
}: {
  metric: Metric;
  emphasis?: boolean;
}) {
  const missing = metric.value === null;
  return (
    <div className="min-w-0">
      <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
        {metric.label}
      </p>
      {missing ? (
        <p
          className="mt-0.5 flex cursor-help items-center gap-1 text-sm font-medium text-muted-foreground"
          title={metric.unavailable ?? undefined}
        >
          <CircleAlert aria-hidden="true" className="size-3.5 shrink-0 text-warning" />
          Needs more data
        </p>
      ) : (
        <p
          className={cn(
            "mt-0.5 font-semibold tabular-nums tracking-tight",
            emphasis ? "text-lg" : "text-sm",
          )}
        >
          {formatRupees(metric.value, { decimals: 0 })}
        </p>
      )}
    </div>
  );
}

/**
 * A capability Baskfy does not have, named rather than drawn.
 *
 * There is no button here on purpose. A greyed-out "Archive" with a tooltip is still a control —
 * it occupies the place a working one would, it invites the click that teaches the user the
 * product is broken, and it says nothing about *when* it might work. This says what the feature
 * would do, why it cannot, and what would unblock it, and offers nothing to press.
 */
export function Unavailable({ action }: { action: ManageAction }) {
  const availability = action.availability;
  if (availability.kind !== "unavailable") return null;
  return (
    <li
      data-testid={`unavailable-${action.id}`}
      data-action={action.id}
      className="flex gap-2.5 py-3"
    >
      <Lock aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 space-y-1">
        <p className="text-sm font-medium">
          {action.title}
          {/* A word as well as an icon: the state survives a greyscale screenshot. */}
          <span className="ml-2 align-middle text-[0.6875rem] font-normal uppercase tracking-wide text-muted-foreground">
            Not available yet
          </span>
        </p>
        <p className="text-xs leading-relaxed text-muted-foreground">{action.blurb}</p>
        <p className="text-xs leading-relaxed text-muted-foreground">
          <span className="font-medium text-foreground">Why: </span>
          {availability.reason}
        </p>
        <p className="text-xs leading-relaxed text-muted-foreground">
          <span className="font-medium text-foreground">What would unblock it: </span>
          {availability.unblockedBy}
        </p>
      </div>
    </li>
  );
}

/**
 * A sentence the drawer states rather than implies — the monitoring-view notice, the
 * net-worth-unchanged notice, the exclusivity invariant.
 *
 * Rendered as a note with an icon, never as a colour alone and never as a footnote. The brief's
 * own wording for the monitoring case is required text, so it is passed through verbatim.
 */
export function Notice({
  children,
  tone = "info",
  testId,
}: {
  children: React.ReactNode;
  tone?: "info" | "warning";
  testId?: string | undefined;
}) {
  return (
    <p
      data-testid={testId}
      className={cn(
        "flex items-start gap-2 rounded-lg border px-3 py-2 text-xs leading-relaxed",
        tone === "warning"
          ? "border-warning/40 bg-warning-muted text-foreground"
          : "border-border bg-muted/60 text-muted-foreground",
      )}
    >
      {tone === "warning" ? (
        <CircleAlert aria-hidden="true" className="mt-px size-3.5 shrink-0 text-warning" />
      ) : (
        <Info aria-hidden="true" className="mt-px size-3.5 shrink-0 opacity-70" />
      )}
      <span>{children}</span>
    </p>
  );
}
