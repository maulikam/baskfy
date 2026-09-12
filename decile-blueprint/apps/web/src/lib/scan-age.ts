import { formatDateTimeIST } from "@/lib/format";

/**
 * How long ago something happened, in the words a person uses — the missing half of every
 * "Scan now" line on `/swing`, `/vbt` and `/twt`.
 *
 * WHY A RELATIVE PHRASE AND NOT JUST A STAMP
 * ------------------------------------------
 * "Last scanned 12 minutes ago" answers the question the reader actually has. "12 Sep 2026,
 * 16:59 IST" answers a different one and makes them do the subtraction — and they cannot do it
 * reliably, because the stamp is IST and their clock may not be. The stamp still matters once the
 * answer stops being "recently", so this degrades to it rather than inventing "22 hours ago".
 *
 * WHY `now` IS AN ARGUMENT AND MAY BE NULL
 * ----------------------------------------
 * These pages are server components and the control that renders the line is a client component
 * under them. If the phrase were computed from `Date.now()` inside render, the server's pass and
 * the browser's hydration pass would each take their own reading and React would report a text
 * mismatch at every bucket boundary. So the caller passes the clock: `null` on the server and on
 * the hydrating pass — which yields the absolute stamp, always correct and never mismatched —
 * and a real reading once the component has mounted, from which point the phrase also ticks.
 *
 * The zone is IST throughout, for the reason `formatDateTimeIST` gives: every schedule these
 * strategies run on is an IST wall-clock time tied to the NSE session.
 */

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;

/** Past this, "N hours ago" stops being the more useful sentence and the stamp takes over. */
export const RELATIVE_HORIZON_MS = 24 * HOUR;

/**
 * "just now" · "a minute ago" · "12 minutes ago" · "an hour ago" · "5 hours ago".
 *
 * `null` when there is no timestamp, when it cannot be parsed, or when it is older than
 * `RELATIVE_HORIZON_MS` — in every one of those the caller should fall back to `stampIST`.
 * A timestamp slightly in the future is a clock that disagrees, not a scan that has not happened
 * yet, so it reads "just now" rather than a negative number.
 */
export function relativeIST(iso: string | null | undefined, now: number | null): string | null {
  if (!iso || now === null) return null;
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return null;
  const elapsed = now - at;
  if (elapsed > RELATIVE_HORIZON_MS) return null;
  if (elapsed < 45 * SECOND) return "just now";
  if (elapsed < 90 * SECOND) return "a minute ago";
  if (elapsed < HOUR) return `${Math.round(elapsed / MINUTE)} minutes ago`;
  if (elapsed < 90 * MINUTE) return "an hour ago";
  return `${Math.round(elapsed / HOUR)} hours ago`;
}

/** "at 12 Sep 2026, 16:59 IST" — the fallback, and the whole of the server's first pass. */
export function stampIST(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const rendered = formatDateTimeIST(iso);
  // `formatDateTimeIST` answers the em dash for anything it cannot parse; a dash in the middle
  // of a sentence is worse than no clause at all.
  return rendered.includes("IST") ? `at ${rendered}` : null;
}

/**
 * The one phrase a scan line puts after "Last scanned": the relative one when it is the more
 * useful of the two, the stamp otherwise, and `null` when the run carries no time at all.
 */
export function whenPhrase(iso: string | null | undefined, now: number | null): string | null {
  return relativeIST(iso, now) ?? stampIST(iso);
}
