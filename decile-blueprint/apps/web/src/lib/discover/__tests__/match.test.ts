import { describe, expect, it } from "vitest";

import type { ExploreBasketCard, ExploreMetrics } from "@/lib/explore/fetch";
import {
  DEFAULT_PREFERENCES,
  type Preferences,
  evaluateMatch,
  matchSummary,
  preferencesToParams,
  startingChoices,
} from "@/lib/discover/match";

function metrics(over: Partial<ExploreMetrics> = {}): ExploreMetrics {
  return {
    as_of_date: "2026-08-22",
    min_amount: "300000.00",
    volatility_bucket: "MEDIUM",
    volatility_value: "0.1820000000",
    ret_1m: null,
    ret_6m: null,
    ret_1y: "22.10",
    cagr_3y: null,
    cagr_5y: null,
    since_inception_pct: null,
    headline_label: "1Y returns",
    headline_pct: "22.10",
    return_convention: "PRICE_RETURN",
    dividends_included: false,
    return_convention_note: "Price return.",
    ...over,
  };
}

function card(over: Partial<ExploreBasketCard> = {}): ExploreBasketCard {
  return {
    slug: "liquid-momentum",
    name: "Liquid Momentum",
    access: "FREE",
    visibility: "PUBLISHED",
    type: "STOCK",
    categories: ["momentum", "liquidity"],
    rebalance_frequency: "QUARTERLY",
    source: "SCAN",
    description_md: null,
    launched_at: "2019-01-01",
    manager: { slug: "baskfy-engine", name: "Baskfy Engine", kind: "ENGINE" },
    metrics: metrics(),
    ...over,
  };
}

const prefs: Preferences = {
  goal: "long-term-growth",
  horizon: "5-plus",
  risk: "moderate",
  amount: 500_000,
  rebalance: "QUARTERLY",
};

describe("the language stays filter language", () => {
  it("never says best, recommended or suitable", () => {
    const result = evaluateMatch(card(), prefs);
    const text = [result.summary, ...result.checks.map((c) => c.reason)].join(" ");
    expect(text).not.toMatch(/best|recommend|suitable|should (buy|invest)/i);
  });

  it("counts preferences it could examine, not preferences that were set", () => {
    // "any rebalance schedule" is not a restriction, so it must not inflate either side of the
    // fraction — a 4-of-5 built partly from unchecked preferences would be a lie with a number.
    const result = evaluateMatch(card(), { ...prefs, rebalance: "any" });
    expect(result.examined).toBe(4);
    expect(result.summary).toMatch(/of 4 preferences/);
  });

  it("names the preferences that matched so a reader can disagree with the filter", () => {
    const result = evaluateMatch(card(), prefs);
    expect(result.summary).toMatch(/Matches \d of \d preferences that could be checked:/);
    expect(result.summary).toMatch(/long-term growth/);
  });

  it("says so plainly when nothing could be checked", () => {
    expect(matchSummary(0, 0, [])).toMatch(/None of your preferences could be checked/);
  });

  it("says none matched rather than listing an empty set", () => {
    expect(matchSummary(0, 3, [])).toBe("Matches none of the 3 preferences that could be checked.");
  });
});

describe("each preference is checked against a real property", () => {
  it("matches an amount at or above the basket's minimum", () => {
    const yes = evaluateMatch(card(), prefs).checks.find((c) => c.key === "amount");
    expect(yes?.matched).toBe(true);
    expect(yes?.reason).toMatch(/₹3\.00L.*within.*₹5\.00L/);

    const no = evaluateMatch(card(), { ...prefs, amount: 100_000 }).checks.find(
      (c) => c.key === "amount",
    );
    expect(no?.matched).toBe(false);
    expect(no?.reason).toMatch(/above/);
  });

  it("does not examine an amount when no minimum has been computed", () => {
    const check = evaluateMatch(
      card({ metrics: metrics({ min_amount: null }) }),
      prefs,
    ).checks.find((c) => c.key === "amount");
    expect(check?.examinable).toBe(false);
    expect(check?.matched).toBe(false);
  });

  it("treats moderate as admitting low and medium, and higher as excluding low", () => {
    const moderate = evaluateMatch(card(), prefs).checks.find((c) => c.key === "risk");
    expect(moderate?.matched).toBe(true);
    const higher = evaluateMatch(card({ metrics: metrics({ volatility_bucket: "LOW" }) }), {
      ...prefs,
      risk: "higher",
    }).checks.find((c) => c.key === "risk");
    expect(higher?.matched).toBe(false);
  });

  it("quotes the measured volatility beside the bucket", () => {
    const check = evaluateMatch(card(), prefs).checks.find((c) => c.key === "risk");
    expect(check?.reason).toMatch(/18\.2% annualised/);
  });

  it("matches the rebalance schedule exactly", () => {
    expect(
      evaluateMatch(card(), prefs).checks.find((c) => c.key === "rebalance")?.matched,
    ).toBe(true);
    expect(
      evaluateMatch(card(), { ...prefs, rebalance: "MONTHLY" }).checks.find(
        (c) => c.key === "rebalance",
      )?.matched,
    ).toBe(false);
  });

  it("checks the goal against category tags, and says it is doing that", () => {
    const check = evaluateMatch(card(), prefs).checks.find((c) => c.key === "goal");
    expect(check?.matched).toBe(true);
    expect(check?.reason).toMatch(/carries the “momentum” tag, which this goal filters for/);
  });

  it("does not examine the goal when the basket carries no tags", () => {
    const check = evaluateMatch(card({ categories: [] }), prefs).checks.find(
      (c) => c.key === "goal",
    );
    expect(check?.examinable).toBe(false);
  });

  it("checks horizon as evidence available, stated as a fact about history", () => {
    const long = evaluateMatch(card(), prefs).checks.find((c) => c.key === "horizon");
    expect(long?.matched).toBe(true);
    expect(long?.reason).toMatch(/months of history/);

    const young = evaluateMatch(card({ launched_at: "2025-06-01" }), prefs).checks.find(
      (c) => c.key === "horizon",
    );
    expect(young?.matched).toBe(false);
    expect(young?.reason).toMatch(/no 5\+ years record to judge it on/);
    // The wording must stay a fact about the basket, never a claim about the reader.
    expect(young?.reason).not.toMatch(/suit|too risky for you/i);
  });

  it("does not examine horizon without a launch date", () => {
    const check = evaluateMatch(card({ launched_at: null }), prefs).checks.find(
      (c) => c.key === "horizon",
    );
    expect(check?.examinable).toBe(false);
  });
});

describe("preferences become filter parameters", () => {
  it("sends the amount as a maximum minimum, cheapest first", () => {
    expect(preferencesToParams(DEFAULT_PREFERENCES)).toMatchObject({
      max_min_amount: "500000",
      sort: "min_amount",
      order: "asc",
    });
  });

  it("only narrows volatility when the reader asked for an end of the range", () => {
    expect(preferencesToParams({ ...DEFAULT_PREFERENCES, risk: "moderate" }).volatility)
      .toBeUndefined();
    expect(preferencesToParams({ ...DEFAULT_PREFERENCES, risk: "lower" }).volatility).toBe("LOW");
    expect(preferencesToParams({ ...DEFAULT_PREFERENCES, risk: "higher" }).volatility).toBe("HIGH");
  });

  it("omits the rebalance filter when the reader did not restrict it", () => {
    expect(
      preferencesToParams({ ...DEFAULT_PREFERENCES, rebalance: "any" }).rebalance_frequency,
    ).toBeUndefined();
    expect(
      preferencesToParams({ ...DEFAULT_PREFERENCES, rebalance: "MONTHLY" }).rebalance_frequency,
    ).toBe("MONTHLY");
  });
});

describe("three starting choices", () => {
  const catalogue = [
    card({ slug: "calm", name: "Calm", metrics: metrics({ volatility_value: "0.1000000000", headline_pct: "8.00" }) }),
    card({ slug: "middle", name: "Middle", metrics: metrics({ volatility_value: "0.1800000000", headline_pct: "22.10" }) }),
    card({ slug: "hot", name: "Hot", metrics: metrics({ volatility_value: "0.4000000000", volatility_bucket: "HIGH", headline_pct: "61.00" }) }),
  ];

  it("never shows the same basket in two columns", () => {
    const choices = startingChoices(catalogue, prefs);
    const slugs = choices.map((choice) => choice.basket.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
  });

  it("puts the calmest remaining basket in the lower-swing column", () => {
    const choices = startingChoices(catalogue, prefs);
    expect(choices.find((c) => c.kind === "lower-swing")?.basket.slug).toBe("calm");
  });

  it("puts the strongest remaining return in the higher-growth column", () => {
    const choices = startingChoices(catalogue, prefs);
    expect(choices.find((c) => c.kind === "higher-growth")?.basket.slug).toBe("hot");
  });

  it("says a higher past return is not a forecast, right where it shows one", () => {
    const growth = startingChoices(catalogue, prefs).find((c) => c.kind === "higher-growth");
    expect(growth?.rationale).toMatch(/not a forecast/i);
  });

  it("says lower volatility is not higher return, right where it shows one", () => {
    const calm = startingChoices(catalogue, prefs).find((c) => c.kind === "lower-swing");
    expect(calm?.rationale).toMatch(/smaller swings, not higher returns/i);
  });

  it("drops a column rather than repeating a basket when the catalogue is thin", () => {
    // A catalogue of one must produce one column, not the same card three times — the failure the
    // collections shelves had.
    const choices = startingChoices([catalogue[0]!], prefs);
    expect(choices).toHaveLength(1);
    expect(choices[0]?.kind).toBe("closest");
  });

  it("does not let the lead column take a basket another column exists to show", () => {
    // calm and middle tie on match count; hot loses on risk. If "closest" took calm, the
    // lower-swing column would then claim the second-calmest basket is the calmest.
    const choices = startingChoices(catalogue, prefs);
    expect(choices.find((c) => c.kind === "closest")?.basket.slug).toBe("middle");
    expect(choices.find((c) => c.kind === "lower-swing")?.rationale).toMatch(
      /the least of the three shown here/,
    );
  });

  it("keeps the superlative honest when every tied basket is an extreme", () => {
    // Two baskets: both are extremes, so the lead column has to take one. The wording of the
    // remaining column must then stop claiming a superlative it cannot support.
    const pair = [catalogue[0]!, catalogue[2]!];
    const choices = startingChoices(pair, prefs);
    expect(choices).toHaveLength(2);
    const slugs = choices.map((c) => c.basket.slug);
    expect(new Set(slugs).size).toBe(2);
    for (const choice of choices) {
      if (choice.kind === "closest") continue;
      const claimsSuperlative = /the (least|highest) of the three shown here/.test(choice.rationale);
      const isExtreme =
        choice.kind === "lower-swing"
          ? choice.basket.slug === "calm"
          : choice.basket.slug === "hot";
      expect(claimsSuperlative, `${choice.kind} claims only what is true`).toBe(isExtreme);
    }
  });

  it("returns nothing at all for an empty catalogue", () => {
    expect(startingChoices([], prefs)).toEqual([]);
  });

  it("carries the match explanation into every column", () => {
    for (const choice of startingChoices(catalogue, prefs)) {
      expect(choice.match.summary.length).toBeGreaterThan(10);
      expect(choice.match.checks.length).toBe(5);
    }
  });
});
