import type { Metadata } from "next";
import Link from "next/link";

import {
  ACTIVITY_EMPTY_CONNECTED,
  ACTIVITY_EMPTY_DISCONNECTED,
  ACTIVITY_FILTER_EMPTY,
  ACTIVITY_READ_ONLY,
  ACTIVITY_UNAVAILABLE,
} from "@/components/portfolio/activity-honest-copy";
import { ActivityTab } from "@/components/portfolio/detail/activity-tab";
import { TradeHistory } from "@/components/portfolio/trade-history";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchBrokerCatalog } from "@/lib/brokers/fetch";
import { fetchPortfolioActivity, fetchPortfolioOverview } from "@/lib/portfolio/fetch";
import { fetchTrades } from "@/lib/trades/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/portfolio/activity` — §7's feed across every portfolio.
 *
 * PORTFOLIO_REDESIGN.md §7 names what belongs here: buys and sells, internal cash assignments
 * (§4.4), dividends, corporate actions (§4.5), rebalances, and reconciliation history (§4.3).
 * `GET /portfolio/activity` is that list. Holdings sync still does not write a trade history, so
 * an empty feed after a sync is a real empty list — not a page that has not been built.
 */
export const metadata: Metadata = {
  title: PAGES["/portfolio/activity"].title,
  description: PAGES["/portfolio/activity"].blurb,
  robots: { index: false, follow: false },
};

export default async function PortfolioActivityPage() {
  // A catalog failure must not break the page. Unknown reads as "not connected", which is the
  // more cautious of the two — it offers a link rather than asserting a connection that may not
  // exist.
  let connected: boolean;
  try {
    const catalog = await fetchBrokerCatalog();
    connected = catalog.brokers.some((broker) => broker.connected);
  } catch {
    connected = false;
  }

  const [overview, activity, trades] = await Promise.all([
    fetchPortfolioOverview().catch(() => null),
    fetchPortfolioActivity(),
    fetchTrades().catch(() => null),
  ]);
  const items = activity === null ? null : (activity.items ?? []);
  const showConnect = !connected && (items === null || items.length === 0);
  const syncSummary =
    overview?.sync_summary ??
    overview?.holdings_synced_label ??
    (connected ? "Holdings not synced yet" : "No broker connected");

  return (
    <div className="flex max-w-5xl flex-col gap-6">
      <SectionTabs section="portfolio" />
      <PageHeader title="Activity" blurb={PAGES["/portfolio/activity"].blurb} />

      <TradeHistory trades={trades} />

      {showConnect ? (
        <div
          data-testid="activity-empty"
          className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center"
        >
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            {ACTIVITY_EMPTY_DISCONNECTED}
          </p>
          <Link
            href="/brokers"
            className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
          >
            Connect a broker
          </Link>
        </div>
      ) : (
        <>
          {connected ? (
            <p className="text-sm text-muted-foreground">
              <span data-testid="sync-summary">{syncSummary}</span>
            </p>
          ) : null}
          <ActivityTab
            items={items}
            unavailableReason={activity === null ? ACTIVITY_UNAVAILABLE : null}
            emptyCopy={ACTIVITY_EMPTY_CONNECTED}
            filterEmptyCopy={ACTIVITY_FILTER_EMPTY}
            countsBlurb="Every kind of event recorded across your portfolios, and how many of each."
            feedBlurb="Buys, sells, cash you assign, dividends, corporate actions and reconciliation answers, newest first."
          />
        </>
      )}

      <p className="text-xs text-muted-foreground">{ACTIVITY_READ_ONLY}</p>
    </div>
  );
}
