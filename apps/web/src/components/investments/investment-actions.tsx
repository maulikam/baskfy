"use client";

import { useState } from "react";

import { PlanHandoffPanel } from "@/components/cb/plan-handoff-panel";
import { Button } from "@/components/ui/button";
import { isMarketOpen } from "@/lib/market/session";

/**
 * Order-shaped CTAs on investor surfaces — Invest more / Exit / Rebalance.
 *
 * AFH 5.3: secondary until `/cb/plans/*` is wired with a real `planId`. Never posts an order;
 * opens PlanHandoffPanel (desk stub). Market-closed is an inline line, not a hollow modal CTA.
 */

export type InvestmentActionKind = "invest_more" | "exit" | "rebalance";

const LABELS: Record<InvestmentActionKind, string> = {
  invest_more: "Invest more",
  exit: "Exit",
  rebalance: "Rebalance",
};

export interface InvestmentActionsProps {
  basketName: string;
  actions?: InvestmentActionKind[];
}

export function InvestmentActions({
  basketName,
  actions = ["invest_more", "exit", "rebalance"],
}: InvestmentActionsProps) {
  const [handoffKind, setHandoffKind] = useState<InvestmentActionKind | null>(null);
  const marketOpen = isMarketOpen();

  return (
    <div className="space-y-3">
      {!marketOpen ? (
        <p
          role="status"
          data-testid="market-closed-inline"
          className="text-sm text-muted-foreground"
        >
          Market is closed. You can still review a plan; the desk will refuse execution until the
          session is open.
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        {actions.map((kind) => (
          <Button
            key={kind}
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setHandoffKind(kind)}
          >
            {LABELS[kind]}
          </Button>
        ))}
      </div>
      {handoffKind ? (
        <PlanHandoffPanel
          basketName={`${basketName} · ${LABELS[handoffKind].toLowerCase()}`}
        />
      ) : null}
    </div>
  );
}
