/**
 * The one announcement the app shell may show, and the reason it is a declared record rather than
 * JSX in the layout.
 *
 * It used to be markup inside `app/(app)/layout.tsx`:
 *
 * > **December 2026 update:** split- and bonus-adjusted history, a longer backfill, and backtests
 * > are on the way.
 *
 * Two things had gone wrong with it by 26 August 2026, and neither could fail a build:
 *
 * 1. **It announced a month that had not happened.** "December 2026" sat in a header served in
 *    August, which reads as a typo at best and as an abandoned site at worst.
 * 2. **All three things it called "on the way" had arrived.** The backfill runs from 2011,
 *    `apply_adjustments` applies corporate actions, and backtests ship at `/build/backtests`.
 *    A banner promising delivered features tells a reader the product is behind where it is.
 *
 * Prose cannot be linted, but a **date** can. {@link Announcement.until} is the day the message
 * stops being true; past it, nothing renders, and `__tests__/announcement.test.ts` fails the suite
 * if the shipped value is already expired. The banner can still be wrong — it cannot be *stale*.
 */

export interface Announcement {
  /** A dismissal is remembered against this id, so changing the message must change the id. */
  readonly id: string;
  /** The bold lead-in. Kept short; the shell renders it inline before the body. */
  readonly lead: string;
  readonly body: string;
  readonly action: { readonly label: string; readonly href: string };
  /**
   * ISO date, exclusive. On and after this day the banner does not render.
   *
   * There is no "no expiry" option on purpose. Every announcement anyone has ever written was
   * true for a while; the ones that rot are the ones nobody set an end for.
   */
  readonly until: string;
}

/**
 * What the shell shows today: **nothing**.
 *
 * The site is gated and pre-launch (NEEDS-MAULIK §19 — the legal drafts are unreviewed), so there
 * is no audience to announce to and nothing to announce. `null` is a real answer here, not an
 * unfinished one, and it is why {@link visibleAnnouncement} takes a nullable.
 */
export const ANNOUNCEMENT: Announcement | null = null;

/**
 * The announcement to render, or `null`.
 *
 * `today` is a parameter rather than a `new Date()` inside, because a function that reads the
 * clock cannot be tested against the day after its own expiry — which is the single behaviour
 * worth testing here.
 */
export function visibleAnnouncement(
  today: Date,
  announcement: Announcement | null = ANNOUNCEMENT,
): Announcement | null {
  if (announcement === null) return null;
  const until = Date.parse(`${announcement.until}T00:00:00Z`);
  if (Number.isNaN(until)) return null; // an unparseable date is a broken record, not a live one
  return today.getTime() < until ? announcement : null;
}
