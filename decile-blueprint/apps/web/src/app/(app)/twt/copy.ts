import { formatDateTimeIST } from "@/lib/format";
import type { TwtScanRun } from "@/lib/twt/fetch";

/**
 * "Scan now" — every sentence the button on `/twt` can say.
 *
 * WHY THIS IS NOT IN `lib/twt/copy.ts`
 * ------------------------------------
 * That file exists because the hub and the operator view "say versions of the same thing about
 * the same gate", and a copy module only one of the two can import is exactly the drift it exists
 * to prevent (DECISIONS-TW TW8.5). These sentences are the opposite case: they belong to a
 * control that exists on this page and nowhere else, and the operator console has its own way of
 * starting a run. Copy shared by one surface is not shared copy.
 *
 * WHY THE SERVER'S OWN SENTENCE IS NOT SHOWN
 * ------------------------------------------
 * The write helper answers a status, not a `detail`, and that is the point. The service's refusal
 * text is written for whoever can act on it: it names jobs, quote sources and tables. On 11 Sep
 * 2026 a screenshot of a customer-facing page was found carrying a loopback address and three
 * column names, with six tests pinning the defect. So the status is mapped here, once, into words
 * a reader already has — and the engineering detail stays in this comment, where the person who
 * can act on it is reading.
 *
 * The three the contract names:
 *   202  the scan was queued
 *   409  one is already on its way — pressing again would not make a second
 *   429  one ran a moment ago; the service allows one a minute
 */

/** 202 — it was taken. Says what happens next, so nobody presses it twice. */
export const SCAN_QUEUED = "Scan started. This page updates on its own while it runs.";

/** 409 — there is already one on its way. Not a failure, and it must not read like one. */
export const SCAN_ALREADY_RUNNING =
  "A scan is already running. This page updates when it finishes.";

/** 429 — one a minute. The wait is named, because "try again" without a when is not an answer. */
export const SCAN_TOO_SOON = "A scan has just been run. The next one can start in about a minute.";

/** 401 — the session went. Nothing was run, and that is the half worth saying first. */
export const SCAN_SIGNED_OUT = "You are signed out, so nothing was run. Sign in and try again.";

/** Anything else, including no answer at all. Never a status code, never a stack. */
export const SCAN_UNAVAILABLE =
  "The scan could not be started just now, so nothing changed. Try again in a moment.";

/** The one sentence a refusal becomes, chosen by status alone. */
export function scanRefusal(status: number): string {
  if (status === 409) return SCAN_ALREADY_RUNNING;
  if (status === 429) return SCAN_TOO_SOON;
  if (status === 401 || status === 403) return SCAN_SIGNED_OUT;
  return SCAN_UNAVAILABLE;
}

/** What the button itself reads while a run is on its way. */
export function scanButtonLabel(pending: boolean, inFlight: boolean): string {
  if (pending) return "Starting…";
  return inFlight ? "Scanning…" : "Scan now";
}

/**
 * The last run, beside the button.
 *
 * A finished run gets its wall clock in IST rather than the reader's zone, for the reason
 * `formatDateTimeIST` gives: every schedule this strategy runs on is an IST wall-clock time tied
 * to the session, and a local rendering turns "is this fresh?" into arithmetic.
 *
 * A failed run gets no reason. `TwtScanRun.error` says things like which quote source was
 * missing; that is a sentence for the operator surfaces, and the reader needs only the two facts
 * that are theirs — nothing changed, and they can press it again.
 */
export function scanRunLine(run: TwtScanRun | null | undefined): string {
  if (!run) return "";
  if (run.status === "QUEUED") return "Scan queued.";
  if (run.status === "RUNNING") return "Scanning now.";
  if (run.status === "FAILED") {
    return "The last scan did not finish, so nothing changed. You can start another.";
  }
  const when = run.finished_at ?? run.requested_at;
  return when ? `Last scan finished ${formatDateTimeIST(when)}.` : "The last scan finished.";
}
