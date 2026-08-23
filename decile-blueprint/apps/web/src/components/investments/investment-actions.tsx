"use client";

import { useState } from "react";

import { MarketClosedModal } from "@/components/cb/market-closed-modal";
import { PlanHandoffPanel } from "@/components/cb/plan-handoff-panel";
import { Button } from "@/components/ui/button";

/**
 * Order-shaped CTAs on investor surfaces — Invest more / Exit / Rebalance.
 * Never posts an order; always opens PlanHandoffPanel or MarketClosedModal (05-ui-spec).
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
  const [marketClosed, setMarketClosed] = useState(false);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {actions.map((kind) => (
          <Button
            key={kind}
            type="button"
            variant={kind === "exit" ? "outline" : "primary"}
            size="sm"
            onClick={() => setHandoffKind(kind)}
          >
            {LABELS[kind]}
          </Button>
        ))}
        <Button type="button" variant="ghost" size="sm" onClick={() => setMarketClosed(true)}>
          If market closed…
        </Button>
      </div>
      {handoffKind ? (
        <PlanHandoffPanel
          basketName={`${basketName} · ${LABELS[handoffKind].toLowerCase()}`}
        />
      ) : null}
      <MarketClosedModal open={marketClosed} onOpenChange={setMarketClosed} />
    </div>
  );
}
