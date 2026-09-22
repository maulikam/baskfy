import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type * as LiveMarksModule from "@/lib/screens/live-marks";
import {
  acMorning,
  afterClose,
  collectorOff,
  emptyChain,
} from "@/lib/options/__tests__/fixtures";

import OptionsPage from "../page";

/**
 * `06` OP5's rendered-DOM acceptance for `/options`, from fixtures written to the API's shapes:
 * an O1 `WOULD_SKIP` with all its reasons, an O2 `ARMED` with its distance to trigger, an O3
 * candidate spread, the `As of close` label outside hours — and the state the box is in when OP5
 * ships, the collector off, rendered as a reason rather than a blank or an error.
 *
 * The Playwright spec (`e2e/options.spec.ts`) renders the page in a browser against the e2e
 * database, which carries no `op_scan` rows; the three sleeve states are asserted here, over a
 * mocked fetch, on the swing hub's precedent (DECISIONS-SW SW4.3; DECISIONS-OP OP5.8).
 */

vi.mock("@/lib/screens/live-marks", async (importOriginal) => {
  const actual = await importOriginal<typeof LiveMarksModule>();
  return { ...actual, useLiveMarks: () => actual.EMPTY_LIVE_MARKS };
});

vi.mock("@/lib/options/fetch", () => ({
  fetchOptionsToday: vi.fn(),
  fetchOptionsChain: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/options",
  useRouter: () => ({ refresh: vi.fn() }),
}));

const { fetchOptionsToday, fetchOptionsChain } =
  await import("@/lib/options/fetch");

async function renderWith(today: typeof acMorning | null) {
  vi.mocked(fetchOptionsToday).mockResolvedValue(today);
  vi.mocked(fetchOptionsChain).mockResolvedValue(emptyChain);
  render(await OptionsPage());
}

describe("/options with the collector off (the state OP5 ships in)", () => {
  it("says why there is nothing, carries the F&O caveat and the paper label", async () => {
    await renderWith(collectorOff);
    expect(screen.getByTestId("options-empty-reason")).toHaveTextContent(
      /options collector is off/,
    );
    expect(screen.getByTestId("options-fno-caveat")).toHaveTextContent(
      /91% lost money in FY25/,
    );
    expect(screen.getByTestId("options-scan-only")).toHaveTextContent(
      /Scan · paper only/,
    );
    expect(
      screen.getByText(/nothing on this page can place an order/i),
    ).toBeInTheDocument();
    // The calendar still shows, from the contract list, with the event day marked.
    expect(screen.getByTestId("options-expiries")).toHaveTextContent(
      /event day, no strategy trades/,
    );
    // No sleeve pretends it looked.
    expect(screen.getAllByTestId("options-sleeve-unscanned")).toHaveLength(5);
  });

  it("says the service did not answer rather than rendering a blank page", async () => {
    await renderWith(null);
    expect(
      screen.getByText(/options service did not answer/),
    ).toBeInTheDocument();
  });
});

describe("/options on the AC morning", () => {
  it("shows O1-W would skip with all three of its reasons", async () => {
    await renderWith(acMorning);
    const card = screen.getByTestId("options-sleeve-O1W");
    expect(within(card).getByTestId("options-state")).toHaveTextContent(
      "Would skip",
    );
    const reasons = within(card).getByTestId("options-reasons");
    expect(reasons).toHaveTextContent("The morning range is too wide");
    expect(reasons).toHaveTextContent("The price left the opening range");
    expect(reasons).toHaveTextContent("The morning is trending, not ranging");
  });

  it("shows O2 armed with the distance to its trigger, and the break it saw but did not trade", async () => {
    await renderWith(acMorning);
    const card = screen.getByTestId("options-sleeve-O2");
    expect(within(card).getByTestId("options-state")).toHaveTextContent(
      "Armed",
    );
    expect(within(card).getByTestId("options-trigger")).toHaveTextContent(
      "Trigger at 25,162.55 — 31.45 points away (0.13%).",
    );
    expect(
      within(card).getByTestId("options-seen-not-traded"),
    ).toHaveTextContent(/10:04/);
  });

  it("shows the O3 candidate spread, priced, as paper one lot", async () => {
    await renderWith(acMorning);
    const card = screen.getByTestId("options-sleeve-O3A");
    const candidate = within(card).getByTestId("options-candidate");
    expect(candidate).toHaveTextContent("Debit spread");
    expect(candidate).toHaveTextContent("Buy 25,100 CE");
    expect(candidate).toHaveTextContent("Sell 25,200 CE");
    expect(candidate).toHaveTextContent("₹2,036.25");
    expect(candidate).toHaveTextContent("paper · one lot");
  });

  it("stamps the page Live with the collector's minute", async () => {
    await renderWith(acMorning);
    expect(screen.getByTestId("options-clock")).toHaveTextContent(
      "Live · 13:14",
    );
  });
});

describe("/options outside hours", () => {
  it("says As of close, never Live", async () => {
    await renderWith(afterClose);
    expect(screen.getByTestId("options-clock")).toHaveTextContent(
      "As of close, Tue 22 Sep · market closed",
    );
    expect(screen.queryByText(/^Live ·/)).not.toBeInTheDocument();
  });
});
