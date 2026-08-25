import Link from "next/link";

import type { Collection } from "@/lib/collections/fetch";
import { cn } from "@/lib/utils";

/**
 * "Take your pick" — the collections grid, docs/smallcase/05 §6.1 / SC9.
 *
 * SC9's acceptance criterion again: *"collections render from data with zero hardcoded slugs in
 * components."* There is no list of themes in this file and there never may be — every card
 * comes from `cb_collection` through `GET /explore/collections`, and a collection that stops
 * existing stops rendering without anybody editing TSX.
 *
 * The empty state is rendered rather than skipped, for the reason `CollectionShelf` gives about
 * an empty shelf: "there are no collections yet" is a true statement about the catalogue, and a
 * module that disappears when its data is empty is indistinguishable from one that is broken.
 *
 * A grid of entry points, not shelves. `/baskets` renders the same rows as full `CollectionShelf`
 * blocks with every basket card on them; home has four other modules to fit and wants the door,
 * not the room.
 */

export interface CollectionsGridProps {
  collections: readonly Collection[];
  className?: string;
}

export function CollectionsGrid({ collections, className }: CollectionsGridProps) {
  return (
    <section
      aria-label="Collections"
      data-testid="collections-grid"
      className={cn("space-y-3", className)}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold">Take your pick</h2>
        <Link
          href="/baskets"
          className="text-xs text-muted-foreground underline-offset-4 hover:underline"
        >
          All baskets
        </Link>
      </div>

      {collections.length === 0 ? (
        <p
          data-testid="collections-empty"
          className="rounded-xl border border-dashed border-border bg-card/50 px-4 py-6 text-center text-sm text-muted-foreground"
        >
          No collections yet. They are themed shelves of baskets — once there are enough
          baskets to group, they appear here.
        </p>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {collections.map((collection) => (
            <li key={collection.slug}>
              <Link
                href={`/baskets/collections/${collection.slug}`}
                data-testid={`collection-${collection.slug}`}
                className="block h-full rounded-xl border border-border/70 bg-card p-4 transition-colors hover:border-accent/60"
              >
                <p className="text-sm font-medium">{collection.title}</p>
                {collection.subtitle ? (
                  <p className="mt-1 text-xs text-muted-foreground">{collection.subtitle}</p>
                ) : null}
                <p className="mt-2 text-xs text-muted-foreground">
                  {collection.baskets.length === 0
                    ? "Nothing on this shelf yet"
                    : `${collection.baskets.length} basket${
                        collection.baskets.length === 1 ? "" : "s"
                      }`}
                </p>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
