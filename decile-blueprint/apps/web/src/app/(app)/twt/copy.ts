import { formatTradeDate } from "@/lib/format";
import { whenPhrase } from "@/lib/scan-age";
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
 * The one fact the run row carries beyond its own columns, served inline by the API.
 *
 * It is declared **here** rather than on `TwtScanRun` in `@/lib/twt/fetch` deliberately: this
 * is copy's own view of a run, the fetch module is the transport's, and widening the transport
 * type would make every consumer of it care about a field only this sentence reads. It is
 * optional, so a run that predates it still type-checks and still renders.
 */
export interface TwtScanFacts {
  /**
   * How many signals the run produced. `null` or absent is "the run did not say"; `0` is "it
   * looked and found none", and the line renders the two differently on purpose.
   */
  found?: number | null;
}

/** What the page knows that the run row does not. */
export interface ScanLineContext {
  /**
   * The browser's clock, once the control has mounted — `null` on the server's pass and on the
   * hydrating one, where a reading of its own would be a text mismatch. See `@/lib/scan-age`.
   */
  now?: number | null;
  /** The latest session this strategy has published, for the case where no run exists at all. */
  session?: string | null;
}

/**
 * What the last run is called in this sleeve's own words. TWT signals about eighteen times a
 * year (`docs/twt/04`), so **zero is the ordinary answer** and the sentence must not read as a
 * fault when it happens.
 */
function foundClause(found: number | null | undefined): string {
  if (found === null || found === undefined) return "";
  if (found === 0) return " — no signals, which is the ordinary result here.";
  return found === 1 ? " — 1 signal." : ` — ${found.toLocaleString("en-IN")} signals.`;
}

/** What the line says when this user has never pressed the button. */
export function neverScannedLine(session: string | null | undefined): string {
  return session
    ? `No scan has been started from here yet — what is shown is the nightly run's, for the ${formatTradeDate(session)} session.`
    : "No scan has run yet.";
}

/**
 * The last run, beside the button — **when it ran, whether it finished, and what it found.**
 *
 * Before 12 Sep 2026 this said nothing at all until a run existed, and said only "Last scan
 * finished <stamp>" when one did. Both halves were wrong for the reader: a strategy whose data
 * came from the nightly looked as though it had never been scanned, and a run that finished
 * looked identical whether it had flagged fifty names or none. The four states a person can
 * actually be in are the four below.
 *
 * A finished run's time is relative while it is recent ("12 minutes ago") and an IST wall clock
 * once it is not — `@/lib/scan-age` carries the reasoning, including why `now` is passed in
 * rather than read here.
 *
 * A failed run still gets **no reason**. `error` says things like which quote source was
 * missing; that is a sentence for the operator surfaces, and the reader needs only the three
 * facts that are theirs — when it stopped, that nothing changed, and that they can press again.
 */
export function scanRunLine(
  run: (TwtScanRun & TwtScanFacts) | null | undefined,
  context: ScanLineContext = {},
): string {
  const now = context.now ?? null;
  if (!run) return neverScannedLine(context.session);
  if (run.status === "QUEUED") return "Scan queued. This page updates when it finishes.";
  if (run.status === "RUNNING") return "Scanning now. This page updates when it finishes.";

  const when = whenPhrase(run.finished_at ?? run.requested_at, now);
  if (run.status === "FAILED") {
    const stopped = when ? ` It stopped ${when}.` : "";
    return `The last scan did not finish, so nothing changed.${stopped} You can start another.`;
  }
  const lead = when ? `Last scanned ${when}` : "The last scan finished";
  const found = foundClause(run.found);
  return found ? `${lead}${found}` : `${lead}.`;
}
