import { describe, expect, it } from "vitest";

import type { Collection } from "@/lib/collections/fetch";
import type { ExploreBasketCard } from "@/lib/explore/fetch";
import { MIN_SHELVES_TO_STACK, selectShelves } from "@/lib/collections/select";

function basket(slug: string): ExploreBasketCard {
  return {
    slug,
    name: slug.replace(/-/g, " "),
    access: "FREE",
    visibility: "PUBLISHED",
    type: "STOCK",
    categories: ["momentum"],
    rebalance_frequency: "WEEKLY",
    source: "SCAN",
    description_md: null,
    launched_at: null,
    manager: { slug: "baskfy-engine", name: "Baskfy Engine", kind: "ENGINE" },
    metrics: null,
  };
}

function shelf(slug: string, slugs: string[], position: number): Collection {
  return {
    slug,
    title: slug.replace(/-/g, " "),
    subtitle: null,
    basket_slugs: slugs,
    baskets: slugs.map(basket),
    position,
    withheld: 0,
  };
}

/**
 * The one-basket shape, measured out of the dev database at the start of this work: `cb_basket`
 * held a single row (`momentum-scan`, momentum, WEEKLY, run by `baskfy-engine`), so three
 * predicates matched it and the quarterly predicate matched nothing. The catalogue has since
 * grown, which is exactly why this stays as a fixture: the degenerate case is the one the browse
 * surfaces have to survive, and it comes back the moment a fresh database is seeded.
 */
function todaysPayload(): Collection[] {
  return [
    shelf("start-here", ["momentum-scan"], 10),
    shelf("momentum", ["momentum-scan"], 20),
    shelf("run-by-the-engine", ["momentum-scan"], 30),
    shelf("quarterly", [], 40),
  ];
}

describe("selectShelves", () => {
  it("drops a shelf with no baskets — it groups nothing", () => {
    const { shelves, suppressed } = selectShelves([
      shelf("momentum", ["a", "b"], 10),
      shelf("quarterly", [], 20),
    ]);
    expect(shelves.map((s) => s.slug)).toEqual(["momentum"]);
    expect(suppressed.map((s) => s.slug)).toEqual(["quarterly"]);
  });

  it("drops a shelf holding exactly the baskets of a shelf above it, whatever the order", () => {
    const { shelves, suppressed } = selectShelves([
      shelf("momentum", ["a", "b", "c"], 10),
      shelf("run-by-the-engine", ["c", "a", "b"], 20),
    ]);
    expect(shelves.map((s) => s.slug)).toEqual(["momentum"]);
    expect(suppressed.map((s) => s.slug)).toEqual(["run-by-the-engine"]);
  });

  it("keeps a narrower shelf that sits entirely inside a broader one", () => {
    // Containment is not redundancy — it is what a shelf *is*. Suppressing "rebalanced
    // quarterly" because its three baskets are also among the six cheapest would delete an
    // editorial claim the catalogue is making, which is the opposite of the fix.
    const { shelves } = selectShelves([
      shelf("start-here", ["a", "b", "c"], 10),
      shelf("quarterly", ["b", "c"], 20),
    ]);
    expect(shelves.map((s) => s.slug)).toEqual(["start-here", "quarterly"]);
  });

  it("keeps two shelves that merely overlap", () => {
    const { shelves } = selectShelves([
      shelf("start-here", ["a", "b"], 10),
      shelf("momentum", ["b", "c"], 20),
    ]);
    expect(shelves.map((s) => s.slug)).toEqual(["start-here", "momentum"]);
  });

  it("keeps curator order and never re-sorts", () => {
    const { shelves } = selectShelves([
      shelf("third", ["c"], 30),
      shelf("first", ["a"], 10),
      shelf("second", ["b"], 20),
    ]);
    expect(shelves.map((s) => s.slug)).toEqual(["third", "first", "second"]);
  });

  it("suppresses the later of two identical shelves, not the earlier", () => {
    const { shelves, suppressed } = selectShelves([
      shelf("momentum", ["a"], 10),
      shelf("run-by-the-engine", ["a"], 20),
    ]);
    expect(shelves.map((s) => s.slug)).toEqual(["momentum"]);
    expect(suppressed.map((s) => s.slug)).toEqual(["run-by-the-engine"]);
  });

  it("a one-basket catalogue leaves one survivor, which is below the stacking threshold", () => {
    const { shelves, suppressed, stack } = selectShelves(todaysPayload());
    expect(shelves.map((s) => s.slug)).toEqual(["start-here"]);
    expect(suppressed.map((s) => s.slug)).toEqual([
      "momentum",
      "run-by-the-engine",
      "quarterly",
    ]);
    expect(shelves.length).toBeLessThan(MIN_SHELVES_TO_STACK);
    expect(stack).toBe(false);
  });

  it("a differentiated catalogue still stacks — shelves that group are not suppressed", () => {
    const { shelves, stack } = selectShelves([
      shelf("start-here", ["cheap-one", "cheap-two"], 10),
      shelf("momentum", ["momentum-scan"], 20),
      shelf("run-by-the-engine", ["momentum-scan"], 30),
      shelf("quarterly", ["quarterly-value"], 40),
    ]);
    expect(shelves.map((s) => s.slug)).toEqual(["start-here", "momentum", "quarterly"]);
    expect(stack).toBe(true);
  });

  it("the differentiated case measured off the live catalogue keeps all three real shelves", () => {
    // Read out of the dev database while this was being written, after the catalogue grew from
    // one basket to seven. `run-by-the-engine` is byte-for-byte `momentum` — every basket is run
    // by the engine — and it is the only shelf that should go.
    const { shelves, suppressed, stack } = selectShelves([
      shelf(
        "start-here",
        ["momentum-scan", "broad-market-sharpe", "liquid-momentum", "momentum-low-volatility",
         "quality-momentum", "six-month-sharpe"],
        10,
      ),
      shelf(
        "momentum",
        ["broad-market-sharpe", "liquid-momentum", "momentum-scan", "momentum-low-volatility",
         "quality-momentum", "six-month-sharpe", "trend-stack"],
        20,
      ),
      shelf(
        "run-by-the-engine",
        ["broad-market-sharpe", "liquid-momentum", "momentum-scan", "momentum-low-volatility",
         "quality-momentum", "six-month-sharpe", "trend-stack"],
        30,
      ),
      shelf("quarterly", ["liquid-momentum", "six-month-sharpe", "quality-momentum"], 40),
    ]);
    expect(shelves.map((s) => s.slug)).toEqual(["start-here", "momentum", "quarterly"]);
    expect(suppressed.map((s) => s.slug)).toEqual(["run-by-the-engine"]);
    expect(stack).toBe(true);
  });

  it("every collection stays reachable — kept plus suppressed is the whole input, losing none", () => {
    // The property that makes suppression safe. If this ever fails, a shelf has been deleted
    // from the product rather than moved off one page.
    const input = todaysPayload();
    const { shelves, suppressed } = selectShelves(input);
    expect([...shelves, ...suppressed].map((s) => s.slug).sort()).toEqual(
      input.map((s) => s.slug).sort(),
    );
    expect(shelves.length + suppressed.length).toBe(input.length);
  });

  it("returns nothing to stack when there are no collections at all", () => {
    expect(selectShelves([])).toEqual({ shelves: [], suppressed: [], stack: false });
  });
});
