import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LiveMarksProvider, LivePrice, LiveStatus } from "@/components/screens/live-price";
import { formatTradeDate } from "@/lib/format";
import type * as LiveMarksModule from "@/lib/screens/live-marks";
import { EMPTY_LIVE_MARKS, type LiveMarks } from "@/lib/screens/live-marks";

/**
 * The screens' live overlay (21 Sep 2026): while live, a row's price is Kite's last price with
 * today's change and the header says so; otherwise the row keeps the close it already had and
 * the header names the close's date and the reason. The ranks' date never moves.
 */

let current: LiveMarks = EMPTY_LIVE_MARKS;

vi.mock("@/lib/screens/live-marks", async (importOriginal) => {
  const actual = await importOriginal<typeof LiveMarksModule>();
  return { ...actual, useLiveMarks: () => current };
});

afterEach(() => {
  cleanup();
  current = EMPTY_LIVE_MARKS;
});

function renderRow(close: number | null) {
  return render(
    <LiveMarksProvider symbols={["RELIANCE"]}>
      <LiveStatus asOf="2026-09-18" />
      <LivePrice symbol="reliance" close={close} />
    </LiveMarksProvider>,
  );
}

const DAY = formatTradeDate("2026-09-18");

describe("LivePrice and LiveStatus", () => {
  it("shows the live price and today's change while live", () => {
    current = {
      ...EMPTY_LIVE_MARKS,
      liveOverlay: true,
      marketOpen: true,
      marks: { RELIANCE: 1520.4 },
      quotes: { RELIANCE: { lastPrice: 1520.4, prevClose: 1500, changePct: 1.36 } },
    };
    renderRow(1500);
    expect(screen.getByTestId("live-price")).toHaveTextContent("1520.40");
    expect(screen.getByTestId("live-change")).toHaveTextContent("+1.36% today");
    expect(screen.queryByTestId("close-price")).toBeNull();
    const status = screen.getByTestId("live-status");
    expect(status).toHaveAttribute("data-live", "true");
    expect(status).toHaveTextContent("Live prices");
    expect(status).toHaveTextContent(`ranks as of ${DAY}`);
  });

  it("keeps the close and says why when the market is closed", () => {
    current = { ...EMPTY_LIVE_MARKS, reason: "market_closed" };
    renderRow(1500);
    expect(screen.getByTestId("close-price")).toHaveTextContent("1500.00");
    expect(screen.queryByTestId("live-price")).toBeNull();
    expect(screen.queryByTestId("live-change")).toBeNull();
    const status = screen.getByTestId("live-status");
    expect(status).toHaveAttribute("data-live", "false");
    expect(status).toHaveTextContent(`Close as of ${DAY} · market closed`);
  });

  it("keeps the close without a Kite session, and names that", () => {
    current = { ...EMPTY_LIVE_MARKS, reason: "no_session", marketOpen: true };
    renderRow(1500);
    expect(screen.getByTestId("close-price")).toHaveTextContent("1500.00");
    expect(screen.getByTestId("live-status")).toHaveTextContent("no Kite session");
  });

  it("renders a page's own fallback when not live", () => {
    render(
      <LiveMarksProvider symbols={["TCS"]}>
        <LivePrice symbol="TCS" fallback={<span data-testid="own">3,000.00</span>} />
      </LiveMarksProvider>,
    );
    expect(screen.getByTestId("own")).toHaveTextContent("3,000.00");
  });
});
