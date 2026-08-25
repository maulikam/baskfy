import { describe, expect, it } from "vitest";

import { FACTOR_COUNT, FACTOR_FAMILIES } from "@/lib/marketing/factor-families";
import {
  INDEX_LISTS,
  INDEX_LIST_COUNT,
  LANDING_CATEGORIES,
  LANDING_STATS,
  COVERAGE_PILLS,
  PRODUCT_PILLS,
  TICKER_LINES,
  RANKING_WINDOWS,
} from "@/lib/marketing/landing-band";

/**
 * The band under the hero states four figures, and the point of this file is that **none of them
 * is a number somebody typed**.
 *
 * docs/14 §Tone allows a claim about what is published and forbids one about an outcome; a figure
 * that is true on the day it is written and silently false a release later is the second kind
 * wearing the first kind's clothes. So the assertions below are about *derivation* — that the
 * rendered value is the registry's own sum, the published list's own length, the window list's own
 * length — rather than about the strings "64" and "14", which is what a test that merely locked in
 * today's behaviour would do (house rule 2).
 */

/**
 * The fourteen slugs `GET /api/v1/meta/universes` published on 23 Aug 2026, transcribed in
 * `src/lib/screens/__tests__/universe-label.test.ts`. Repeated here because this module builds its
 * list from two *different* sources — the twelve size bands plus two classifications — and the
 * only thing that catches a mistake in that seam is comparing the result against the whole.
 */
const PUBLISHED_SLUGS = [
  "nifty-allcap",
  "nifty-50",
  "nifty-next-50",
  "nifty-100",
  "nifty-200",
  "nifty-500",
  "nifty-total-market",
  "nifty-large-mid-250",
  "nifty-midcap-150",
  "nifty-smallcap-250",
  "nifty-microcap-250",
  "nifty-mid-small-400",
  "nifty-fno",
  "etf",
] as const;

describe("the published list of universes", () => {
  it("is the whole published set, size bands and classifications together", () => {
    expect([...INDEX_LISTS.map((list) => list.slug)].sort()).toEqual([...PUBLISHED_SLUGS].sort());
  });

  it("names each one once", () => {
    expect(new Set(INDEX_LISTS.map((list) => list.slug)).size).toBe(INDEX_LIST_COUNT);
  });

  it("shows the human label, never the exchange's shouted name", () => {
    expect(INDEX_LISTS.map((list) => list.label)).toContain("NIFTY Total Market");
    for (const list of INDEX_LISTS) expect(list.label).not.toMatch(/\bMARKET\b|\bMIDCAP\b/);
  });
});

describe("the four figures", () => {
  it("counts factors by summing the registry rather than by restating it", () => {
    expect(LANDING_STATS[0]?.value).toBe(String(FACTOR_COUNT));
    expect(FACTOR_COUNT).toBe(FACTOR_FAMILIES.reduce((total, f) => total + f.count, 0));
  });

  it("counts lists by measuring the published list", () => {
    expect(LANDING_STATS[1]?.value).toBe(String(INDEX_LISTS.length));
  });

  it("counts windows by measuring the window list", () => {
    expect(LANDING_STATS[2]?.value).toBe(String(RANKING_WINDOWS.length));
  });

  it("promises a price, and a price is the only number here that is ours to set", () => {
    expect(LANDING_STATS[3]?.value).toBe("₹0");
  });

  it("never states a return, a target or a period performance", () => {
    for (const stat of LANDING_STATS) {
      expect(`${stat.value} ${stat.label}`).not.toMatch(/%|\breturn(s)?\b|\bcagr\b|\bp\.?a\.?\b/i);
    }
  });
});

/**
 * The two rows stopped describing the data plant on 24 Aug 2026.
 *
 * They scrolled the fourteen index names over the sixty-four factor keys — the tables underneath
 * the product. Maulik's objection was exact: that is what every competitor also holds, and it says
 * nothing about baskets, sleeves, brokers or plans. So the first row now names **what you can run**
 * and the second names **what it reaches**, and the spec these assert is that neither row drifts
 * back into plumbing.
 */
describe("the scrolling pills", () => {
  const ALL = [...PRODUCT_PILLS, ...COVERAGE_PILLS];

  it("names no index and no factor key — those are the tables, not the product", () => {
    for (const pill of ALL) {
      expect(pill, `${pill} is a factor key`).not.toMatch(/^[a-z]+(?:_[a-z0-9]+)+$/);
      expect(pill, `${pill} is an index name`).not.toMatch(/^NIFTY\b/);
    }
    for (const family of FACTOR_FAMILIES) expect(ALL).not.toContain(family.label);
    for (const window of RANKING_WINDOWS) expect(ALL).not.toContain(window);
  });

  /**
   * The product is a basket platform and a portfolio manager as well as a screener (24 Aug 2026).
   * A row that names only the screener half puts the page back to selling a third of the product,
   * which is the failure this row was rewritten to fix.
   */
  it("names all three halves of the product, not just the screener", () => {
    const joined = PRODUCT_PILLS.join(" | ").toLowerCase();
    expect(joined, "manager baskets are missing").toMatch(/basket/);
    expect(joined, "the portfolio half is missing").toMatch(/sleeve|portfolio/);
    expect(joined, "building your own is missing").toMatch(/screen|backtest/);
  });

  it("says the shares are yours and names the broker path", () => {
    const joined = COVERAGE_PILLS.join(" | ").toLowerCase();
    expect(joined).toMatch(/demat/);
    expect(joined).toMatch(/kite|broker|holdings/);
  });

  it("repeats nothing — a marquee shows every pill, so a duplicate is visible", () => {
    expect(new Set(ALL).size).toBe(ALL.length);
  });

  it("keeps each row long enough to fill the viewport before it wraps", () => {
    expect(PRODUCT_PILLS.length).toBeGreaterThanOrEqual(8);
    expect(COVERAGE_PILLS.length).toBeGreaterThanOrEqual(8);
  });
});

describe("the ticker", () => {
  it("says more than one thing", () => {
    expect(TICKER_LINES.length).toBeGreaterThanOrEqual(4);
    expect(new Set(TICKER_LINES).size).toBe(TICKER_LINES.length);
  });

  it("promises no outcome — every line is a fact about what is computed", () => {
    for (const line of TICKER_LINES) {
      expect(line).not.toMatch(/\b(guarantee|assured|profit|beat the market|will outperform)\b/i);
    }
  });
});

describe("the four categories", () => {
  it("is four, because the row is a four-column grid and a fifth would wrap alone", () => {
    expect(LANDING_CATEGORIES).toHaveLength(4);
  });

  /**
   * Maulik, 24 Aug 2026: the product is a basket platform and a portfolio manager, not a momentum
   * screener with a checkout button. The screener is one of four things this row names, and a
   * later edit that quietly drops the portfolio or the basket half puts the page back to
   * describing a third of the product.
   */
  it("names all three products and the broker hand-off, not just the screener", () => {
    expect(LANDING_CATEGORIES.map((category) => category.title)).toEqual([
      "Screen",
      "Baskets",
      "Portfolios",
      "Brokers",
    ]);
  });

  it("says the shares are held directly, which is the whole difference from a fund", () => {
    const baskets = LANDING_CATEGORIES.find((category) => category.title === "Baskets");
    expect(baskets?.body).toMatch(/nothing is pooled/i);
  });

  it("says a portfolio can nest, because sub-portfolios are the point", () => {
    const portfolios = LANDING_CATEGORIES.find((category) => category.title === "Portfolios");
    expect(portfolios?.body).toMatch(/sleeves/i);
  });

  it("keeps the non-negotiable that this site never places the order", () => {
    const brokers = LANDING_CATEGORIES.find((category) => category.title === "Brokers");
    expect(brokers?.body).toMatch(/you confirm/i);
  });
});
