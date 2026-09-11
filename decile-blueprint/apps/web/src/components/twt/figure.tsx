import type { ReactNode } from "react";

import { cn } from "@/lib/utils";
import type { Figure } from "@/lib/twt/numbers";

/**
 * A number, or the reason there is not one. **Never a dash** — TW8, and the rule the portfolio
 * work established on 11 Sep 2026.
 *
 * The brief that produced the rule: *"never display '—' without explaining why the value is
 * unavailable"*. A dash is the worst of both answers — it neither gives the figure nor admits
 * that the figure is missing, so the reader supplies their own explanation, and on a page about
 * resting stops the explanation they supply may be "there is no risk here".
 *
 * The reason is written for the person reading the screen, not for the person who would fix it:
 * "No live price for this name yet", never the route that would have served one. The engineering
 * detail belongs in the comment beside the string in `@/lib/twt/copy`.
 */
export function FigureValue({
  figure,
  className,
  tone,
}: {
  figure: Figure;
  className?: string;
  /** A semantic token class, applied only to a real value — an absence is never green or red. */
  tone?: string;
}) {
  if (figure.value !== null) {
    return <span className={cn("tabular-nums", tone, className)}>{figure.value}</span>;
  }
  return (
    <span
      className={cn("text-xs font-normal text-muted-foreground", className)}
      data-testid="twt-unavailable"
    >
      {figure.unavailable}
    </span>
  );
}

/**
 * A labelled figure for a card, with its label always beside it.
 *
 * The label is not optional because a number nobody named is a number a reader has to guess the
 * meaning of, and this page's numbers are levels at which shares are sold.
 */
export function FigureCell({
  label,
  figure,
  tone,
  hint,
}: {
  label: string;
  figure: Figure;
  tone?: string;
  hint?: ReactNode;
}) {
  return (
    <div className="space-y-0.5">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="text-base font-medium">
        <FigureValue figure={figure} {...(tone ? { tone } : {})} />
      </div>
      {hint ? <div className="text-xs text-muted-foreground">{hint}</div> : null}
    </div>
  );
}
