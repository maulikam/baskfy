import type { ReactNode } from "react";

import { BrokerSessionBanner } from "@/components/portfolio/broker-session-banner";
import { LiveRefresh } from "@/components/portfolio/live-refresh";
import { fetchPortfolioOverview } from "@/lib/portfolio/fetch";

/**
 * The shared frame for every Portfolio tab — M84 / audit 4.12.
 *
 * Added because two things belong on all of them and were on none. An expired Kite session makes
 * every number below it stale, and a stale number looks exactly like a fresh one; and prices only
 * moved when the reader happened to navigate. Putting both in the layout means Overview,
 * Portfolios, Holdings, Activity and Watchlist inherit them without five copies that drift.
 *
 * `LiveRefresh` polls only while the market is open **and** the overview reports a live overlay
 * (a Kite session that returned last_price). Without the overlay, refresh is pure load.
 */
export default async function PortfolioLayout({ children }: { children: ReactNode }) {
  const overview = await fetchPortfolioOverview().catch(() => null);
  return (
    <div className="flex flex-col gap-4">
      <BrokerSessionBanner />
      {children}
      <LiveRefresh overlayActive={overview?.live_overlay === true} />
    </div>
  );
}
