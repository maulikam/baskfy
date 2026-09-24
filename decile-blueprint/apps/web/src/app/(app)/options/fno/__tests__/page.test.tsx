import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { info } from "@/lib/fno/__tests__/fixtures";

import StockFnoPage from "../page";

/**
 * FO5's rendered-DOM acceptance for `/options/fno` (`docs/fno/05` §3, `04` §5): the banner
 * verbatim above the table, `As of close, <date>`, `04` §5's columns and nothing coloured as a
 * signal, the verdict table with its latest re-test and caveat, and the MISSING nights.
 */

vi.mock("@/lib/fno/fetch", () => ({ fetchFnoInfo: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/options/fno",
  useRouter: () => ({ refresh: vi.fn() }),
}));

const { fetchFnoInfo } = await import("@/lib/fno/fetch");

async function renderWith(view: typeof info | null) {
  vi.mocked(fetchFnoInfo).mockResolvedValue(view);
  render(await StockFnoPage());
}

describe("/options/fno", () => {
  it("leads with 04 §5's banner, linking the research", async () => {
    await renderWith(info);
    const banner = screen.getByTestId("fno-not-a-signal");
    expect(banner).toHaveTextContent(
      "None of these numbers predicted a profitable trade after costs in 2022–2026.",
    );
    expect(within(banner).getByRole("link")).toHaveAttribute(
      "href",
      "#families",
    );
    expect(screen.getByTestId("fno-clock")).toHaveTextContent(
      "As of close, Tue 22 Sep",
    );
  });

  it("has 04 §5's columns in its order, sorted by turnover, with no signal colour", async () => {
    await renderWith(info);
    const table = screen.getByTestId("fno-info-table");
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((th) => th.textContent);
    expect(headers).toEqual([
      "Symbol",
      "Lot",
      "Days to monthly",
      "Futures settle",
      "Basis (a.y.)",
      "OI chg (5 sessions)",
      "IV",
      "RV20",
      "IV ÷ RV20",
      "1-year IV percentile",
      "Ban",
    ]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.getAttribute("data-symbol"))).toEqual([
      "RELIANCE",
      "TCS",
    ]);
    expect(table.innerHTML).not.toMatch(
      /text-(positive|negative)|bg-(positive|negative)/,
    );
    expect(rows[0]).toHaveTextContent("1.23");
    expect(rows[1]).toHaveTextContent("In ban");
  });

  it("collapses on a phone to symbol, IV ÷ RV, OI change and ban", async () => {
    await renderWith(info);
    const phone = screen.getByTestId("fno-info-phone");
    const first = within(phone).getAllByRole("listitem")[0];
    expect(first?.querySelector("summary")).toHaveTextContent(
      /RELIANCE\s*1\.23\s*\+1\.64%/,
    );
  });

  it("shows the families tested and rejected with the latest re-test and its caveat", async () => {
    await renderWith(info);
    const families = screen.getByTestId("fno-families");
    expect(families).toHaveTextContent(
      "C1 Debit spreads on the breakout signal",
    );
    expect(within(families).getByTestId("fno-family-retest")).toHaveTextContent(
      "-0.210R",
    );
    expect(within(families).getByTestId("fno-tier-caveat")).toHaveTextContent(
      /End-of-day closes, not fills/,
    );
    const status = screen.getByTestId("fno-data-status");
    expect(status).toHaveTextContent("2 sessions over 1 underlyings");
    expect(status).toHaveTextContent(
      /Missing, never interpolated: 18 Sept? 2026/,
    );
  });

  it("says the service did not answer rather than rendering a blank page", async () => {
    await renderWith(null);
    expect(screen.getByText(/F&O service did not answer/)).toBeInTheDocument();
  });
});
