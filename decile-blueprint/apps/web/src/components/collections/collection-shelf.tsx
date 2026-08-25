import Link from "next/link";

import { BasketCard } from "@/components/explore/basket-card";
import type { Collection } from "@/lib/collections/fetch";

/**
 * One editorial shelf: a title, a reason, and the baskets on it.
 *
 * The empty case is rendered, not skipped. A shelf whose rule matches nothing today is a true
 * statement about the catalogue ("nothing here yet"); dropping it would be a false statement
 * about the product, and would make an empty shelf indistinguishable from a broken one.
 */
export function CollectionShelf({
  collection,
  headingLevel = "h2",
  showAll = true,
}: {
  collection: Collection;
  headingLevel?: "h1" | "h2";
  showAll?: boolean;
}) {
  const Heading = headingLevel;
  const count = collection.baskets.length;

  return (
    <section
      className="space-y-4"
      data-testid="collection-shelf"
      data-slug={collection.slug}
      data-count={count}
      data-empty={count === 0 ? "true" : "false"}
    >
      <div className="flex items-end justify-between gap-4">
        <div className="space-y-1">
          <Heading className="text-lg font-semibold tracking-tight">
            {showAll ? (
              <Link
                href={`/baskets/collections/${collection.slug}`}
                className="hover:underline"
              >
                {collection.title}
              </Link>
            ) : (
              collection.title
            )}
          </Heading>
          {collection.subtitle ? (
            <p className="text-sm text-muted-foreground">{collection.subtitle}</p>
          ) : null}
        </div>
        {showAll && count > 0 ? (
          <Link
            href={`/baskets/collections/${collection.slug}`}
            className="shrink-0 text-sm text-muted-foreground hover:text-foreground hover:underline"
          >
            See all {count}
          </Link>
        ) : null}
      </div>

      {count === 0 ? (
        <p
          className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground"
          data-testid="collection-empty"
        >
          No baskets meet this yet. The shelf fills itself as baskets are published — nothing is
          being hidden from you here.
        </p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {collection.baskets.map((basket) => (
            <BasketCard key={basket.slug} basket={basket} />
          ))}
        </div>
      )}
    </section>
  );
}
