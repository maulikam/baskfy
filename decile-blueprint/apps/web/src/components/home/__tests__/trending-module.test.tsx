import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TrendingModule } from "@/components/home/trending-module";
import { EMPTY_TRENDING, type Trending, type TrendingList } from "@/lib/home/types";

afterEach(cleanup);

function list(overrides: Partial<TrendingList>): TrendingList {
  return {
    key: "TOP_1Y",
    title: "Moved most this year",
    ranks_by: "Ranked by 1-year price return, highest first.",
    metric_label: "1Y return",
    metric_kind: "PCT",
    population_based: false,
    population: null,
    eligible: 3,
    entries: [],
    withheld_reason: null,
    withheld_note: null,
    price_return_caveat: true,
    ...overrides,
  };
}

function trending(items: TrendingList[]): Trending {
  return {
    ...EMPTY_TRENDING,
    items,
    count: items.length,
    min_entries: 3,
    min_population: 5,
    return_convention_note:
      "Returns are price returns: splits and bonuses included, cash dividends not.",
  };
}

const RANKED = list({
  entries: [
    {
      rank: 1,
      basket_slug: "alpha",
      basket_name: "Alpha",
      metric_value: "12.00",
      metric_date: null,
      metric_display: "12.00%",
    },
    {
      rank: 2,
      basket_slug: "bravo",
      basket_name: "Bravo",
      metric_value: "8.00",
      metric_date: null,
      metric_display: "8.00%",
    },
  ],
});

describe("the trending module", () => {
  it("renders what each list actually ranks, next to its title (SC9 AC)", () => {
    render(<TrendingModule trending={trending([RANKED])} />);
    expect(screen.getByText("Moved most this year")).toBeTruthy();
    expect(screen.getByText(/Ranked by 1-year price return/)).toBeTruthy();
  });

  it("shows the rounded figure the API sent, never its own formatting", () => {
    render(<TrendingModule trending={trending([RANKED])} />);
    expect(screen.getByText("12.00%")).toBeTruthy();
    expect(screen.getByText("8.00%")).toBeTruthy();
  });

  it("carries the price-return caveat once a return is actually on screen", () => {
    render(<TrendingModule trending={trending([RANKED])} />);
    expect(screen.getByText(/cash dividends not/)).toBeTruthy();
  });

  it("renders a withheld list with the reason in readable words, not as a blank", () => {
    const withheld = list({
      key: "MOST_INVESTED",
      title: "Most invested in",
      ranks_by: "Ranked by how many people currently hold it.",
      population_based: true,
      population: 1,
      eligible: 1,
      withheld_reason: "TOO_FEW_PEOPLE",
      withheld_note: "Not enough people yet — 1 person has done it. It stays hidden until 5.",
      price_return_caveat: false,
    });
    render(<TrendingModule trending={trending([withheld])} />);

    const card = screen.getByTestId("trending-withheld-MOST_INVESTED");
    expect(card.getAttribute("data-withheld-reason")).toBe("TOO_FEW_PEOPLE");
    expect(screen.getByText(/Not enough people yet/)).toBeTruthy();
    expect(screen.getByText(/Ranked by how many people currently hold it/)).toBeTruthy();
  });

  it("says out loud when nothing at all can be published — today's single-tenant reality", () => {
    const all = ["TOP_1Y", "MOST_WATCHED", "MOST_INVESTED"].map((key) =>
      list({
        key,
        withheld_reason: "TOO_FEW_BASKETS",
        withheld_note: "Not enough baskets to rank yet — 1 has a 1y return, and a ranking needs 3.",
      }),
    );
    render(<TrendingModule trending={trending(all)} />);
    expect(screen.getByText(/No ranking can be published yet/)).toBeTruthy();
    expect(screen.queryByText(/cash dividends not/)).toBeNull();
  });

  it("renders nothing at all when there is no ranking to show", () => {
    /* This expected the sentence "Rankings are not available right now." until AFG (`f08d60f`,
       audit §1.14) removed it: a Trending heading over an apology is a module the reader has to
       read to learn it has nothing, and Home already has surfaces that do. The module now stands
       down entirely. The assertion is the strong half of that — an empty *frame* would be the
       regression, and this catches it. */
    const { container } = render(<TrendingModule trending={EMPTY_TRENDING} />);
    expect(screen.queryByTestId("trending-module")).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });

  it("hardcodes no list of its own — every heading comes from the payload", () => {
    render(<TrendingModule trending={trending([RANKED])} />);
    expect(screen.queryByText("Most invested in")).toBeNull();
  });
});
