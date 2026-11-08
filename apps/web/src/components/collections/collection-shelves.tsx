import Link from "next/link";

import { CollectionDirectory } from "@/components/collections/collection-directory";
import { CollectionShelf } from "@/components/collections/collection-shelf";
import type { Collection } from "@/lib/collections/fetch";
import { selectShelves } from "@/lib/collections/select";

/**
 * The collections block on a page that already lists the catalogue above it (`/discover`).
 *
 * It has two presentations and `selectShelves` picks between them:
 *
 *  - **Stacked shelves**, when at least two shelves genuinely group different baskets. This is
 *    the browse experience the shelves exist for, and it is what a grown catalogue gets.
 *  - **The directory**, when they do not. Today three shelves resolve to the same single basket
 *    and a fourth to none, so stacking drew that one basket card three times under three
 *    headings and then a dashed empty box — the symptom this component exists to remove. Four
 *    tiles say the same thing without repeating a single card.
 *
 * Either way every collection stays one click away, which is the line between suppressing a
 * repeat and hiding a shelf.
 */
export function CollectionShelves({ collections }: { collections: readonly Collection[] }) {
  if (collections.length === 0) return null;

  const { shelves, stack } = selectShelves(collections);

  return (
    <section
      className={stack ? "space-y-8" : "space-y-3"}
      data-testid="browse-collections"
      data-mode={stack ? "shelves" : "directory"}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Collections
        </h2>
        <Link
          href="/discover/collections"
          className="text-xs text-muted-foreground underline-offset-4 hover:underline"
        >
          All collections
        </Link>
      </div>

      {stack ? (
        <div className="space-y-8">
          {shelves.map((collection) => (
            <CollectionShelf key={collection.slug} collection={collection} />
          ))}
        </div>
      ) : (
        <>
          <p className="text-xs text-muted-foreground" data-testid="collections-thin-note">
            The catalog is small enough that every shelf holds the same baskets you can already
            see above. They are listed here, and fill out as more baskets are published.
          </p>
          <CollectionDirectory collections={collections} />
        </>
      )}
    </section>
  );
}
