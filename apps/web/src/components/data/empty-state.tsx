import { SearchX } from "lucide-react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * docs/08 §"Results panel":
 *
 *     "Empty state must explain *why*: '0 results — the 1-year filters exclude instruments listed
 *      after 19 Aug 2025', with a one-click 'loosen this filter' affordance."
 *
 * So `reason` is required, not optional: an empty state that only says "no results" is the one
 * this specification exists to forbid. `action` is the affordance.
 */
export interface EmptyStateProps {
  title: string;
  /** Why there is nothing here. Required — see above. */
  reason: string;
  action?: { label: string; onClick: () => void } | undefined;
  icon?: React.ReactNode;
  className?: string | undefined;
}

export function EmptyState({ title, reason, action, icon, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 rounded-md border border-dashed border-border px-6 py-12 text-center",
        className,
      )}
    >
      <span aria-hidden="true" className="text-muted-foreground">
        {icon ?? <SearchX className="size-6" />}
      </span>
      <div className="space-y-1">
        <p className="text-sm font-medium">{title}</p>
        <p className="mx-auto max-w-prose text-sm text-muted-foreground">{reason}</p>
      </div>
      {action ? (
        <Button variant="outline" size="sm" onClick={action.onClick}>
          {action.label}
        </Button>
      ) : null}
    </div>
  );
}
