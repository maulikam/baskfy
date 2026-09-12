import Link from "next/link";

import { fetchInstrumentWatchlist } from "@/lib/watchlist/instruments";
import { formatDateTimeIST, formatPercent } from "@/lib/format";

/**
 * Stocks section for the Portfolio watchlist (AF I.2).
 *
 * Mounted from `portfolio/watchlist/layout.tsx` so the existing baskets list (owned elsewhere)
 * does not need an edit.
 */

export async function StocksWatchSection() {
  const list = await fetchInstrumentWatchlist();

  return (
    <section className="space-y-3" aria-label="Stocks" data-testid="watchlist-stocks">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">Stocks</h2>
        <span className="text-xs text-muted-foreground">
          {list.count} name{list.count === 1 ? "" : "s"}
        </span>
      </div>

      {list.items.length === 0 ? (
        <p className="rounded-xl border border-dashed border-border bg-card/50 px-4 py-8 text-center text-sm text-muted-foreground">
          No stocks saved yet. Use Save on an instrument factsheet (or a screen row once wired) to
          add names here.
        </p>
      ) : (
        <ul className="space-y-2">
          {list.items.map((item) => (
            <li
              key={item.symbol}
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/70 bg-card px-4 py-3"
            >
              <div>
                <Link
                  href={`/instruments/${item.symbol}`}
                  className="text-sm font-semibold underline-offset-4 hover:underline"
                >
                  {item.symbol}
                </Link>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {item.name} · Watched {formatDateTimeIST(item.watched_at)}
                  {item.moved_pct != null
                    ? ` · Moved ${formatPercent(item.moved_pct)} since watchlisted`
                    : ""}
                </p>
              </div>
              <Link
                href={`/instruments/${item.symbol}`}
                className="text-xs text-accent underline-offset-4 hover:underline"
              >
                Open
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
