import type { ReactNode } from "react";

import { StocksWatchSection } from "@/components/watchlist/stocks-section";

/**
 * Appends the Stocks section under the existing basket watchlist without editing that page
 * (AF I.2). Baskets stay above; stocks below.
 */

export default async function PortfolioWatchlistLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col gap-8">
      {children}
      <StocksWatchSection />
    </div>
  );
}
