import type { Metadata } from "next";
import Link from "next/link";

import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { EMPTY_CELL, formatDateTimeIST, formatPercent } from "@/lib/format";
import { fetchWatchlist } from "@/lib/investments/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/portfolio/watchlist` — SC6. Count, watch date, moved-since-watchlisted, CTA to basket.
 * The last tab of the Portfolio section (PORTFOLIO_REDESIGN.md §2); moved from Me unchanged,
 * because a watchlist is money you are thinking about, not an account setting.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/portfolio/watchlist"].title,
  description: PAGES["/portfolio/watchlist"].blurb,
  robots: { index: false, follow: false },
};

export default async function PortfolioWatchlistPage() {
  const list = await fetchWatchlist();

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <SectionTabs section="portfolio" />
      <PageHeader
        title={PAGES["/portfolio/watchlist"].title}
        blurb={PAGES["/portfolio/watchlist"].blurb}
        meta={
          <span className="text-xs text-muted-foreground">
            {list.count} basket{list.count === 1 ? "" : "s"}
          </span>
        }
      />

      {list.items.length === 0 ? (
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            Nothing on the watchlist yet. Star a basket from Discover — the Save control on any
            basket card adds it here.
          </p>
          <Link
            href="/discover"
            className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
          >
            Discover baskets
          </Link>
        </div>
      ) : (
        <ul className="space-y-2" aria-label="Watchlist">
          {list.items.map((item) => (
            <li
              key={item.basket_slug}
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/70 bg-card px-4 py-3"
            >
              <div>
                <Link
                  href={`/basket/${item.basket_slug}`}
                  className="text-sm font-semibold underline-offset-4 hover:underline"
                >
                  {item.basket_name}
                </Link>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Watched {formatDateTimeIST(item.watched_at)}
                  {item.moved_pct != null
                    ? ` · Moved ${formatPercent(item.moved_pct)} since watchlisted`
                    : ""}
                </p>
              </div>
              <Link
                href={`/basket/${item.basket_slug}`}
                className="text-xs text-accent underline-offset-4 hover:underline"
              >
                Open
              </Link>
            </li>
          ))}
        </ul>
      )}

      {list.items.length > 0 && list.items.every((i) => i.moved_pct == null) ? (
        <p className="text-xs text-muted-foreground">
          Moved-since-watchlisted needs chain-linked NAV (SC4). Daily change shows {EMPTY_CELL}{" "}
          until that lands.
        </p>
      ) : null}
    </div>
  );
}
