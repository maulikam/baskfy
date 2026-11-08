import type { Metadata } from "next";

import { CollectionDirectory } from "@/components/collections/collection-directory";
import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { ExploreUnavailable, fetchCollections } from "@/lib/collections/fetch";

/**
 * `/discover/collections` — every editorial shelf, in curator order.
 *
 * A directory, not a stack. An index's job is to be complete, and stacking every shelf with its
 * cards made that impossible to do honestly: three of the four shelves resolve to the same single
 * basket today, so the page drew one card three times and then a dashed empty box. Tiles list all
 * four — the empty one included, saying so — and the cards live on each shelf's own page, which
 * is the one place a person has asked for that shelf by name.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Collections · Baskets",
  description: "Baskets grouped into shelves you can browse.",
  robots: { index: false, follow: false },
};

export default async function CollectionsIndexPage() {
  let collections;
  try {
    collections = (await fetchCollections()).items;
  } catch (error) {
    if (!(error instanceof ExploreUnavailable)) throw error;
    return (
      <div className="space-y-6">
        <PageHeader title="Collections" blurb="Shelves could not be loaded." />
        <p className="text-sm text-muted-foreground" data-testid="collections-unavailable">
          The catalogue is unreachable right now. Nothing about your holdings has changed.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-10">
      <PageHeader
        title="Collections"
        blurb="Baskets grouped into shelves — by cost of entry, by how they pick, by who runs them."
      />
      <CollectionDirectory collections={collections} />
      <p className="text-xs text-muted-foreground">
        Browsing a collection is read-only — nothing here places an order, and investing
        builds an order plan you confirm elsewhere.
      </p>
      <DisclosureBlock variant="performance-not-verified" />
    </div>
  );
}
