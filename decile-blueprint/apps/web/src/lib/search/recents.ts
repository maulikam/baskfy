"use client";

import type { CatalogHitOut } from "@baskfy/api-client";

/**
 * The palette's recent items — `baskfynavrefactorreport` §F11: "…searching stocks, indices,
 * baskets, and screens, **with recent items**".
 *
 * Recent items are what makes a palette faster than the nav on the second use: the thing you
 * opened yesterday is usually the thing you want today, and typing three letters to find it again
 * is the tax a palette exists to remove. An empty palette that says nothing until you type is the
 * version that gets opened once.
 *
 * **`localStorage`, deliberately, and per browser.** The alternative is a `recent_item` table and
 * a write on every navigation — a schema, a migration and a request per click, to remember four
 * rows. This is a convenience, not a record: losing it costs a person three keystrokes.
 * `docs/DECISIONS-MERGE.md` M46.3.
 *
 * Every read and write is guarded. Storage throws outright in a Safari private window and in some
 * embedded contexts, and a search box that cannot open because a convenience feature threw is a
 * far worse failure than one that has forgotten what you looked at.
 */

const KEY = "baskfy.search.recents.v1";

/** Enough to be useful, few enough that the list is scannable at a glance. */
export const MAX_RECENTS = 5;

/** What is stored per entry. The same shape the API returns, so a recent renders like a hit. */
export type RecentItem = Pick<CatalogHitOut, "kind" | "id" | "title" | "subtitle">;

const KINDS = new Set(["instrument", "index", "basket", "screen"]);

/**
 * Narrow one parsed entry, or reject it.
 *
 * The stored value is a string this app wrote in some earlier version of itself, which makes it
 * exactly as trustworthy as user input. A `kind` that is no longer in the union would otherwise
 * reach `hrefFor` and index an object with `undefined`.
 */
function toRecent(value: unknown): RecentItem | null {
  if (typeof value !== "object" || value === null) return null;
  const row = value as Record<string, unknown>;
  if (typeof row.kind !== "string" || !KINDS.has(row.kind)) return null;
  if (typeof row.id !== "string" || row.id === "") return null;
  if (typeof row.title !== "string") return null;
  return {
    kind: row.kind as RecentItem["kind"],
    id: row.id,
    title: row.title,
    subtitle: typeof row.subtitle === "string" ? row.subtitle : null,
  };
}

export function readRecents(): RecentItem[] {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map(toRecent)
      .filter((item): item is RecentItem => item !== null)
      .slice(0, MAX_RECENTS);
  } catch {
    // Unreadable, unparseable, or storage itself threw. An empty list is the honest answer and
    // the next `rememberRecent` overwrites whatever the bad value was.
    return [];
  }
}

/**
 * Move one item to the front of the recent list and persist it.
 *
 * Returns the new list rather than only writing it, so a caller can render immediately instead of
 * reading storage back — and so this stays testable without a DOM assertion.
 */
export function rememberRecent(item: RecentItem): RecentItem[] {
  const deduped = [
    item,
    ...readRecents().filter((seen) => !(seen.kind === item.kind && seen.id === item.id)),
  ].slice(0, MAX_RECENTS);
  try {
    window.localStorage.setItem(KEY, JSON.stringify(deduped));
  } catch {
    // Quota exceeded, or storage disabled. The palette still works; it just will not remember.
  }
  return deduped;
}

export function clearRecents(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // Nothing to do: there is no state to repair, and throwing here would close the palette.
  }
}
