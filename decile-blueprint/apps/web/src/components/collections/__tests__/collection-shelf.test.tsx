import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CollectionShelf } from "@/components/collections/collection-shelf";
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
    rebalance_frequency: "QUARTERLY",
    source: "MANUAL",
    description_md: null,
    launched_at: null,
    manager: { slug: "baskfy-engine", name: "Baskfy Engine", kind: "ENGINE" },
    metrics: null,
  };
}

function shelf(over: Partial<Collection> = {}): Collection {
  return {
    slug: "momentum",
    title: "Momentum",
    subtitle: "Holds what is working.",
    basket_slugs: ["alpha-one"],
    baskets: [basket("alpha-one")],
    position: 20,
    withheld: 0,
    ...over,
  };
}

describe("CollectionShelf", () => {
  it("renders the shelf title and its reason", () => {
    render(<CollectionShelf collection={shelf()} />);
    expect(screen.getByText("Momentum")).toBeInTheDocument();
    expect(screen.getByText("Holds what is working.")).toBeInTheDocument();
  });

  it("renders one card per basket", () => {
    render(
      <CollectionShelf
        collection={shelf({
          basket_slugs: ["alpha-one", "beta-two"],
          baskets: [basket("alpha-one"), basket("beta-two")],
        })}
      />,
    );
    const section = screen.getByTestId("collection-shelf");
    expect(section).toHaveAttribute("data-count", "2");
    expect(screen.getByText("alpha one")).toBeInTheDocument();
    expect(screen.getByText("beta two")).toBeInTheDocument();
  });

  it("renders an empty shelf as an explicit statement, not as nothing", () => {
    // A shelf whose rule matches nothing is a true statement about the catalogue. Dropping it
    // would make "empty" indistinguishable from "broken", which is the failure this guards.
    render(<CollectionShelf collection={shelf({ basket_slugs: [], baskets: [] })} />);
    const section = screen.getByTestId("collection-shelf");
    expect(section).toHaveAttribute("data-empty", "true");
    expect(screen.getByTestId("collection-empty")).toBeInTheDocument();
    expect(screen.getByTestId("collection-empty").textContent).toMatch(/nothing is being hidden/i);
  });

  it("does not offer a see-all link for an empty shelf", () => {
    render(<CollectionShelf collection={shelf({ basket_slugs: [], baskets: [] })} />);
    expect(screen.queryByText(/see all/i)).not.toBeInTheDocument();
  });

  it("links the shelf to its own page", () => {
    render(<CollectionShelf collection={shelf()} />);
    const link = screen.getByRole("link", { name: "Momentum" });
    expect(link).toHaveAttribute("href", "/discover/collections/momentum");
  });

  it("omits the see-all link when the shelf is already the whole page", () => {
    render(<CollectionShelf collection={shelf()} showAll={false} />);
    expect(screen.queryByText(/see all/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Momentum" })).not.toBeInTheDocument();
  });

  it("renders a shelf with no subtitle without inventing one", () => {
    render(<CollectionShelf collection={shelf({ subtitle: null })} />);
    expect(screen.getByText("Momentum")).toBeInTheDocument();
    expect(screen.getByTestId("collection-shelf")).toHaveAttribute("data-count", "1");
  });

  it("never renders the withheld count to a browsing user", () => {
    // `withheld` exists so an operator can tell "empty" from "hidden". Showing a viewer that
    // baskets exist which they may not open would leak exactly what `visibility` protects.
    const { container } = render(
      <CollectionShelf collection={shelf({ basket_slugs: [], baskets: [], withheld: 3 })} />,
    );
    expect(container.textContent).not.toMatch(/withheld/i);
    expect(container.textContent).not.toMatch(/\b3\b/);
  });
});
