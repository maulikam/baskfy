import Link from "next/link";

import type { Collection } from "@/lib/collections/fetch";

/**
 * One collection as a door rather than a room: title, reason, and how much is behind it.
 *
 * Extracted so home's "Take your pick" grid and the collections directory are the same tile
 * rather than two that drift. It renders no basket cards on purpose — the whole point of a
 * directory is that it can list a shelf without repeating what is on it, which is what let
 * `/discover` stop drawing the same basket three times.
 *
 * A shelf with nothing on it is still listed, and says so. "Nothing on this shelf yet" is a true
 * statement about a young catalogue; a tile that vanished would be indistinguishable from a bug.
 */
export function CollectionTile({ collection }: { collection: Collection }) {
  const count = collection.baskets.length;

  return (
    <Link
      href={`/discover/collections/${collection.slug}`}
      data-testid={`collection-${collection.slug}`}
      data-count={count}
      data-empty={count === 0 ? "true" : "false"}
      className="block h-full rounded-xl border border-border/70 bg-card p-4 transition-colors hover:border-accent/60"
    >
      <p className="text-sm font-medium">{collection.title}</p>
      {collection.subtitle ? (
        <p className="mt-1 text-xs text-muted-foreground">{collection.subtitle}</p>
      ) : null}
      <p className="mt-2 text-xs text-muted-foreground">
        {count === 0 ? "Nothing on this shelf yet" : `${count} basket${count === 1 ? "" : "s"}`}
      </p>
    </Link>
  );
}
