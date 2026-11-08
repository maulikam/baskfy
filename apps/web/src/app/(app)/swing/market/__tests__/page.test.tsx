import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SwingMarketDay } from "@/lib/swing/fetch";

import SwingMarketPage from "../page";

/**
 * SW14 — `05` §2's Market tab, completed: the gate as a colour band over time, the rung and the
 * drawdown per session with the lock-out shaded, the parabolic count, the index with its 10-
 * and 20-day averages, and the "what would change the gate" line carrying the index word.
 */

vi.mock("@/lib/swing/fetch", () => ({
  fetchMarket: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/swing/market",
}));

const { fetchMarket } = await import("@/lib/swing/fetch");

function day(overrides: Partial<SwingMarketDay> = {}): SwingMarketDay {
  return {
    date: "2026-09-02",
    constituent_count: 41,
    pct_up_strong_1m: 3.8,
    pct_new_52w_high: 1.2,
    pct_above_ma_slow: 55,
    index_slug: "nifty-500",
    index_close: 20240,
    index_ma_fast: 20200,
    index_ma_slow: 20150,
    gate: "AMBER",
    exposure_level: 0,
    max_open_positions: 2,
    max_exposure_pct: 25,
    new_entries_allowed: true,
    parabolic_count: 3,
    drawdown_pct: 4.2,
    drawdown_locked: false,
    ...overrides,
  };
}

async function renderWith(days: SwingMarketDay[] | null) {
  vi.mocked(fetchMarket).mockResolvedValue(days === null ? null : { data: days });
  return render(await SwingMarketPage());
}

describe("the market page shows the series the gate is made from", () => {
  it("draws one band cell per session, coloured by the gate, with the lock-out marked", async () => {
    await renderWith([
      day({ date: "2026-08-31", gate: "GREEN" }),
      day({ date: "2026-09-01", gate: "RED", drawdown_locked: true, drawdown_pct: 15.3 }),
      day(),
    ]);
    const band = screen.getByTestId("gate-band");
    const cells = [...band.querySelectorAll("li")];
    expect(cells.map((cell) => cell.getAttribute("data-gate"))).toEqual(["GREEN", "RED", "AMBER"]);
    expect(cells[1]).toHaveAttribute("data-locked", "true");
    expect(cells[0]).not.toHaveAttribute("data-locked");
  });

  it("lists the rung, the drawdown, the parabolic count and the index averages per session", async () => {
    await renderWith([day({ exposure_level: 1, parabolic_count: 7 })]);
    const rows = screen.getAllByRole("row");
    const session = rows.find((row) => row.textContent?.includes("2 of 4"));
    expect(session).toBeDefined();
    expect(session).toHaveTextContent("4.20%");
    expect(session).toHaveTextContent("7");
    expect(session).toHaveTextContent("20240.00");
    expect(session).toHaveTextContent("20200.00");
    expect(session).toHaveTextContent("20150.00");
    expect(session).not.toHaveAttribute("data-locked");
  });

  it("shades a locked-out session and says so in the header instead of the rung", async () => {
    await renderWith([day({ drawdown_locked: true, drawdown_pct: 15.3, new_entries_allowed: false })]);
    const rows = screen.getAllByRole("row");
    const session = rows.find((row) => row.getAttribute("data-locked") === "true");
    expect(session).toBeDefined();
    expect(session).toHaveTextContent("locked out");
    expect(
      screen.getByText(/Locked out · allocation 15.3% below its peak · resumes inside 10%/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Rung 1 of 4/)).toBeNull();
  });

  it("says what would change the gate, with the two numbers and the index word", async () => {
    await renderWith([day()]);
    const line = screen.getByLabelText("What would change the gate");
    expect(line).toHaveTextContent("GREEN needs at least 5.0% of liquid names up 25% in a month");
    expect(line).toHaveTextContent("today 3.8%, 10-day above 20-day");
    expect(screen.getByTestId("gate-detail")).toHaveTextContent(
      "AMBER because 3.8% of names up 25% in a month, 1.2% at a year high · 10-day above 20-day",
    );
  });

  it("says no index when the averages are missing, and nothing at all before the first row", async () => {
    await renderWith([day({ index_ma_fast: null, index_ma_slow: null })]);
    expect(screen.getByTestId("gate-detail")).toHaveTextContent("no index");
    await renderWith(null);
    expect(screen.getByText(/No market row has been written yet/)).toBeInTheDocument();
  });

  it("renders no form and no button — the page cannot do anything", async () => {
    await renderWith([day()]);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(document.querySelector("form")).toBeNull();
  });
});
