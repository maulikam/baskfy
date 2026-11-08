import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DiscoverBasketCard } from "@/components/discover/basket-card";
import { SelectionProvider } from "@/components/discover/selection-provider";
import { StrategyMark, familyOf } from "@/components/discover/strategy-mark";
import type { ExploreBasketCard, ExploreMetrics } from "@/lib/explore/fetch";

function metrics(over: Partial<ExploreMetrics> = {}): ExploreMetrics {
  return {
    as_of_date: "2026-08-22",
    min_amount: "684000.00",
    volatility_bucket: "MEDIUM",
    volatility_value: "0.1820000000",
    ret_1m: null,
    ret_6m: null,
    ret_1y: "53.30",
    cagr_3y: null,
    cagr_5y: null,
    since_inception_pct: null,
    headline_label: "1Y returns",
    headline_pct: "53.30",
    return_convention: "PRICE_RETURN",
    dividends_included: false,
    return_convention_note:
      "Price return: dividends are not included, so a total-return series would be higher.",
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
    rebalance_frequency: "MONTHLY",
    source: "SCAN",
    description_md: "Ranks liquid stocks using 12-month risk-adjusted momentum.",
    launched_at: "2026-08-01",
    manager: { slug: "baskfy-engine", name: "Baskfy Engine", kind: "ENGINE" },
    metrics: metrics(),
    ...over,
  };
}

function renderCard(props: Partial<Parameters<typeof DiscoverBasketCard>[0]> = {}) {
  return render(
    <SelectionProvider>
      <DiscoverBasketCard basket={card()} {...props} />
    </SelectionProvider>,
  );
}

describe("question 1 — what does it do", () => {
  it("shows a drawn strategy mark instead of two initials", () => {
    renderCard();
    const mark = screen.getByTestId("strategy-mark");
    expect(mark).toBeInTheDocument();
    // "Liquid Momentum" used to render as "LM".
    expect(mark.textContent).not.toBe("LM");
  });

  it("picks the more specific tag when a basket carries several", () => {
    // Every basket in this catalogue is momentum, so the qualifier is what distinguishes them.
    expect(familyOf(["momentum", "liquidity"])).toBe("liquidity");
    expect(familyOf(["momentum", "low-volatility"])).toBe("low-volatility");
    expect(familyOf(["momentum"])).toBe("momentum");
    expect(familyOf([])).toBe("general");
  });

  it("gives the mark a readable meaning, not only a shape", () => {
    render(<StrategyMark categories={["low-volatility"]} />);
    expect(screen.getByRole("img").getAttribute("aria-label")).toMatch(/smaller swings/i);
  });

  it("says what the strategy does in words too", () => {
    renderCard();
    expect(screen.getByText(/12-month risk-adjusted momentum/)).toBeInTheDocument();
    expect(screen.getByText(/Monthly rebalance/)).toBeInTheDocument();
  });

  it("admits when no summary was written rather than showing an empty line", () => {
    render(
      <SelectionProvider>
        <DiscoverBasketCard basket={card({ description_md: null })} />
      </SelectionProvider>,
    );
    expect(screen.getByText(/No one-line summary has been written/)).toBeInTheDocument();
  });
});

describe("question 2 — how has it performed", () => {
  it("renders the return with its unit", () => {
    // The reported defect: 53.30 with no percent sign, because the value arrives as a string.
    renderCard();
    const stat = screen
      .getAllByTestId("metric-stat")
      .find((node) => node.dataset.metric === "headline")!;
    expect(within(stat).getByText("+53.30%")).toBeInTheDocument();
  });

  it("names the window the return is measured over", () => {
    renderCard();
    expect(screen.getByText("1Y returns")).toBeInTheDocument();
  });
});

describe("question 3 — what can go wrong", () => {
  it("puts risk before return in the reading order", () => {
    renderCard();
    const stats = screen.getAllByTestId("metric-stat").map((node) => node.dataset.metric);
    expect(stats).toEqual(["volatility", "headline", "min_amount"]);
  });

  it("labels the risk measure by name, never as Swing", () => {
    renderCard();
    expect(screen.getByText("Annualised volatility")).toBeInTheDocument();
    expect(screen.queryByText(/^Swing$/)).not.toBeInTheDocument();
  });

  it("carries the return convention at the number, not in a page footer", () => {
    renderCard();
    const basis = screen.getByTestId("return-basis");
    expect(basis).toHaveAttribute("data-dividends", "false");
    expect(within(basis).getByText(/dividends not included/i)).toBeInTheDocument();
    expect(within(basis).getByText(/total-return series would be higher/i)).toBeInTheDocument();
  });

  it("explains every figure it shows", () => {
    renderCard();
    for (const stat of screen.getAllByTestId("metric-stat")) {
      expect(within(stat).getByRole("group")).toBeInTheDocument();
    }
  });

  it("explains why a minimum is large, where the minimum is shown", () => {
    renderCard();
    expect(screen.getByText(/at least one share of every holding/i)).toBeInTheDocument();
  });
});

describe("question 4 — what can I do next", () => {
  it("offers a prominent primary action", () => {
    renderCard();
    const primary = screen.getByTestId("view-analysis");
    expect(primary).toHaveAttribute("href", "/basket/liquid-momentum");
    expect(primary.textContent).toBe("View analysis");
  });

  it("offers Save and Compare without opening the basket first", () => {
    renderCard();
    expect(screen.getByTestId("save-button")).toBeInTheDocument();
    expect(screen.getByTestId("compare-toggle")).toBeInTheDocument();
  });

  it("does not wrap the whole card in one link", () => {
    // The old card was a single anchor, which is why it had no room for an action and why every
    // click was a navigation.
    const { container } = renderCard();
    const article = container.querySelector('[data-testid="discover-basket-card"]')!;
    expect(article.tagName).toBe("ARTICLE");
    expect(article.closest("a")).toBeNull();
  });

  it("can be rendered without actions where they would not belong", () => {
    renderCard({ showActions: false });
    expect(screen.queryByTestId("save-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("view-analysis")).not.toBeInTheDocument();
  });
});

describe("the match note", () => {
  it("is shown when a filter produced this card", () => {
    renderCard({ matchNote: "Matches 3 of 4 preferences that could be checked: long-term growth." });
    expect(screen.getByTestId("match-note").textContent).toMatch(/Matches 3 of 4/);
  });

  it("is absent when the card was not produced by a filter", () => {
    renderCard();
    expect(screen.queryByTestId("match-note")).not.toBeInTheDocument();
  });
});

describe("thin data", () => {
  it("shows an em dash and a reason rather than a zero", () => {
    render(
      <SelectionProvider>
        <DiscoverBasketCard basket={card({ metrics: metrics({ headline_pct: null }) })} />
      </SelectionProvider>,
    );
    const stats = screen.getAllByTestId("metric-stat");
    const headline = stats.find((node) => node.dataset.metric === "headline")!;
    expect(headline).toHaveAttribute("data-available", "false");
    expect(within(headline).getByText("—")).toBeInTheDocument();
  });

  it("renders at all with no metrics whatsoever", () => {
    render(
      <SelectionProvider>
        <DiscoverBasketCard basket={card({ metrics: null })} />
      </SelectionProvider>,
    );
    expect(screen.getByTestId("discover-basket-card")).toBeInTheDocument();
    expect(screen.queryByTestId("return-basis")).not.toBeInTheDocument();
  });
});
