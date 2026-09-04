/**
 * Is the NSE cash session open right now, in the browser's opinion?
 *
 * Mirrors `SESSION_OPEN`/`SESSION_CLOSE` in `packages/core/src/baskfy_core/market_hours_cb.py`
 * (09:15–15:30 IST, docs/smallcase/04 §5). Kept as a browser-side copy rather than an API call
 * because the only thing it gates is whether to poll: asking the server whether to ask the server
 * is a round trip to save a round trip.
 *
 * **Weekday-only, and deliberately not holiday-aware.** The authoritative trading calendar lives
 * in the database, and a holiday here costs one wasted refresh that returns the same numbers — the
 * server's price cache absorbs it. Being wrong in the other direction, by not polling on a day the
 * market is open, is the failure that matters, so the loose check errs towards polling.
 */
const IST_OFFSET_MINUTES = 5 * 60 + 30;
const OPEN_MINUTES = 9 * 60 + 15;
const CLOSE_MINUTES = 15 * 60 + 30;

/** Minutes since midnight IST, and the IST weekday, whatever the viewer's own timezone is. */
export function istNow(now: Date = new Date()): { minutes: number; weekday: number } {
  const utc = now.getTime() + now.getTimezoneOffset() * 60_000;
  const ist = new Date(utc + IST_OFFSET_MINUTES * 60_000);
  return { minutes: ist.getHours() * 60 + ist.getMinutes(), weekday: ist.getDay() };
}

export function isMarketOpen(now: Date = new Date()): boolean {
  const { minutes, weekday } = istNow(now);
  if (weekday === 0 || weekday === 6) return false;
  return minutes >= OPEN_MINUTES && minutes <= CLOSE_MINUTES;
}
