import Link from "next/link";

import { Button } from "@/components/ui/button";
import { DESK_CONSOLE_URL } from "@/lib/site";
import { cn } from "@/lib/utils";

/**
 * Plan hand-off — docs/smallcase/05. Web never places orders; Invest CTAs end here.
 *
 * Stub until SC3 plan generation lands: shows the framing and points at the desk console.
 * When a real `plan_id` arrives, the copy switches to the expiry sentence.
 */

export { DESK_CONSOLE_URL };

export interface PlanHandoffPanelProps {
  basketName?: string;
  planId?: string | null;
  expiresAt?: string | null;
  className?: string;
}

export function PlanHandoffPanel({
  basketName,
  planId,
  expiresAt,
  className,
}: PlanHandoffPanelProps) {
  const hasPlan = Boolean(planId);

  return (
    <aside
      aria-label="Plan hand-off"
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-border bg-card p-4",
        className,
      )}
    >
      <div className="space-y-1">
        <h2 className="text-sm font-semibold text-foreground">
          {hasPlan ? `Plan #${planId} created` : "Ready when the desk is"}
        </h2>
        <p className="text-sm leading-relaxed text-muted-foreground">
          {hasPlan ? (
            <>
              Open the desk console to execute
              {basketName ? ` ${basketName}` : ""}. Plans expire
              {expiresAt ? ` at ${expiresAt}` : " in 30 minutes"} — nothing here can place an
              order.
            </>
          ) : (
            <>
              Investing builds a read-only plan first. Execution stays in the desk console
              {basketName ? ` for ${basketName}` : ""}. This page never gains an order route.
            </>
          )}
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button asChild variant="primary" size="sm">
          <a href={DESK_CONSOLE_URL} rel="noopener noreferrer" target="_blank">
            Open desk console
          </a>
        </Button>
        <Button asChild variant="outline" size="sm">
          <Link href="/discover">Today&apos;s scan basket</Link>
        </Button>
      </div>
    </aside>
  );
}
