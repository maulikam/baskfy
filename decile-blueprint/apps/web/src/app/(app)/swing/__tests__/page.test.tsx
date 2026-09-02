import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SwingSetup, SwingSetups } from "@/lib/swing/fetch";

import SwingSetupsPage from "../page";

/**
 * SW11B, the setups page half — `docs/swing/STANDING-ANSWERS.md` A3 and `05` §2:
 *
 * - a candidate with a catalyst renders the headline as a **link** to the exchange's copy,
 *   opening in a new tab with `rel="noopener"`, and never any filing text;
 * - the earnings badge appears when the calendar names a result meeting and not otherwise;
 * - a candidate the feed has nothing for renders no link and no badge — an empty cell, not a
 *   broken one.
 */

vi.mock("@/lib/swing/fetch", () => ({
  fetchSetups: vi.fn(),
  fetchSectors: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/swing",
}));

const { fetchSetups, fetchSectors } = await import("@/lib/swing/fetch");

const FILING =
  "https://nsearchives.nseindia.com/corporate/FLAGCO_02092026084105_PR.pdf?x=1&y=2";

function setup(overrides: Partial<SwingSetup> = {}): SwingSetup {
  return {
    instrument_id: 1,
    symbol: "FLAGCO",
    name: "FLAGCO LIMITED",
    setup: "FLAG",
    status: "SETTING_UP",
    score: 62,
    close: 145.1,
    trigger: 149.6,
    stop_ref: 141.86,
    pivot_high: 149.6,
    stop_distance_pct: 5.17,
    adr_pct: 5.6,
    prior_move_pct: 42,
    base_depth_pct: 12,
    tightness_adr: 0.8,
    dryup_ratio: 0.6,
    rvol: null,
    gap_pct: null,
    turnover_avg: 120000000,
    base_bars: 14,
    up_streak: null,
    locked_upper_circuit: false,
    sector_slug: "nifty-it",
    listed_within_2y: false,
    catalyst_feed: null,
    ...overrides,
  };
}

function page(rows: SwingSetup[]): SwingSetups {
  return {
    as_of: "2026-09-02",
    gate: "GREEN",
    exposure_level: 2,
    max_open_positions: 6,
    max_exposure_pct: 75,
    new_entries_allowed: true,
    funnel: { instruments: 2500, liquid: 41 },
    data: rows,
  };
}

async function renderWith(rows: SwingSetup[]) {
  vi.mocked(fetchSetups).mockResolvedValue(page(rows));
  vi.mocked(fetchSectors).mockResolvedValue({ as_of: "2026-09-02", data: [] });
  return render(await SwingSetupsPage());
}

describe("the setups page links out to the catalyst and never reproduces it", () => {
  it("renders the headline as a new-tab link to the exchange's filing", async () => {
    await renderWith([
      setup({
        catalyst_feed: {
          headline: "Press Release - FLAGCO wins a multi-year order",
          published_at: "2026-09-02T08:41:05+05:30",
          url: FILING,
          earnings_date: null,
        },
      }),
    ]);
    const link = screen.getByRole("link", {
      name: "Press Release - FLAGCO wins a multi-year order",
    });
    expect(link).toHaveAttribute("href", FILING);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.queryByText(/earnings/i)).toBeNull();
  });

  it("shows the earnings badge when the calendar names a result meeting", async () => {
    await renderWith([
      setup({
        catalyst_feed: {
          headline: null,
          published_at: null,
          url: null,
          earnings_date: "2026-10-15",
        },
      }),
    ]);
    expect(screen.getByText(/earnings 15 Oct 2026/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /filing/i })).toBeNull();
  });

  it("renders an empty catalyst cell for a name the feed has nothing for", async () => {
    await renderWith([setup({ catalyst_feed: null })]);
    expect(screen.getByText("FLAGCO")).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "Catalyst" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/nseindia/)).toBeNull();
    expect(screen.queryByText(/earnings/i)).toBeNull();
  });
});
