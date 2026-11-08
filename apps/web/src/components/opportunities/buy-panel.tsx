"use client";

import { BuyTicketButton } from "@/components/orders/buy-ticket";
import { Button } from "@/components/ui/button";
import type { SwingOpportunity } from "@/lib/opportunities/types";
import { useLiveMarks } from "@/lib/screens/live-marks";

export function SwingBuyButton({ opportunity }: { opportunity: SwingOpportunity }) {
  const live = useLiveMarks([opportunity.symbol]);
  const lastPrice =
    live.marks[opportunity.symbol] ??
    live.quotes[opportunity.symbol]?.lastPrice ??
    opportunity.close ??
    opportunity.entry.price;
  const trigger = opportunity.trigger.level || opportunity.entry.price;
  const stop = opportunity.invalidation;
  return (
    <BuyTicketButton
      symbol={opportunity.symbol}
      name={opportunity.name}
      instrumentId={opportunity.instrument_id}
      price={lastPrice}
      source="opportunities"
      setup={opportunity.setup}
      entry={trigger}
      stop={stop > 0 && stop < trigger ? stop : null}
      target={opportunity.targets[0]?.price ?? null}
    />
  );
}

export function OptionsBuyUnavailable() {
  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      disabled
      title="Options Buy is not wired — the desk F&O execute path is F3 credit spreads, not these structures."
    >
      Buy
    </Button>
  );
}
