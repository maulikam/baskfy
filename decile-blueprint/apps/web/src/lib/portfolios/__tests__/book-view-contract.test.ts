import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..", "..", "..", "components", "portfolios");

const BOOK_BOX = readFileSync(join(ROOT, "book-box.tsx"), "utf8");
const BOOK_OVERALL = readFileSync(join(ROOT, "book-overall.tsx"), "utf8");
const LIST = readFileSync(join(ROOT, "portfolios-list.tsx"), "utf8");

describe("the book view is a ledger, not a buy list", () => {
  it("renders no unit count", () => {
    for (const source of [BOOK_BOX, BOOK_OVERALL, LIST]) {
      const withoutHoldingsWord = source.replace(/holdings?/gi, "");
      for (const word of ["quantity", "qty", "lot size"]) {
        expect(withoutHoldingsWord.toLowerCase(), `mentions ${word}`).not.toContain(word);
      }
    }
  });

  it("says it places nothing", () => {
    expect(LIST).toMatch(/places an order/i);
  });

  it("has no control that could submit an order", () => {
    for (const source of [BOOK_BOX, BOOK_OVERALL, LIST]) {
      for (const word of ["/execute", "place_order", "placeOrder", "confirm=true"]) {
        expect(source).not.toContain(word);
      }
    }
  });

  it("uses no advice language", () => {
    for (const source of [BOOK_BOX, BOOK_OVERALL, LIST]) {
      const lower = source.toLowerCase();
      for (const phrase of ["we recommend", "you should", "recommended for you", "best for you"]) {
        expect(lower, `says '${phrase}'`).not.toContain(phrase);
      }
    }
  });

  it("does not add basket marks and sleeve capital into one NAV", () => {
    expect(BOOK_OVERALL).toMatch(/not a combined figure/i);
    expect(BOOK_OVERALL).not.toMatch(/net worth/i);
  });
});
