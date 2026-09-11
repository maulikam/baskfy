import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type * as DeskFetchModule from "@/lib/desk/fetch";
import type { Regime } from "@/lib/desk/fetch";

type DeskFetch = typeof DeskFetchModule;

import RegimePage from "../page";

/**
 * The desk's stance page had no test at all, which is how it carried a dead branch for months.
 *
 * PC5 found it while building the command centre's regime panel against the same payload
 * (`docs/pc-findings/pc5.md` §3.4): the page's explanation map was keyed `full | half | none`,
 * and the desk's `NewBuyMode` is `full | half | **blocked**`. So the sentence explaining a blocked
 * book never rendered — on precisely the stance a reader most needs explained — while the headline
 * above it still read "None" because "blocked" fell into a ternary's else branch. The page looked
 * correct and said nothing.
 *
 * These tests assert the SPEC — the desk's own vocabulary, and the brief's rule that a missing
 * value carries its reason rather than a dash — rather than the shape the markup happens to have.
 */

vi.mock("@/lib/desk/fetch", async (importOriginal) => {
  const actual = await importOriginal<DeskFetch>();
  return { ...actual, fetchRegime: vi.fn() };
});

vi.mock("next/navigation", () => ({
  usePathname: () => "/regime",
}));

const { fetchRegime } = await import("@/lib/desk/fetch");

function regime(over: Partial<Regime> = {}): Regime {
  return {
    evaluated_at: "2026-09-11T04:00:00Z",
    signal_date: "2026-09-10",
    tier: "R2",
    previous_tier: "R1",
    new_buys: "half",
    mode: "enforce",
    breadth_pct: 48.2,
    actual_equity_pct: 78,
    target_equity_cap_pct: 70,
    reasons: ["Breadth weakened below its threshold."],
    data_stale: false,
    manual_action_required: false,
    next_evaluation_date: "2026-09-18",
    ...over,
  };
}

async function renderPage(over: Partial<Regime> = {}) {
  vi.mocked(fetchRegime).mockResolvedValue(regime(over));
  render(await RegimePage());
}

describe("the desk's new-buy policy", () => {
  it("blocked: a blocked portfolio gets its explanation, which is the stance that most needs one", async () => {
    await renderPage({ new_buys: "blocked" });

    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent(
      "No new positions are being opened.",
    );
    expect(screen.getByText("Blocked")).toBeInTheDocument();
  });

  it("blocked: the headline no longer reads None for a blocked portfolio", async () => {
    /* "None" and "Blocked" are not the same claim. The first says the desk chose to open nothing;
       the second says a rule forbade it. The old ternary printed the first for both. */
    await renderPage({ new_buys: "blocked" });

    expect(screen.queryByText("None")).not.toBeInTheDocument();
  });

  it("full: full size is explained as well as labelled", async () => {
    await renderPage({ new_buys: "full" });

    expect(screen.getByText("Full size")).toBeInTheDocument();
    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent(
      "New positions open at full size.",
    );
  });

  it("half: half size is explained as well as labelled", async () => {
    await renderPage({ new_buys: "half" });

    expect(screen.getByText("Half size")).toBeInTheDocument();
    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent(
      "New positions open at half size.",
    );
  });

  it("unavailable: no recorded policy says so, and never renders a bare dash", async () => {
    await renderPage({ new_buys: null });

    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent(
      "The desk did not record a new-buy policy on this evaluation.",
    );
    expect(screen.getByText("Not recorded")).toBeInTheDocument();
    expect(screen.queryByText("–")).not.toBeInTheDocument();
    expect(screen.queryByText("—")).not.toBeInTheDocument();
  });

  it("unrecognised: a mode this page has no sentence for prints as the desk wrote it", async () => {
    /* Mapping an unknown value onto "None" would turn a value this page does not understand into
       a claim about the market. The desk is allowed to gain a fourth mode without this page
       inventing its meaning. */
    await renderPage({ new_buys: "throttled" });

    expect(screen.getByText("throttled")).toBeInTheDocument();
    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent(
      /does not have a sentence for/,
    );
  });

  it("R2: cautious is never described as being out of the market", async () => {
    /* The brief's own instruction, and the desk's: R2 is reduced exposure, not an exit. */
    await renderPage({ tier: "R2", new_buys: "half" });

    const page = document.body.textContent ?? "";
    expect(page).not.toMatch(/out of the market/i);
    expect(page).toMatch(/still invested/i);
  });
});
