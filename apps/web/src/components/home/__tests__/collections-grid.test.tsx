import { readFileSync } from "node:fs";
import { join } from "node:path";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { CollectionsGrid } from "@/components/home/collections-grid";
import type { Collection } from "@/lib/collections/fetch";

afterEach(cleanup);

function collection(overrides: Partial<Collection> = {}): Collection {
  return {
    slug: "park-your-savings",
    title: "Park your savings",
    subtitle: "Somewhere to leave cash that is not the current account.",
    basket_slugs: ["alpha"],
    baskets: [],
    position: 0,
    withheld: 0,
    ...overrides,
  };
}

describe("the collections grid", () => {
  it("renders one card per collection, from the payload", () => {
    render(
      <CollectionsGrid
        collections={[
          collection(),
          collection({ slug: "high-dividend", title: "High dividend", basket_slugs: [] }),
        ]}
      />,
    );
    expect(screen.getByTestId("collection-park-your-savings")).toBeTruthy();
    expect(screen.getByTestId("collection-high-dividend")).toBeTruthy();
  });

  it("links each card at the collection page the browse surface already uses", () => {
    render(<CollectionsGrid collections={[collection()]} />);
    expect(
      screen.getByTestId("collection-park-your-savings").getAttribute("href"),
    ).toBe("/discover/collections/park-your-savings");
  });

  it("counts the baskets a viewer may actually see", () => {
    render(
      <CollectionsGrid
        collections={[
          collection({
            baskets: [
              { slug: "a" },
              { slug: "b" },
            ] as unknown as Collection["baskets"],
          }),
        ]}
      />,
    );
    expect(screen.getByText("2 baskets")).toBeTruthy();
  });

  it("says an empty shelf is empty rather than rendering a bare card", () => {
    render(<CollectionsGrid collections={[collection({ baskets: [] })]} />);
    expect(screen.getByText("Nothing on this shelf yet")).toBeTruthy();
  });

  it("renders its empty state instead of vanishing when there are no collections", () => {
    render(<CollectionsGrid collections={[]} />);
    // A module that disappears when its data is empty is indistinguishable from a broken one.
    expect(screen.getByTestId("collections-empty")).toBeTruthy();
    expect(screen.getByTestId("collections-grid")).toBeTruthy();
  });

  it("hardcodes no collection slug — SC9's acceptance criterion, checked in the source", () => {
    const source = readFileSync(
      join(__dirname, "..", "collections-grid.tsx"),
      "utf8",
    );
    for (const slug of [
      "park-your-savings",
      "most-subscribed",
      "popular-etfs",
      "high-dividend",
    ]) {
      expect(source.includes(slug), `${slug} is hardcoded in the component`).toBe(false);
    }
  });
});
