import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppearancesStrip } from "@/components/instrument/appearances-strip";
import { fetchAppearances } from "@/lib/instrument/appearances";
import type * as AppearancesModule from "@/lib/instrument/appearances";

vi.mock("@/lib/instrument/appearances", async (importOriginal) => {
  const actual =
    await importOriginal<typeof AppearancesModule>();
  return { ...actual, fetchAppearances: vi.fn() };
});
vi.mock("server-only", () => ({}));
vi.mock("@/lib/auth", () => ({ auth: () => Promise.resolve(null) }));

/**
 * Maulik, 14 Sep 2026: "when I open any stock if it is in any screens or strategy came in results
 * or scan do mention that". Stored results only; an empty answer still says what was checked.
 */
describe("where this stock appears", () => {
  it("names each screen with its rank and each scan with its state, linked to its page", async () => {
    vi.mocked(fetchAppearances).mockResolvedValue({
      symbol: "SUNDRMFAST",
      appearances: [
        {
          kind: "screen",
          name: "momentum accer",
          ref: "abc123",
          as_of: "2026-09-11",
          rank: 3,
          of: 57,
          definition_changed: true,
        },
        {
          kind: "swing",
          name: "Swing",
          ref: "/swing",
          as_of: "2026-09-11",
          detail: "VCP · WATCH · score 72.5",
          definition_changed: false,
        },
      ],
      screens_checked: 10,
      screens_never_run: ["Trend Stack"],
      strategies_checked: ["Swing", "Volume breakout", "Three weeks tight"],
    });
    render(await AppearancesStrip({ symbol: "SUNDRMFAST" }));

    const strip = screen.getByTestId("instrument-appearances");
    expect(strip).toHaveTextContent("SUNDRMFAST appears in 2 results");
    expect(strip).toHaveTextContent(
      "checked 10 screens, Swing, Volume breakout, Three weeks tight",
    );
    const links = screen.getAllByRole("link");
    expect(links[0]).toHaveTextContent("momentum accer · #3 of 57");
    expect(links[0]).toHaveAttribute("href", "/build/abc123");
    expect(links[0]?.getAttribute("title")).toContain("edited after this run");
    expect(links[1]).toHaveTextContent("Swing · VCP · WATCH · score 72.5");
    expect(links[1]).toHaveAttribute("href", "/swing");
    expect(strip).toHaveTextContent("No stored result yet for Trend Stack");
  });

  it("says plainly when the stock is in none of them", async () => {
    vi.mocked(fetchAppearances).mockResolvedValue({
      symbol: "SUNDRMFAST",
      appearances: [],
      screens_checked: 4,
      screens_never_run: [],
      strategies_checked: [],
    });
    render(await AppearancesStrip({ symbol: "SUNDRMFAST" }));
    expect(screen.getByTestId("instrument-appearances")).toHaveTextContent(
      "SUNDRMFAST is not in any of your screens or strategy scans",
    );
  });
});
