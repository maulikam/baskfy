import Link from "next/link";

import { InstrumentLink } from "@/components/instrument/instrument-link";
import { formatNumber, formatTradeDate } from "@/lib/format";
import type { MbName } from "@/lib/mb/fetch";

const BOOK_SIZE = 20;

/**
 * Staff only (PM7, DECISIONS-PM PM7.2): the day's top 20 investable names as a watchlist for the desk.
 * It creates no plan line. Buying one is a person's own scan uploaded on the weekly desk, then a confirm.
 */
export function MbDeskWatchlist({ names, asOf }: { names: readonly MbName[]; asOf: string | null }) {
  const top = names.filter((n) => n.investable).slice(0, BOOK_SIZE);
  return (
    <section aria-labelledby="mb-desk-heading" className="space-y-3 rounded-md border border-dashed border-border p-4" data-testid="mb-desk">
      <h2 id="mb-desk-heading" className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
        Desk watchlist
      </h2>
      {asOf === null || top.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing scored yet.</p>
      ) : (
        <ol className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4" data-testid="mb-desk-list">
          {top.map((n) => (
            <li key={n.instrument_id} className="flex gap-2 tabular-nums">
              <span className="w-6 text-muted-foreground">{n.rank}</span>
              <InstrumentLink symbol={n.symbol} flag={false} />
              <span className="text-muted-foreground">{formatNumber(n.close, { decimals: 2 })}</span>
            </li>
          ))}
        </ol>
      )}
      <p className="max-w-[80ch] text-xs text-muted-foreground">
        Closes of {formatTradeDate(asOf)}. This list places nothing and builds no plan. To buy a tranche, run the
        weekly desk&rsquo;s manual Analyze with your own scan and confirm each line there:{" "}
        <Link href="/rebalance" className="underline" data-testid="mb-desk-link">
          open the weekly desk
        </Link>
        .
      </p>
    </section>
  );
}
