import type { Metadata } from "next";

import { PageHeader } from "@/components/shell/page-header";
import { WatchBoardPanel } from "@/components/watchlist/watch-board";
import { PAGES } from "@/lib/vocabulary";
import { fetchWatchBoard } from "@/lib/watchlist/board";

/** `/watchlist` — the sixth primary destination (Maulik, 12 Nov 2026). */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/watchlist"].title,
  description: PAGES["/watchlist"].blurb,
  robots: { index: false, follow: false },
};

export default async function WatchlistPage() {
  const board = await fetchWatchBoard();
  const count = board?.items.length ?? 0;
  return (
    <div className="space-y-6">
      <PageHeader
        title={PAGES["/watchlist"].title}
        blurb={PAGES["/watchlist"].blurb}
        meta={
          <span className="text-xs text-muted-foreground">
            {count} stock{count === 1 ? "" : "s"}
          </span>
        }
      />
      <WatchBoardPanel board={board} />
    </div>
  );
}
