import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * SC6: investor surfaces never grow an order route.
 * Invest more / Exit / Rebalance end in PlanHandoffPanel or MarketClosedModal.
 */

const INVEST_LIB = join(__dirname, "..");
const INVEST_COMPONENTS = join(__dirname, "..", "..", "..", "components", "investments");
const CB_COMPONENTS = join(__dirname, "..", "..", "..", "components", "cb");
const INVEST_PAGES = join(__dirname, "..", "..", "..", "app", "(app)", "investments");
const ME_INVEST_PAGES = join(__dirname, "..", "..", "..", "app", "(app)", "me", "investments");
const PORTFOLIO_COMPONENTS = join(__dirname, "..", "..", "..", "components", "portfolios");
const PORTFOLIO_LIB = join(__dirname, "..", "..", "portfolios");
const WATCHLIST_PAGE = join(__dirname, "..", "..", "..", "app", "(app)", "watchlist");
const FEES_PAGE = join(__dirname, "..", "..", "..", "app", "(app)", "fees");

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
  ...filesUnder(INVEST_LIB),
  ...filesUnder(INVEST_COMPONENTS),
  ...filesUnder(CB_COMPONENTS),
  ...filesUnder(INVEST_PAGES),
  ...filesUnder(ME_INVEST_PAGES),
  ...filesUnder(PORTFOLIO_COMPONENTS),
  ...filesUnder(PORTFOLIO_LIB),
  ...filesUnder(WATCHLIST_PAGE),
  ...filesUnder(FEES_PAGE),
].filter((path) => (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"));

describe("the investor surfaces place no orders", () => {
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

  it("wires order-shaped CTAs through PlanHandoffPanel and MarketClosedModal", () => {
    const actions = readFileSync(join(INVEST_COMPONENTS, "investment-actions.tsx"), "utf8");
    expect(actions).toContain("PlanHandoffPanel");
    expect(actions).toContain("MarketClosedModal");
    expect(actions).not.toMatch(/<form[\s>]/);
    expect(actions).not.toContain('"use server"');
  });

  it("keeps handoff stubs in components/cb", () => {
    const handoff = readFileSync(join(CB_COMPONENTS, "plan-handoff-panel.tsx"), "utf8");
    const closed = readFileSync(join(CB_COMPONENTS, "market-closed-modal.tsx"), "utf8");
    expect(handoff).toContain("PlanHandoffPanel");
    expect(closed).toContain("MarketClosedModal");
  });
});
