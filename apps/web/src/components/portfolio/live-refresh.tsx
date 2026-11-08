"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { isMarketOpen } from "@/lib/market/session";

/**
 * Re-render the portfolio on a timer while the NSE session is open **and** a live overlay is on
 * (audit 4.1 / 4.12).
 *
 * The portfolio pages are server components, and M82 made them value positions at Kite's
 * `last_price` instead of the previous close — when a Kite session exists. Without that overlay,
 * refreshing every 30 s is pure load for numbers that cannot move.
 *
 * `router.refresh()` rather than a client fetch: the pricing lives in the server component, behind
 * a 20-second memo, so a refresh picks up new marks without duplicating the valuation logic in the
 * browser or exposing a second endpoint. Two refreshes inside that memo window cost one Kite call.
 *
 * **Stops when the market does, and when the overlay is off.** Outside 09:15–15:30 IST the price
 * cannot change; without a Kite session there is nothing live to poll.
 */
const POLL_MS = 30_000;

export function LiveRefresh({
  intervalMs = POLL_MS,
  /** True when the API is overlaying Kite last_price (a live broker session exists). */
  overlayActive = false,
}: {
  intervalMs?: number;
  overlayActive?: boolean;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const tick = () => {
      const marketOpen = isMarketOpen();
      setOpen(marketOpen && overlayActive);
      // `document.hidden`: a background tab refreshing every 30s is load nobody is reading.
      if (marketOpen && overlayActive && !document.hidden) router.refresh();
    };
    tick();
    const id = window.setInterval(tick, intervalMs);
    return () => window.clearInterval(id);
  }, [router, intervalMs, overlayActive]);

  if (!open) return null;
  return (
    <p data-testid="live-refresh" className="text-xs text-muted-foreground">
      Close, plus live overlay while the market is open and a Kite session is connected.
    </p>
  );
}
