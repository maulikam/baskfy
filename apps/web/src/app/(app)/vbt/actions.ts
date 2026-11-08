"use server";

import { revalidatePath } from "next/cache";

import { vbtWrite, type VbtScanResult } from "@/lib/vbt/write";

import { SCAN_QUEUED, scanRefusal } from "./copy";

/**
 * The `/vbt` hub's server actions — **one**, and it queues a detection run.
 *
 * WHAT CHANGED, AND WHY IT IS NOT A WEAKENING
 * -------------------------------------------
 * Until 12 Sep 2026 this tree had no `actions.ts` at all, and `__tests__/read-only.test.tsx`
 * asserted exactly that: DECISIONS-VB VB8.4 declined to build `05` §2's per-row *Dismiss* note
 * because `03` had no table to put it in, so "this tree has no server action" was the strongest
 * true claim available and the test made it. Maulik asked for "Scan now" on this page, so that
 * claim is no longer available — and the census that replaces it is the swing hub's, which is
 * stricter than a prose promise: the file walk enumerates every export of every `use server`
 * module under the tree and the set must be exactly `["scanNow"]`.
 *
 * **A scan is money-free by construction.** It queues the detector; bars in, detection rows out.
 * It reaches `/vbt/scan` through a write helper whose path type admits one string, and it has no
 * way to name the desk's confirm route — which is what `docs/vbt/02` Track C §4 keeps out of this
 * application. A volume-breakout line still becomes an order in the desk console, on a click
 * Maulik makes, and nowhere else. DECISIONS-VB VB14.
 */

const HUB = "/vbt";

/**
 * "Scan now" — `POST /vbt/scan`. 202 queued, 409 one already in flight, 429 one a minute.
 *
 * The status becomes a sentence in `copy.ts` rather than here, and the service's own `detail` is
 * never rendered: a refusal written for an operator names jobs and tables, and this page is read
 * by the person whose money it is.
 */
export async function scanNow(
  _previous: VbtScanResult | null,
  _formData: FormData,
): Promise<VbtScanResult> {
  const outcome = await vbtWrite("/vbt/scan");
  if (!outcome.ok) return { ok: false, error: scanRefusal(outcome.status) };
  revalidatePath(HUB, "layout");
  return { ok: true, message: SCAN_QUEUED };
}
