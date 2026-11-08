import type { Collection } from "@/lib/collections/fetch";
import { CollectionTile } from "@/components/collections/collection-tile";
import { cn } from "@/lib/utils";

/**
 * Every collection, as doors. The complete map of the shelves, and never a repeat of a basket.
 *
 * This is the presentation `selectShelves` falls back to when stacking shelves would only redraw
 * the catalogue: four tiles that say what each shelf is and how much is on it, instead of the
 * same basket card three times over and a dashed box. It is also what the collections index page
 * always renders, because an index's job is to be complete — including the shelves a stacked
 * view would have suppressed, and including the empty ones.
 *
 * There is no list of collection slugs in this file and there may never be (SC9): every tile
 * comes from `cb_collection` through `GET /explore/collections`.
 */
export function CollectionDirectory({
  collections,
  className,
}: {
  collections: readonly Collection[];
  className?: string;
}) {
  return (
    <div
      data-testid="collection-directory"
      data-count={collections.length}
      className={cn("space-y-3", className)}
    >
      {collections.length === 0 ? (
        <p
          data-testid="collection-directory-empty"
          className="rounded-xl border border-dashed border-border bg-card/50 px-4 py-6 text-center text-sm text-muted-foreground"
        >
          No collections yet. They are themed shelves of baskets — once there are enough baskets
          to group, they appear here.
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
    </div>
  );
}
