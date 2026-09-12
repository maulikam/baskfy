/**
 * Honest Activity copy (AF I.6).
 *
 * The ledger behind buys/sells/dividends is not built. This module is the single source for
 * that sentence so the Activity page (and tests) cannot drift into inventing a feed.
 */

export const ACTIVITY_EMPTY_CONNECTED =
  "Activity — buys, sells, dividends and corporate actions — is not recorded yet, so there is nothing to list. Syncing again will not change that; this page fills in once the ledger behind it is built.";

export const ACTIVITY_EMPTY_DISCONNECTED =
  "Nothing has happened yet. Once a broker is connected, every buy, sell, dividend and cash move it reports is listed here, newest first.";

export const ACTIVITY_READ_ONLY = "Read-only. Nothing on this page places an order.";
