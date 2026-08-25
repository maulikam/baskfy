import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";

import type { CatalogKind } from "@baskfy/api-client";
import { describe, expect, it } from "vitest";

import { hrefFor, KIND_LABELS, KIND_ORDER } from "@/lib/search/hrefs";

/**
 * `hrefFor` carries `as Route` on all four patterns (typedRoutes cannot check a string built from
 * a runtime id), so the assertion the cast cannot make is made here instead: every kind's href
 * resolves to a route directory that actually exists in `src/app`.
 *
 * That is the failure this file exists to catch. Tree 6 moved `/screens/{id}` to `/build/{id}` and
 * `/explore` to `/baskets`; a palette that keeps sending people to the old paths would look
 * perfectly healthy in a typecheck and 404 for every user.
 */
/* From the package root: vitest runs with `apps/web` as its working directory. */
const APP_DIR = join(process.cwd(), "src", "app", "(app)");

/** The first path segment of a href, which is the route directory under `(app)`. */
function segment(href: string): string {
  const [path = ""] = href.split("?");
  return path.split("/").filter(Boolean)[0] ?? "";
}

describe("hrefFor", () => {
  it("sends every kind to a route directory that exists", () => {
    const cases: Array<[CatalogKind, string]> = [
      ["instrument", "CUPID"],
      ["index", "nifty-midcap-150"],
      ["basket", "momentum-scan"],
      ["screen", "exmpl0000001"],
    ];
    const directories = new Set(readdirSync(APP_DIR));
    for (const [kind, id] of cases) {
      const href = hrefFor({ kind, id });
      expect(directories.has(segment(href)), `${kind} → ${href}`).toBe(true);
    }
  });

  it("routes a stock to the factsheet, a basket to the basket page, a screen to Build", () => {
    expect(hrefFor({ kind: "instrument", id: "CUPID" })).toBe("/instruments/CUPID");
    expect(hrefFor({ kind: "basket", id: "momentum-scan" })).toBe("/basket/momentum-scan");
    expect(hrefFor({ kind: "screen", id: "exmpl0000001" })).toBe("/build/exmpl0000001");
  });

  it("routes an index to the dashboard filtered to that index", () => {
    /* There is no index detail page; `/market/today` is the ~145-row table and its search box is
       URL state, so `?q=` lands on that index's row. DECISIONS-MERGE M40.2. */
    expect(hrefFor({ kind: "index", id: "nifty-midcap-150" })).toBe(
      "/market/today?q=nifty-midcap-150",
    );
    expect(existsSync(join(APP_DIR, "market", "today", "page.tsx"))).toBe(true);
  });

  it("escapes an id rather than pasting it into the path", () => {
    /* A slug is well-behaved today. A symbol with a slash or a space would silently build a
       different route, and that is a bug worth failing on rather than discovering in production. */
    expect(hrefFor({ kind: "instrument", id: "M&M" })).toBe("/instruments/M%26M");
    expect(hrefFor({ kind: "index", id: "NIFTY 50" })).toBe("/market/today?q=NIFTY%2050");
  });

  it("labels and orders every kind the API can return", () => {
    /* A kind added on the API and forgotten here is a group that silently never renders. */
    expect(Object.keys(KIND_LABELS).sort()).toEqual([...KIND_ORDER].sort());
    expect(Object.values(KIND_LABELS)).toEqual(["Stocks", "Indices", "Baskets", "Screens"]);
  });

  it("orders the groups the way the report's mock-up reads them", () => {
    expect(KIND_ORDER).toEqual(["instrument", "index", "basket", "screen"]);
  });
});
