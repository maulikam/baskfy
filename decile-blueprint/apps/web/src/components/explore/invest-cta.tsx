"use client";

import { useState } from "react";

import { MarketClosedModal } from "@/components/cb/market-closed-modal";
import { PlanHandoffPanel } from "@/components/cb/plan-handoff-panel";
import { Button } from "@/components/ui/button";

/**
 * Sticky Invest CTA — never an order route. Opens the hand-off panel; market-closed is
 * available as a parallel state for when SC3 wires session hours.
 */
export function InvestCta({ basketName }: { basketName: string }) {
  const [showHandoff, setShowHandoff] = useState(false);
  const [marketClosed, setMarketClosed] = useState(false);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="primary" onClick={() => setShowHandoff(true)}>
          Invest now
        </Button>
        <Button type="button" variant="outline" onClick={() => setMarketClosed(true)}>
          If market closed…
        </Button>
      </div>
      {showHandoff ? <PlanHandoffPanel basketName={basketName} /> : null}
      <MarketClosedModal open={marketClosed} onOpenChange={setMarketClosed} />
    </div>
  );
}
