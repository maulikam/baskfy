"use client";

import Link from "next/link";
import type { Route } from "next";

import { useSelection } from "@/components/discover/selection-provider";
import { canCompare, compareHref, selectionSummary } from "@/lib/discover/selection";

/**
 * The bar that appears once something is selected: "2 of 3 baskets selected — Compare".
 *
 * It is `sticky` at the bottom of the page rather than `fixed` over it. Fixed would sit on top of
 * the last row of every long catalogue for as long as anything is selected, and this product's
 * own header made exactly that decision for exactly that reason (`shell/top-nav.tsx`).
 *
 * With one basket selected the bar still shows, saying what is missing. Hiding it until two are
 * picked leaves a reader who has selected one with no evidence that anything happened, and no
 * way to unselect except finding the card again.
 */
export function CompareBar() {
  const { selection, clear, hydrated } = useSelection();
  if (!hydrated || selection.length === 0) return null;

  const ready = canCompare(selection);

  return (
    <div
      className="sticky bottom-3 z-10 mt-6"
      role="region"
      aria-label="Basket comparison"
      data-testid="compare-bar"
      data-count={selection.length}
    >
      <div className="mx-auto flex w-full max-w-3xl flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card/95 px-4 py-3 shadow-lg backdrop-blur">
        <div className="min-w-0">
          <p className="text-sm font-medium">{selectionSummary(selection)}</p>
          <p className="truncate text-xs text-muted-foreground">
            {ready
              ? "Compared over the longest period all of them share."
              : "Pick one more to compare."}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={clear}
            className="rounded-md px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            Clear
          </button>
          {ready ? (
            <Link
              href={compareHref(selection) as Route}
              data-testid="compare-bar-go"
              className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-accent-foreground transition-opacity hover:opacity-90"
            >
              Compare
            </Link>
          ) : (
            <span
              aria-disabled="true"
              className="cursor-not-allowed rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground"
            >
              Compare
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
