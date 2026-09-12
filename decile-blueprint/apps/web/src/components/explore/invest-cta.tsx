"use client";

import { useState } from "react";

import { MarkInvestedForm } from "@/components/cb/mark-invested-form";
import { MarketClosedModal } from "@/components/cb/market-closed-modal";
import { KiteBasketInvest } from "@/components/cb/kite-basket-invest";
import { Button } from "@/components/ui/button";

/**
 * Sticky Invest CTA — never an order route. Opens the hand-off; market-closed is available as a
 * parallel state for when SC3 wires session hours. Mark-as-invested records the book after the
 * user has already traded at the broker (T8.1).
 *
 * **What "Invest now" opens changed on 27 Aug 2026.** It used to be `PlanHandoffPanel`, whose
 * button pointed at `desk.modelbasket.in` — the *operator* console. That is Maulik's own desk,
 * reachable by nobody else, so for every signed-in user the invest flow ended at a door they
 * could not open. It now opens the Kite hand-off: choose an amount, and the basket goes to the
 * user's **own** Zerodha account for them to review and confirm.
 *
 * The posture is unchanged and is the one `broker_connections.BROKER_OAUTH_REVIEW` states —
 * "publish baskets; user executes in their own broker account after confirm". Baskfy still places
 * nothing; what moved is *whose* terminal the plan is handed to.
 */
export function InvestCta({
  basketName,
  basketSlug,
  minAmount = null,
}: {
  basketName: string;
  basketSlug: string;
  /** Catalog min. amount — warned when the typed amount is below it (audit §1.2). */
  minAmount?: string | number | null;
}) {
  const [showHandoff, setShowHandoff] = useState(false);
  const [marketClosed, setMarketClosed] = useState(false);

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="primary" onClick={() => setShowHandoff(true)}>
          Invest now
        </Button>
        <Button type="button" variant="outline" onClick={() => setMarketClosed(true)}>
          If market closed…
        </Button>
      </div>
      {showHandoff ? (
        <div className="min-w-0 space-y-3 overflow-x-hidden">
          <KiteBasketInvest
            basketSlug={basketSlug}
            basketName={basketName}
            minAmount={minAmount}
          />
          <MarkInvestedForm basketSlug={basketSlug} basketName={basketName} />
        </div>
      ) : null}
      <MarketClosedModal open={marketClosed} onOpenChange={setMarketClosed} />
    </div>
  );
}
