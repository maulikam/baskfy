import type { Route } from "next";

import type { CatalogHitOut, CatalogKind } from "@baskfy/api-client";

/**
 * Where a catalog hit goes, and what its group is called.
 *
 * The API returns `kind` + `id` and deliberately no href (`baskfy_api.search`,
 * `docs/DECISIONS-MERGE.md` M46.1). Tree 6 moved half of these routes; minting the href server-side
 * would have made the next nav refactor an API deploy. So the mapping lives here, next to
 * `lib/nav.ts`, which is where every other route decision in this app already lives.
 *
 * `CatalogKind` is the generated union, so adding a kind on the API makes this file fail to
 * typecheck until it is handled — a record keyed by the union, not a switch with a default.
 *
 * `typedRoutes` is on (`next.config.ts`), and these four hrefs are built from runtime ids, so each
 * carries `as Route` — the same assertion `lib/nav.ts` makes for the section tabs. What the cast
 * cannot check, `hrefs.test.ts` does: every kind's pattern is asserted against the app's actual
 * route table.
 */

/** The group heading the palette draws for each kind, in the order the API returns them. */
export const KIND_LABELS: Record<CatalogKind, string> = {
  instrument: "Stocks",
  index: "Indices",
  basket: "Baskets",
  screen: "Screens",
};

/** Group order in the dialog. Mirrors `baskfy_api.search.KIND_ORDER`. */
export const KIND_ORDER: readonly CatalogKind[] = ["instrument", "index", "basket", "screen"];

/**
 * There is no index detail page, and inventing one was out of scope for this sitting.
 *
 * `/market/today` is the ~145-row dashboard, and its search box is URL state (`?q=`), so a hit
 * lands on that index's row with its level, change, P/E, P/B and sparkline — the closest thing to
 * an index page the product has. `encodeURIComponent` because index names carry spaces.
 * `docs/DECISIONS-MERGE.md` M46.2 records this and how to reverse it when an index page exists.
 */
function indexHref(slug: string): Route {
  return `/market/today?q=${encodeURIComponent(slug)}` as Route;
}

const HREFS: Record<CatalogKind, (id: string) => Route> = {
  instrument: (symbol) => `/instruments/${encodeURIComponent(symbol)}` as Route,
  index: indexHref,
  basket: (slug) => `/basket/${encodeURIComponent(slug)}` as Route,
  screen: (publicId) => `/build/${encodeURIComponent(publicId)}` as Route,
};

export function hrefFor(hit: Pick<CatalogHitOut, "kind" | "id">): Route {
  return HREFS[hit.kind](hit.id);
}
