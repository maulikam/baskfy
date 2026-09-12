/**
 * Honest Activity copy (AF I.6).
 *
 * The feed exists (`GET /portfolio/activity`). Holdings sync still does not write a trade
 * history, so an empty list after a sync is a real empty list — not a page that has not been
 * built. These strings are the single source so the page and the tests cannot drift into
 * inventing a feed or sending the reader back to Sync.
 */

export const ACTIVITY_EMPTY_CONNECTED =
  "Nothing is on this feed yet. Holdings sync records what you hold, not the trades that got you there — syncing again will not invent buys and sells. Dividends and corporate actions on names you hold appear here when we have them. Cash you assign to a portfolio appears when you move it.";

export const ACTIVITY_EMPTY_DISCONNECTED =
  "Nothing has happened yet. Once a broker is connected, every buy, sell, dividend and cash move it reports is listed here, newest first.";

export const ACTIVITY_UNAVAILABLE =
  "The activity feed did not load. What happened here is missing rather than nothing.";

export const ACTIVITY_READ_ONLY = "Read-only. Nothing on this page places an order.";

export const ACTIVITY_FILTER_EMPTY =
  "Nothing of that kind is on this feed. Every other event is still here; only this view is narrowed.";
