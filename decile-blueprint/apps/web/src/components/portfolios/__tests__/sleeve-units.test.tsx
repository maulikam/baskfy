import type { SleeveAllocationOut } from "@baskfy/api-client";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SleeveAllocationCard } from "@/components/portfolios/sleeve-allocation-card";

/**
 * A sleeve shows what the amount buys, and shows a blank with a reason when it cannot.
 *
 * The failure being guarded against is a silent `0` in the unit column for a name the allocator
 * could not price: "you buy none of this" and "nobody knows what this costs" are different
 * statements, and only one of them is true.
 */

function sleeve(partial: Partial<SleeveAllocationOut> = {}): SleeveAllocationOut {
  return {
    capital: "500000",
    cash: "0",
    deployed: "500000",
    kind: "screen",
    name: "Momentum",
    screen_name: "12-month momentum",
    rows: [],
    unpriced: [],
    ...partial,
  };
}

const PRICED = sleeve({
  rows: [
    { symbol: "CUPID", amount: "250000", weight_pct: "50.00", price: "284.56", units: 878 },
    { symbol: "HFCL", amount: "250000", weight_pct: "50.00", price: "89.10", units: 2805 },
  ],
});

describe("a sleeve puts a unit count beside every rupee amount", () => {
  it("shows the amount, the price and the whole shares it buys", () => {
    render(<SleeveAllocationCard sleeve={PRICED} />);
    const rows = screen.getAllByTestId("allocation-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("₹2,50,000");
    expect(rows[0]).toHaveTextContent("₹284.56");
    expect(rows[0]).toHaveTextContent("878");
    expect(screen.getByRole("columnheader", { name: "Units" })).toBeInTheDocument();
  });

  it("marks no row as unpriced when every price was known", () => {
    render(<SleeveAllocationCard sleeve={PRICED} />);
    for (const cell of screen.getAllByTestId("units-cell")) {
      expect(cell).toHaveAttribute("data-unpriced", "false");
    }
    expect(screen.queryByTestId("sleeve-unpriced-note")).not.toBeInTheDocument();
  });
});

describe("an unpriced row shows a blank with its reason, never a zero unit count", () => {
  const partlyPriced = sleeve({
    unpriced: ["IDEA"],
    units_note: "No close for IDEA on 2026-08-18, so it has an amount and no unit count.",
    rows: [
      { symbol: "CUPID", amount: "250000", weight_pct: "50.00", price: "284.56", units: 878 },
      { symbol: "IDEA", amount: "250000", weight_pct: "50.00", price: null, units: null },
    ],
  });

  it("renders an em dash rather than a zero for the unpriced name", () => {
    render(<SleeveAllocationCard sleeve={partlyPriced} />);
    const cells = screen.getAllByTestId("units-cell");
    const idea = cells.find((cell) => cell.getAttribute("data-symbol") === "IDEA");
    expect(idea).toBeDefined();
    expect(idea).toHaveTextContent("—");
    expect(idea).not.toHaveTextContent("0");
    expect(idea).toHaveAttribute("data-unpriced", "true");
  });

  it("keeps the rupee amount, because the amount is still known", () => {
    render(<SleeveAllocationCard sleeve={partlyPriced} />);
    const rows = screen.getAllByTestId("allocation-row");
    const idea = rows.find((row) => within(row).queryByText("IDEA") !== null);
    expect(idea).toHaveTextContent("₹2,50,000");
  });

  it("attaches the server's reason to the cell and repeats it under the sleeve", () => {
    render(<SleeveAllocationCard sleeve={partlyPriced} />);
    const idea = screen
      .getAllByTestId("units-cell")
      .find((cell) => cell.getAttribute("data-symbol") === "IDEA");
    expect(idea).toHaveAttribute("title", partlyPriced.units_note);
    expect(screen.getByTestId("sleeve-unpriced-note")).toHaveTextContent(
      "No close for IDEA on 2026-08-18",
    );
  });

  it("counts the unpriced names on the card itself", () => {
    render(<SleeveAllocationCard sleeve={partlyPriced} />);
    expect(screen.getByTestId("sleeve-allocation")).toHaveAttribute("data-unpriced", "1");
  });
});

describe("a genuine zero unit count is not the same cell as a blank", () => {
  it("shows 0 with its own reason when the capital does not cover one share", () => {
    render(
      <SleeveAllocationCard
        sleeve={sleeve({
          rows: [{ symbol: "MRF", amount: "1000", weight_pct: "100.00", price: "125000", units: 0 }],
        })}
      />,
    );
    const cell = screen.getByTestId("units-cell");
    expect(cell).toHaveTextContent("0");
    expect(cell).toHaveAttribute("data-unpriced", "false");
    expect(cell).toHaveAttribute("title", "₹1,000.00 does not cover one share at ₹1,25,000.00.");
  });
});

describe("the sleeve card says where its names came from", () => {
  it("shows the screen name, or the basket name, or that the reader runs it", () => {
    render(<SleeveAllocationCard sleeve={PRICED} />);
    expect(screen.getByText("12-month momentum")).toBeInTheDocument();

    render(<SleeveAllocationCard sleeve={sleeve({ screen_name: null, basket_name: "Steady ten" })} />);
    expect(screen.getByText("Steady ten")).toBeInTheDocument();

    render(<SleeveAllocationCard sleeve={sleeve({ screen_name: null, kind: "manual" })} />);
    expect(screen.getByText("You run this one")).toBeInTheDocument();
  });

  it("shows the source note that tells an empty screen apart from a failed one", () => {
    render(
      <SleeveAllocationCard
        sleeve={sleeve({ source_note: "The screen matched nothing on 2026-08-18." })}
      />,
    );
    expect(screen.getByText("The screen matched nothing on 2026-08-18.")).toBeInTheDocument();
  });
});
