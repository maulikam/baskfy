import type { Metadata } from "next";

import { createPortfolioAction } from "@/app/actions/portfolio";
import { UnallocatedSection } from "@/components/portfolio/unallocated-section";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchBrokerCatalog } from "@/lib/brokers/fetch";
import {
  fetchGroupingSuggestions,
  fetchPortfolioHoldings,
  fetchPortfolioOverview,
} from "@/lib/portfolio/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/portfolio/holdings` — PORTFOLIO_REDESIGN.md §2: "the flat, broker-level truth".
 *
 * §6.6 puts the Unallocated section at the top of it as a **centrepiece, not a footer**, and this
 * page is where the §6.7 grouping flow lives: the two-panel picker that turns forty unallocated
 * holdings into four named portfolios. That is the activation event the spec names, and it is
 * holdings-first by construction — with nothing connected, the one way forward here is
 * "Connect your broker" (acceptance criterion 8), never the basket catalog (§1 problem 6).
 *
 * Every read can fail, and the section renders the difference: an API that did not answer says so,
 * an account with nothing unallocated says so, and neither is drawn as a zero.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/portfolio/holdings"].title,
  description: PAGES["/portfolio/holdings"].blurb,
  robots: { index: false, follow: false },
};

export default async function PortfolioHoldingsPage() {
  const [overview, holdings, suggestions, brokers] = await Promise.all([
    fetchPortfolioOverview(),
    fetchPortfolioHoldings(),
    fetchGroupingSuggestions(),
    fetchBrokerCatalog().catch(() => null),
  ]);

  const rows = holdings?.rows ?? [];
  const connected = (brokers?.brokers ?? []).filter((broker) => broker.connected).length;

  return (
    <div className="flex max-w-5xl flex-col gap-6">
      <SectionTabs section="portfolio" />
      <PageHeader title="Holdings" blurb={PAGES["/portfolio/holdings"].blurb} />

      <UnallocatedSection
        unallocated={overview?.unallocated ?? null}
        rows={rows}
        suggestions={suggestions.suggestions}
        suggestionsUnavailableReason={suggestions.unavailableReason}
        sectors={suggestions.sectors}
        connectedBrokerCount={connected}
        // §6.7's confirm. A server action passed straight into a client component: the write
        // runs on the server, so the bearer token never reaches the browser, and the flow does
        // not need to know that is what happened.
        onCreate={createPortfolioAction}
      />

      {holdings ? (
        <p className="text-xs text-muted-foreground">
          {holdings.prices_label} · {holdings.holdings_synced_label}
        </p>
      ) : null}

      <p className="text-xs text-muted-foreground">
        Read-only. Shares stay in your demat account; nothing on this page places an order.
      </p>
    </div>
  );
}
