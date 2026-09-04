"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { isMarketOpen } from "@/lib/market/session";

/**
 * Re-render the portfolio on a timer while the NSE session is open — M84.
 *
 * The portfolio pages are server components, and M82 made them value positions at Kite's
 * `last_price` instead of the previous close. That made the numbers live *per render*, which meant
 * they only moved when you navigated. Maulik asked for the market, and a price that updates when
 * you happen to click is not the market.
 *
 * `router.refresh()` rather than a client fetch: the pricing lives in the server component, behind
 * a 20-second memo, so a refresh picks up new marks without duplicating the valuation logic in the
 * browser or exposing a second endpoint. Two refreshes inside that memo window cost one Kite call.
 *
 * **Stops when the market does.** Outside 09:15–15:30 IST the price cannot change, so polling would
 * be pure load on Kite and on the box for numbers that are already final. The interval keeps
 * running but does nothing, so the page starts refreshing on its own when the session opens
 * without the reader having to reload.
 */
const POLL_MS = 30_000;

export function LiveRefresh({ intervalMs = POLL_MS }: { intervalMs?: number }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const tick = () => {
      const marketOpen = isMarketOpen();
      setOpen(marketOpen);
      // `document.hidden`: a background tab refreshing every 30s is load nobody is reading.
      if (marketOpen && !document.hidden) router.refresh();
    };
    tick();
    const id = window.setInterval(tick, intervalMs);
    return () => window.clearInterval(id);
  }, [router, intervalMs]);

  if (!open) return null;
  return (
    <p data-testid="live-refresh" className="text-xs text-muted-foreground">
      Prices update while the market is open.
    </p>
  );
}
