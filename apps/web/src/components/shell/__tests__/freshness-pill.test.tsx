import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FreshnessPill } from "@/components/shell/freshness-pill";
import { TooltipProvider } from "@/components/ui/tooltip";

/**
 * "On every page, it shows yesterday's date" — Maulik, 9 Sep 2026, the third time in a week.
 *
 * The pill was RIGHT and read wrong. `as_of` is the last COMPLETED session, so during an open
 * market it is necessarily yesterday: today's daily bar does not exist until today ends. What
 * it failed to say is that the product is not therefore a day stale — the portfolio's marks and
 * the swing book's setups are live from Kite while the pill shows yesterday.
 */
const status = vi.hoisted(() => ({ market_open: true, session_day: true }));
vi.mock("@/lib/api/browser", () => ({
  browserApi: () => ({
    GET: () =>
      Promise.resolve({
        data: {
          as_of: "2026-09-08",
          data_version: 15,
          degraded: false,
          pipeline_running: false,
          market_open: status.market_open,
          session_day: status.session_day,
        },
      }),
  }),
}));

function renderPill() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <FreshnessPill />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  status.market_open = true;
  status.session_day = true;
  vi.useRealTimers();
});

describe("the freshness pill during an open market", () => {
  it("says the market is open beside yesterday's session date", async () => {
    const { container } = renderPill();
    expect(await screen.findByTestId("market-open")).toBeInTheDocument();
    // The pill splits its text across spans, so read the whole thing.
    expect(container.textContent).toMatch(/8 Sept 2026/);
    expect(container.textContent).toMatch(/market open/);
  });

  it("still shows the last completed session, never today", async () => {
    // The point that keeps being relitigated: claiming today would be claiming a complete day
    // that does not exist yet. The pill must not invent one.
    const { container } = renderPill();
    await screen.findByTestId("market-open");
    expect(container.textContent).not.toMatch(/9 Sept 2026/);
  });

  it("drops the marker once the market has closed", async () => {
    status.market_open = false;
    const { container, findByText } = renderPill();
    await findByText(/Data:/);
    expect(container.textContent).toMatch(/8 Sept 2026/);
    expect(screen.queryByTestId("market-open")).not.toBeInTheDocument();
  });
});

/**
 * 14 Sep 2026, an NSE holiday: the pill read "Data: 11 Sept 2026 · market open" at midday because
 * it asked the browser's clock. The server's `market_open` reads the trading calendar; the pill
 * must believe it over the clock.
 */
describe("the freshness pill on a holiday", () => {
  it("does not say market open during clock session hours when the API says closed", async () => {
    // Monday 14 Sep 2026, 11:00 IST — inside 09:15–15:30 on a weekday, so the clock alone says open.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-14T05:30:00Z"));
    status.market_open = false;
    status.session_day = false;
    const { container, findByText } = renderPill();
    await findByText(/Data:/);
    expect(container.textContent).toMatch(/8 Sept 2026/);
    expect(screen.queryByTestId("market-open")).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/market open/);
  });
});
