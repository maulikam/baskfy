import type { ReactNode } from "react";

import { BrokerSessionBanner } from "@/components/portfolio/broker-session-banner";
import { LiveRefresh } from "@/components/portfolio/live-refresh";

/**
 * The shared frame for every Portfolio tab — M84.
 *
 * Added because two things belong on all of them and were on none. An expired Kite session makes
 * every number below it stale, and a stale number looks exactly like a fresh one; and prices only
 * moved when the reader happened to navigate. Putting both in the layout means Overview,
 * Portfolios, Holdings, Activity and Watchlist inherit them without five copies that drift.
 */
export default function PortfolioLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col gap-4">
      <BrokerSessionBanner />
      {children}
      <LiveRefresh />
    </div>
  );
}
