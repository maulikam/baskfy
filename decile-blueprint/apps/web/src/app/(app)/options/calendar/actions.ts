"use server";

import { revalidatePath } from "next/cache";

import { optionsWrite, type OptionsFormResult } from "@/lib/options/write";

/**
 * The calendar's one write, in its two halves (`docs/options/05` §2, "add/remove of `MANUAL`
 * event days — one of exactly two allowed mutations").
 *
 * An event day is a day **no sleeve trades** (`04` §1.3): adding one can only make the book do
 * less. Removing is limited by the API to a day this person added; a seeded, source-verified RBI
 * date is refused there, and the page does not offer the button for one.
 */

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function text(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value.trim() : "";
}

export async function addEventDay(
  _previous: OptionsFormResult | null,
  formData: FormData,
): Promise<OptionsFormResult> {
  const date = text(formData, "date");
  if (!ISO_DATE.test(date))
    return { ok: false, error: "Pick a date.", field: "date" };
  const note = text(formData, "note");
  const result = await optionsWrite("POST", "/options/event-day", {
    body: { date, ...(note ? { note } : {}) },
  });
  if (!result.ok) return result;
  revalidatePath("/options", "layout");
  return { ok: true, message: "Added. No options strategy will trade that day." };
}

export async function removeEventDay(formData: FormData): Promise<void> {
  const date = text(formData, "date");
  if (!ISO_DATE.test(date)) return;
  await optionsWrite("DELETE", "/options/event-day", { search: { date } });
  revalidatePath("/options", "layout");
}
