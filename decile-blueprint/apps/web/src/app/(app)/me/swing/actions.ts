"use server";

import { revalidatePath } from "next/cache";

import { swingWrite, type SwingFormResult } from "@/lib/swing/write";

/**
 * `settingsSave` — the `/me/swing` form's one action, SW14 (`docs/swing/05` §2 "/swing/settings").
 *
 * One `PATCH /swing/config` with the bearer from the server session. The route writes nine
 * numbers into `sw_config` and cannot name the exposure rung, the first-live countdown or the
 * drawdown state (`SwingConfigPatch` forbids unknown fields — DECISIONS-SW SW4.1), so this form
 * cannot either: those are shown on the page and never posted. A value above a server ceiling
 * comes back as `422 setting-above-ceiling` naming the field and the ceiling, and that is
 * exactly what the form renders beside the field.
 *
 * Decimals travel as the strings typed. `Number("0.50")` would send `0.5`, and a risk setting is
 * a number the sizer multiplies capital by (house rule 8).
 */

const DECIMAL_FIELDS = [
  "sleeve_capital_inr",
  "risk_per_trade_pct",
  "max_position_pct",
  "adr_min_pct",
  "turnover_min_inr",
  "price_min",
] as const;

const LABELS: Record<string, string> = {
  sleeve_capital_inr: "Allocation capital",
  risk_per_trade_pct: "Risk per trade",
  max_position_pct: "Largest position",
  max_open_positions: "Most positions open",
  or_window_minutes: "Opening-range window",
  stop_mode: "Stop mode",
  adr_min_pct: "Least daily range",
  turnover_min_inr: "Least turnover",
  price_min: "Lowest price",
};

function text(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value.trim() : "";
}

export async function settingsSave(
  _previous: SwingFormResult | null,
  formData: FormData,
): Promise<SwingFormResult> {
  const body: Record<string, unknown> = {};
  for (const field of DECIMAL_FIELDS) {
    const raw = text(formData, field);
    if (!raw) continue;
    if (!/^\d+(\.\d+)?$/.test(raw)) {
      return { ok: false, error: `${LABELS[field]} must be a number, 0 or more.`, field };
    }
    body[field] = raw;
  }
  const positions = text(formData, "max_open_positions");
  if (positions) {
    if (!/^\d+$/.test(positions) || Number(positions) < 1) {
      return {
        ok: false,
        error: `${LABELS.max_open_positions} must be a whole number, 1 or more.`,
        field: "max_open_positions",
      };
    }
    body.max_open_positions = Number(positions);
  }
  const window = text(formData, "or_window_minutes");
  if (window) {
    if (!["1", "5", "60"].includes(window)) {
      return {
        ok: false,
        error: `${LABELS.or_window_minutes} is 1, 5 or 60 minutes.`,
        field: "or_window_minutes",
      };
    }
    body.or_window_minutes = Number(window);
  }
  const stopMode = text(formData, "stop_mode");
  if (stopMode) {
    if (stopMode !== "LOW_OF_DAY" && stopMode !== "OPENING_RANGE_LOW") {
      return { ok: false, error: "Pick a stop mode.", field: "stop_mode" };
    }
    body.stop_mode = stopMode;
  }
  if (Object.keys(body).length === 0) return { ok: false, error: "Nothing to save." };

  const result = await swingWrite("PATCH", "/swing/config", body);
  if (!result.ok) {
    if (result.ceiling && result.field) {
      /*
       * The ceiling is the reader's; the name of the variable that sets it is not.
       *
       * Until 12 Sep 2026 this appended ` (BASKFY_SWING_RISK_PER_TRADE_PCT_MAX)` whenever the
       * problem document carried `env_var`, and a test pinned that exact sentence. It is the
       * defect of 11 Sep 2026 said again: `no-internals.test.tsx` bans "a setting or alert name,
       * which is the same defect wearing capitals", and the hint rendered one component away
       * (`_components/settings-form.tsx`, `max 1.0% — set by the server`) already got it right.
       * A person who cannot edit the server's environment cannot act on its spelling; the person
       * who can is reading this comment. `result.env_var` is still carried on the result for a
       * caller that wants to log it — it is simply never concatenated into a sentence.
       *
       * The field name is the same rule: an unlabelled field is one this form does not own, and
       * its stored spelling is snake_case. Say which limit was hit without naming the column.
       */
      const label = LABELS[result.field];
      return {
        ...result,
        error: label
          ? `${label}: max ${result.ceiling} — set by the server. Nothing was saved.`
          : `That is above the limit the server allows (${result.ceiling}). Nothing was saved.`,
      };
    }
    return result;
  }
  revalidatePath("/me/swing");
  revalidatePath("/swing", "layout");
  return { ok: true, message: "Saved. The next plan is built with these." };
}
