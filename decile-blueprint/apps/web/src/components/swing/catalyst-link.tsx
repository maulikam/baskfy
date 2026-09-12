import { formatTradeDate, formatDateTimeIST } from "@/lib/format";
import type { SwingCatalystFeed } from "@/lib/swing/fetch";

/**
 * SW11B — the catalyst feed, as a page shows it (`docs/swing/STANDING-ANSWERS.md` A3).
 *
 * **A link, never the text.** The headline is NSE's own subject line; clicking it opens the
 * exchange's copy of the filing in a new tab (`target="_blank" rel="noopener noreferrer"`).
 * Nothing from the filing is rendered here, and nothing here can be copied onward from this
 * app — the feed is single-tenant own-use (Track C §7 amendment). The earnings badge is the
 * nearest result meeting the event calendar lists; it is a flag, not a forecast.
 */
export function CatalystLink({
  feed,
}: {
  feed: SwingCatalystFeed | null | undefined;
}) {
  if (!feed || (!feed.url && !feed.earnings_date)) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      {feed.url ? (
        <a
          href={feed.url}
          target="_blank"
          rel="noopener noreferrer"
          className="underline decoration-border underline-offset-2 hover:decoration-foreground"
          title={
            feed.published_at
              ? `NSE filing, ${formatDateTimeIST(feed.published_at)} — opens on nseindia.com`
              : "NSE filing — opens on nseindia.com"
          }
        >
          {feed.headline ?? "NSE filing"}
        </a>
      ) : null}
      {feed.earnings_date ? (
        <span
          className="rounded border border-warning/50 px-1.5 py-0.5 text-[11px] uppercase tracking-wide text-warning"
          title="A result meeting is on the exchange's calendar. A stop through a result is a gap, not a fill."
        >
          earnings {formatTradeDate(feed.earnings_date)}
        </span>
      ) : null}
    </span>
  );
}
