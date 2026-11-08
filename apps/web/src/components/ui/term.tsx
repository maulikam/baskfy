"use client";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { TERMS, type TermId } from "@/lib/vocabulary";
import { cn } from "@/lib/utils";

/**
 * A plain-English label with the professional one a hover away (M36).
 *
 * The trigger is a `<button>` rather than a styled `<span>` on purpose: a tooltip that only opens
 * on hover is invisible to a keyboard and to a touch screen, and the explanation is the part of
 * this component that carries the meaning. A button is focusable, tappable, and announced.
 *
 * The dotted underline is the affordance. It reads as "there is more here" without looking like a
 * link, which would promise navigation this does not do.
 */
export function Term({ id, className }: { id: TermId; className?: string }) {
  const entry = TERMS[id];

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className={cn(
            "cursor-help decoration-dotted underline-offset-4 hover:decoration-solid",
            "underline decoration-border decoration-1 transition-colors duration-150 hover:decoration-muted-foreground",
            className,
          )}
        >
          {entry.label}
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs space-y-1 text-left">
        <p className="font-medium">{entry.term}</p>
        <p className="text-xs leading-relaxed opacity-90">{entry.plain}</p>
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * The explanation on its own, as a small mark beside a label somebody else is rendering.
 *
 * A table's column heading is already a button — it sorts — and an interactive element cannot
 * contain another one: React refuses to hydrate it and a screen reader cannot describe it. So a
 * sortable heading gets the plain label inside its own sort button and this mark next to it,
 * which is a sibling rather than a child.
 */
export function TermHint({ id, className }: { id: TermId; className?: string }) {
  const entry = TERMS[id];

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`What ${entry.label} means`}
          className={cn(
            "grid size-4 shrink-0 place-items-center rounded-full border border-border",
            "text-[9px] font-semibold leading-none text-muted-foreground",
            "transition-colors duration-150 hover:border-muted-foreground hover:text-foreground",
            className,
          )}
        >
          <span aria-hidden="true">?</span>
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs space-y-1 text-left">
        <p className="font-medium">{entry.term}</p>
        <p className="text-xs leading-relaxed opacity-90">{entry.plain}</p>
      </TooltipContent>
    </Tooltip>
  );
}

/** The plain label alone, for a place that cannot host a tooltip — a `<title>`, a chart legend. */
export function termLabel(id: TermId): string {
  return TERMS[id].label;
}
