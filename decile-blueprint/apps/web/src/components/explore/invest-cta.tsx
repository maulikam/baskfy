"use client";

import { useState } from "react";

import { MarkInvestedForm } from "@/components/cb/mark-invested-form";
import { MarketClosedModal } from "@/components/cb/market-closed-modal";
import { PlanHandoffPanel } from "@/components/cb/plan-handoff-panel";
import { Button } from "@/components/ui/button";

/**
 * Sticky Invest CTA — never an order route. Opens the hand-off panel; market-closed is
 * available as a parallel state for when SC3 wires session hours. Mark-as-invested records
 * the book after the user has already traded at the broker (T8.1).
 */
export function InvestCta({
  basketName,
  basketSlug,
}: {
  basketName: string;
  basketSlug: string;
}) {
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
      {showHandoff ? (
        <div className="space-y-3">
          <PlanHandoffPanel basketName={basketName} />
          <MarkInvestedForm basketSlug={basketSlug} basketName={basketName} />
        </div>
      ) : null}
      <MarketClosedModal open={marketClosed} onOpenChange={setMarketClosed} />
    </div>
  );
}
