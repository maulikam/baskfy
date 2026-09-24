import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type * as LiveMarksModule from "@/lib/screens/live-marks";
import { scanOff, scanned } from "@/lib/fno/__tests__/fixtures";

import OvernightPage from "../page";

/**
 * FO5's rendered-DOM acceptance for `/options/overnight` (`docs/fno/05` §2), over a mocked fetch
 * written to the API's shapes: the state the box ships in (scan off) said as a reason; the clock
 * labels exactly `05`'s; the red hard-exit line; the proposed condor with "recorded, not used";
 * the evidence card with its Tier 2E caveat; F2's permanent banner, verbatim and not dismissible.
 */

vi.mock("@/lib/screens/live-marks", async (importOriginal) => {
  const actual = await importOriginal<typeof LiveMarksModule>();
  return { ...actual, useLiveMarks: () => actual.EMPTY_LIVE_MARKS };
});

vi.mock("@/lib/fno/fetch", () => ({ fetchFnoOvernight: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/options/overnight",
  useRouter: () => ({ refresh: vi.fn() }),
}));

const { fetchFnoOvernight } = await import("@/lib/fno/fetch");

async function renderWith(view: typeof scanned | null) {
  vi.mocked(fetchFnoOvernight).mockResolvedValue(view);
  render(await OvernightPage());
}

const F2_BANNER =
  "Built by choice against the research: +0.017R a trade after rolls, negative in 2022, 2024, " +
  "2025 and 2026 (RESEARCH.md).";

describe("/options/overnight with the scan off (the state FO5 ships in)", () => {
  it("says why there is nothing and still carries every disclaimer", async () => {
    await renderWith(scanOff);
    expect(screen.getByTestId("fno-empty-reason")).toHaveTextContent(
      /scan is switched off/,
    );
    expect(screen.getByTestId("options-fno-caveat")).toBeInTheDocument();
    expect(screen.getByTestId("fno-f2-banner")).toHaveTextContent(F2_BANNER);
    expect(screen.getAllByTestId("fno-unscanned")).toHaveLength(2);
    expect(screen.queryByTestId("fno-hard-exit-tomorrow")).toBeNull();
    expect(screen.getByTestId("fno-evidence")).toHaveTextContent(
      /End-of-day closes, not fills\. Slippage is an assumed 3 % of premium per leg per crossing/,
    );
  });

  it("says the service did not answer rather than rendering a blank page", async () => {
    await renderWith(null);
    expect(screen.getByText(/F&O service did not answer/)).toBeInTheDocument();
  });
});

describe("/options/overnight after a scanned night", () => {
  it("uses 05's clock labels exactly", async () => {
    await renderWith(scanned);
    expect(screen.getByTestId("fno-clock")).toHaveTextContent(
      "As of close, Tue 22 Sep",
    );
    expect(screen.getByTestId("fno-mark-clock")).toHaveTextContent(
      "Marked at settle, 22 Sep",
    );
    // Outside the overlay the level is the close, and says so — never "live".
    const nifty = within(screen.getByTestId("fno-f1-NIFTY")).getByTestId(
      "fno-level",
    );
    expect(nifty).toHaveAttribute("data-live", "false");
    expect(nifty).toHaveTextContent("NIFTY close, 22 Sep");
  });

  it("shows the red hard-exit line for a structure whose exit is the next session", async () => {
    await renderWith(scanned);
    expect(screen.getByTestId("fno-hard-exit-tomorrow")).toHaveTextContent(
      "Hard exit tomorrow 15:00",
    );
  });

  it("shows the proposed condor with IV ÷ RV20 recorded, not used", async () => {
    await renderWith(scanned);
    const card = within(screen.getByTestId("fno-f1-BANKNIFTY"));
    expect(card.getByTestId("fno-state")).toHaveTextContent("Candidate");
    const condor = card.getByTestId("fno-proposed-condor");
    expect(condor).toHaveTextContent("recorded, not used");
    expect(condor).toHaveTextContent("1000 / 1000");
    expect(condor).toHaveTextContent("₹23,629.50");
    expect(card.getByTestId("fno-next-entry")).toHaveTextContent(
      /23 Sept? 2026/,
    );
  });

  it("lists F2's candidate and says when live would refuse its size", async () => {
    await renderWith(scanned);
    const table = screen.getByTestId("fno-f2-candidates");
    expect(table).toHaveTextContent("RELIANCE");
    expect(table).toHaveTextContent("₹36,100.00");
    expect(table).toHaveTextContent("live would be rejected: size");
    expect(screen.getByTestId("fno-f2-banner")).toHaveTextContent(F2_BANNER);
    // Not dismissible: the banner has no control in it.
    expect(
      within(screen.getByTestId("fno-f2-banner")).queryByRole("button"),
    ).toBeNull();
  });

  it("binds no action and draws the Options sub-nav", async () => {
    await renderWith(scanned);
    expect(
      screen.queryByRole("button", { name: /confirm|execute|place/i }),
    ).toBeNull();
    const nav = screen.getByTestId("options-subnav");
    expect(within(nav).getByText("Overnight").closest("a")).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});
