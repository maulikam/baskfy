import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { OverlapMatrix } from "@/components/overlap/overlap-matrix";
import { membershipOf } from "@/lib/overlap/overlap";

const membership = membershipOf([
  { key: "vbt", label: "Volume breakout", symbols: ["RELIANCE", "TCS"] },
  { key: "twt", label: "Three weeks tight", symbols: ["TCS", "INFY"] },
  { key: "swing", label: "Swing", symbols: ["INFY"] },
  { key: "exmpl0000001", label: "Investing 001", symbols: ["TCS", "WIPRO"] },
]);

describe("OverlapMatrix", () => {
  it("defaults to names on two or more sources, and includes the screen as a column", () => {
    render(<OverlapMatrix membership={membership} screenKeys={["exmpl0000001"]} />);

    const table = screen.getByTestId("overlap-matrix-table");
    expect(within(table).getByRole("columnheader", { name: /Volume/ })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: /Tight/ })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: /Swing/ })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: /Investing 001/ })).toBeInTheDocument();

    const symbols = screen
      .getAllByTestId("overlap-matrix-row")
      .map((row) => row.getAttribute("data-symbol"));
    expect(symbols).toEqual(["TCS", "INFY"]);
    expect(symbols).not.toContain("WIPRO");
    expect(symbols).not.toContain("RELIANCE");
  });

  it("shows screen-only names when asked for the screen view", async () => {
    const user = userEvent.setup();
    render(<OverlapMatrix membership={membership} screenKeys={["exmpl0000001"]} />);

    await user.click(screen.getByTestId("overlap-matrix-view-screen"));

    const symbols = screen
      .getAllByTestId("overlap-matrix-row")
      .map((row) => row.getAttribute("data-symbol"));
    expect(symbols).toEqual(["TCS", "WIPRO"]);
  });

  it("omits an unread source from the columns rather than marking every row empty", () => {
    const unread = membershipOf([
      { key: "vbt", label: "Volume breakout", symbols: ["TCS"] },
      { key: "twt", label: "Three weeks tight", symbols: null },
      { key: "swing", label: "Swing", symbols: ["TCS"] },
    ]);
    render(<OverlapMatrix membership={unread} screenKeys={[]} />);

    expect(screen.getByTestId("overlap-matrix-summary").textContent).toMatch(/could not be read/);
    expect(screen.queryByRole("columnheader", { name: /Tight/ })).toBeNull();
    expect(screen.getByTestId("overlap-matrix-row")).toHaveAttribute("data-symbol", "TCS");
  });
});
