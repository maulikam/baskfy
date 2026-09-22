"use server";

import { revalidatePath } from "next/cache";

import { optionsWrite, type OptionsFormResult } from "@/lib/options/write";

/**
 * The options settings' one write (`docs/options/05` §2, "the second allowed mutation").
 *
 * What it can send is exactly `OptionsConfigPatch`: the book's four money fields and, per sleeve
 * group, capital, risk %, most lots, whether paper runs, and the hard exit. **It cannot name a
 * pause, an execution switch or a ceiling** — the API forbids unknown fields, and none of those is
 * a field. A value above a ceiling comes back as a 422 naming the ceiling, rendered beside the
 * form; the patch is atomic, so nothing is saved.
 */

const GROUPS = ["O1M", "O1W", "O2", "O3"] as const;
const BOOK_FIELDS = [
  "account_inr",
  "margin_pool_inr",
  "daily_loss_limit_inr",
  "monthly_pause_inr",
] as const;
const SLEEVE_DECIMALS = ["sleeve_capital_inr", "risk_per_trade_pct"] as const;

const LABELS: Record<string, string> = {
  account_inr: "Account size",
  margin_pool_inr: "Margin pool",
  daily_loss_limit_inr: "Daily loss limit",
  monthly_pause_inr: "Monthly pause",
  sleeve_capital_inr: "Capital for this strategy",
  risk_per_trade_pct: "Risk per trade",
  risk_per_trade_inr: "Risk per trade in rupees",
  max_lots: "Most lots",
  hard_exit_time: "Hard exit",
};

const DECIMAL = /^\d+(\.\d+)?$/;
const TIME = /^\d{2}:\d{2}$/;

function text(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value.trim() : "";
}

export async function saveOptionsConfig(
  _previous: OptionsFormResult | null,
  formData: FormData,
): Promise<OptionsFormResult> {
  const book: Record<string, string> = {};
  for (const field of BOOK_FIELDS) {
    const raw = text(formData, `book.${field}`);
    if (!raw) continue;
    if (!DECIMAL.test(raw))
      return {
        ok: false,
        error: `${LABELS[field]} must be a number, 0 or more.`,
      };
    book[field] = raw;
  }
  const sleeves: Record<string, Record<string, unknown>> = {};
  for (const group of GROUPS) {
    const patch: Record<string, unknown> = {};
    for (const field of SLEEVE_DECIMALS) {
      const raw = text(formData, `${group}.${field}`);
      if (!raw) continue;
      if (!DECIMAL.test(raw))
        return {
          ok: false,
          error: `${LABELS[field]} must be a number, 0 or more.`,
        };
      patch[field] = raw;
    }
    const lots = text(formData, `${group}.max_lots`);
    if (lots) {
      if (!/^\d+$/.test(lots) || Number(lots) < 1) {
        return {
          ok: false,
          error: `${LABELS.max_lots} must be a whole number, 1 or more.`,
        };
      }
      patch.max_lots = Number(lots);
    }
    const exit = text(formData, `${group}.hard_exit_time`);
    if (exit) {
      if (!TIME.test(exit))
        return { ok: false, error: `${LABELS.hard_exit_time} must be a time.` };
      patch.hard_exit_time = exit;
    }
    const paper = text(formData, `${group}.paper_enabled`);
    if (paper === "yes" || paper === "no")
      patch.paper_enabled = paper === "yes";
    if (Object.keys(patch).length > 0) sleeves[group] = patch;
  }
  if (Object.keys(book).length === 0 && Object.keys(sleeves).length === 0) {
    return { ok: false, error: "Nothing to save." };
  }
  const result = await optionsWrite("PATCH", "/options/config", {
    body: { ...(Object.keys(book).length > 0 ? { book } : {}), sleeves },
  });
  if (!result.ok) {
    if (result.ceiling && result.field) {
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
  revalidatePath("/me/options");
  revalidatePath("/options", "layout");
  return {
    ok: true,
    message: "Saved. The next scan and the next plan use these.",
  };
}
