import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Collections are a browse surface. Same bar as SC5's explore suite: they never grow an order
 * route, and they never render the count of baskets a viewer is not allowed to see.
 */

const COLLECTIONS_LIB = join(__dirname, "..");
const COLLECTIONS_COMPONENTS = join(__dirname, "..", "..", "..", "components", "collections");
const COLLECTION_PAGES = join(
  __dirname, "..", "..", "..", "app", "(app)", "baskets", "collections",
);

function filesUnder(dir: string): string[] {
  try {
    return readdirSync(dir).flatMap((entry) => {
      const path = join(dir, entry);
      return statSync(path).isDirectory() ? filesUnder(path) : [path];
    });
  } catch {
    return [];
  }
}

const SOURCES = [
  ...filesUnder(COLLECTIONS_LIB),
  ...filesUnder(COLLECTIONS_COMPONENTS),
  ...filesUnder(COLLECTION_PAGES),
].filter(
  (path) =>
    (path.endsWith(".ts") || path.endsWith(".tsx")) &&
    // A sweep must not scan itself: this file names the banned strings in order to ban them.
    !path.includes("__tests__"),
);

const BANNED = ["place_order", "/execute", "confirm=true", "OrderGateway"];

describe("collections stay a browse surface", () => {
  it("opens enough files to be a real sweep", () => {
    // Declared, per the anti-laziness rule: a sweep that silently found nothing is not a sweep.
    expect(SOURCES.length).toBeGreaterThanOrEqual(4);
  });

  it("never reaches an order path", () => {
    for (const path of SOURCES) {
      const text = readFileSync(path, "utf8");
      for (const banned of BANNED) {
        expect(text, `${path} mentions ${banned}`).not.toContain(banned);
      }
    }
  });

  it("reuses the explore reader rather than forking auth and timeout handling", () => {
    const fetchSource = readFileSync(join(COLLECTIONS_LIB, "fetch.ts"), "utf8");
    expect(fetchSource).toContain("readExploreJson");
    expect(fetchSource, "a second copy of the bearer/timeout logic would drift").not.toContain(
      "serverFetchJson",
    );
  });

  it("encodes the slug it puts in a URL", () => {
    const fetchSource = readFileSync(join(COLLECTIONS_LIB, "fetch.ts"), "utf8");
    expect(fetchSource).toContain("encodeURIComponent(slug)");
  });

  it("never renders the withheld count on a viewer-facing surface", () => {
    // `withheld` distinguishes "empty" from "hidden" for an operator. Rendering it would tell a
    // viewer that baskets exist which they may not open — exactly what `visibility` prevents.
    for (const path of [...filesUnder(COLLECTIONS_COMPONENTS), ...filesUnder(COLLECTION_PAGES)]) {
      if (!path.endsWith(".tsx") || path.includes("__tests__")) continue;
      const text = readFileSync(path, "utf8");
      expect(text, `${path} renders withheld`).not.toMatch(/\{\s*collection\.withheld/);
    }
  });
});
