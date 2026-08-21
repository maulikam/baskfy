/**
 * The announcement banner's dismissal cookie, shared by the server layout that reads it and the
 * client button that writes it.
 *
 * A plain module, not part of `announcement-banner.tsx`: that file is `"use client"`, and a
 * function exported from a client module cannot be called during server rendering.
 */
export const COOKIE_PREFIX = "baskfy_announcement_";

/** A year: long enough that a dismissed announcement stays dismissed, short enough to expire. */
export const COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60;

export function cookieName(id: string): string {
  return `${COOKIE_PREFIX}${id.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}
