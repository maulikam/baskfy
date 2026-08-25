import "server-only";

import { apiOrigin } from "@/lib/api/config";
import { serverFetchJsonOrNull } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";
import {
  ExploreUnavailable,
  fetchCollections as fetchCollectionList,
  type Collection,
} from "@/lib/collections/fetch";
import {
  EMPTY_TRENDING,
  type Trending,
  type UpdatePost,
} from "@/lib/home/types";
import { fetchInvestments, type InvestmentList } from "@/lib/investments/fetch";

/**
 * Server-side reads for `/home` — the signed-in landing surface (SC9, docs/smallcase/05 §6.1).
 *
 * Four reads, one page. Two rules govern this module:
 *
 * **One source of truth per number.** Net worth and pending actions are *not* fetched again
 * here; they come from `fetchInvestments`, the same `GET /cb/investments` payload
 * `/me/investments` renders. A second net-worth computation is how two pages start disagreeing
 * about the same person's money.
 *
 * **Every read degrades to an empty shape.** Home is where somebody lands. A single unreachable
 * upstream must cost that module and nothing else — `serverFetchJsonOrNull` already returns
 * `null` on timeout, network error and non-OK, and every helper below turns that into the
 * honest empty state rather than a throw that takes the whole page down. The four reads run
 * concurrently, so the page costs one hop, not four.
 *
 * **No write helpers.** Dismissing a pending action is a server action (`app/actions/
 * pending-actions.ts`); nothing here can place an order — Track C / PACK.2.
 */

async function authHeaders(): Promise<HeadersInit> {
  const session = await auth();
  const token = session?.accessToken;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function tryJson(path: string): Promise<unknown> {
  return serverFetchJsonOrNull({
    url: `${apiOrigin()}/api/v1${path}`,
    headers: await authHeaders(),
  });
}

/** `GET /cb/trending` — every ranked list, published or withheld with its reason. */
export async function fetchTrending(): Promise<Trending> {
  const data = await tryJson("/cb/trending");
  if (data === null) return EMPTY_TRENDING;
  const raw = data as Partial<Trending>;
  return { ...EMPTY_TRENDING, ...raw, items: raw.items ?? [] };
}

/**
 * The "Take your pick" shelves, through `lib/collections/fetch` — **not** a second reader.
 *
 * `/baskets` already reads `GET /explore/collections` through that module and renders the same
 * rows as full shelves. Home wants a compact grid of entry points rather than shelves, which is
 * a different *component*, not a different *source*: a second fetcher here would be two readers
 * of one endpoint drifting apart the moment its shape changes.
 *
 * It throws where home must not, so the throw is caught here and turned into the empty grid —
 * the same degradation rule every other read on this page follows.
 */
export async function fetchHomeCollections(): Promise<Collection[]> {
  try {
    return (await fetchCollectionList()).items;
  } catch (error) {
    if (!(error instanceof ExploreUnavailable)) throw error;
    return [];
  }
}

/** `GET /cb/updates` — the manager/engine feed, newest first. */
export async function fetchUpdates(limit = 3): Promise<UpdatePost[]> {
  const data = await tryJson("/cb/updates");
  if (data === null) return [];
  const raw = data as { items?: Array<UpdatePost & { id: string | number }> };
  return (raw.items ?? []).slice(0, limit).map((row) => ({ ...row, id: String(row.id) }));
}

export interface HomeSnapshot {
  investments: InvestmentList;
  trending: Trending;
  collections: Collection[];
  updates: UpdatePost[];
}

/**
 * Everything `/home` renders, in one concurrent hop.
 *
 * `Promise.all` and not a sequence: these four reads have no dependency on each other, and
 * serialising them would put four API round trips on the critical path of the page a person
 * lands on. Each already degrades on its own, so `all` cannot reject here.
 */
export async function fetchHome(): Promise<HomeSnapshot> {
  const [investments, trending, collections, updates] = await Promise.all([
    fetchInvestments(),
    fetchTrending(),
    fetchHomeCollections(),
    fetchUpdates(),
  ]);
  return { investments, trending, collections, updates };
}

export { EMPTY_TRENDING };
export type { Collection } from "@/lib/collections/fetch";
export type { Trending, TrendingEntry, TrendingList, UpdatePost } from "@/lib/home/types";
