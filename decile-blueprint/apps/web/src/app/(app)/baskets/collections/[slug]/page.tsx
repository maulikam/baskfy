import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { CollectionShelf } from "@/components/collections/collection-shelf";
import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { ReturnConventionNote } from "@/components/explore/return-convention-note";
import { PageHeader } from "@/components/shell/page-header";
import { ExploreUnavailable, fetchCollection } from "@/lib/collections/fetch";

/**
 * `/baskets/collections/[slug]` — one editorial shelf.
 *
 * `cb_collection` had existed since migration 0014 with no page and no content. This is the page.
 * The brief asked for `/collection/[slug]`; that path redirects here, because Tree 6 moved the
 * whole consumer IA under `/baskets` and a second top-level noun would fight it.
 */

export const dynamic = "force-dynamic";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  return {
    title: `Collection · Baskets`,
    description: `Baskets grouped as ${slug.replace(/-/g, " ")}.`,
    robots: { index: false, follow: false },
  };
}

export default async function CollectionPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;

  let collection;
  try {
    collection = await fetchCollection(slug);
  } catch (error) {
    if (error instanceof ExploreUnavailable) {
      return (
        <div className="space-y-6">
          <PageHeader title="Collection" blurb="This shelf could not be loaded." />
          <p className="text-sm text-muted-foreground" data-testid="collection-unavailable">
            The catalogue is unreachable right now. Nothing about your holdings has changed.
          </p>
        </div>
      );
    }
    notFound();
  }

  return (
    <div className="space-y-8">
      <PageHeader
        title={collection.title}
        blurb={collection.subtitle ?? "Baskets grouped for browsing."}
      />
      <CollectionShelf collection={collection} headingLevel="h2" showAll={false} />
      <ReturnConventionNote metrics={collection.baskets[0]?.metrics ?? null} />
      <p className="text-sm text-muted-foreground">
        <Link href="/baskets/collections" className="hover:underline">
          All collections
        </Link>
      </p>
      <p className="text-xs text-muted-foreground">
        Browsing a collection is read-only — nothing here places an order, and investing
        builds an order plan you confirm elsewhere.
      </p>
      <DisclosureBlock variant="performance-not-verified" />
    </div>
  );
}
