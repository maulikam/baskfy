import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * What the sleeve planner must and must not put on screen (M34, amended by tree 5).
 *
 * The page divides real money across screens. Two properties decide whether it stays on the right
 * side of the line the desk console draws:
 *
 * 1. **A count of shares is a description, never an instruction.**
 *
 *    ⚠️ **This clause changed, deliberately.** It used to read "amounts and weights, never a
 *    number of units", on the reasoning that a unit count needs a quote and the allocator received
 *    none. Tree 5 gave the allocator a price map (`baskfy_core.portfolio_units`) and the API now
 *    sends `units` and `price` per row, so the premise is gone: "₹5,00,000 of CUPID" is not
 *    something a reader can check against a demat statement and "1,757 shares" is.
 *
 *    What survives from the old rule is the part that was actually about safety — **there is no
 *    control on this page that could place anything, and there is none anywhere in this app.** A
 *    count is not an order, and the words that belong to an order path (`quantity`, `qty`, `lot
 *    size`) stay out of the planner's vocabulary.
 *
 *    The second half of the new rule is the one the units feature exists for: an unpriced row's
 *    count is **`null`, rendered as a blank with a reason — never a `0`.** A zero reads as "buy
 *    none of this", which is a different and false statement. The rendering is asserted in
 *    `components/portfolios/__tests__/sleeve-units.test.tsx`; what is asserted here is that the
 *    source never launders that null into a zero on the way to the cell.
 *
 * 2. **The stance is stated, not urged.** Baskfy publishes no advice and is not SEBI-registered.
 *    "Under R1 the strategy caps equity at 100%" describes a strategy; telling the reader to
 *    deploy a sum is advice.
 *
 * Asserted over the source because both are prose and markup: a refactor that turns a statement
 * into a suggestion passes every behavioural test there is.
 */
const COMPONENTS = join(__dirname, "..", "..", "..", "components", "portfolios");
const PLANNER = readFileSync(join(COMPONENTS, "sleeve-planner.tsx"), "utf8");
const CARD = readFileSync(join(COMPONENTS, "sleeve-allocation-card.tsx"), "utf8");

describe("the sleeve planner is a plan, not a buy list", () => {
  it("shows a unit count without borrowing an order path's vocabulary", () => {
    expect(CARD).toMatch(/unitsCell/);
    expect(CARD).toMatch(/>\s*Units\s*</);
    // `top_n` is how many *names* a sleeve takes, which is not a quantity of anything tradable.
    for (const source of [PLANNER.replace(/top_n/g, ""), CARD]) {
      for (const word of ["quantity", "qty", "lot size"]) {
        expect(source.toLowerCase(), `mentions ${word}`).not.toContain(word);
      }
    }
  });

  it("never turns a missing unit count into a zero", () => {
    for (const source of [PLANNER, CARD]) {
      for (const laundering of ["units ?? 0", "units || 0", "Number(row.units)", "units: 0"]) {
        expect(source, `launders a null unit count with \`${laundering}\``).not.toContain(
          laundering,
        );
      }
    }
  });

  it("says on the page that it places nothing", () => {
    expect(PLANNER).toMatch(/nothing here places an order/i);
    expect(PLANNER).toMatch(/desk console/i);
  });

  it("has no control that could submit an order", () => {
    for (const source of [PLANNER, CARD]) {
      for (const word of ["/execute", "place_order", "placeOrder", "confirm=true"]) {
        expect(source).not.toContain(word);
      }
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
    // The default is what matters, not where it is stored. It moved from component state into
    // the URL so a capped view can be linked to; `withDefault(false)` keeps it off until ticked.
    expect(PLANNER).toMatch(/withDefault\(false\)/);
    expect(PLANNER).toMatch(/Size my screen allocations to this cap/i);
  });

  it("puts the cap in the URL so the view can be linked to", () => {
    expect(PLANNER).toContain('useQueryState(');
    expect(PLANNER).toContain('"cap"');
  });

  it("says a cap withholds capital rather than shrinking the portfolio", () => {
    // At R2 a crore is still a crore. Reporting it as 70 lakh would be a bug wearing a stance.
    expect(PLANNER).toMatch(/held as cash, not removed/i);
  });
});

describe("the manual sleeve is presented as the reader's own", () => {
  it("offers it as a source and names it in the reader's terms", () => {
    // The picker is still the planner's; the allocation card was extracted so the unit cell could
    // be tested on its own, and the sleeve's own name for a hand-run slice went with it.
    expect(PLANNER).toMatch(/I run this myself/);
    expect(CARD).toMatch(/You run this one/);
  });
});
