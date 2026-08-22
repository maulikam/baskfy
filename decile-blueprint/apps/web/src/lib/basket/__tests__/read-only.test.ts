import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * M22's acceptance, from the web app's side: **no order-placing route is reachable from here.**
 *
 * The API asserts the same thing about its own surface
 * (`services/api/tests/test_baskets_readonly.py`). This asserts it about the client: no fetch
 * helper that mutates, no server action behind a button, no form posting anywhere near an order.
 *
 * Execution stays in the desk console. That is the SEBI gate — the desk trades one account, its
 * owner's, and a web surface that could place an order for a logged-in user is a different
 * regulated activity — and it is the desk's non-negotiable #1, that `packages/execution` is the
 * only path to an order.
 */

const HERE = join(__dirname, "..");
const BASKET_PAGES = join(__dirname, "..", "..", "..", "app", "(app)", "baskets");

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const SOURCES = [...filesUnder(HERE), ...filesUnder(BASKET_PAGES)].filter(
  (path) => (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"),
);

describe("the basket surfaces are read-only", () => {
  it("has sources to check", () => {
    expect(SOURCES.length).toBeGreaterThan(0);
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
    }
  });

  it("names no order, execution or broker endpoint", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8").toLowerCase();
      for (const word of ["/execute", "place_order", "placeorder", "gtt", "kiteconnect"]) {
        expect(source, `${path} mentions ${word}`).not.toContain(word);
      }
    }
  });

  it("says so on the page, where a person can see it", () => {
    // Not decoration. Someone looking at a plan table needs to know this is a record and not a
    // control, and the guarantee is worth as much to the reader as to the linter.
    for (const page of filesUnder(BASKET_PAGES).filter((p) => p.endsWith("page.tsx"))) {
      expect(readFileSync(page, "utf8")).toContain("read-only");
    }
  });
});
