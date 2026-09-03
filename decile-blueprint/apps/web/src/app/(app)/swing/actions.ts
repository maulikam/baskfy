"use server";

import { revalidatePath } from "next/cache";

import { swingWrite, type SwingFormResult } from "@/lib/swing/write";

/**
 * The hub's five server actions — SW14's four for the watchlist and SW15's "Scan now";
 * `docs/swing/05` §2 and `02` Track A.
 *
 * Each one is a non-money write: a name added to `sw_watch`, its note or catalyst edited, a
 * MANUAL row's ten-session clock restarted (STANDING-ANSWERS A14), a row marked DISMISSED, a
 * detection run queued. Each makes exactly one API call with the bearer from the server
 * session, revalidates the hub and answers the form with `{ ok, error? }` rather than throwing.
 * None of them can reach an order: `__tests__/read-only.test.tsx` enumerates every export of
 * this file and asserts the set is exactly these five plus `settingsSave` under `/me/swing`.
 *
 * Levels are posted as the strings the person typed. A trigger of `149.60` is a number somebody
 * types into a broker, and `Number("149.60")` would send `149.6` (house rule 8).
 */

function text(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value.trim() : "";
}

/** A positive decimal as typed, or `null` for blank; `undefined` when it is not a number. */
function level(formData: FormData, name: string): string | null | undefined {
  const raw = text(formData, name);
  if (!raw) return null;
  return /^\d+(\.\d+)?$/.test(raw) && Number(raw) > 0 ? raw : undefined;
}

function rowId(formData: FormData): number | null {
  const raw = text(formData, "id");
  return /^\d+$/.test(raw) ? Number(raw) : null;
}

const HUB = "/swing";

/** Add a name by hand — `POST /swing/watch`. The symbol comes from the `/search` box as typed. */
export async function watchAdd(
  _previous: SwingFormResult | null,
  formData: FormData,
): Promise<SwingFormResult> {
  const symbol = text(formData, "symbol").toUpperCase();
  const instrumentId = text(formData, "instrument_id");
  if (!symbol && !/^\d+$/.test(instrumentId)) {
    return { ok: false, error: "Type a symbol first." };
  }
  const setup = text(formData, "setup");
  if (setup !== "FLAG" && setup !== "EP") {
    return { ok: false, error: "Pick a setup: a flag or an episodic pivot." };
  }
  const trigger = level(formData, "trigger");
  const stopRef = level(formData, "stop_ref");
  if (trigger === undefined) return { ok: false, error: "The trigger must be a price above 0." };
  if (stopRef === undefined) {
    return { ok: false, error: "The stop reference must be a price above 0." };
  }
  if (trigger !== null && stopRef !== null && Number(stopRef) >= Number(trigger)) {
    return { ok: false, error: "The stop reference sits below the trigger, not above it." };
  }
  const note = text(formData, "note");
  const catalyst = text(formData, "catalyst");
  const result = await swingWrite("POST", "/swing/watch", {
    ...(/^\d+$/.test(instrumentId) ? { instrument_id: Number(instrumentId) } : { symbol }),
    setup,
    trigger,
    stop_ref: stopRef,
    ...(note ? { note } : {}),
    ...(catalyst ? { catalyst } : {}),
  });
  if (!result.ok) {
    if (result.status === 404) {
      return { ok: false, error: `${symbol || instrumentId} is not a symbol NSE lists.` };
    }
    return result;
  }
  revalidatePath(HUB, "layout");
  return { ok: true, message: `Watching ${symbol || instrumentId}.` };
}

/** Stop watching — `DELETE /swing/watch/{id}`, a state change to DISMISSED, never a delete. */
export async function watchDismiss(
  _previous: SwingFormResult | null,
  formData: FormData,
): Promise<SwingFormResult> {
  const id = rowId(formData);
  if (id === null) return { ok: false, error: "That row is not on the list any more." };
  const result = await swingWrite("DELETE", `/swing/watch/${id}`);
  if (!result.ok) return result;
  revalidatePath(HUB, "layout");
  return { ok: true, message: "Dismissed." };
}

/** The note and the catalyst — `PATCH /swing/watch/{id}`. Levels are not editable here, by design. */
export async function watchAnnotate(
  _previous: SwingFormResult | null,
  formData: FormData,
): Promise<SwingFormResult> {
  const id = rowId(formData);
  if (id === null) return { ok: false, error: "That row is not on the list any more." };
  const result = await swingWrite("PATCH", `/swing/watch/${id}`, {
    note: text(formData, "note") || null,
    catalyst: text(formData, "catalyst") || null,
  });
  if (!result.ok) return result;
  revalidatePath(HUB, "layout");
  return { ok: true, message: "Saved." };
}

/** "Still watching" — `PATCH /swing/watch/{id} {"reconfirm": true}` restarts a MANUAL row's clock (A14). */
export async function watchReconfirm(
  _previous: SwingFormResult | null,
  formData: FormData,
): Promise<SwingFormResult> {
  const id = rowId(formData);
  if (id === null) return { ok: false, error: "That row is not on the list any more." };
  const result = await swingWrite("PATCH", `/swing/watch/${id}`, { reconfirm: true });
  if (!result.ok) return result;
  revalidatePath(HUB, "layout");
  return { ok: true, message: "Still watching — ten more sessions." };
}

/**
 * "Scan now" — `POST /swing/scan` (SW15). Queues a detection run; during the session the worker
 * detects on a bar built from live Kite quotes and labels every row provisional. A 409 (one
 * already in flight) and a 429 (one a minute) come back as the server's own sentence, so the
 * button says why not rather than failing silently.
 */
export async function scanNow(
  _previous: SwingFormResult | null,
  _formData: FormData,
): Promise<SwingFormResult> {
  const result = await swingWrite("POST", "/swing/scan");
  if (!result.ok) return result;
  revalidatePath(HUB, "layout");
  return { ok: true, message: "Scan queued — the page refreshes as it runs." };
}
