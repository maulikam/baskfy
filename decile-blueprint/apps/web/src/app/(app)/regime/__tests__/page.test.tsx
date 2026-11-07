import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RegimeData, RegimePayload } from "@/components/regime/types";

import RegimePage from "../page";

/**
 * `/regime` is a port of the desk's Regime page, read from the desk's own payload. These assert
 * the spec: the desk's vocabulary (`NewBuyMode` is `full | half | blocked`, never `none` — PC5,
 * docs/pc-findings/pc5.md §3.4), every gate listed whether or not it fired, and a GET-only read.
 */

vi.mock("next/navigation", () => ({ usePathname: () => "/regime" }));
vi.mock("next/dynamic", () => ({ default: () => () => null }));

function regime(over: Partial<RegimeData> = {}): RegimeData {
  return {
    available: true,
    dry_run: true,
    mode: "observe",
    signal_session_date: "2026-11-04",
    scheduled_week_end: "2026-11-04",
    next_evaluation_date: "2026-11-06",
    last_transition_date: null,
    algorithm_version: "regime/1.0.0",
    config_hash: "571bfb93ce64019f",
    policy_tier: "R2",
    tier_name: "Caution",
    tier_key: "caution",
    tier_meaning: "Still invested, new entries at half size",
    raw_candidate_tier: "R2",
    raw_candidate_name: "Caution",
    previous_policy_tier: "R1",
    transition_limited: false,
    new_buys: "half",
    new_buys_label: "Half size",
    forced_action: "none",
    forced_action_label: "None",
    override_active: false,
    sentinel_veto: false,
    target_equity_cap_pct: 70,
    actual_equity_pct: 66.9,
    exposure_gap_pct: 0,
    required_reduction_pct: 0,
    current_cash_pct: 33.1,
    pending_sell_pct: 0,
    execution_status: "not_planned",
    exposure_observed_at: "2026-11-05T10:35:42",
    exposure_cap_mismatch: false,
    bar: { within_cap: 66.9, excess: 0, cash: 33.1, cap_marker: 70 },
    target_position_range: [12, 15],
    cluster_weights: {},
    cluster_cap: 25,
    largest_position: null,
    allocation_feasible: null,
    plan_summary: {},
    gates: [
      {
        code: "R3_H200_WEAK",
        label: "Long-term structural health (H200)",
        current: "0.70",
        threshold: "≤ 0.50",
        status: "Clear",
        note: "Weighted share of the book above its 200-DMA",
        fired: false,
      },
      {
        code: "R1_ALL_CONDITIONS_MET",
        label: "Risk-On health requirement",
        current: "H50 0.60 · H200 0.70",
        threshold: "≥ 0.80 and ≥ 0.80",
        status: "Not met",
        note: "Both must hold for Risk-On",
        fired: false,
      },
    ],
    narrative: ["NIFTY 50 is above its 200-DMA."],
    index_cards: [],
    health: { "20": 0.5, "50": 0.6, "200": 0.7 },
    bearish_stack_weight: 0,
    book_weights: { largecap: 0.2, midcap: 0.3, smallcap: 0.5 },
    book_weight_source: "config_default",
    book_weights_are_defaults: false,
    breadth_pct: 48.2,
    breadth_coverage_pct: 100,
    breadth_usable: true,
    breadth_band: "Caution",
    breadth_band_key: "caution",
    breadth_bands: [
      { lo: 0, hi: 30, label: "Crash breadth", key: "crash" },
      { lo: 30, hi: 40, label: "Risk-off", key: "off" },
      { lo: 40, hi: 55, label: "Caution", key: "caution" },
      { lo: 55, hi: 101, label: "Recovery", key: "on" },
    ],
    breadth_observed: 56,
    breadth_eligible: 56,
    breadth_as_of: "2026-11-04",
    breadth_universe: "scan",
    breadth_universe_hash: "ed68d978d3e5b482",
    min_breadth_coverage_pct: 90,
    veto: {
      active: false,
      dist_50: 1.2,
      state_50: "above",
      conf_50: "3/3 above",
      dist_200: 4.1,
      state_200: "above",
      conf_200: "3/3 above",
      below_200: false,
      effect: "None",
      release: "Not applicable",
    },
    proposed_orders: [],
    proposed_sells: [],
    proposed_buys: [],
    guard_orders: [],
    no_plan_reason: null,
    tax_data_incomplete: false,
    evaluation_id: "664ef29a6aa11f4a7dbf5e1f",
    planned_sell_value: null,
    planned_buy_value: null,
    rebalance_plan: null,
    index_status: [],
    store_status: null,
    forced_actions_permitted: true,
    config: {
      ma_lengths: [20, 50, 200],
      buffer_bps: 150,
      confirm_days: 3,
      breadth_thresholds: {},
      health_thresholds: {},
      exposure_map: { R1: 100, R2: 70, R3: 40, R4: 10 },
      default_weights: {},
      recovery_breadth_min: 55,
      recovery_h20_min: 0.5,
      ltcg_review_days: 45,
      weekly_evaluation_weekday: 4,
      r4_residual: "max 2 names",
      breadth_required: true,
      min_breadth_coverage_pct: 90,
      index_stale_days: 5,
      whipsaw: "≥20pt reduction",
      algorithm_version: "regime/1.0.0",
      config_hash: "571bfb93ce64019f",
    },
    portfolio: { available: false },
    holdings_now: 14,
    full_sized: 14,
    half_sized: [],
    below_half: [],
    headline_reason: "Breadth weakened below its threshold.",
    tax_detail: [],
    recovery_confirmations: 0,
    recovery_required: 2,
    tier_ladder: [],
    mode_explainer: [],
    data_health: "Current",
    history: [],
    timeline: null,
    timeline_min_weeks: 3,
    banners: [],
    safe_state: null,
    ...over,
  };
}

const fetchMock = vi.fn<typeof fetch>();

function respond(payload: RegimePayload, status = 200) {
  fetchMock.mockResolvedValue(new Response(JSON.stringify(payload), { status }));
  vi.stubGlobal("fetch", fetchMock);
}

async function renderPage(payload: RegimePayload) {
  respond(payload);
  render(<RegimePage />);
  await screen.findByTestId("new-buy-explanation");
}

afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

describe("the desk's new-buy policy", () => {
  it("blocked: a blocked portfolio gets its explanation, and the headline never reads None", async () => {
    await renderPage(regime({ new_buys: "blocked" }));

    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent("No new positions are being opened.");
    expect(screen.getByTestId("new-buy-headline")).toHaveTextContent(/^Blocked$/);
  });

  it("full and half are explained as well as labelled", async () => {
    await renderPage(regime({ new_buys: "full" }));
    expect(screen.getByTestId("new-buy-headline")).toHaveTextContent("Full size");
    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent("New positions open at full size.");
  });

  it("half: half size is explained as well as labelled", async () => {
    await renderPage(regime({ new_buys: "half" }));
    expect(screen.getByTestId("new-buy-headline")).toHaveTextContent("Half size");
    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent("New positions open at half size.");
  });

  it("unavailable: no recorded policy says so, never a bare dash", async () => {
    await renderPage(regime({ new_buys: null }));
    expect(screen.getByTestId("new-buy-headline")).toHaveTextContent("Not recorded");
    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent(
      "The desk did not record a new-buy policy on this evaluation.",
    );
  });

  it("unrecognised: a mode this page has no sentence for prints as the desk wrote it", async () => {
    await renderPage(regime({ new_buys: "throttled" }));
    expect(screen.getByTestId("new-buy-headline")).toHaveTextContent("throttled");
    expect(screen.getByTestId("new-buy-explanation")).toHaveTextContent(/does not have a sentence for/);
  });
});

describe("the desk's regime, as the desk shows it", () => {
  it("reads the desk's payload with a GET, same-origin", async () => {
    await renderPage(regime());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/desk/regime/data");
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("prints the tier the desk named, in the desk's own words", async () => {
    await renderPage(regime());
    expect(screen.getByTestId("regime-tier")).toHaveTextContent("R2 Caution");
    expect(document.body.textContent).toMatch(/still invested/i);
    expect(document.body.textContent).not.toMatch(/out of the market/i);
  });

  it("lists every gate, including those that did not fire", async () => {
    await renderPage(regime());
    expect(screen.getByText("R3_H200_WEAK")).toBeInTheDocument();
    expect(screen.getByText("R1_ALL_CONDITIONS_MET")).toBeInTheDocument();
    expect(screen.getByText("Clear")).toBeInTheDocument();
    expect(screen.getByText("Not met")).toBeInTheDocument();
  });

  it("no evaluation yet: says so instead of rendering an empty dial", async () => {
    respond({ available: false, message: "No regime evaluation has been stored." });
    render(<RegimePage />);
    expect(await screen.findByText("No evaluation yet")).toBeInTheDocument();
  });

  it("desk unreachable: says the data could not be loaded", async () => {
    respond(regime(), 502);
    render(<RegimePage />);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/could not be loaded/),
    );
  });
});
