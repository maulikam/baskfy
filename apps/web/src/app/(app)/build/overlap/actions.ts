"use server";

import { revalidatePath } from "next/cache";

import {
  OPINION_LABELS,
  OVERLAP_EVENT_TYPES,
  type CorrectTagResult,
  type FilingsScan,
  type FilingsScanResult,
  type LabelRowResult,
  type OverlapEventType,
  type OverlapOpinionLabel,
  type OverlapScope,
} from "@/lib/overlap/candidates";
import { overlapWrite } from "@/lib/overlap/write";

/**
 * `/build/overlap`'s server actions — a person's word on a headline, a person's word on a
 * row's opinion, and (25 Sep 2026) the "Scan filings with Laya" button's two: queue one read of
 * the listed names' filings, and read its progress. The scan fills the Results and Filing cells
 * and nothing else.
 *
 * The tag under a filing is read from the headline by fixed rules and by Laya; the order settled
 * for that tag (`baskfy_core.catalyst_tags`) is a baseline first, corrections collected against
 * it, and only then a fine-tune. This is where the corrections come from: the small select on
 * the chip. It reaches `PUT /overlap/tags` (a word) or `DELETE /overlap/tags` (the word taken
 * back) through a write helper whose path type admits one string, and it can name nothing else.
 *
 * **A correction is money-free by construction.** It changes what the chip says and what the
 * corrections export contains. It changes no rank, no filter, no size and no order, because the
 * tag it corrects never entered one. The filings scan is display context the same way, and this
 * page still places nothing.
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

function labelRefusal(status: number): string {
  switch (status) {
    case 0:
      return "The service could not be reached; the label was not changed.";
    case 401:
      return "Sign in again to label a row.";
    case 404:
      return "That name is not on today's list, or labels belong to the account that runs the scans.";
    default:
      return "The label was not saved.";
  }
}

/**
 * Record the person's word on the row's opinion, or take it back with `null`.
 *
 * The server keys the label on the row's state as the page showed it — the setup in words and
 * the filing — so a label given here applies wherever that exact state appears, and the export
 * is the set the row question's fine-tune trains on. Idempotent: a second word on the same
 * state updates the one row. The page is revalidated so the cell shows the label from the same
 * read everything else on it comes from.
 */
export async function labelRow(
  instrumentId: number,
  label: OverlapOpinionLabel | null,
): Promise<LabelRowResult> {
  if (!Number.isInteger(instrumentId) || instrumentId <= 0) {
    return { ok: false, error: "There is no row to label." };
  }
  if (label !== null && !OPINION_LABELS.includes(label)) {
    return { ok: false, error: "That is not one of the three words a row can be." };
  }
  const outcome =
    label === null
      ? await overlapWrite("/overlap/reviews", {
          method: "DELETE",
          query: { instrument_id: instrumentId },
        })
      : await overlapWrite("/overlap/reviews", {
          method: "PUT",
          body: { instrument_id: instrumentId, label, note: null },
        });
  if (!outcome.ok && !(label === null && outcome.status === 404)) {
    return { ok: false, error: labelRefusal(outcome.status) };
  }
  revalidatePath(PAGE);
  return { ok: true };
}

function isFilingsScan(body: unknown): body is FilingsScan {
  return typeof body === "object" && body !== null && "state" in body;
}

function scanRefusal(status: number): string {
  switch (status) {
    case 0:
      return "The service could not be reached; no scan was started.";
    case 401:
      return "Sign in again to scan filings.";
    case 404:
      return "Filings scans belong to the account that runs the scans, and this is not that account.";
    case 409:
      return "A filings scan is already running, or it is 09:10–09:30 IST, when the exchange's feed belongs to the swing monitor. Try again after 09:30.";
    default:
      return "The filings scan could not be started.";
  }
}

/** Queue one read of every listed name's filings and result dates; Laya tags what it finds. */
export async function startFilingsScan(scope: OverlapScope): Promise<FilingsScanResult> {
  const outcome = await overlapWrite("/overlap/catalyst-scan", {
    method: "POST",
    query: { scope: scope === "all" ? "all" : "actionable" },
  });
  if (!outcome.ok || !isFilingsScan(outcome.body)) {
    return { ok: false, error: scanRefusal(outcome.status) };
  }
  return { ok: true, scan: outcome.body };
}

/**
 * The last filings scan's progress. A read, and nothing else.
 *
 * It used to `revalidatePath` whenever the last scan's state was `done` — which is the state
 * every scan ends in and stays in, so **every mount of the button re-rendered the page**: the
 * table appeared, the status read came back, the page rendered a second time, and when that
 * second `GET /overlap` lost the race against the three screen re-runs (2.5 s budget) the table
 * was replaced by "could not be read" over the old symbol matrix (Maulik, 25 Sep 2026: "the newer
 * version loads first, and then the older version comes back"). The refresh a finished scan
 * needs is the button's own `router.refresh()`, on the running→done edge it observes.
 */
export async function filingsScanStatus(): Promise<FilingsScanResult> {
  const outcome = await overlapWrite("/overlap/catalyst-scan", { method: "GET" });
  if (!outcome.ok || !isFilingsScan(outcome.body)) {
    return { ok: false, error: "The scan's progress could not be read." };
  }
  return { ok: true, scan: outcome.body };
}
