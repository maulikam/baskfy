import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * What the sleeve planner must and must not put on screen (M34).
 *
 * The page divides real money across screens. Two properties decide whether it stays on the right
 * side of the line the desk console draws:
 *
 * 1. **Amounts and weights, never a number of units.** A unit count needs a market quote and turns
 *    a plan into a buy list. The API cannot produce one — `baskfy_core.sleeves` receives no quote —
 *    and the page must not invent one either.
 * 2. **The stance is stated, not urged.** Baskfy publishes no advice and is not SEBI-registered.
 *    "Under R1 the strategy caps equity at 100%" describes a strategy; telling the reader to
 *    deploy a sum is advice.
 *
 * Asserted over the source because both are prose and markup: a refactor that turns a statement
 * into a suggestion passes every behavioural test there is.
 */
const PLANNER = readFileSync(
  join(__dirname, "..", "..", "..", "components", "portfolios", "sleeve-planner.tsx"),
  "utf8",
);

describe("the sleeve planner is a plan, not a buy list", () => {
  it("renders no unit count", () => {
    // `top_n` is how many *names* a sleeve takes, which is not a quantity of anything tradable.
    const withoutTopN = PLANNER.replace(/top_n/g, "");
    for (const word of ["quantity", "shares", "qty", "lot size"]) {
      expect(withoutTopN.toLowerCase(), `the planner mentions ${word}`).not.toContain(word);
    }
  });

  it("says on the page that it places nothing", () => {
    expect(PLANNER).toMatch(/nothing here places an order/i);
    expect(PLANNER).toMatch(/desk console/i);
  });

  it("has no control that could submit an order", () => {
    for (const word of ["/execute", "place_order", "placeOrder", "confirm=true"]) {
      expect(PLANNER).not.toContain(word);
    }
  });
});

describe("the sleeve planner states the stance rather than urging it", () => {
  it("describes what the strategy does", () => {
    expect(PLANNER).toMatch(/the strategy caps equity at/i);
  });

  it("uses no advice language", () => {
    for (const phrase of ["we recommend", "you should", "recommended for you", "best for you"]) {
      expect(PLANNER.toLowerCase(), `the planner says '${phrase}'`).not.toContain(phrase);
    }
  });

  it("leaves applying the cap to the reader, unticked", () => {
    expect(PLANNER).toContain("useState(false)");
    expect(PLANNER).toMatch(/Size my screen sleeves to this cap/i);
  });

  it("says a cap withholds capital rather than shrinking the portfolio", () => {
    // At R2 a crore is still a crore. Reporting it as 70 lakh would be a bug wearing a stance.
    expect(PLANNER).toMatch(/held as cash, not removed/i);
  });
});

describe("the manual sleeve is presented as the reader's own", () => {
  it("offers it as a source and names it in the reader's terms", () => {
    expect(PLANNER).toMatch(/I run this myself/);
    expect(PLANNER).toMatch(/You run this one/);
  });
});
