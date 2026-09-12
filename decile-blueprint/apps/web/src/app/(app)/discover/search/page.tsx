import type { Metadata } from "next";
import Link from "next/link";

import { DiscoverSearchClient } from "@/app/(app)/discover/search/discover-search-client";
import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import {
  ExploreUnavailable,
  fetchExploreList,
  type ExploreBasketCard,
} from "@/lib/explore/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/discover/search` — client-side basket search (AF I.3).
 *
 * Catalogue is loaded once on the server; filtering happens in the browser. The Discover hub
 * can link here without owning this route.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/discover/search"].title,
  description: PAGES["/discover/search"].blurb,
  robots: { index: false, follow: false },
};

export default async function DiscoverSearchPage() {
  let items: ExploreBasketCard[] = [];
  try {
    items = (await fetchExploreList({ sort: "name", order: "asc" })).items;
  } catch (error) {
    if (!(error instanceof ExploreUnavailable)) throw error;
  }

  return (
    <div className="flex w-full max-w-[104rem] flex-col gap-6">
      <SectionTabs section="discover" />
      <PageHeader
        title="Search baskets"
        blurb="Type a name, manager or category. Filtering happens in your browser."
        meta={
          <Link href="/discover" className="text-xs text-accent underline-offset-4 hover:underline">
            Back to Discover
          </Link>
        }
      />
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">The catalogue could not be loaded.</p>
      ) : (
        <DiscoverSearchClient items={items} />
      )}

      {/* The results carry a headline return, so this page owes the reader the same block every
          other Discover surface carries. */}
      <DisclosureBlock variant="performance-not-verified" />
      <p className="text-xs text-muted-foreground">
        Searching the catalog is read-only — investing builds an order plan elsewhere; nothing
        here places an order.
      </p>
    </div>
  );
}
