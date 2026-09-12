import Link from "next/link";

import { CollectionTile } from "@/components/collections/collection-tile";
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
 * A grid of entry points, not shelves — the tile itself is `CollectionTile`, shared with the
 * collections directory so the two cannot drift. Home has four other modules to fit and wants
 * the door, not the room; `/discover` decides for itself whether its shelves are worth opening.
 */

export interface CollectionsGridProps {
  collections: readonly Collection[];
  className?: string;
}

export function CollectionsGrid({ collections, className }: CollectionsGridProps) {
  // Audit §1.14: hide the module when every shelf names the same baskets — undifferentiated
  // themes are noise, not discovery.
  const signatures = collections.map((c) => [...c.basket_slugs].sort().join(","));
  const differentiated =
    signatures.length === 0 || new Set(signatures).size > 1 || collections.length === 1;
  if (!differentiated) return null;

  return (
    <section
      aria-label="Collections"
      data-testid="collections-grid"
      className={cn("space-y-3", className)}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold">Take your pick</h2>
        <Link
          href="/discover"
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
              <CollectionTile collection={collection} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
