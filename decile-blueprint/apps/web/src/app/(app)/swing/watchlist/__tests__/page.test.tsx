import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SwingWatchRow } from "@/lib/swing/fetch";

import SwingWatchlistPage from "../page";

/**
 * SW11B, the watchlist half — `docs/swing/STANDING-ANSWERS.md` A3 and `05` §2:
 *
 * - the typed (or auto-filled) `catalyst` text and the feed's **link** are two things on the
 *   same cell: the text stays, the headline links to the exchange's copy in a new tab;
 * - the earnings badge follows the row's own flag even when the feed has no announcement;
 * - nothing from a filing is rendered.
 */

vi.mock("@/lib/swing/fetch", () => ({
  fetchWatchlist: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/swing/watchlist",
}));

const { fetchWatchlist } = await import("@/lib/swing/fetch");

const FILING =
  "https://nsearchives.nseindia.com/corporate/FLAGCO_02092026084105_PR.pdf";

function row(overrides: Partial<SwingWatchRow> = {}): SwingWatchRow {
  return {
    id: 1,
    instrument_id: 1,
    symbol: "FLAGCO",
    name: "FLAGCO LIMITED",
    setup: "FLAG",
    source: "DETECTOR",
    added_on: "2026-09-01",
    expires_on: "2026-09-15",
    trigger: 149.6,
    stop_ref: 141.86,
    distance_to_trigger_pct: 3.01,
    last_close: 145.1,
    note: null,
    catalyst: null,
    state: "WATCHING",
    earnings_date: null,
    catalyst_feed: null,
    ...overrides,
  };
}

async function renderWith(rows: SwingWatchRow[]) {
  vi.mocked(fetchWatchlist).mockResolvedValue({ data: rows });
  return render(await SwingWatchlistPage());
}

describe("the watchlist links out to the catalyst beside the typed note", () => {
  it("keeps the typed text and links the feed's headline in a new tab", async () => {
    await renderWith([
      row({
        catalyst: "Q2 result, order book up",
        catalyst_feed: {
          headline: "Press Release - FLAGCO wins a multi-year order",
          published_at: "2026-09-02T08:41:05+05:30",
          url: FILING,
          earnings_date: null,
        },
      }),
    ]);
    expect(screen.getByText("Q2 result, order book up")).toBeInTheDocument();
    const link = screen.getByRole("link", {
      name: "Press Release - FLAGCO wins a multi-year order",
    });
    expect(link).toHaveAttribute("href", FILING);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("shows the earnings badge from the row's own flag when the feed has no announcement", async () => {
    await renderWith([
      row({ earnings_date: "2026-10-15", catalyst_feed: null }),
    ]);
    expect(screen.getByText(/earnings 15 Oct 2026/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /filing/i })).toBeNull();
  });

  it("renders an em dash and nothing else for a name with no catalyst at all", async () => {
    await renderWith([row()]);
    const cell = screen.getByText("FLAGCO").closest("tr");
    expect(cell).not.toBeNull();
    expect(cell?.textContent).toContain("—");
    expect(cell?.textContent).not.toMatch(/earnings/i);
    expect(cell?.querySelector("a")).toBeNull();
  });
});
