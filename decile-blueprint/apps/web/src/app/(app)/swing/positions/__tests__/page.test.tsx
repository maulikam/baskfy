import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SwingConfig, SwingPlan, SwingPlanLine, SwingPosition } from "@/lib/swing/fetch";

import { firstLiveHeader } from "../copy";
import SwingPositionsPage from "../page";

/**
 * SW14 — `05` §2's Positions tab, completed, and the two STANDING-ANSWERS clauses it renders:
 * A7's `PENDING_RANGE` line shown with no confirm affordance of any kind, A9's first-live header
 * at the risk in force, plus Simulated labels, days held and the distance to the trail average.
 */

vi.mock("@/lib/swing/fetch", () => ({
  fetchPositions: vi.fn(),
  fetchConfig: vi.fn(),
  fetchBars: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/swing/positions",
}));

const { fetchPositions, fetchConfig, fetchBars } = await import("@/lib/swing/fetch");

function position(overrides: Partial<SwingPosition> = {}): SwingPosition {
  return {
    id: 1,
    instrument_id: 1,
    symbol: "FLAGCO",
    name: "FLAGCO LIMITED",
    setup: "FLAG",
    entry_date: "2026-08-25",
    entry_avg: 100,
    quantity_entered: 100,
    quantity_open: 100,
    initial_stop: 96,
    stop: 97,
    gtt_id: "gtt-1",
    naked: false,
    trail: "MA10",
    partial_done: false,
    state: "OPEN",
    closed_on: null,
    exit_avg: null,
    close_reason: null,
    r_multiple: null,
    pnl_inr: null,
    simulated: true,
    last_close: 104,
    ...overrides,
  };
}

function line(overrides: Partial<SwingPlanLine> = {}): SwingPlanLine {
  return {
    id: 10,
    kind: "BUY_ON_TRIGGER",
    symbol: "FLAGCO",
    name: "FLAGCO LIMITED",
    setup: "FLAG",
    quantity: 50,
    trigger: 149.6,
    stop: 141.86,
    risk_inr: 387,
    position_value: 7480,
    trail: "MA10",
    note: null,
    state: "PROPOSED",
    ...overrides,
  };
}

function plan(lines: SwingPlanLine[]): SwingPlan {
  return {
    plan_id: "p-1",
    as_of: "2026-09-02",
    source: "EOD",
    built_at: "2026-09-02T15:35:00+05:30",
    expires_at: "2026-09-03T10:45:00+05:30",
    gate: "AMBER",
    exposure_level: 0,
    total_risk_inr: 387,
    total_new_exposure_inr: 7480,
    lines,
    skips: [],
  };
}

function config(overrides: Partial<SwingConfig> = {}): SwingConfig {
  return {
    sleeve_capital_inr: 1_000_000,
    risk_per_trade_pct: 0.5,
    max_position_pct: 25,
    max_open_positions: 6,
    or_window_minutes: 5,
    stop_mode: "LOW_OF_DAY",
    adr_min_pct: 4,
    turnover_min_inr: 50_000_000,
    price_min: 20,
    exposure_level: 0,
    first_live_sessions_left: 5,
    updated_at: "2026-09-02T10:00:00+00:00",
    updated_by: "seed",
    ceilings: { risk_per_trade_pct: "1.0", max_position_pct: "25.0", max_open_positions: "8" },
    execution_enabled: false,
    ...overrides,
  };
}

async function renderWith(opts: {
  positions?: SwingPosition[];
  plan?: SwingPlan | null;
  config?: SwingConfig | null;
}) {
  vi.mocked(fetchPositions).mockResolvedValue({ data: opts.positions ?? [], plan: opts.plan ?? null });
  vi.mocked(fetchConfig).mockResolvedValue(opts.config === undefined ? config() : opts.config);
  vi.mocked(fetchBars).mockResolvedValue({
    data: [{ date: "2026-09-02", close: 104, ma_fast: 100, ma_slow: 98 }],
  });
  return render(await SwingPositionsPage());
}

describe("a PENDING_RANGE line is shown for what it is, with nothing to confirm", () => {
  it("renders the line with the range, no stop, no quantity and no button", async () => {
    await renderWith({
      plan: plan([line({ id: 11, kind: "PENDING_RANGE", symbol: "EPSILONGAP", quantity: 0, trigger: 92, stop: null, risk_inr: 0 })]),
    });
    const row = screen.getByText("EPSILONGAP").closest("tr");
    expect(row).toHaveAttribute("data-kind", "PENDING_RANGE");
    expect(row).toHaveTextContent("range at 92.00");
    expect(row).toHaveTextContent("no stop yet");
    expect(row?.querySelector("button")).toBeNull();
    expect(row?.querySelector("form")).toBeNull();
    expect(screen.getByTestId("pending-range")).toHaveTextContent("EPSILONGAP holds a slot with no stop yet");
    expect(screen.getByTestId("pending-range")).toHaveTextContent("nothing to confirm on it, anywhere");
  });

  it("puts no button or form anywhere on the page", async () => {
    await renderWith({ positions: [position()], plan: plan([line(), line({ id: 12, kind: "PENDING_RANGE", symbol: "GAPCO", quantity: 0, stop: null })]) });
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(document.querySelector("form")).toBeNull();
  });
});

describe("the first-live header", () => {
  it("shows the count and the full risk while execution is disabled", async () => {
    await renderWith({ plan: plan([line()]) });
    expect(screen.getByTestId("first-live")).toHaveTextContent(
      "first live sessions: 5 left · risk 0.500%",
    );
  });

  it("halves the risk only when a confirm would be real, and disappears at zero", () => {
    expect(firstLiveHeader(config({ execution_enabled: true }))).toBe(
      "first live sessions: 5 left · risk 0.250%",
    );
    expect(firstLiveHeader(config({ first_live_sessions_left: 0 }))).toBeNull();
  });

  it("is absent when the countdown is over", async () => {
    await renderWith({ plan: plan([line()]), config: config({ first_live_sessions_left: 0 }) });
    expect(screen.queryByTestId("first-live")).toBeNull();
  });
});

describe("the open row", () => {
  it("labels a simulated position, counts the days held and shows the distance to the trail", async () => {
    await renderWith({ positions: [position()] });
    const row = screen.getByText("FLAGCO").closest("tr") as HTMLElement;
    expect(within(row).getByText("Simulated")).toBeInTheDocument();
    expect(row).toHaveTextContent("FLAG");
    // (104 - 100) / 100: 4.00% above the 10-day average of 100.00.
    expect(row).toHaveTextContent("MA10 · 100.00 · +4.00%");
    expect(row).toHaveTextContent(/\d+d/);
    expect(row).toHaveTextContent("1.00R");
    expect(screen.getByTestId("simulated-note")).toHaveTextContent("Every open position is Simulated");
    expect(fetchBars).toHaveBeenCalledWith(1);
  });

  it("does not label a real position and says how many are simulated", async () => {
    await renderWith({
      positions: [position({ simulated: false }), position({ id: 2, symbol: "SIMCO", instrument_id: 2 })],
    });
    const real = screen.getByText("FLAGCO").closest("tr") as HTMLElement;
    expect(within(real).queryByText("Simulated")).toBeNull();
    expect(screen.getByTestId("simulated-note")).toHaveTextContent("1 of 2 open positions are Simulated");
  });

  it("leads with an unprotected position", async () => {
    await renderWith({ positions: [position({ gtt_id: null, naked: true })] });
    expect(screen.getByText(/has no resting stop/)).toBeInTheDocument();
    expect(screen.getByText("none")).toBeInTheDocument();
  });
});
