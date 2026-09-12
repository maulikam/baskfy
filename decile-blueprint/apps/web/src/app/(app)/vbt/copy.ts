import { formatTradeDate } from "@/lib/format";
import { whenPhrase } from "@/lib/scan-age";
import type { VbtScanRun, VbtToday } from "@/lib/vbt/fetch";

/**
 * The sentences `/vbt` says about a gate, a funnel and an empty session — in one file, because
 * three pages say versions of the same thing and a copy that drifts between them is a page that
 * contradicts itself.
 *
 * Every number here comes from the payload. Nothing is hard-coded: `04` §4.2's forty is served
 * as `gate_threshold_pct` precisely so a page cannot disagree with the detector about what the
 * gate is.
 */

/** `05` §2: "62.4% of 1,412 names are above their 200-day average · the gate opens above 40%". */
export function breadthLine(today: VbtToday): string {
  const pct = today.pct_above_dma;
  const measured = today.measured_count;
  if (pct === null || measured === null)
    return "the breadth of the traded universe is unknown";
  return (
    `${pct.toFixed(1)}% of ${measured.toLocaleString("en-IN")} names are above their ` +
    `200-day average · the gate opens above ${today.gate_threshold_pct.toFixed(0)}%`
  );
}

/**
 * What the book does under each gate. The SHUT sentence matters more than the OPEN one: a person
 * reading "SHUT" needs to know it does not mean "sell everything" (`04` §4.3).
 */
export function gateCopy(gate: string): string {
  if (gate === "OPEN") return "new limits may be placed";
  return "no new limits are placed. Positions are managed as usual — stops stay, exits still fire";
}

/**
 * `05` §2's funnel line, always rendered, even at zero:
 * "4,186 names → 1,412 with a bar and a 200-day average → 37 met the volume scan → 4 are signals".
 *
 * Without it an empty list and a detector that never ran render identically, and those two need
 * opposite responses from whoever is reading.
 */
export function funnelLine(today: VbtToday): string {
  const f = today.funnel?.funnel;
  if (!f) {
    return "No scan has run for this session, so the empty list below is not a statement about the market.";
  }
  const n = (value: number | undefined) => (value ?? 0).toLocaleString("en-IN");
  return (
    `${n(f.universe)} names → ${n(f.with_dma ?? f.with_bar)} with a bar and a 200-day average → ` +
    `${n(f.scan_hits)} met the volume scan → ${n(f.signals)} ${
      (f.signals ?? 0) === 1 ? "is a signal" : "are signals"
    }`
  );
}

/** How often the gate has been shut lately — the context a single day's reading does not carry. */
export function shutLine(today: VbtToday): string {
  const shut = today.shut_sessions_recent;
  if (shut === 0)
    return `The gate has been open every one of the last ${today.shut_window} sessions.`;
  return `The gate has been shut on ${shut} of the last ${today.shut_window} sessions.`;
}

/**
 * `04` §7.2's window, as a person reads it on a working order. The third session's close cancels
 * it, so "3 of 3" and "cancels tonight" are the same fact said twice — deliberately, because the
 * first is a number in a column and the second is the thing to act on.
 */
export function sessionsLine(
  worked: number,
  allowed: number,
  expiring: boolean,
): string {
  return expiring
    ? `${worked} of ${allowed} · cancels tonight`
    : `${worked} of ${allowed}`;
}

/* ------------------------------------------------------------------------------------------- *
 * "Scan now" — every sentence the button can say.
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
 * ------------------------------------------------------------------------------------------- */

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
 * The one fact the run row carries beyond its own columns, served inline on `/vbt/today`.
 *
 * Declared here rather than widened onto `VbtScanRun` for the reason the `/twt` copy gives: this
 * is copy's view of a run, the fetch module is the transport's. Optional, so a run that predates it
 * still type-checks and still renders.
 */
export interface VbtScanFacts {
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
 * What the run found, in this sleeve's own noun. About thirteen signals a week in a normal
 * market and **none at all when the tape is thin** (`VbtFunnel`), so zero is a legitimate
 * answer and the sentence must not read as a fault.
 */
function foundClause(found: number | null | undefined): string {
  if (found === null || found === undefined) return "";
  if (found === 0) return " — no signals, which is an ordinary day when the tape is quiet.";
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
 * finished <stamp>" when one did. Both halves were wrong for the reader: a strategy whose
 * candidates came from the nightly looked as though it had never been scanned, and a run that
 * finished looked identical whether it had flagged fifty names or none.
 *
 * A finished run's time is relative while it is recent ("12 minutes ago") and an IST wall clock
 * once it is not — `@/lib/scan-age` carries the reasoning, including why `now` is passed in
 * rather than read here.
 *
 * A failed run still gets **no reason**. `VbtScanRun.error` says things like which quote source
 * was missing; that is a sentence for the operator surfaces, and the reader needs only the three
 * facts that are theirs — when it stopped, that nothing changed, and that they can press again.
 */
export function scanRunLine(
  run: (VbtScanRun & VbtScanFacts) | null | undefined,
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
