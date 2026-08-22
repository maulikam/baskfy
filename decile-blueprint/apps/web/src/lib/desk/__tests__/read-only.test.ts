import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * M26's acceptance, from the web app's side: **no order-placing route is reachable from here.**
 *
 * The same assertion `lib/basket/__tests__/read-only.test.ts` makes about the basket surfaces,
 * extended over the five desk pages. It is a separate file rather than a widened one because the
 * two modules can be deleted independently, and a guarantee that quietly stops covering a
 * directory is worse than one that never covered it.
 *
 * The API asserts the same thing about its own surface
 * (`services/api/tests/test_desk_readonly.py`).
 *
 * Execution stays in the desk console. That is the SEBI gate — the desk trades one account, its
 * owner's, and a web surface that could place an order for a logged-in user is a different
 * regulated activity — and it is the desk's non-negotiable #1, that `packages/execution` is the
 * only path to an order. Neither is settled until D3 has a written answer, so nothing here may
 * anticipate one.
 */

const APP = join(__dirname, "..", "..", "..", "app", "(app)");
const PAGE_DIRS = ["performance", "holdings", "tradebook", "regime", "reconcile"];

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const PAGES = PAGE_DIRS.flatMap((dir) => filesUnder(join(APP, dir)));

const SOURCES = [
  ...filesUnder(join(__dirname, "..")),
  ...filesUnder(join(__dirname, "..", "..", "..", "components", "desk")),
  ...PAGES,
].filter((path) => (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"));

describe("the desk surfaces are read-only", () => {
  it("covers all five pages", () => {
    expect(PAGES.filter((path) => path.endsWith("page.tsx"))).toHaveLength(PAGE_DIRS.length);
  });

  it("never issues a non-GET request", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      for (const verb of ['method: "POST"', 'method: "PUT"', 'method: "PATCH"', 'method: "DELETE"']) {
        expect(source, `${path} uses ${verb}`).not.toContain(verb);
      }
    }
  });

  it("declares no server action", () => {
    for (const path of SOURCES) {
      expect(readFileSync(path, "utf8"), `${path} declares a server action`).not.toContain(
        '"use server"',
      );
    }
  });

  it("renders no form and no submit control", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} renders a form`).not.toMatch(/<form[\s>]/);
      expect(source, `${path} renders a submit button`).not.toMatch(/type="submit"/);
      expect(source, `${path} renders a button`).not.toMatch(/<button[\s>]/);
    }
  });

  it("names no order, execution or broker endpoint", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8").toLowerCase();
      for (const word of ["/execute", "place_order", "placeorder", "gtt", "kiteconnect", "confirm=true"]) {
        expect(source, `${path} mentions ${word}`).not.toContain(word);
      }
    }
  });

  it("reads only from the desk's read endpoints", () => {
    // The fetch helper is the only thing that talks to the API, and every path it names is a
    // `/desk/*` read. A helper pointed anywhere else is how a mutation would arrive.
    const fetcher = readFileSync(join(__dirname, "..", "fetch.ts"), "utf8");
    const paths = [...fetcher.matchAll(/readJson\("([^"]+)"\)/g)].map((match) => match[1]);
    expect(paths.length).toBeGreaterThan(0);
    for (const path of paths) {
      expect(path, `${path} is not a desk read`).toMatch(/^\/desk\//);
    }
  });

  it("says so on every page, where a person can see it", () => {
    // Not decoration. Someone looking at a position table needs to know it is a record and not a
    // control, and the guarantee is worth as much to the reader as to the linter.
    for (const page of PAGES.filter((path) => path.endsWith("page.tsx"))) {
      expect(readFileSync(page, "utf8"), `${page} does not say it is read-only`).toContain(
        "ReadOnlyFooter",
      );
    }
    expect(
      readFileSync(join(__dirname, "..", "..", "..", "components", "desk", "ui.tsx"), "utf8"),
    ).toContain("read-only");
  });
});
