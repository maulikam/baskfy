import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * SC5: explore / basket detail surfaces never grow an order route.
 * Same bar as M22's basket read-only suite — Invest ends in PlanHandoffPanel.
 */

const EXPLORE_LIB = join(__dirname, "..");
const EXPLORE_COMPONENTS = join(__dirname, "..", "..", "..", "components", "explore");
const CB_COMPONENTS = join(__dirname, "..", "..", "..", "components", "cb");
const EXPLORE_PAGE = join(__dirname, "..", "..", "..", "app", "(app)", "explore");
const BASKET_PAGES = join(__dirname, "..", "..", "..", "app", "(app)", "basket");

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
  ...filesUnder(EXPLORE_LIB),
  ...filesUnder(EXPLORE_COMPONENTS),
  ...filesUnder(CB_COMPONENTS),
  ...filesUnder(EXPLORE_PAGE),
  ...filesUnder(BASKET_PAGES),
].filter((path) => (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"));

describe("the explore surfaces place no orders", () => {
  it("has sources to check", () => {
    expect(SOURCES.length).toBeGreaterThan(0);
  });

  it("never names an execute or broker order endpoint", () => {
    for (const path of SOURCES) {
      const source = readFileSync(path, "utf8").toLowerCase();
      for (const word of ["/execute", "place_order", "placeorder", "kiteconnect", "confirm=true"]) {
        expect(source, `${path} mentions ${word}`).not.toContain(word);
      }
    }
  });

  it("wires Invest through PlanHandoffPanel, not a form post", () => {
    const invest = readFileSync(join(EXPLORE_COMPONENTS, "invest-cta.tsx"), "utf8");
    expect(invest).toContain("PlanHandoffPanel");
    expect(invest).not.toMatch(/<form[\s>]/);
    expect(invest).not.toContain('"use server"');
  });
});
