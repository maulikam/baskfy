"use client";

import { Loader2, RotateCcw, Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** Floating pill when filter edits are unsaved (§1.4). */
export function ApplyFiltersPill({
  changeCount,
  saving,
  onApply,
  onReset,
  className,
}: {
  changeCount: number;
  saving: boolean;
  onApply: () => void;
  onReset: () => void;
  className?: string;
}) {
  if (changeCount <= 0) return null;

  const label = changeCount === 1 ? "1 change" : `${changeCount} changes`;

  return (
    <div
      role="status"
      className={cn(
        "vaaya-pill-bar fixed bottom-20 left-1/2 z-[60] flex -translate-x-1/2 items-center gap-2 px-3 py-2 md:bottom-8",
        "motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-2",
        className,
      )}
      data-testid="apply-filters-pill"
    >
      <span className="px-1 text-sm font-light tabular-nums text-muted-foreground">{label}</span>
      <span className="sr-only" data-testid="unsaved-badge">
        Unsaved changes
      </span>
      <Button
        variant="secondary"
        size="sm"
        className="rounded-full px-4"
        disabled={saving}
        data-testid="apply-filters"
        onClick={onApply}
      >
        {saving ? (
          <Loader2 aria-hidden="true" className="animate-spin" />
        ) : (
          <Save aria-hidden="true" />
        )}
        Apply
      </Button>
      <Button
        variant="ghost"
        size="sm"
        className="rounded-full"
        disabled={saving}
        onClick={onReset}
        aria-label="Reset filters"
      >
        <RotateCcw aria-hidden="true" />
        Reset
      </Button>
    </div>
  );
}
