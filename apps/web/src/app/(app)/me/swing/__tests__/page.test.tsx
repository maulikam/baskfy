import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SwingConfig } from "@/lib/swing/fetch";

import SwingSettingsPage from "../page";

/**
 * SW14 — `05` §2's "/swing/settings (inside /me, not a hub tab)": the `sw_config` form with the
 * ceilings under the bounded fields, the rung shown and not editable, the execution flag shown
 * as a sentence and not a control, and the empty state before the allocation is seeded.
 */

vi.mock("@/lib/swing/fetch", () => ({
  fetchConfig: vi.fn(),
}));

// The action reaches `next-auth` through the write helper; the page only binds it to the form.
vi.mock("../actions", () => ({
  settingsSave: vi.fn(),
}));

const { fetchConfig } = await import("@/lib/swing/fetch");

function config(overrides: Partial<SwingConfig> = {}): SwingConfig {
  return {
    sleeve_capital_inr: 1_000_000,
    risk_per_trade_pct: 0.5,
    max_position_pct: 25,
    max_open_positions: 6,
    or_window_minutes: 5,
    stop_mode: "OPENING_RANGE_LOW",
    adr_min_pct: 4,
    turnover_min_inr: 50_000_000,
    price_min: 20,
    exposure_level: 1,
    first_live_sessions_left: 5,
    updated_at: "2026-09-02T10:00:00+00:00",
    updated_by: "seed",
    ceilings: { risk_per_trade_pct: "1.0", max_position_pct: "25.0", max_open_positions: "8" },
    execution_enabled: false,
    ...overrides,
  };
}

async function renderWith(value: SwingConfig | null) {
  vi.mocked(fetchConfig).mockResolvedValue(value);
  return render(await SwingSettingsPage());
}

describe("the settings form", () => {
  it("renders every sw_config field with its stored value, and the ceilings under the bounded ones", async () => {
    await renderWith(config());
    const form = screen.getByRole("button", { name: "Save" }).closest("form") as HTMLFormElement;
    const names = [...form.querySelectorAll("input[name], select[name]")].map((el) =>
      el.getAttribute("name"),
    );
    expect(names.sort()).toEqual(
      [
        "sleeve_capital_inr",
        "risk_per_trade_pct",
        "max_position_pct",
        "max_open_positions",
        "or_window_minutes",
        "stop_mode",
        "adr_min_pct",
        "turnover_min_inr",
        "price_min",
      ].sort(),
    );
    expect(within(form).getByLabelText("Risk per trade (%)")).toHaveValue("0.500");
    expect(within(form).getByLabelText("Allocation capital (₹)")).toHaveValue("1000000.00");
    expect(within(form).getByLabelText("Stop mode")).toHaveValue("OPENING_RANGE_LOW");
    expect(within(form).getByLabelText("Opening-range window")).toHaveValue("5");
    expect(form).toHaveTextContent("max 1.0% — set by the server");
    expect(form).toHaveTextContent("max 25.0% — set by the server");
    expect(form).toHaveTextContent("max 8 — set by the server");
    expect(form).not.toHaveAttribute("method");
  });

  it("shows the rung and never a field for it", async () => {
    await renderWith(config({ exposure_level: 2 }));
    expect(screen.getByTestId("rung")).toHaveTextContent("Rung 3 of 4");
    expect(document.querySelector('[name="exposure_level"]')).toBeNull();
    expect(document.querySelector('[name="first_live_sessions_left"]')).toBeNull();
    expect(document.querySelector('[name="execution_enabled"]')).toBeNull();
  });

  it("says Execution: disabled on this server as a sentence, not a control", async () => {
    await renderWith(config());
    expect(screen.getByTestId("execution")).toHaveTextContent("Execution: disabled on this server");
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Save"]);
  });

  it("reads enabled when the server has it on, still not a control", async () => {
    await renderWith(config({ execution_enabled: true }));
    expect(screen.getByTestId("execution")).toHaveTextContent("Execution: enabled on this server");
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("explains itself before the allocation is seeded", async () => {
    await renderWith(null);
    expect(screen.getByTestId("not-seeded")).toHaveTextContent("not set up on this deployment");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});
