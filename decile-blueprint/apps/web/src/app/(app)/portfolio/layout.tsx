import type { ReactNode } from "react";

import { BrokerSessionBanner } from "@/components/portfolio/broker-session-banner";

/**
 * The shared frame for every Portfolio tab — M84 / audit 4.12.
 *
 * An expired Kite session makes every number below it stale, and a stale number looks exactly
 * like a fresh one. The banner lives here so Overview, Portfolios, Holdings, Activity and
 * Watchlist inherit it without five copies that drift.
 *
 * Live price polling moved to `AppShell`: screens, scanners and the portfolio all share one
 * refresh while a Kite session exists and the market is open.
 */
export default function PortfolioLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col gap-4">
      <BrokerSessionBanner />
      {children}
    </div>
  );
}
