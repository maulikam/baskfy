"use server";

import { revalidatePath } from "next/cache";

import {
  OVERLAP_EVENT_TYPES,
  type CorrectTagResult,
  type OverlapEventType,
} from "@/lib/overlap/candidates";
import { overlapWrite } from "@/lib/overlap/write";

/**
 * `/build/overlap`'s server actions — **one**, and it records a person's word on a headline.
 *
 * The tag under a filing is read from the headline by fixed rules and by Laya; the order settled
 * for that tag (`baskfy_core.catalyst_tags`) is a baseline first, corrections collected against
 * it, and only then a fine-tune. This is where the corrections come from: the small select on
 * the chip. It reaches `PUT /overlap/tags` (a word) or `DELETE /overlap/tags` (the word taken
 * back) through a write helper whose path type admits one string, and it can name nothing else.
 *
 * **A correction is money-free by construction.** It changes what the chip says and what the
 * corrections export contains. It changes no rank, no filter, no size and no order, because the
 * tag it corrects never entered one. This page still queues no scan and places nothing.
 */

const PAGE = "/build/overlap";

function refusal(status: number): string {
  switch (status) {
    case 0:
      return "The service could not be reached; the tag was not changed.";
    case 401:
      return "Sign in again to correct a tag.";
    case 404:
      return "Corrections belong to the account that runs the scans, and this is not that account.";
    default:
      return "The correction was not saved.";
  }
}

/**
 * Record the person's word on `headline`, or take it back with `null`.
 *
 * Idempotent: a second word on the same headline updates the one row. The page is revalidated
 * so the chip shows the corrected tag from the same read everything else on it comes from.
 */
export async function correctTag(
  headline: string,
  eventType: OverlapEventType | null,
): Promise<CorrectTagResult> {
  const text = headline.trim();
  if (!text) return { ok: false, error: "There is no headline to correct." };
  if (eventType !== null && !OVERLAP_EVENT_TYPES.includes(eventType)) {
    return { ok: false, error: "That is not one of the eight words a tag can be." };
  }
  const outcome =
    eventType === null
      ? await overlapWrite("/overlap/tags", { method: "DELETE", query: { headline: text } })
      : await overlapWrite("/overlap/tags", {
          method: "PUT",
          body: { headline: text, event_type: eventType, note: null },
        });
  if (!outcome.ok && !(eventType === null && outcome.status === 404)) {
    return { ok: false, error: refusal(outcome.status) };
  }
  revalidatePath(PAGE);
  return { ok: true };
}
