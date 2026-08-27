import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CollectionDirectory } from "@/components/collections/collection-directory";
import { CollectionShelves } from "@/components/collections/collection-shelves";
import type { Collection } from "@/lib/collections/fetch";
import type { ExploreBasketCard } from "@/lib/explore/fetch";

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

function shelf(
  slug: string,
  title: string,
  slugs: string[],
  position: number,
): Collection {
  return {
    slug,
    title,
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
    shelf("start-here", "Start here", ["momentum-scan"], 10),
    shelf("momentum", "Momentum", ["momentum-scan"], 20),
    shelf("run-by-the-engine", "Run by the engine", ["momentum-scan"], 30),
    shelf("quarterly", "Rebalanced quarterly", [], 40),
  ];
}

/** The live shelves, read out of the dev database after the catalogue grew to seven baskets. */
function livePayload(): Collection[] {
  const engineRun = [
    "broad-market-sharpe",
    "liquid-momentum",
    "momentum-scan",
    "momentum-low-volatility",
    "quality-momentum",
    "six-month-sharpe",
    "trend-stack",
  ];
  return [
    shelf("start-here", "Start here", engineRun.slice(0, 6), 10),
    shelf("momentum", "Momentum", engineRun, 20),
    shelf("run-by-the-engine", "Run by the engine", engineRun, 30),
    shelf(
      "quarterly",
      "Rebalanced quarterly",
      ["liquid-momentum", "six-month-sharpe", "quality-momentum"],
      40,
    ),
  ];
}

function differentiatedPayload(): Collection[] {
  return [
    shelf("start-here", "Start here", ["cheap-one", "cheap-two"], 10),
    shelf("momentum", "Momentum", ["momentum-scan"], 20),
    shelf("run-by-the-engine", "Run by the engine", ["momentum-scan"], 30),
    shelf("quarterly", "Rebalanced quarterly", ["quarterly-value"], 40),
  ];
}

/** Every basket card links to `/basket/<slug>`; counting those counts rendered cards. */
function basketCardHrefs(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('a[href^="/basket/"]')).map(
    (node) => node.getAttribute("href") ?? "",
  );
}

describe("CollectionShelves on a page that already lists the catalogue", () => {
  it("renders no basket card at all when every shelf holds what the grid above already shows", () => {
    // The reported symptom: three shelves drew the same single card, one after another.
    const { container } = render(<CollectionShelves collections={todaysPayload()} />);
    expect(basketCardHrefs(container)).toEqual([]);
  });

  it("never renders one shelf twice under two names", () => {
    // The reported symptom, stated as a property. Two shelves may overlap — that is browsing —
    // but no two rendered shelves may hold the same set, because then one of them is furniture.
    for (const payload of [todaysPayload(), differentiatedPayload(), livePayload()]) {
      const { unmount } = render(<CollectionShelves collections={payload} />);
      const sets = screen
        .queryAllByTestId("collection-shelf")
        .map((node) =>
          Array.from(node.querySelectorAll('a[href^="/basket/"]'))
            .map((card) => card.getAttribute("href"))
            .sort()
            .join(","),
        );
      expect(new Set(sets).size, `two shelves rendered the same baskets: ${sets.join(" | ")}`).toBe(
        sets.length,
      );
      unmount();
    }
  });

  it("never lists the same basket twice inside one shelf", () => {
    // Counted as cards, not as anchors: a card carries two links to the same basket on purpose
    // (its title and its primary action), and counting anchors would read that as a duplicate.
    for (const payload of [todaysPayload(), differentiatedPayload(), livePayload()]) {
      const { unmount } = render(<CollectionShelves collections={payload} />);
      for (const node of screen.queryAllByTestId("collection-shelf")) {
        const slugs = Array.from(
          node.querySelectorAll('[data-testid="discover-basket-card"]'),
        ).map((card) => card.getAttribute("data-slug"));
        expect(new Set(slugs).size, `${node.dataset.slug} repeats a basket`).toBe(slugs.length);
      }
      unmount();
    }
  });

  it("falls back to the directory when the shelves cannot group anything", () => {
    render(<CollectionShelves collections={todaysPayload()} />);
    const section = screen.getByTestId("browse-collections");
    expect(section).toHaveAttribute("data-mode", "directory");
    expect(screen.getByTestId("collection-directory")).toBeInTheDocument();
    expect(screen.queryAllByTestId("collection-shelf")).toHaveLength(0);
  });

  it("says why the shelves are collapsed rather than silently dropping them", () => {
    render(<CollectionShelves collections={todaysPayload()} />);
    expect(screen.getByTestId("collections-thin-note").textContent).toMatch(
      /every shelf holds the same baskets/i,
    );
  });

  it("lists every collection even when it collapses — including the empty one", () => {
    render(<CollectionShelves collections={todaysPayload()} />);
    for (const slug of ["start-here", "momentum", "run-by-the-engine", "quarterly"]) {
      expect(screen.getByTestId(`collection-${slug}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId("collection-quarterly")).toHaveAttribute("data-empty", "true");
  });

  it("stacks the live catalogue's three real shelves and drops the duplicate one", () => {
    render(<CollectionShelves collections={livePayload()} />);
    expect(screen.getByTestId("browse-collections")).toHaveAttribute("data-mode", "shelves");
    expect(screen.queryAllByTestId("collection-shelf").map((n) => n.dataset.slug)).toEqual([
      "start-here",
      "momentum",
      "quarterly",
    ]);
  });

  it("stacks real shelves when the catalogue is deep enough for them to group", () => {
    render(<CollectionShelves collections={differentiatedPayload()} />);
    expect(screen.getByTestId("browse-collections")).toHaveAttribute("data-mode", "shelves");
    expect(screen.queryAllByTestId("collection-shelf").map((n) => n.dataset.slug)).toEqual([
      "start-here",
      "momentum",
      "quarterly",
    ]);
  });

  it("keeps a way through to every collection from the catalogue page", () => {
    render(<CollectionShelves collections={differentiatedPayload()} />);
    // `run-by-the-engine` is suppressed from the stack; the all-collections link is how it stays
    // one click away. Suppressing a repeat must never be the same thing as hiding a shelf.
    expect(screen.getByRole("link", { name: /all collections/i })).toHaveAttribute(
      "href",
      "/discover/collections",
    );
  });

  it("renders nothing when there are no collections, rather than an empty heading", () => {
    const { container } = render(<CollectionShelves collections={[]} />);
    expect(container.innerHTML).toBe("");
  });
});

describe("CollectionDirectory", () => {
  it("is complete — one tile per collection, empty shelves included", () => {
    render(<CollectionDirectory collections={todaysPayload()} />);
    expect(screen.getByTestId("collection-directory")).toHaveAttribute("data-count", "4");
    expect(screen.getByTestId("collection-quarterly").textContent).toMatch(
      /nothing on this shelf yet/i,
    );
  });

  it("renders doors, not rooms — no basket card appears in a directory", () => {
    const { container } = render(<CollectionDirectory collections={differentiatedPayload()} />);
    expect(basketCardHrefs(container)).toEqual([]);
  });

  it("points each tile at that collection's own page", () => {
    render(<CollectionDirectory collections={todaysPayload()} />);
    expect(screen.getByTestId("collection-momentum")).toHaveAttribute(
      "href",
      "/discover/collections/momentum",
    );
  });

  it("says there are no collections rather than vanishing", () => {
    render(<CollectionDirectory collections={[]} />);
    expect(screen.getByTestId("collection-directory-empty")).toBeInTheDocument();
  });

  it("never leaks the withheld count to a browsing user", () => {
    const withheld = todaysPayload().map((c) => ({ ...c, withheld: 7 }));
    const { container } = render(<CollectionDirectory collections={withheld} />);
    expect(container.textContent).not.toMatch(/withheld/i);
    expect(container.textContent).not.toMatch(/\b7\b/);
  });
});
