import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DiscoverBasketCard } from "@/components/discover/basket-card";
import { ReturnBasis } from "@/components/discover/return-basis";
import { SelectionProvider } from "@/components/discover/selection-provider";
import type { ExploreBasketCard, ExploreMetrics } from "@/lib/explore/fetch";

/**
 * Disclosure has to be where the number is.
 *
 * The catalogue used to put the return convention and the standing disclaimer after every card,
 * at the foot of the page. A reader who scanned six returns and left met neither. House rule 9
 * says disclaimers are components rather than footers; this suite is that rule applied to the
 * one sentence that changes what a return *means*.
 */

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
    categories: ["momentum"],
    rebalance_frequency: "QUARTERLY",
    source: "SCAN",
    description_md: null,
    launched_at: "2024-01-01",
    manager: { slug: "baskfy-engine", name: "Baskfy Engine", kind: "ENGINE" },
    metrics: metrics(),
    ...over,
  };
}

describe("the return convention travels with the return", () => {
  it("is on the card, not only at the foot of the page", () => {
    render(
      <SelectionProvider>
        <DiscoverBasketCard basket={card()} />
      </SelectionProvider>,
    );
    const basis = screen.getByTestId("return-basis");
    expect(basis).toBeInTheDocument();
    expect(within(basis).getByText(/dividends not included/i)).toBeInTheDocument();
  });

  it("says the convention up front and the consequence on opening", () => {
    render(<ReturnBasis metrics={metrics()} />);
    expect(screen.getByText("Price only — dividends not included")).toBeInTheDocument();
    expect(screen.getByText(/total-return series would be higher/)).toBeInTheDocument();
  });

  it("says so differently when dividends are included", () => {
    render(<ReturnBasis metrics={metrics({ dividends_included: true })} />);
    expect(screen.getByTestId("return-basis")).toHaveAttribute("data-dividends", "true");
    expect(screen.getByText("Includes dividends")).toBeInTheDocument();
  });

  it("dates the figures, so a stale number cannot pass for a fresh one", () => {
    render(<ReturnBasis metrics={metrics()} />);
    expect(screen.getByText(/Figures as of 2026-08-22/)).toBeInTheDocument();
  });

  it("renders nothing rather than an empty disclosure when there are no metrics", () => {
    const { container } = render(<ReturnBasis metrics={null} />);
    expect(container.innerHTML).toBe("");
  });

  it("is keyboard reachable and announced, being a native disclosure", () => {
    render(<ReturnBasis metrics={metrics()} />);
    expect(screen.getByRole("group")).toBeInTheDocument();
  });
});

const DISCOVER_PAGES = join(__dirname, "..", "..", "..", "app", "(app)", "discover");
const DISCOVER_COMPONENTS = join(__dirname, "..");

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

const SOURCES = [...filesUnder(DISCOVER_PAGES), ...filesUnder(DISCOVER_COMPONENTS)].filter(
  (path) => (path.endsWith(".ts") || path.endsWith(".tsx")) && !path.includes("__tests__"),
);

describe("the Discover surfaces as a whole", () => {
  it("opens enough files to be a real sweep", () => {
    // Declared, per the anti-laziness rule: a sweep that silently found nothing is not a sweep.
    expect(SOURCES.length).toBeGreaterThanOrEqual(10);
  });

  it("keeps every page carrying the standing disclosure block", () => {
    const pages = SOURCES.filter((path) => path.endsWith(`${"page"}.tsx`));
    expect(pages.length).toBeGreaterThanOrEqual(4);
    for (const page of pages) {
      const text = readFileSync(page, "utf8");
      // `plan` hands off to the desk console and carries its own; every other page shows
      // performance and owes the reader the block.
      if (page.includes("/plan/")) continue;
      expect(text, `${page} carries a disclosure`).toMatch(/DisclosureBlock|DisclosureNote/);
    }
  });

  it("never reaches an order path from any Discover surface", () => {
    for (const path of SOURCES) {
      const text = readFileSync(path, "utf8");
      for (const banned of ["place_order", "confirm=true", "OrderGateway"]) {
        expect(text, `${path} mentions ${banned}`).not.toContain(banned);
      }
    }
  });

  it("uses no advice language anywhere on a Discover surface", () => {
    const banned = /best for you|recommended for you|we recommend|suitable for you/i;
    for (const path of SOURCES) {
      expect(banned.test(readFileSync(path, "utf8")), `${path} advises`).toBe(false);
    }
  });
});
