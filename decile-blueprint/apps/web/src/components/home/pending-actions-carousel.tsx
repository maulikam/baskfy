"use client";

import { useState, useTransition } from "react";

import { PendingActionCard } from "@/components/pending-action-card";
import type { MutationResult } from "@/lib/auth/me";
import type { PendingActionBrief } from "@/lib/investments/fetch";
import { cn } from "@/lib/utils";

/**
 * The pending-actions carousel — docs/smallcase/05 §6.1, SC9.
 *
 * Three things the observed product's carousel does, and this one keeps:
 *
 * 1. It **ends in a terminator card**. A carousel that just stops leaves the reader wondering
 *    whether there is another swipe of bad news off-screen; "That's all" is the answer, and it
 *    is the reason the empty case renders the same card rather than rendering nothing.
 * 2. Each card is **dismissible**, and dismissal is optimistic — the card leaves on the click,
 *    not on the round trip, because the alternative is a person clicking X twice.
 * 3. Nothing here resolves anything. Dismissing hides a card; the underlying drift or rebalance
 *    is still there, and the flow that fixes it is the only thing that may call resolve. No
 *    order-shaped affordance appears on this component at all (Track C / PACK.2).
 *
 * If the dismiss fails the card comes **back**, with the reason. A card that vanished on a
 * failed request would be the product quietly deciding the person had dealt with it.
 */

export const TERMINATOR_COPY = "That's all — no other actions need your attention.";

export interface PendingActionsCarouselProps {
  actions: readonly PendingActionBrief[];
  /** Server action; injected so the component stays renderable in a unit test. */
  dismiss: (id: string) => Promise<MutationResult>;
  className?: string;
}

export function PendingActionsCarousel({
  actions,
  dismiss,
  className,
}: PendingActionsCarouselProps) {
  const [hidden, setHidden] = useState<readonly string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  const visible = actions.filter((action) => !hidden.includes(action.id));

  function onDismiss(id: string) {
    setError(null);
    setHidden((current) => [...current, id]);
    startTransition(() => {
      void dismiss(id).then((result) => {
        if (!result.ok) {
          setHidden((current) => current.filter((entry) => entry !== id));
          setError(result.message);
        }
      });
    });
  }

  return (
    <section
      aria-label="Pending actions"
      data-testid="pending-actions-carousel"
      className={cn("space-y-2", className)}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold">Needs a decision</h2>
        {visible.length > 0 ? (
          <p className="text-xs text-muted-foreground">
            {visible.length} waiting on you
          </p>
        ) : null}
      </div>

      {error ? (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      ) : null}

      {/* Horizontal on a phone the way the observed product is, wrapping on a desktop where
          there is room. Scroll snapping so a half-card never becomes the resting state. */}
      <ul className="flex snap-x snap-mandatory gap-3 overflow-x-auto pb-1 [scrollbar-width:thin]">
        {visible.map((action) => (
          <li
            key={action.id}
            className="relative min-w-[17rem] max-w-[22rem] flex-1 snap-start"
          >
            <PendingActionCard
              type={action.type}
              title={action.title}
              body={action.body}
              className="h-full pr-10"
            />
            <button
              type="button"
              onClick={() => onDismiss(action.id)}
              aria-label={`Dismiss: ${action.title}`}
              data-testid={`dismiss-${action.id}`}
              className="absolute right-2 top-2 grid size-7 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-foreground/5 hover:text-foreground"
            >
              <span aria-hidden="true">×</span>
            </button>
          </li>
        ))}

        <li className="min-w-[17rem] max-w-[22rem] flex-1 snap-start">
          <p
            data-testid="pending-actions-terminator"
            className="grid h-full place-items-center rounded-xl border border-dashed border-border bg-card/50 px-4 py-3 text-center text-sm text-muted-foreground"
          >
            {TERMINATOR_COPY}
          </p>
        </li>
      </ul>
    </section>
  );
}
