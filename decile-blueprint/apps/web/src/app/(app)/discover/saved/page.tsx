import type { Metadata } from "next";
import Link from "next/link";

import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { EMPTY_CELL, formatPercent, formatTradeDate } from "@/lib/format";
import { fetchWatchlist } from "@/lib/investments/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/discover/saved` — the watchlist, where a reader who saved something goes to find it.
 *
 * The same `GET /api/v1/watchlist` the Me hub's page reads. It is deliberately in two places:
 * saving happens in Discover, so Discover is where a reader looks for what they saved, while
 * `/portfolio/watchlist` keeps its place among the things that belong to *you* rather than to the
 * catalogue. One store, one fetch, two doors.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/discover/saved"].title,
  description: PAGES["/discover/saved"].blurb,
  robots: { index: false, follow: false },
};

export default async function SavedPage() {
  const list = await fetchWatchlist();

  return (
    <div className="flex w-full max-w-5xl flex-col gap-6">
      <SectionTabs section="discover" />
      <PageHeader
        title={PAGES["/discover/saved"].title}
        blurb={PAGES["/discover/saved"].blurb}
        meta={
          <span className="text-xs text-muted-foreground">
            {list.count} basket{list.count === 1 ? "" : "s"}
          </span>
        }
      />

      {list.items.length === 0 ? (
        <div
          className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center"
          data-testid="saved-empty"
        >
          <p className="max-w-[56ch] text-sm leading-relaxed text-muted-foreground">
            Nothing saved yet. Every basket card has a Save control — use it while you are
            browsing and they collect here, with how each has moved since you saved it.
          </p>
          <Link
            href="/discover/all"
            className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
          >
            Browse all baskets
          </Link>
        </div>
      ) : (
        <ul className="space-y-2" aria-label="Saved baskets">
          {list.items.map((item) => (
            <li
              key={item.basket_slug}
              data-testid="saved-item"
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/70 bg-card px-4 py-3"
            >
              <div className="min-w-0">
                <Link
                  href={`/basket/${item.basket_slug}`}
                  className="text-sm font-medium underline-offset-2 hover:underline"
                >
                  {item.basket_name}
                </Link>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Saved {formatTradeDate(item.watched_at.slice(0, 10))}
                </p>
              </div>
              <div className="text-right">
                <div className="eyebrow">Since you saved it</div>
                <div className="mt-0.5 text-sm font-medium tabular-nums">
                  {item.moved_pct === null || item.moved_pct === undefined
                    ? EMPTY_CELL
                    : formatPercent(item.moved_pct)}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      <DisclosureBlock variant="performance-not-verified" />
      <p className="text-xs text-muted-foreground">
        Saving is read-only — it records what you are watching and places no order.
      </p>
    </div>
  );
}
