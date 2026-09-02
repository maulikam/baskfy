import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * SW14 — `settingsSave` end to end from a `FormData` to one `PATCH /swing/config`: decimals go
 * as the strings typed (house rule 8), the exposure rung has no field to post, and a 422
 * `setting-above-ceiling` comes back as a sentence naming the ceiling beside its field.
 */

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("@/lib/swing/write", () => ({ swingWrite: vi.fn() }));

const { swingWrite } = await import("@/lib/swing/write");
const { revalidatePath } = await import("next/cache");
const { settingsSave } = await import("../actions");

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.set(key, value);
  return data;
}

beforeEach(() => {
  vi.mocked(swingWrite).mockReset();
  vi.mocked(revalidatePath).mockReset();
});

describe("settingsSave", () => {
  it("makes one PATCH with the values as typed and revalidates", async () => {
    vi.mocked(swingWrite).mockResolvedValue({ ok: true, body: {}, status: 200 });
    const result = await settingsSave(
      null,
      form({
        sleeve_capital_inr: "1000000.00",
        risk_per_trade_pct: "0.50",
        max_position_pct: "25.00",
        max_open_positions: "6",
        or_window_minutes: "5",
        stop_mode: "LOW_OF_DAY",
        adr_min_pct: "4.00",
        turnover_min_inr: "50000000.00",
        price_min: "20.00",
        exposure_level: "3",
      }),
    );
    expect(result.ok).toBe(true);
    expect(swingWrite).toHaveBeenCalledTimes(1);
    expect(swingWrite).toHaveBeenCalledWith("PATCH", "/swing/config", {
      sleeve_capital_inr: "1000000.00",
      risk_per_trade_pct: "0.50",
      max_position_pct: "25.00",
      max_open_positions: 6,
      or_window_minutes: 5,
      stop_mode: "LOW_OF_DAY",
      adr_min_pct: "4.00",
      turnover_min_inr: "50000000.00",
      price_min: "20.00",
    });
    expect(revalidatePath).toHaveBeenCalledWith("/me/swing");
  });

  it("renders a ceiling refusal as the ceiling, beside its field", async () => {
    vi.mocked(swingWrite).mockResolvedValue({
      ok: false,
      status: 422,
      error: "risk_per_trade_pct may not exceed 1.0; 1.5 was requested.",
      field: "risk_per_trade_pct",
      ceiling: "1.0",
      env_var: "BASKFY_SWING_RISK_PER_TRADE_PCT_MAX",
    });
    const result = await settingsSave(null, form({ risk_per_trade_pct: "1.5" }));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.field).toBe("risk_per_trade_pct");
    expect(result.ceiling).toBe("1.0");
    expect(result.error).toBe(
      "Risk per trade: max 1.0 — set by the server (BASKFY_SWING_RISK_PER_TRADE_PCT_MAX). Nothing was saved.",
    );
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("refuses a non-number before making any call", async () => {
    const result = await settingsSave(null, form({ risk_per_trade_pct: "half" }));
    expect(result.ok).toBe(false);
    expect(swingWrite).not.toHaveBeenCalled();
    const empty = await settingsSave(null, form({}));
    expect(empty).toEqual({ ok: false, error: "Nothing to save." });
  });
});
