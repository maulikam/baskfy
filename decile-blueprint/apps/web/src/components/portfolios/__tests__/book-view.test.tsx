import { render, screen } from "@testing-library/react";
import type { Route } from "next";
import { describe, expect, it } from "vitest";

import { BookBoxCard } from "@/components/portfolios/book-box";
import { BookOverall } from "@/components/portfolios/book-overall";
import type { BookBox } from "@/lib/portfolios/book";

const managerBox: BookBox = {
  id: "inv-1",
  name: "A manager's basket",
  kind: "manager",
  capital: "3500000",
  currentValue: "3600000",
  returnsPct: "2.86",
  xirr: "11.2",
  holdingsCount: null,
  valueIsLiveMark: true,
  /* The real thing is built from a portfolio id at runtime, which is why `BookBox.href` is a
     `Route`; a fixture standing in for one makes the same assertion. */
  href: "/portfolio/1" as Route,
};

const ruleBox: BookBox = {
  id: "sleeve-9-1",
  name: "Momentum rule",
  kind: "rule",
  capital: "500000",
  currentValue: null,
  returnsPct: null,
  xirr: null,
  holdingsCount: null,
  valueIsLiveMark: false,
  href: "/portfolios/9/sleeves" as Route,
  portfolioId: 9,
};

describe("book boxes put their own stats on the card", () => {
  it("shows current value, return, XIRR and money put in for a live mark", () => {
    render(<BookBoxCard box={managerBox} />);
    expect(screen.getByTestId("book-box")).toHaveAttribute("data-kind", "manager");
    expect(screen.getByText("A manager's basket")).toBeInTheDocument();
    expect(screen.getByText("Manager")).toBeInTheDocument();
    expect(screen.getByText("Current value")).toBeInTheDocument();
    expect(screen.getByText("₹36,00,000")).toBeInTheDocument();
    expect(screen.getByText("Return")).toBeInTheDocument();
    expect(screen.getByText("+2.86%")).toBeInTheDocument();
    expect(screen.getByText("XIRR")).toBeInTheDocument();
    expect(screen.getByText("Put in")).toBeInTheDocument();
    expect(screen.getByText("₹35,00,000")).toBeInTheDocument();
  });

  it("shows assigned capital and not a live mark for a screen sleeve", () => {
    render(<BookBoxCard box={ruleBox} />);
    expect(screen.getByTestId("book-box")).toHaveAttribute("data-kind", "rule");
    expect(screen.getByText("My screen")).toBeInTheDocument();
    expect(screen.getByText("Assigned capital")).toBeInTheDocument();
    expect(screen.getByText("₹5,00,000")).toBeInTheDocument();
    expect(screen.getByText(/not a live mark/i)).toBeInTheDocument();
    expect(screen.queryByText("XIRR")).not.toBeInTheDocument();
  });
});

describe("the overall roll-up keeps two ledgers apart", () => {
  it("renders basket marks and sleeve capital as separate figures", () => {
    render(
      <BookOverall
        totals={{
          boxCount: 3,
          basketValue: "3600000.00",
          sleeveCapital: "500000.00",
          hasLiveMarks: true,
        }}
      />,
    );
    expect(screen.getByTestId("book-overall")).toBeInTheDocument();
    expect(screen.getByText("Current value in baskets you hold")).toBeInTheDocument();
    expect(screen.getByText("₹36,00,000")).toBeInTheDocument();
    expect(screen.getByText("Capital assigned across allocations")).toBeInTheDocument();
    expect(screen.getByText("₹5,00,000")).toBeInTheDocument();
    expect(screen.getByText(/3 portfolios/)).toBeInTheDocument();
    expect(screen.getByText(/not a combined figure/i)).toBeInTheDocument();
    expect(screen.getByText(/price returns/i)).toBeInTheDocument();
  });
});
