"use server";

import { revalidatePath } from "next/cache";

import { twtWrite, type TwtScanResult } from "@/lib/twt/write";

import { SCAN_QUEUED, scanRefusal } from "./copy";

/**
 * The `/twt` hub's server actions — **one**, and it queues a detection run.
 *
 * WHAT CHANGED, AND WHY IT IS NOT A WEAKENING
 * -------------------------------------------
 * Until 12 Sep 2026 this tree had no `actions.ts` at all, and `__tests__/read-only.test.tsx`
 * asserted exactly that: `05` §1 permits two money-free writes here (a note and a dismissal) and
 * DECISIONS-TW TW8.7 declined to build either, because `03`'s data model has no table to put a
 * note in — so "this tree has no server action" was the strongest true claim available and the
 * test made it. Maulik asked for "Scan now" on this page, so that claim is no longer available —
 * and the census that replaces it is the swing hub's, which is stricter than a prose promise: the
 * file walk enumerates every export of every `use server` module under the tree and the set must
 * be exactly `["scanNow"]`.
 *
 * **A scan is money-free by construction.** It queues the detector; prices in, detection rows
 * out. It reaches `/twt/scan` through a write helper whose path type admits one string, and it
 * has no way to name the desk's confirm route — which is what `docs/twt/02` Track C §4 keeps out
 * of this application. `05` §2 still says a plan line becomes an order in the desk console, on a
 * click a person makes, and nowhere else. DECISIONS-TW TW11.
 *
 * It also sets no capital and flips no flag: this sleeve's execution switch and its sleeve
 * capital are Maulik's alone (`docs/twt/02` §3), and nothing on this path can reach either.
 */

const HUB = "/twt";

/**
 * "Scan now" — `POST /twt/scan`. 202 queued, 409 one already in flight, 429 one a minute.
 *
 * The status becomes a sentence in `copy.ts` rather than here, and the service's own `detail` is
 * never rendered: a refusal written for an operator names jobs and tables, and this page is read
 * by the person whose money it is.
 */
export async function scanNow(
  _previous: TwtScanResult | null,
  _formData: FormData,
): Promise<TwtScanResult> {
  const outcome = await twtWrite("/twt/scan");
  if (!outcome.ok) return { ok: false, error: scanRefusal(outcome.status) };
  revalidatePath(HUB, "layout");
  return { ok: true, message: SCAN_QUEUED };
}
