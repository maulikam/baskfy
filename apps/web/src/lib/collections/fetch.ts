import "server-only";

import {
  ExploreUnavailable,
  type ExploreBasketCard,
  readExploreJson,
} from "@/lib/explore/fetch";

/**
 * Editorial shelves — the browse surface smallcase is mostly made of.
 *
 * `cb_collection` has existed since migration 0014 and held nothing; there was no page and no
 * content. The API now returns whole `BasketCardOut`s rather than slugs, so a shelf renders in
 * one request instead of one-plus-N.
 */
export interface Collection {
  slug: string;
  title: string;
  subtitle: string | null;
  basket_slugs: string[];
  baskets: ExploreBasketCard[];
  position: number;
  /**
   * Baskets the shelf names that this caller may not see (PRIVATE or archived). Not rendered —
   * it exists so "this shelf is empty" can be told apart from "this shelf is hidden".
   */
  withheld: number;
}

export interface CollectionList {
  items: Collection[];
}

export async function fetchCollections(): Promise<CollectionList> {
  return (await readExploreJson("/explore/collections")) as CollectionList;
}

export async function fetchCollection(slug: string): Promise<Collection> {
  return (await readExploreJson(
    `/explore/collections/${encodeURIComponent(slug)}`,
  )) as Collection;
}

export { ExploreUnavailable };
