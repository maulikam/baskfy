/**
 * The illustrative allocation figure on the landing page.
 *
 * It is labelled "Illustrative — not a real position", which is what makes the made-up numbers
 * permissible. It does not make them free: a stacked bar whose segments do not fill their track,
 * or a list of amounts that do not add to the total the bar implies, is a picture of arithmetic
 * that does not work — on a page arguing that the arithmetic underneath is honest.
 *
 * So the only things asserted here are the ones a reader could check with a pencil, plus the
 * §9 claim the figure's paragraph carries.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ThreeWays } from "@/components/marketing/three-ways";

function figureText(): string {
  render(<ThreeWays />);
  return (document.body.textContent ?? "").replace(/\s+/g, " ");
}

describe("the allocation figure adds up", () => {
  it("fills the bar exactly, with one segment per row", () => {
    render(<ThreeWays />);
    const segments = Array.from(screen.getByTestId("allocation-bar").children);
    const widths = segments.map((segment) =>
      Number((segment as HTMLElement).style.width.replace("%", "")),
    );

    expect(widths.length).toBeGreaterThanOrEqual(4);
    // A bar that summed to 97 would leave a sliver of track showing and read as a rounding bug in
    // the product rather than in the figure.
    expect(widths.reduce((total, width) => total + width, 0)).toBe(100);
    for (const width of widths) expect(width).toBeGreaterThan(0);
  });

  it("shows one amount per segment, and they total the round number the bar implies", () => {
    const text = figureText();
    const amounts = [...text.matchAll(/₹([\d,]+)/g)].map((match) =>
      Number((match[1] ?? "").replace(/,/g, "")),
    );
    expect(amounts.length).toBeGreaterThanOrEqual(4);
    // ₹1,00,00,000 — a crore, chosen so the percentages are readable off the rupees.
    expect(amounts.reduce((total, amount) => total + amount, 0)).toBe(10_000_000);
  });

  it("titles itself with the number of rows it actually draws", () => {
    render(<ThreeWays />);
    const rows = screen.getByTestId("allocation-bar").children.length;
    const WORD = ["", "one", "two", "three", "four", "five", "six"][rows] ?? String(rows);
    // The heading said "three allocations" for a day after the fourth row landed, in a review
    // nobody caught. It is a count, so it is countable.
    expect(document.body.textContent ?? "").toContain(`One portfolio, ${WORD} allocations`);
  });
});

describe("the figure keeps the claims it is allowed to make", () => {
  it("labels itself illustrative, which is what licenses the numbers", () => {
    expect(figureText()).toMatch(/illustrative — not a real position/i);
  });

  it("says the slices the reader runs are reported and never allocated", () => {
    // `portfolio_sleeve.kind = manual`: capital the owner runs themselves, reported so the totals
    // are honest and never allocated. Two rows are manual now, so the sentence is plural.
    const text = figureText();
    expect(text).toMatch(/reported and never allocated/i);
    expect(text).toMatch(/its own capital/i);
  });

  it("covers the fundamentals-and-IPO slice without selling a screener that does not exist", () => {
    const text = figureText();
    expect(text).toMatch(/fundamental picks and ipos/i);
    // `factor_registry` exposes `marketcap_cr` and `pe` and nothing else fundamental. The figure
    // may say such names can be a slice; it may not call this a fundamentals screener.
    expect(text).not.toMatch(/fundamentals screener|fundamental screener/i);
  });
});
