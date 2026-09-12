"use client";

import Link from "next/link";
import { CircleAlert, Clock, Info, Lock } from "lucide-react";
import type { ReactNode } from "react";

import { useAmounts } from "@/components/portfolio/amounts";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatTradeDate } from "@/lib/format";
import type { Metric } from "@/lib/portfolio/command-center";
import type { BlockedMetric, WorkspaceState } from "@/lib/portfolio/detail-tabs";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * The pieces every tab in the detail workspace is built from.
 *
 * PC1's `metric-band.tsx` set the visual language and this matches it deliberately: one bordered
 * surface divided by hairlines rather than a scatter of cards, tabular numerals everywhere,
 * direction as a glyph as well as a hue, and the *reason* standing where a missing figure would.
 *
 * THE ONE RULE THESE PRIMITIVES EXIST TO MAKE UNBREAKABLE
 * ------------------------------------------------------
 * There is no component in this file that accepts a raw `string | null`. Everything takes a
 * {@link Metric}, which cannot hold a missing value without also holding the sentence explaining
 * it. That is what makes "never a bare dash" structural rather than a thing eight tabs have to
 * remember — and it is why none of `formatNumber`, `formatRupees`, `formatQuantity` or
 * `formatTradeDate` is ever called here with a null: each of those returns an em dash for one,
 * and an em dash is precisely the output the brief forbids.
 *
 * `detail-workspace.test.tsx` renders every tab against a payload where every optional field is
 * absent and asserts the character never appears. That test is the reason the copy in this
 * directory uses no em dashes of its own either: a rule you can only check by reading is a rule
 * that drifts.
 */

/** Tabular numerals so a column of figures aligns on its decimal point. */
export const FIGURE = "tabular-nums tracking-tight";

export type MetricKind = "rupees" | "percent" | "points" | "text" | "count";

function toneOf(value: string | null): "up" | "down" | "flat" {
  if (value === null) return "flat";
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed === 0) return "flat";
  return parsed > 0 ? "up" : "down";
}

/** Direction survives greyscale and a colourblind reader, because it is a glyph too. */
export function DirectionMark({ tone }: { tone: "up" | "down" | "flat" }) {
  if (tone === "flat") return null;
  return (
    <span aria-hidden="true" className="mr-0.5 text-[0.8em]">
      {tone === "up" ? "▲" : "▼"}
    </span>
  );
}

function renderValue(value: string, kind: MetricKind, signed: boolean, visible: boolean): string {
  if (kind === "text") return value;
  if (!visible && kind === "rupees") return "••••••";
  if (kind === "rupees") {
    const rupees = formatRupees(value, { decimals: 0 });
    if (!signed) return rupees;
    return Number(value) > 0 ? `+${rupees}` : rupees;
  }
  if (kind === "count") return value;
  const sign = signed && Number(value) > 0 ? "+" : "";
  return kind === "points" ? `${sign}${value} pts` : `${sign}${value}%`;
}

/**
 * A figure, or the reason there is not one. There is no third outcome.
 *
 * `compact` is for a dense table cell, where the full sentence would be four lines inside a
 * 90 pixel column. It shows a short reason in words (never a dash), carries the whole sentence on
 * `title` and to a screen reader, and the table prints every distinct sentence once underneath —
 * the idiom `detail-holdings.tsx` established, kept because a tooltip alone is invisible to
 * anyone not holding a mouse.
 */
export function MetricValue({
  metric,
  kind = "rupees",
  signed = false,
  compact = false,
  short = "Not available",
  className,
}: {
  metric: Metric;
  kind?: MetricKind;
  signed?: boolean;
  compact?: boolean;
  /** The words that stand in the cell when there is no figure. Never punctuation. */
  short?: string;
  className?: string;
}) {
  const { visible } = useAmounts();

  if (metric.value === null) {
    const reason = metric.unavailable ?? "No reason was recorded for this figure being absent.";
    if (compact) {
      return (
        <span className={cn("text-xs text-muted-foreground", className)} title={reason}>
          <span aria-hidden="true">{short}</span>
          <span className="sr-only">{`${metric.label}: ${reason}`}</span>
        </span>
      );
    }
    return (
      <span className={cn("block", className)}>
        <span className="flex items-center gap-1 text-sm font-medium text-muted-foreground">
          <CircleAlert aria-hidden="true" className="size-3.5 shrink-0 text-warning" />
          Not available
        </span>
        <span className="mt-0.5 block max-w-[44ch] text-xs leading-snug text-muted-foreground">
          {reason}
        </span>
      </span>
    );
  }

  const tone = signed ? toneOf(metric.value) : "flat";
  return (
    <span
      className={cn(
        FIGURE,
        tone === "up" && "text-positive",
        tone === "down" && "text-negative",
        className,
      )}
    >
      <DirectionMark tone={tone} />
      {renderValue(metric.value, kind, signed, visible)}
      {metric.pct !== null && metric.pct !== undefined ? (
        <span className="ml-1.5 text-sm font-medium opacity-80">
          {Number(metric.pct) > 0 ? "+" : ""}
          {metric.pct}%
        </span>
      ) : null}
    </span>
  );
}

/** A labelled figure, with the definition on the label rather than beside the number. */
export function Figure({
  metric,
  kind = "rupees",
  signed = false,
  emphasis = "normal",
  className,
}: {
  metric: Metric;
  kind?: MetricKind;
  signed?: boolean;
  emphasis?: "hero" | "normal";
  className?: string;
}) {
  return (
    <div className={cn("min-w-0 px-4 py-3", emphasis === "hero" && "md:px-5 md:py-4", className)}>
      <Tooltip>
        <TooltipTrigger asChild>
          <p className="flex cursor-help items-center gap-1 text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
            {metric.label}
            <Info aria-hidden="true" className="size-3 opacity-60" />
          </p>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs text-xs leading-relaxed">
          {metric.definition}
          {metric.since ? (
            <span className="mt-1 block text-muted-foreground">Measured from {metric.since}.</span>
          ) : null}
        </TooltipContent>
      </Tooltip>
      <div className={cn("mt-1 font-semibold", emphasis === "hero" ? "text-2xl md:text-3xl" : "text-base")}>
        <MetricValue metric={metric} kind={kind} signed={signed} />
      </div>
    </div>
  );
}

/** One bordered surface with a heading. The unit every tab is assembled from. */
export function Panel({
  title,
  blurb,
  actions,
  children,
  testId,
  className,
}: {
  title: string;
  blurb?: string;
  actions?: ReactNode;
  children: ReactNode;
  testId?: string;
  className?: string;
}) {
  return (
    <section
      aria-label={title}
      data-testid={testId}
      className={cn("rounded-xl border border-border bg-card", className)}
    >
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">{title}</h2>
          {blurb ? (
            <p className="mt-0.5 max-w-[70ch] text-xs leading-snug text-muted-foreground">{blurb}</p>
          ) : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * The metrics a surface cannot show, said once and plainly instead of as a row of dashes.
 *
 * Rendered *on the tab that would have shown them*. A reader looking for a sector split should
 * find out on the Allocation tab that there is no sector map, rather than by reading a design
 * document, and they should be told what it would take rather than being told no.
 */
export function NotYetMeasured({
  items,
  heading,
  intro,
  testId,
}: {
  items: readonly BlockedMetric[];
  heading: string;
  intro: string;
  testId: string;
}) {
  return (
    <section
      aria-label={heading}
      data-testid={testId}
      className="rounded-xl border border-dashed border-border bg-muted/20"
    >
      <div className="border-b border-border/60 px-4 py-3">
        <h2 className="flex items-center gap-1.5 text-sm font-semibold">
          <Lock aria-hidden="true" className="size-3.5 text-muted-foreground" />
          {heading}
        </h2>
        <p className="mt-0.5 max-w-[76ch] text-xs leading-snug text-muted-foreground">{intro}</p>
      </div>
      <ul className="divide-y divide-border/50">
        {items.map((item) => (
          <li key={item.name} className="px-4 py-2.5" data-testid={`blocked-${slug(item.name)}`}>
            <p className="text-sm font-medium">{item.name}</p>
            <p className="mt-0.5 max-w-[76ch] text-xs leading-snug text-muted-foreground">
              Not shown because {item.why}.
            </p>
            <p className="mt-0.5 max-w-[76ch] text-xs leading-snug text-muted-foreground">
              <span className="font-medium text-foreground">It needs:</span> {item.unblockedBy}.
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function slug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

const SEVERITY = {
  critical: { word: "Action required", Icon: CircleAlert, className: "text-negative" },
  review: { word: "Worth a look", Icon: Clock, className: "text-warning" },
  info: { word: "For information", Icon: Info, className: "text-muted-foreground" },
} as const;

/**
 * One of the states the brief names, with exactly one next step.
 *
 * Exactly one is the rule, and it is asserted rather than remembered. Two buttons on a problem
 * hands the decision back to the person who came here to be told what to do, and the second
 * button is always the one nobody presses.
 *
 * Severity is a word as well as a colour and an icon, so the ordering survives greyscale.
 */
export function StateCallout({ state }: { state: WorkspaceState }) {
  const tone = SEVERITY[state.severity];
  const { Icon } = tone;
  return (
    <li
      data-testid={`state-${state.id}`}
      data-severity={state.severity}
      className="flex gap-2.5 px-4 py-3"
    >
      <Icon aria-hidden="true" className={cn("mt-0.5 size-4 shrink-0", tone.className)} />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">
          {state.headline}
          <span className="ml-2 text-xs font-normal text-muted-foreground">{tone.word}</span>
        </p>
        <p className="mt-0.5 max-w-[76ch] text-xs leading-snug text-muted-foreground">
          {state.detail}
        </p>
        <p className="mt-1 flex flex-wrap items-center gap-2 text-xs">
          <Link
            href={state.action.href}
            className="font-medium text-brand-strong underline-offset-4 hover:underline"
          >
            {state.action.label}
          </Link>
          <span className="text-muted-foreground">{state.since}</span>
        </p>
      </div>
    </li>
  );
}

/** A share of something, as a bar and a number. Shape reads faster than digits. */
export function ShareBar({ metric, className }: { metric: Metric; className?: string }) {
  const width = metric.value === null ? 0 : Math.max(0, Math.min(100, Number(metric.value)));
  return (
    <span className={cn("flex items-center justify-end gap-2", className)}>
      <span aria-hidden="true" className="h-1.5 w-12 overflow-hidden rounded-full bg-muted">
        <span className="block h-full bg-accent" style={{ width: `${width}%` }} />
      </span>
      <MetricValue metric={metric} kind="percent" compact short="not priced" />
    </span>
  );
}

/** A date the payload actually sent. Never called with a null, by construction. */
export function TradeDate({ iso }: { iso: string }) {
  return <span className={FIGURE}>{formatTradeDate(iso)}</span>;
}

/** A definition attached to a dense header, where a full tooltip would be too much furniture. */
export function HeaderLabel({ label, definition }: { label: string; definition: string }) {
  return (
    <span title={definition} className="cursor-help border-b border-dotted border-border/80">
      {label}
    </span>
  );
}
