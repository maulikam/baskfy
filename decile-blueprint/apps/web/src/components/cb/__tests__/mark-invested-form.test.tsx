import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MarkInvestedForm } from "@/components/cb/mark-invested-form";
import { parseHoldingLines } from "@/lib/investments/mark";
import type * as MarkModule from "@/lib/investments/mark";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

vi.mock("@/lib/investments/mark", async () => {
  const actual = await vi.importActual<typeof MarkModule>(
    "@/lib/investments/mark",
  );
  return {
    ...actual,
    markInvested: vi.fn(),
  };
});

afterEach(() => {
  cleanup();
});

describe("MarkInvestedForm", () => {
  it("renders the mark-as-invested contract, not an order route", () => {
    render(<MarkInvestedForm basketSlug="momentum-scan" basketName="Momentum" />);
    expect(screen.getByTestId("mark-invested-form")).toBeTruthy();
    expect(screen.getByTestId("mark-invested-submit").textContent).toMatch(/Mark as invested/i);
    expect(screen.getByText(/cannot place/i)).toBeTruthy();
  });

  it("refuses submit until the broker-action box is ticked", async () => {
    const user = userEvent.setup();
    render(<MarkInvestedForm basketSlug="momentum-scan" basketName="Momentum" />);
    await user.type(screen.getByLabelText(/Amount invested/i), "10000");
    await user.type(screen.getByTestId("mark-holdings"), "CUPID 10 80");
    await user.click(screen.getByTestId("mark-invested-submit"));
    expect(screen.getByRole("alert").textContent).toMatch(/already placed/i);
  });
});

describe("parseHoldingLines", () => {
  it("reads SYMBOL QTY AVG_PRICE lines", () => {
    expect(parseHoldingLines("CUPID 10 81.8\nINFY 2 1500")).toEqual([
      { symbol: "CUPID", qty: 10, avg_price: 81.8 },
      { symbol: "INFY", qty: 2, avg_price: 1500 },
    ]);
  });
});
