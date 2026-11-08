import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SelectionProvider } from "@/components/discover/selection-provider";
import { MatchBreakdown, StartingChoices } from "@/components/discover/starting-choices";
import { type Preferences, startingChoices } from "@/lib/discover/match";
import type { ExploreBasketCard, ExploreMetrics } from "@/lib/explore/fetch";

function metrics(over: Partial<ExploreMetrics> = {}): ExploreMetrics {
  return {
    as_of_date: "2026-08-22",
    min_amount: "300000.00",
    volatility_bucket: "MED",
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

function card(slug: string, over: Partial<ExploreBasketCard> = {}): ExploreBasketCard {
  return {
    slug,
    name: slug.replace(/-/g, " "),
    access: "FREE",
    visibility: "PUBLISHED",
    type: "STOCK",
    categories: ["momentum"],
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

const catalogue = [
  card("calm", { metrics: metrics({ volatility_value: "0.1000000000", headline_pct: "8.00" }) }),
  card("middle"),
  card("hot", {
    metrics: metrics({
      volatility_value: "0.4000000000",
      volatility_bucket: "HIGH",
      headline_pct: "61.00",
    }),
  }),
];

function renderChoices(baskets = catalogue) {
  return render(
    <SelectionProvider>
      <StartingChoices choices={startingChoices(baskets, prefs)} />
    </SelectionProvider>,
  );
}

describe("three starting choices", () => {
  it("shows three columns, each with its own heading", () => {
    renderChoices();
    const columns = screen.getAllByTestId("starting-choice");
    expect(columns).toHaveLength(3);
    expect(columns.map((node) => node.dataset.kind)).toEqual([
      "closest",
      "lower-swing",
      "higher-growth",
    ]);
  });

  it("explains the match as a count of preferences, naming them", () => {
    renderChoices();
    const closest = screen
      .getAllByTestId("starting-choice")
      .find((node) => node.dataset.kind === "closest")!;
    expect(within(closest).getByText(/Matches \d of \d preferences that could be checked:/))
      .toBeInTheDocument();
  });

  it("states a property of the basket, never a judgement about the reader", () => {
    renderChoices();
    const text = screen.getByTestId("starting-choices").textContent ?? "";
    expect(text).not.toMatch(/best for you|recommended|suitable for you|you should/i);
  });

  it("says a past return is not a forecast, beside the column that shows the biggest one", () => {
    renderChoices();
    const growth = screen
      .getAllByTestId("starting-choice")
      .find((node) => node.dataset.kind === "higher-growth")!;
    expect(within(growth).getByText(/not a forecast/i)).toBeInTheDocument();
  });

  it("says lower volatility is not higher return, beside the calmest column", () => {
    renderChoices();
    const calm = screen
      .getAllByTestId("starting-choice")
      .find((node) => node.dataset.kind === "lower-swing")!;
    expect(within(calm).getByText(/smaller swings, not higher returns/i)).toBeInTheDocument();
  });

  it("never shows the same basket in two columns", () => {
    renderChoices();
    const slugs = screen
      .getAllByTestId("discover-basket-card")
      .map((node) => node.dataset.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
  });
});

describe("a thin catalogue", () => {
  it("drops a column rather than repeating a basket, and says it did", () => {
    renderChoices([catalogue[0]!]);
    expect(screen.getAllByTestId("starting-choice")).toHaveLength(1);
    expect(screen.getByTestId("starting-choices-short").textContent).toMatch(
      /would tell you nothing/,
    );
  });

  it("does not claim a shortfall when all three columns are filled", () => {
    renderChoices();
    expect(screen.queryByTestId("starting-choices-short")).not.toBeInTheDocument();
  });

  it("says what to do next when nothing matched at all", () => {
    render(
      <SelectionProvider>
        <StartingChoices choices={[]} />
      </SelectionProvider>,
    );
    expect(screen.getByTestId("starting-choices-empty").textContent).toMatch(
      /Widen the amount or the volatility range/,
    );
  });
});

describe("the match breakdown", () => {
  it("lists every preference, matched or not", () => {
    const choice = startingChoices(catalogue, prefs)[0]!;
    render(<MatchBreakdown choice={choice} />);
    expect(screen.getAllByTestId("match-check")).toHaveLength(5);
  });

  it("marks a preference it could not examine as counted neither way", () => {
    const noTags = card("untagged", { categories: [] });
    const choice = startingChoices([noTags], prefs)[0]!;
    render(<MatchBreakdown choice={choice} />);
    expect(screen.getByText(/not counted either way/)).toBeInTheDocument();
  });

  it("gives a reason for every check, so a reader can disagree with the filter", () => {
    const choice = startingChoices(catalogue, prefs)[0]!;
    render(<MatchBreakdown choice={choice} />);
    for (const item of screen.getAllByTestId("match-check")) {
      expect((item.textContent ?? "").length).toBeGreaterThan(25);
    }
  });
});
