import type { Metadata } from "next";
import Link from "next/link";

import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchBrokerCatalog } from "@/lib/brokers/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/portfolio/activity` — the ledger behind Overview and Holdings.
 *
 * PORTFOLIO_REDESIGN.md §7 names what belongs here: buys and sells, internal cash assignments
 * (§4.4), dividends, corporate actions (§4.5), rebalances, and reconciliation history (§4.3).
 * All six are Phase 1 of §10 and none exists yet, so the page is honest about being empty rather
 * than inventing a feed. The route exists now so the §2 tab row is complete.
 *
 * **What it said to a connected account, and why that was wrong (M83).** The copy was static:
 * "Once a broker is connected…" above a "Connect a broker" link, shown to everyone. Maulik read it
 * with Zerodha already connected and 17 holdings synced, and reasonably asked why it wanted him to
 * connect again.
 *
 * Two separate untruths. It implied he was not connected when he was — the same defect the broker
 * panel had — and it implied that connecting is what would fill this page, when nothing would:
 * the feed does not exist. Prompting an action that cannot help is worse than an empty box,
 * because it sends someone to re-do work that was already done.
 *
 * So the page asks who is connected and says the true thing for each case. It still invents no
 * feed.
 */
export const metadata: Metadata = {
  title: PAGES["/portfolio/activity"].title,
  description: PAGES["/portfolio/activity"].blurb,
  robots: { index: false, follow: false },
};

export default async function PortfolioActivityPage() {
  // A catalog failure must not break the page: this is prose, not data. Unknown reads as
  // "not connected", which is the more cautious of the two — it offers a link rather than
  // asserting a connection that may not exist.
  let connected: boolean;
  try {
    const catalog = await fetchBrokerCatalog();
    connected = catalog.brokers.some((broker) => broker.connected);
  } catch {
    connected = false;
  }

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <SectionTabs section="portfolio" />
      <PageHeader title="Activity" blurb={PAGES["/portfolio/activity"].blurb} />

      <div
        data-testid="activity-empty"
        className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center"
      >
        {connected ? (
          <p className="max-w-[56ch] text-sm leading-relaxed text-muted-foreground">
            Your broker is connected and your holdings are synced. Activity — buys, sells,
            dividends and corporate actions — is not recorded yet, so there is nothing to list.
            Syncing again will not change that; this page fills in once the ledger behind it is
            built.
          </p>
        ) : (
          <>
            <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
              Nothing has happened yet. Once a broker is connected, every buy, sell, dividend and
              cash move it reports is listed here, newest first.
            </p>
            <Link
              href="/brokers"
              className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
            >
              Connect a broker
            </Link>
          </>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        A record of what happened, not an instruction to do anything. Nothing here places an order.
      </p>
    </div>
  );
}
