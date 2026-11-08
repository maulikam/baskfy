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

  /**
   * THE CEILING IS THE READER'S; THE VARIABLE THAT SETS IT IS NOT.
   *
   * This test asserted the opposite until 12 Sep 2026. It pinned
   * `"Risk per trade: max 1.0 — set by the server (BASKFY_SWING_RISK_PER_TRADE_PCT_MAX). Nothing
   * was saved."` — a customer-facing refusal carrying the name of a server environment variable,
   * which `components/twt/__tests__/no-internals.test.tsx` bans by name as "a setting or alert
   * name, which is the same defect wearing capitals". It was not a lazy test: it was a careful
   * test of the wrong thing, and it defended the leak.
   *
   * The tell was one component away. `_components/settings-form.tsx` renders the same ceiling as
   * the field's inline hint — `max 1.0% — set by the server` — with no variable name at all, and
   * `page.test.tsx` asserts that. The form already agreed with the rule; only the refusal did not.
   *
   * `env_var` still arrives on the result and is still carried on it. What must never happen is
   * that it is concatenated into a sentence a person reads.
   */
  it("renders a ceiling refusal as the ceiling, beside its field, and names no server variable", async () => {
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
    expect(result.error).toBe("Risk per trade: max 1.0 — set by the server. Nothing was saved.");
    // The three facts that are the reader's: which setting, what the limit is, that nothing
    // was written. Not the fourth, which names a variable only the server's operator can change.
    expect(result.error).not.toContain("BASKFY_SWING_RISK_PER_TRADE_PCT_MAX");
    expect(result.error).not.toMatch(/[A-Z][A-Z0-9]*(_[A-Z0-9]+)+/);
    // Nor the server's own sentence, which spells the field as the column it is stored in.
    expect(result.error).not.toContain("risk_per_trade_pct");
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  /**
   * A ceiling on a field this form does not own. The old expression fell back to `result.field` —
   * the stored spelling — so a server that bounded a setting the form has no label for would have
   * printed snake_case at the reader. Say which limit was hit instead.
   */
  it("refuses a ceiling on an unlabelled field without printing its stored spelling", async () => {
    vi.mocked(swingWrite).mockResolvedValue({
      ok: false,
      status: 422,
      error: "trail_atr_multiple may not exceed 3.0; 4.0 was requested.",
      field: "trail_atr_multiple",
      ceiling: "3.0",
      env_var: "BASKFY_SWING_TRAIL_ATR_MULTIPLE_MAX",
    });
    const result = await settingsSave(null, form({ risk_per_trade_pct: "0.5" }));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error).toBe("That is above the limit the server allows (3.0). Nothing was saved.");
    expect(result.error).not.toContain("trail_atr_multiple");
    expect(result.error).not.toMatch(/[a-z][a-z0-9]*(_[a-z0-9]+)+/);
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
